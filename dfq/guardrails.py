"""护栏：代码强制执行，模型的任何输出都必须经过这里，绕不过去。

1. 卖出线低于回本价（含最低利润率）的买入建议 → 降级为观望
2. 模型对规则阈值的调整幅度有上限，且不能超出近期真实价格区间
3. 数量按风控上限自动削减：每日最大投入、单品种最大持仓占比、总仓位上限
4. 有效期有上限；过期计划作废
5. 只允许关注清单内的品种；价格必须满足 止损 < 买入 < 卖出
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import fees, timeutil


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def portfolio_state(cfg, db) -> dict:
    """当前资金与持仓（真实成交口径）。余额未知时用 risk.assumed_capital 估算。"""
    from . import ledger
    fee = cfg.fee
    bal = ledger.balance(db, fee)
    pos = ledger.open_positions(db, fee)
    pos_value = 0.0
    for name, p in pos.items():
        snap = db.latest_snapshot(p["oid"]) if p.get("oid") else None
        px = snap["price"] if snap is not None and snap["price"] else p["avg_cost"]
        p["mark"] = px
        p["value"] = px * p["qty"]
        pos_value += p["value"]
    cash = bal["amount"] if bal["known"] else float(cfg.get("risk.assumed_capital"))
    return {"cash": max(cash, 0.0), "cash_known": bal["known"], "positions": pos,
            "positions_value": pos_value, "equity": max(cash, 0.0) + pos_value,
            "spent_today": ledger.spent_today(db)}


def apply(cfg, candidates: list[dict], feats: dict[int, dict], rule: dict[int, dict],
          state: dict, now: datetime | None = None, valid_until: datetime | None = None) -> dict:
    """candidates：模型（或规则）给出的建议；返回 {"items": [...], "valid_until", "notes": [...]}。"""
    now = now or timeutil.now()
    fee = cfg.fee
    R = lambda k: cfg.get(f"risk.{k}")  # noqa: E731
    min_margin = float(cfg.get("rules.min_margin", 0.0))
    stop_pct = float(cfg.get("rules.stop_pct", 0.10))
    adj = float(R("llm_max_adjust_pct"))
    lot = max(int(R("lot_size")), 1)
    min_qty = int(R("min_qty"))
    notes: list[str] = []

    # —— 有效期 ——
    max_valid = now + timedelta(hours=float(R("max_valid_hours")))
    default_valid = now + timedelta(hours=float(cfg.get("general.plan_valid_hours", 24)))
    if valid_until is None or valid_until <= now:
        valid_until = default_valid
    if valid_until > max_valid:
        notes.append(f"有效期超过上限，已截到 {timeutil.short(max_valid)}")
        valid_until = max_valid

    allowed = {w["oid"] for w in feats.values()} if feats else set()
    items: list[dict] = []
    for c in candidates:
        oid = c.get("oid")
        if oid not in allowed or oid not in rule:
            notes.append(f"丢弃关注清单之外的建议：{c.get('name') or oid}")
            continue
        f, r = feats[oid], rule[oid]
        g: list[str] = []
        it = {
            "oid": oid, "name": f["name"], "action": c.get("action") or "watch",
            "buy_price": c.get("buy_price"), "sell_price": c.get("sell_price"),
            "stop_price": c.get("stop_price"), "qty": c.get("qty"),
            "reason": c.get("reason") or r.get("reason") or "", "risk": c.get("risk") or r.get("risk") or "",
            "confidence": c.get("confidence", r.get("confidence", 0.0)),
            "rule": {k: r.get(k) for k in ("action", "buy_price", "sell_price", "stop_price")},
            "guard_notes": g, "current": f.get("current"),
        }
        if it["action"] not in ("buy", "watch"):
            g.append(f"未知动作 {it['action']!r}，按观望处理")
            it["action"] = "watch"

        if it["action"] == "buy":
            try:
                buy, sell = float(it["buy_price"]), float(it["sell_price"])
                if not (buy > 0 and sell > 0):
                    raise ValueError
            except (TypeError, ValueError):
                g.append("买卖线缺失或非法，降级为观望")
                it["action"] = "watch"

        if it["action"] == "buy":
            # 2) 调整幅度上限：相对规则线 ±adj；规则线不存在时只受价格区间约束
            if r.get("buy_price"):
                nb = _clamp(buy, r["buy_price"] * (1 - adj), r["buy_price"] * (1 + adj))
                if abs(nb - buy) > 0.5:
                    g.append(f"买入线 {buy:.0f} 超出规则线 ±{adj:.0%}，收回到 {nb:.0f}")
                    buy = nb
            if r.get("sell_price"):
                ns = _clamp(sell, r["sell_price"] * (1 - adj), r["sell_price"] * (1 + adj))
                if abs(ns - sell) > 0.5:
                    g.append(f"卖出线 {sell:.0f} 超出规则线 ±{adj:.0%}，收回到 {ns:.0f}")
                    sell = ns
            wmin, wmax = f.get("window_min"), f.get("window_max")
            if wmax and sell > wmax * 1.05:
                g.append(f"卖出线 {sell:.0f} 高于近期最高价 {wmax:.0f} 的 105%，收回")
                sell = wmax * 1.05
            if wmin and buy < wmin * 0.9:
                g.append(f"买入线 {buy:.0f} 低于近期最低价 {wmin:.0f} 的 90%，几乎不可能成交，收回")
                buy = wmin * 0.9
            buy, sell = int(round(buy)), int(round(sell))

            # 1) 回本价硬约束
            be = fees.breakeven_price(buy, fee)
            need = int(math.ceil(be * (1 + min_margin)))
            it["breakeven"] = be
            if sell < need:
                g.append(f"卖出线 {sell} 低于回本要求 {need}（回本价 {be}），降级为观望")
                it["action"] = "watch"

            # 5) 止损线
            stop = it["stop_price"]
            try:
                stop = int(round(float(stop))) if stop is not None else None
            except (TypeError, ValueError):
                stop = None
            from .rules import stop_line
            default_stop = stop_line(cfg, buy, f.get("window_min"))
            if stop is None or not (0 < stop < buy):
                stop = default_stop
            elif stop < buy * (1 - 3 * stop_pct):
                g.append(f"止损线 {stop} 过远，收到 {int(buy * (1 - 3 * stop_pct))}")
                stop = int(buy * (1 - 3 * stop_pct))
            it.update(buy_price=buy, sell_price=sell, stop_price=stop,
                      exp_net_return=round(fees.net_return(buy, sell, fee), 4),
                      stop_net_return=round(fees.net_return(buy, stop, fee), 4))
        if it["action"] != "buy":
            # 观望也保留参考线，便于事后看“如果买了会怎样”
            it["qty"], it["budget"] = 0, 0
            for k in ("buy_price", "sell_price", "stop_price"):
                if it.get(k) is None:
                    it[k] = r.get(k)
        items.append(it)

    # —— 3) 仓位：三道上限 ——
    equity = state["equity"]
    daily_cap = max(equity * float(R("max_daily_spend_pct")) - state["spent_today"], 0)
    total_cap = max(equity * float(R("max_total_exposure_pct")) - state["positions_value"], 0)
    pool = min(daily_cap, total_cap, state["cash"])
    buys = sorted((i for i in items if i["action"] == "buy"),
                  key=lambda i: -float(i.get("confidence") or 0))
    if buys:
        share = pool / len(buys)
    for it in buys:
        g = it["guard_notes"]
        held = state["positions"].get(it["name"], {}).get("value", 0.0)
        per_item_cap = max(equity * float(R("max_position_pct")) - held, 0)
        price = it["buy_price"]
        want = it.get("qty")
        try:
            want = int(want) if want else None
        except (TypeError, ValueError):
            want = None
        if want is None or want <= 0:
            want = int(share // price)             # 没给数量：平均分配
        cap_qty = int(min(per_item_cap, pool) // price)
        qty = min(want, cap_qty)
        qty -= qty % lot
        if qty < want - (want % lot):
            g.append(f"数量 {want} 超出风控上限，削减为 {qty}")
        if qty < min_qty:
            g.append(f"可买数量 {qty} 低于最小下单量 {min_qty}，降级为观望")
            it.update(action="watch", qty=0, budget=0)
            continue
        it["qty"] = qty
        it["budget"] = qty * price
        pool -= it["budget"]

    sells = build_sells(cfg, state, items)

    return {"items": items, "sells": sells, "valid_until": valid_until, "notes": notes,
            "caps": {"daily": round(daily_cap), "total": round(total_cap),
                     "per_item": round(equity * float(R("max_position_pct")))}}


def build_sells(cfg, state: dict, items: list[dict]) -> list[dict]:
    """给每个真实持仓生成卖出指令：卖出线不低于持仓回本价；止损线按持仓均价算。"""
    fee = cfg.fee
    stop_pct = float(cfg.get("rules.stop_pct", 0.10))
    by_name = {i["name"]: i for i in items}
    sells = []
    for name, p in state["positions"].items():
        plan_it = by_name.get(name)
        cost_be = fees.breakeven_price(p["avg_cost"], fee)
        target = plan_it["sell_price"] if plan_it and plan_it.get("sell_price") else cost_be
        note = ""
        if target < cost_be:
            note = f"今日卖出线 {target} 低于持仓回本价 {cost_be}，按回本价挂"
            target = cost_be
        sells.append({
            "oid": p.get("oid"), "name": name, "qty": p["qty"], "avg_cost": round(p["avg_cost"], 1),
            "breakeven": cost_be, "sell_price": int(target),
            "stop_price": int(min(math.floor(p["avg_cost"] * (1 - stop_pct)),
                                  (plan_it or {}).get("stop_price") or float("inf"))),
            "mark": p.get("mark"), "note": note,
        })
    return sells


def is_expired(plan: dict, now: datetime | None = None) -> bool:
    now = now or timeutil.now()
    try:
        return timeutil.parse_dt(plan["valid_until"]) < now
    except (KeyError, ValueError):
        return True
