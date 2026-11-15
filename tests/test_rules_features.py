from datetime import datetime, timedelta

import pytest

from dfq import features as F, fees, rules

NOW = datetime(2026, 11, 13, 21, 30)
ITEM = {"oid": 626, "name": "12.7x55mm PS12B", "grade": 5}


def seed_minutes(db, days=7, low=3400, high=4400, oid=626):
    """每天 0 点最低、12 点最高的三角波，5 分钟一个点。"""
    pts = []
    t = NOW - timedelta(days=days)
    while t <= NOW:
        frac = abs((t.hour * 60 + t.minute) / 720 - 1)       # 0 点=1, 12 点=0, 24 点=1
        pts.append((t, round(high - (high - low) * frac)))
        t += timedelta(minutes=5)
    db.add_minutes(oid, pts)
    db.upsert_item(oid, ITEM["name"], 5, 11)
    db.execute("INSERT INTO snapshots(oid,ts,price,bl,day7_bl,day30_bl,quick_price) VALUES(?,?,?,?,?,?,?)",
               (oid, "2026-11-13 21:30", pts[-1][1], 1.0, -2.0, 3.0, pts[-1][1] * 0.86))


def test_quantile_and_rank():
    v = [1, 2, 3, 4, 5]
    assert F.quantile(v, 0) == 1 and F.quantile(v, 1) == 5 and F.quantile(v, 0.5) == 3
    assert F.quantile(v, 0.125) == pytest.approx(1.5)
    assert F.pct_rank(v, 3) == pytest.approx(0.6) and F.pct_rank(v, 0) == 0 and F.pct_rank(v, 9) == 1
    with pytest.raises(ValueError):
        F.quantile([], 0.5)


def test_features_from_minute_history(cfg, db):
    seed_minutes(db)
    f = F.compute(cfg, db, ITEM, NOW)
    assert f["basis"] == "minute" and f["minute_days"] >= 6
    assert 3400 <= f["low_ref"] < f["q"]["q50"] < f["high_ref"] <= 4400
    assert f["window_min"] == 3400 and f["window_max"] == 4400
    assert 0 in f["low_hours"] or 23 in f["low_hours"]       # 低点在零点附近
    assert 12 in f["high_hours"] or 11 in f["high_hours"]
    assert f["amp_avg"] == pytest.approx((4400 - 3400) / 3400, abs=0.02)
    assert f["trend_7d"] == -2.0 and f["implied_fee"] == pytest.approx(0.14, abs=0.001)
    assert 0 <= f["pct_rank"] <= 1


def test_features_cold_start_uses_daily_and_says_so(cfg, db):
    db.upsert_item(626, ITEM["name"], 5, 11)
    for i in range(10):
        d = (NOW - timedelta(days=i)).strftime("%Y-%m-%d")
        db.execute("INSERT INTO day_prices(oid,d,price,pmin,pmax,min_at,max_at) VALUES(?,?,?,?,?,?,?)",
                   (626, d, 3900, 3400 + i, 4400 - i, f"{d} 03:10", f"{d} 18:40"))
    db.execute("INSERT INTO snapshots(oid,ts,price) VALUES(626,'2026-11-13 21:30',3900)")
    f = F.compute(cfg, db, ITEM, NOW)
    assert f["basis"] == "daily"
    assert 3400 < f["low_ref"] < 3900 < f["high_ref"] < 4400  # 向均价收缩，比极值保守
    assert f["low_hours"] == [3] and f["high_hours"] == [18]
    assert any("分钟历史不足" in w for w in f["warnings"])


def test_features_no_data_is_reported_not_invented(cfg, db):
    f = F.compute(cfg, db, ITEM, NOW)
    assert f["current"] is None and f["low_ref"] is None and f["warnings"]
    r = rules.baseline(cfg, f)
    assert r["action"] == "watch" and r["buy_price"] is None


def test_stale_price_is_flagged(cfg, db):
    seed_minutes(db)
    f = F.compute(cfg, db, ITEM, NOW + timedelta(hours=10))
    assert f["stale_hours"] == 10 and any("过期" in w for w in f["warnings"])


def test_rule_buy_when_spread_covers_fee(cfg, db):
    seed_minutes(db, low=3400, high=4400)                    # 振幅 29%，远超 13% 费率
    r = rules.baseline(cfg, F.compute(cfg, db, ITEM, NOW))
    assert r["action"] == "buy"
    assert r["stop_price"] < r["buy_price"] < r["breakeven"] < r["sell_price"]
    assert r["sell_price"] >= r["breakeven"] * (1 + cfg.get("rules.min_margin")) - 1
    assert r["stop_price"] <= 3400 * 0.98                    # 止损在近期最低价下方
    assert "实亏" in r["risk"]


def test_rule_watch_when_spread_too_small(cfg, db):
    seed_minutes(db, low=3800, high=4100)                    # 振幅 8%，不够付手续费
    r = rules.baseline(cfg, F.compute(cfg, db, ITEM, NOW))
    assert r["action"] == "watch" and "空间不足" in r["reason"]
    assert r["buy_price"] and r["sell_price"]                # 仍给出参考线


def test_rule_blocks_falling_knife(cfg, db):
    seed_minutes(db)
    db.execute("UPDATE snapshots SET day7_bl=-25")
    r = rules.baseline(cfg, F.compute(cfg, db, ITEM, NOW))
    assert r["action"] == "watch" and "跌幅" in r["reason"]


def test_stop_line_takes_the_lower_of_two(cfg):
    assert rules.stop_line(cfg, 3600, None) == 3240          # 买入线 × 0.9
    assert rules.stop_line(cfg, 3600, 3000) == 2940          # 近期最低 × 0.98 更低
    assert rules.stop_line(cfg, 3600, 3500) == 3240


def test_fee_check_warns_only_on_large_gap(cfg):
    assert F.fee_check(cfg, [{"implied_fee": 0.14}, {"implied_fee": 0.135}]) is None
    msg = F.fee_check(cfg, [{"implied_fee": 0.20}, {"implied_fee": 0.21}, {"implied_fee": None}])
    assert msg and "20" in msg
    assert F.fee_check(cfg, [{}]) is None
