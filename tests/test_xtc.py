import json
from datetime import datetime

import pytest

from dfq import timeutil
from dfq.sources import xtc as X
from dfq.sources.mock import MockXtcTransport

TOKEN = "tk_secret_ABCDEF123456"


class Counting:
    """包一层假传输，记录真正“发出去”的请求。"""

    def __init__(self, inner=None, fail=None):
        self.inner, self.fail, self.sent = inner or MockXtcTransport(), fail, []

    def __call__(self, method, url, params, body, headers, timeout):
        self.sent.append((url, dict(params or {})))
        if self.fail:
            return self.fail
        return self.inner(method, url, params, body, headers, timeout)


def make(cfg, db, **kw):
    t = kw.pop("transport", None) or Counting()
    return X.XtcClient(cfg, db, TOKEN, transport=t, **kw), t


def test_cache_prevents_duplicate_requests(cfg, db):
    client, t = make(cfg, db)
    a = client.minute(626)
    b = client.minute(626)
    assert len(t.sent) == 1 and client.cost_this_session == 1 and client.cache_hits == 1
    assert b["_from_cache"] and a["data"] == b["data"]
    client.minute(640)                                   # 不同参数才会再发
    assert len(t.sent) == 2


def test_cache_expires_after_ttl(cfg, db):
    client, t = make(cfg, db)
    client.minute(626)
    timeutil.set_now("2026-11-13 21:45:00")              # ttl 600 秒，过了 15 分钟
    client.minute(626)
    assert len(t.sent) == 2


def test_cache_file_and_usage_table_never_store_token(cfg, db):
    client, t = make(cfg, db)
    client.minute(626)
    assert t.sent[0][1]["token"] == TOKEN                # 确实带着 token 发出去了
    blob = "".join(p.read_text(encoding="utf-8") + p.name for p in client.cache_dir.glob("*"))
    assert blob and TOKEN not in blob
    rows = db.query("SELECT * FROM api_usage")
    assert TOKEN not in json.dumps([dict(r) for r in rows])


def test_daily_limit_stops_requests(cfg, db):
    cfg.set("xtc.daily_limit", 3)
    client, t = make(cfg, db)
    for oid in (626, 640, 635):
        client.minute(oid)
    with pytest.raises(X.DailyLimitExceeded):
        client.minute(629)
    assert len(t.sent) == 3                              # 第 4 次根本没发出去
    assert db.usage_on("xtc") == 3 and client.remaining_today() == 0
    client.minute(626)                                   # 缓存命中不受限额影响
    assert len(t.sent) == 3


def test_daily_limit_resets_next_day(cfg, db):
    cfg.set("xtc.daily_limit", 1)
    client, t = make(cfg, db)
    client.minute(626)
    with pytest.raises(X.DailyLimitExceeded):
        client.minute(640)
    timeutil.set_now("2026-11-14 00:05:00")
    client.minute(640)
    assert db.usage_on("xtc") == 1 and db.usage_total("xtc") == 2


def test_expensive_endpoint_costs_more_and_free_one_costs_nothing(cfg, db):
    cfg.set("xtc.daily_limit", 25)
    client, t = make(cfg, db)
    client.filter_options()
    assert db.usage_on("xtc") == 0                       # 筛选项不扣次
    with pytest.raises(X.XtcError):                      # 模拟源不支持该接口，但请求已发出
        client.call("item_price_all", {"isZb": 0}, use_cache=False)
    assert db.usage_on("xtc") == 20                      # 全物品接口按 20 次计
    with pytest.raises(X.DailyLimitExceeded):
        client.call("item_price_all", {"isZb": 0}, use_cache=False)


def test_retries_are_counted_and_capped(cfg, db):
    cfg.set("xtc.retries", 2)
    client, t = make(cfg, db, transport=Counting(fail=(503, b"busy")))
    import dfq.http
    orig = dfq.http.time.sleep
    dfq.http.time.sleep = lambda s: None
    try:
        with pytest.raises(X.XtcError) as ei:
            client.minute(626)
    finally:
        dfq.http.time.sleep = orig
    assert len(t.sent) == 3 and db.usage_on("xtc") == 3  # 每次重试都记账：宁可多算
    assert TOKEN not in str(ei.value)


def test_api_error_code_is_not_cached(cfg, db):
    client, t = make(cfg, db, transport=Counting(fail=(200, json.dumps({"code": 1001, "msg": "次数不足"}).encode())))
    with pytest.raises(X.XtcError, match="1001"):
        client.minute(626)
    assert not list(client.cache_dir.glob("*")) if client.cache_dir.exists() else True


def test_missing_token_and_cache_only(cfg, db):
    with pytest.raises(X.TokenMissing):
        X.XtcClient(cfg, db, None, transport=Counting()).minute(626)
    t = Counting()
    with pytest.raises(X.XtcError, match="dry-run"):
        X.XtcClient(cfg, db, TOKEN, transport=t, cache_only=True).minute(626)
    assert t.sent == [] and db.usage_on("xtc") == 0


def test_token_cannot_be_passed_in_params(cfg, db):
    client, _ = make(cfg, db)
    with pytest.raises(ValueError):
        client.call("minute", {"id": 1, "token": "x"})


def test_list_all_paginates_until_count(cfg, db):
    client, t = make(cfg, db)
    items = client.list_all("子弹", "全部")
    assert len(items) == 59 and len(t.sent) == 3         # 59 条 / 每页 20 = 3 页
    assert len({i["oid"] for i in items}) == 59


def test_parse_minute_curve_from_doc_shape():
    resp = {"data": {"a": ["25 16:02分", "25 16:07分", "26 15:57分"], "b": [21500, 21930, 20640], "zb": [0, 0, 0]}}
    pts = X.parse_minute_curve(resp, datetime(2026, 12, 26, 16, 0))
    assert pts == [(datetime(2026, 12, 25, 16, 2), 21500.0), (datetime(2026, 12, 25, 16, 7), 21930.0),
                   (datetime(2026, 12, 26, 15, 57), 20640.0)]


def test_parse_minute_curve_drops_bad_points():
    resp = {"data": {"a": ["13 21:00分", "13 21:05分", "bad", "13 21:15分"], "b": [100, None, 5, 0]}}
    assert X.parse_minute_curve(resp, datetime(2026, 11, 13, 21, 30)) == [(datetime(2026, 11, 13, 21, 0), 100.0)]


def test_parse_day_and_hour_curves():
    ref = datetime(2026, 12, 26, 16, 0)
    day = {"data": {"a": ["12-25", "12-26"], "b": [20923, 18618], "max": [29240, 29240],
                    "min": [16340, 16340], "maxD": ["12-25 00:02分", "12-26 00:02分"],
                    "minD": ["12-25 03:12分", "12-26 00:12分"]}}
    rows = X.parse_day_curve(day, ref)
    assert rows[0] == {"d": "2026-12-25", "price": 20923.0, "pmin": 16340.0, "pmax": 29240.0,
                       "min_at": "2026-12-25 03:12", "max_at": "2026-12-25 00:02"}
    # 实测结构（2026-11-15 probe）：yc 与 a 等长、按下标对应，历史时段是字符串 "null"
    hour = {"data": {"a": ["12-26 14时", "12-26 15时", "12-26 16时", "12-26 17时"], "b": [100, 110],
                     "max": [105, 111], "min": [99, 108], "yc": ["null", "null", 120, 130]}}
    rows = X.parse_hour_curve(hour, ref)
    assert [(r["ts"].hour, r["price"], r["forecast"]) for r in rows] == \
        [(14, 100.0, 0), (15, 110.0, 0), (16, 120.0, 1), (17, 130.0, 1)]


def test_parse_list_item_and_untradable():
    d = {"ShopSellType": ["交易行上架", "3", "3132"], "bl": -1.5, "day_7_price": 3700, "day_7_bl": -2.7,
         "day_30_price": 3500, "day_30_bl": 2.8, "grade": 5, "id": 11, "name": " 12.7x55mm PS12B ",
         "oid": 626, "price": 3600}
    it = X.parse_list_item(d)
    assert it["oid"] == 626 and it["name"] == "12.7x55mm PS12B" and it["quick_price"] == 3132.0
    assert X.parse_list_item({**d, "oid": 0}) is None    # oid=0 表示不能在交易行交易


def test_parse_ammo_day_and_find_category():
    resp = {"data": [{"hour": 3, "data": [{"grade": 5, "id": 11, "name": "12.7x55mm PS12B", "oid": 626, "price": 3400}]},
                     {"hour": 4, "data": []}]}
    assert X.parse_ammo_day(resp) == [{"oid": 626, "name": "12.7x55mm PS12B", "grade": 5, "hour": 3, "price": 3400.0}]
    opts = [{"label": "枪械", "value": 1, "children": [{"label": "步枪"}]},
            {"label": "子弹", "value": 9, "children": [{"label": "全部"}, {"label": "5.56x45mm"}]}]
    assert X.find_category(opts) == [{"key1": "子弹", "value": 9, "key2": ["全部", "5.56x45mm"]}]
