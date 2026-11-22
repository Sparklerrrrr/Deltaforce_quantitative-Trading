from datetime import datetime, timedelta

import pytest

from dfq import fees, guardrails as G, timeutil

NOW = datetime(2026, 11, 13, 21, 30)


def feat(oid=626, name="12.7x55mm PS12B", wmin=3300, wmax=4600):
    return {"oid": oid, "name": name, "window_min": wmin, "window_max": wmax, "current": 3900}


def rule(oid=626, name="12.7x55mm PS12B", buy=3600, sell=4400, stop=3234, action="buy"):
    return {"oid": oid, "name": name, "action": action, "buy_price": buy, "sell_price": sell,
            "stop_price": stop, "confidence": 0.5, "reason": "规则", "risk": ""}


def state(cash=3_000_000, positions=None, spent=0.0):
    positions = positions or {}
    pv = sum(p["value"] for p in positions.values())
    return {"cash": cash, "cash_known": True, "positions": positions, "positions_value": pv,
            "equity": cash + pv, "spent_today": spent}


def run(cfg, cands, feats=None, rules=None, st=None, **kw):
    feats = feats or {626: feat()}
    rules = rules or {626: rule()}
    return G.apply(cfg, cands, feats, rules, st or state(), NOW, **kw)


def test_sell_below_breakeven_is_downgraded_to_watch(cfg):
    # 买 3600 的回本价是 4138；卖出线 4100 必亏
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4100, "qty": 100}],
              rules={626: rule(sell=4100)})
    it = out["items"][0]
    assert it["action"] == "watch" and it["qty"] == 0
    assert any("回本" in g for g in it["guard_notes"])


def test_sell_must_also_cover_min_margin(cfg):
    cfg.set("rules.min_margin", 0.03)
    be = fees.breakeven_price(3600)                       # 4138
    just_be = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": be}],
                  rules={626: rule(sell=be)})
    assert just_be["items"][0]["action"] == "watch"       # 刚好回本不够
    ok = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4300}],
             rules={626: rule(sell=4300)})
    assert ok["items"][0]["action"] == "buy"


def test_model_cannot_move_lines_beyond_limit(cfg):
    cfg.set("risk.llm_max_adjust_pct", 0.10)
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 2000, "sell_price": 9000, "qty": 50}])
    it = out["items"][0]
    assert it["buy_price"] == 3240                        # 3600 × 0.9
    assert it["sell_price"] == 4830                       # min(4400 × 1.1, 近期最高 4600 × 1.05)
    assert len(it["guard_notes"]) >= 2


def test_model_cannot_turn_a_loser_into_a_buy(cfg):
    """规则说空间不足；模型硬说买，并把卖出线抬高 —— 抬高幅度被限制后仍过不了回本价。"""
    r = rule(buy=3600, sell=3700, action="watch")
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 5000, "qty": 500}],
              rules={626: r})
    it = out["items"][0]
    assert it["sell_price"] == 4070                       # 最多抬到 3700 × 1.1，仍低于回本要求
    assert it["action"] == "watch" and it["qty"] == 0


def test_quantity_cut_by_position_cap(cfg):
    cfg.set("risk.max_position_pct", 0.25)                # 单品种最多 75 万
    cfg.set("risk.max_loss_per_trade_pct", 1.0)           # 只测持仓占比这一道；单笔亏损上限另有测试（test_risk.py）
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4400, "qty": 5000}])
    it = out["items"][0]
    assert it["qty"] == 200                               # 750000 // 3600 = 208 → 按 10 取整
    assert it["budget"] == 200 * 3600 <= 750_000
    assert any("削减" in g for g in it["guard_notes"])


def test_daily_spend_cap_shared_across_items(cfg):
    cfg.set("risk.max_daily_spend_pct", 0.2)              # 全天最多 60 万
    cfg.set("risk.max_position_pct", 1.0)
    feats = {626: feat(), 640: feat(640, "5.7x28mm SS190")}
    rules = {626: rule(), 640: rule(640, "5.7x28mm SS190")}
    cands = [{"oid": o, "action": "buy", "buy_price": 3600, "sell_price": 4400, "qty": 1000} for o in (626, 640)]
    out = run(cfg, cands, feats, rules)
    assert sum(i["budget"] for i in out["items"]) <= 600_000
    assert out["caps"]["daily"] == 600_000


def test_already_spent_today_reduces_budget(cfg):
    cfg.set("risk.max_daily_spend_pct", 0.2)
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4400, "qty": 1000}],
              st=state(spent=550_000))
    it = out["items"][0]
    assert it["action"] == "watch" and any("最小下单量" in g for g in it["guard_notes"])   # 只剩 5 万，买不了 20 发


def test_total_exposure_cap_counts_existing_positions(cfg):
    cfg.set("risk.max_total_exposure_pct", 0.5)
    cfg.set("risk.max_position_pct", 1.0)
    cfg.set("risk.max_daily_spend_pct", 1.0)
    held = {"5.7x28mm SS190": {"oid": 640, "qty": 300, "avg_cost": 4000, "value": 1_200_000, "mark": 4000}}
    st = state(cash=1_800_000, positions=held)            # 总资产 300 万，上限 150 万，已占 120 万
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4400, "qty": 1000}], st=st)
    assert out["items"][0]["budget"] <= 300_000


def test_cannot_spend_more_than_cash(cfg):
    for k in ("max_position_pct", "max_daily_spend_pct", "max_total_exposure_pct"):
        cfg.set(f"risk.{k}", 1.0)
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4400, "qty": 9999}],
              st=state(cash=100_000))
    assert out["items"][0]["budget"] <= 100_000


def test_missing_qty_is_allocated_within_caps(cfg):
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4400, "qty": None}])
    it = out["items"][0]
    assert it["qty"] > 0 and it["qty"] % 10 == 0 and it["budget"] <= 750_000


def test_items_outside_watchlist_are_dropped(cfg):
    out = run(cfg, [{"oid": 99999, "name": "乱入", "action": "buy", "buy_price": 1, "sell_price": 9, "qty": 1}])
    assert out["items"] == [] and any("之外" in n for n in out["notes"])


@pytest.mark.parametrize("bad", [
    {"buy_price": None, "sell_price": 4400}, {"buy_price": "abc", "sell_price": 4400},
    {"buy_price": -5, "sell_price": 4400}, {"buy_price": 3600, "sell_price": 0},
])
def test_garbage_prices_become_watch(cfg, bad):
    out = run(cfg, [{"oid": 626, "action": "buy", "qty": 100, **bad}])
    assert out["items"][0]["action"] == "watch"


def test_unknown_action_becomes_watch(cfg):
    out = run(cfg, [{"oid": 626, "action": "ALL-IN", "buy_price": 3600, "sell_price": 4400, "qty": 100}])
    assert out["items"][0]["action"] == "watch"


def test_stop_is_always_below_buy(cfg):
    out = run(cfg, [{"oid": 626, "action": "buy", "buy_price": 3600, "sell_price": 4400, "stop_price": 3700}])
    it = out["items"][0]
    assert it["stop_price"] < it["buy_price"] < it["sell_price"]
    assert it["stop_net_return"] < -cfg.fee               # 止损的真实亏损一定超过费率


def test_validity_is_capped_and_expiry_detected(cfg):
    out = run(cfg, [], valid_until=NOW + timedelta(days=30))
    assert out["valid_until"] == NOW + timedelta(hours=cfg.get("risk.max_valid_hours"))
    out = run(cfg, [], valid_until=NOW - timedelta(hours=1))          # 已过期的时间无效 → 用默认
    assert out["valid_until"] == NOW + timedelta(hours=24)
    plan = {"valid_until": timeutil.fmt(NOW)}
    assert not G.is_expired(plan, NOW - timedelta(minutes=1))
    assert G.is_expired(plan, NOW + timedelta(minutes=1))
    assert G.is_expired({}, NOW)                                      # 缺字段按过期处理


def test_sell_instruction_for_position_never_below_cost_breakeven(cfg):
    held = {"12.7x55mm PS12B": {"oid": 626, "qty": 200, "avg_cost": 4000, "value": 780_000, "mark": 3900}}
    out = run(cfg, [{"oid": 626, "action": "watch"}], st=state(positions=held))
    s = out["sells"][0]
    assert s["sell_price"] == fees.breakeven_price(4000) and s["qty"] == 200   # 4598 > 规则卖出线 4400
    assert "回本价" in s["note"] and s["stop_price"] < 4000
