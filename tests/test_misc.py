from dfq import funds, ledger, probe, timeutil, watch
from dfq.mail.transport import FileTransport
from dfq.sources import xtc as X
from dfq.sources.mock import MockXtcTransport


def test_watch_costs_two_requests_and_dedups_alerts(cfg, db, tmp_path):
    cfg.set("watch.move_pct", 0.0)                         # 任何波动都算异动，便于测试
    client = X.XtcClient(cfg, db, "T-123456", transport=MockXtcTransport())
    ft = FileTransport(tmp_path)
    res = watch.check(cfg, db, client, ft)
    assert db.usage_on("xtc") == 2 and res["checked"] > 0 and res["sent"] == len(res["alerts"]) > 0
    assert len(list((tmp_path / "outbox").glob("*.eml"))) == 1      # 多条异动合并成一封
    res2 = watch.check(cfg, db, client, ft)
    assert res2["sent"] == 0 and db.usage_on("xtc") == 2            # 6 小时内不重复提醒；请求走缓存


def test_watch_flags_position_below_stop(cfg, db, tmp_path):
    cfg.set("watch.move_pct", 99.0)
    client = X.XtcClient(cfg, db, "T-123456", transport=MockXtcTransport())
    row = next(i for i in (X.parse_list_item(d) for d in client.list_all("子弹", "全部")) if i["name"] == "12.7x55mm PS12B")
    ledger.record_trade(db, "buy", {"name": row["name"], "oid": row["oid"]}, 100, row["price"] * 1.5)
    timeutil.set_now("2026-11-13 22:30:00")
    res = watch.check(cfg, db, client, FileTransport(tmp_path), dry_run=True)
    assert any("止损线" in a and "PS12B" in a for a in res["alerts"]) and res["sent"] == 0


def test_funds_official_is_stub_and_falls_back(cfg, db):
    assert funds.current_balance(cfg, db)["known"] is False
    ledger.set_balance(db, 1234567)
    cfg.set("funds.official_enabled", True)
    bal = funds.current_balance(cfg, db)
    assert bal["amount"] == 1234567 and bal["source"] == "manual" and "回退" in bal["note"]


def test_probe_stays_within_hard_limit_and_writes_report(cfg, db, tmp_path):
    res = probe.run(cfg, db, None, tmp_path, transport=MockXtcTransport())
    assert res["ok"] and 0 < res["used"] <= probe.HARD_LIMIT
    text = (tmp_path / "接口探测报告-实测.md").read_text(encoding="utf-8")
    assert "子弹分类索引" in text and "| 626 | 12.7x55mm PS12B | 5 |" in text and "本次探测消耗" in text
    assert "✅ 「12.7x55mm PS12B」" in text


def test_probe_refuses_without_token_and_spends_nothing(cfg, db, tmp_path):
    res = probe.run(cfg, db, None, tmp_path)
    assert not res["ok"] and res["used"] == 0 and db.usage_total("xtc") == 0
    assert not (tmp_path / "接口探测报告-实测.md").exists()


def test_probe_cannot_exceed_15_even_if_api_misbehaves(cfg, db, tmp_path):
    """接口把 count 报得巨大（想让我们一直翻页）时，探测仍然不超过硬上限。"""
    inner = MockXtcTransport()

    def lying(method, url, params, body, headers, timeout):
        status, raw = inner(method, url, params, body, headers, timeout)
        if url.endswith("item_list_pro"):
            import json
            d = json.loads(raw)
            d["count"] = 100000
            d["data"] = (d["data"] or inner._h_item_list_pro({"p": 1, "limit": 20})["data"])[:20]
            while len(d["data"]) < 20:
                d["data"].append(d["data"][0])
            raw = json.dumps(d).encode()
        return status, raw
    cfg.set("xtc.list_max_pages", 500)
    res = probe.run(cfg, db, None, tmp_path, transport=lying)
    assert res["used"] <= probe.HARD_LIMIT and db.usage_total("xtc") <= probe.HARD_LIMIT
