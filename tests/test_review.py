from datetime import datetime, timedelta

import pytest

from dfq import fees, review as R, timeutil

T0 = datetime(2026, 11, 10, 21, 30)


def add_decision(db, *, buy=1000, sell=1200, stop=900, qty=100, action="buy", variant="final",
                 valid_hours=24, oid=626):
    cur = db.execute(
        "INSERT INTO decisions(plan_id,variant,created_at,oid,name,action,buy_price,sell_price,stop_price,"
        "qty,budget,valid_from,valid_until,paper) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        ("p1", variant, timeutil.fmt(T0), oid, "X", action, buy, sell, stop, qty, buy * qty,
         timeutil.fmt(T0), timeutil.fmt(T0 + timedelta(hours=valid_hours))))
    return cur.lastrowid


def path(db, prices, start=T0, step=60, oid=626):
    """从 start 开始每 step 分钟一个价格。"""
    db.add_minutes(oid, [(start + timedelta(minutes=step * (i + 1)), p) for i, p in enumerate(prices)])


def score(cfg, db, dec_id, now):
    R.score_open_decisions(cfg, db, now)
    return db.one("SELECT * FROM decisions WHERE id=?", (dec_id,))


def test_win_buy_hit_then_sell_hit(cfg, db):
    d = add_decision(db)
    path(db, [1050, 1000, 1100, 1200, 1300])
    r = score(cfg, db, d, T0 + timedelta(hours=6))
    assert (r["status"], r["outcome"], r["buy_hit"]) == ("final", "win", 1)
    assert r["buy_hit_at"] == "2026-11-10 23:30" and r["exit_at"] == "2026-11-11 01:30"
    assert r["net_return"] == pytest.approx(fees.net_return(1000, 1200), abs=1e-5)
    assert r["net_profit"] == pytest.approx((1200 * 0.87 - 1000) * 100)


def test_stop_buy_hit_then_stop_hit(cfg, db):
    d = add_decision(db)
    path(db, [990, 950, 890, 1300])                       # 先破止损，之后再涨也不算
    r = score(cfg, db, d, T0 + timedelta(hours=6))
    assert r["outcome"] == "stop"
    assert r["net_return"] == pytest.approx(900 * 0.87 / 1000 - 1, abs=1e-5)   # −21.7%：止损很贵


def test_miss_when_buy_line_never_touched(cfg, db):
    d = add_decision(db)
    path(db, [1100] * 30)
    assert score(cfg, db, d, T0 + timedelta(hours=10))["status"] == "open"     # 有效期没过：还不能下结论
    r = score(cfg, db, d, T0 + timedelta(hours=25))
    assert (r["status"], r["outcome"], r["buy_hit"], r["net_return"]) == ("final", "miss", 0, 0.0)


def test_buy_after_expiry_does_not_count(cfg, db):
    d = add_decision(db, valid_hours=3)
    path(db, [1100, 1100, 1100, 1000, 1300])              # 第 4 小时才到买入线，计划已过期
    assert score(cfg, db, d, T0 + timedelta(hours=8))["outcome"] == "miss"


def test_hold_is_marked_to_liquidation_value(cfg, db):
    cfg.set("review.sell_horizon_hours", 4)
    d = add_decision(db)
    path(db, [1000, 1050, 1080, 1100, 1090, 1500])        # 买入后 4 小时内没到 1200；之后的 1500 不算
    r = score(cfg, db, d, T0 + timedelta(hours=7))
    assert r["outcome"] == "hold" and r["exit_price"] == 1090
    assert r["net_return"] == pytest.approx(1090 * 0.87 / 1000 - 1, abs=1e-5)


def test_open_until_horizon_passes(cfg, db):
    d = add_decision(db)
    path(db, [1000, 1050])
    r = score(cfg, db, d, T0 + timedelta(hours=3))
    assert r["status"] == "open" and r["buy_hit"] == 1    # 已买入，卖没卖还要再看


def test_watch_decision_scores_zero(cfg, db):
    d = add_decision(db, action="watch")
    r = score(cfg, db, d, T0 + timedelta(hours=1))
    assert (r["status"], r["outcome"], r["net_return"]) == ("final", "watch", 0.0)


def test_no_data_is_not_scored_as_miss(cfg, db):
    d = add_decision(db)
    r = score(cfg, db, d, T0 + timedelta(hours=30))
    assert r["outcome"] == "nodata" and r["net_return"] is None


def test_falls_back_to_daily_bars_when_minutes_missing(cfg, db):
    d = add_decision(db, valid_hours=72)
    for day, lo, hi in (("2026-11-11", 980, 1100), ("2026-11-12", 1050, 1250)):
        db.execute("INSERT INTO day_prices(oid,d,price,pmin,pmax) VALUES(626,?,?,?,?)", (day, 1000, lo, hi))
    r = score(cfg, db, d, T0 + timedelta(days=4))
    assert r["outcome"] == "win" and "day" in r["score_note"]


def test_perf_compares_final_rule_and_nothing(cfg, db):
    win = add_decision(db, variant="final")
    add_decision(db, variant="rule", buy=1000, sell=1400)             # 规则版卖出线太高 → 到期持有
    path(db, [1000, 1100, 1200] + [1100] * 60)
    R.score_open_decisions(cfg, db, T0 + timedelta(hours=60))
    p = R.perf(cfg, db)
    assert p["final"]["wins"] == 1 and p["final"]["profit"] > 0
    assert p["rule"]["holds"] == 1 and p["rule"]["profit"] < 0
    text = R.format_perf(p)
    assert "最终建议优于规则基线" in text and "什么都不做" in text and "样本只有 1 笔" in text
    assert db.one("SELECT outcome FROM decisions WHERE id=?", (win,))["outcome"] == "win"


def test_perf_with_no_data_says_cannot_judge(cfg, db):
    assert "无法判断" in R.format_perf(R.perf(cfg, db))


def add_opinion(db, direction, ref, source="UP主甲", days=3, stype="bili"):
    db.execute("INSERT INTO opinions(source_type,source_name,collected_at,oid,item_name,direction,"
               "horizon_days,ref_price,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
               (stype, source, timeutil.fmt(T0), 626, "X", direction, days, ref,
                f"{source}{direction}{ref}{days}"))


def test_source_credibility_scoring(cfg, db):
    add_opinion(db, "up", 1000, "说对的")
    add_opinion(db, "down", 1000, "说错的")
    add_opinion(db, "up", 1090, "没怎么动")               # 3 天后 1100，只涨 0.9% → 不算命中
    db.add_minutes(626, [(T0 + timedelta(days=3), 1100)])
    assert R.score_opinions(cfg, db, T0 + timedelta(days=2)) == 0      # 还没到期
    assert R.score_opinions(cfg, db, T0 + timedelta(days=3, hours=1)) == 3
    s = {x["source_name"]: x for x in R.source_scores(db)}
    assert s["说对的"]["score"] == pytest.approx(2 / 3, abs=1e-3)      # (1+1)/(1+2)
    assert s["说错的"]["score"] == pytest.approx(1 / 3, abs=1e-3)
    assert s["没怎么动"]["hits"] == 0


def test_source_score_shrinks_toward_half_with_few_samples(cfg, db):
    for i in range(8):
        add_opinion(db, "up", 1000 + i, "老手")
    add_opinion(db, "up", 1000, "新人")
    db.add_minutes(626, [(T0 + timedelta(days=3), 1100)])
    R.score_opinions(cfg, db, T0 + timedelta(days=4))
    s = {x["source_name"]: x["score"] for x in R.source_scores(db)}
    assert s["老手"] == pytest.approx(9 / 10) and s["新人"] == pytest.approx(2 / 3, abs=1e-3)   # 一次命中不等于可信


def test_platform_low_price_prediction_is_scored_like_any_source(cfg, db):
    add_opinion(db, "low", 1000, "小涛查·低价预测", days=1, stype="platform")
    add_opinion(db, "low", 1200, "小涛查·低价预测", days=1, stype="platform")
    d0 = T0.replace(hour=0, minute=0)
    db.add_minutes(626, [(d0 + timedelta(minutes=10 * i), 1010 + (i % 7) * 20) for i in range(140)])
    R.score_opinions(cfg, db, T0 + timedelta(days=1, hours=3))
    rows = db.query("SELECT ref_price, hit, move_pct FROM opinions ORDER BY ref_price")
    assert [(r["ref_price"], r["hit"]) for r in rows] == [(1000, 1), (1200, 0)]   # 实际最低 1010
