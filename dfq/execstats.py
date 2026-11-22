"""真实成交统计：建议到底有没有真的成交、成交价差多少、钱被占用多久、实际赚没赚。

为什么单独做：复盘（review.perf）是“理论触线统计”——按之后的行情回看，价格碰到买入线就当成交，
这既不代表你真的挂上了单，也不代表能按那个价成交足量。要判断策略在真实世界里是否有效，只能看成交账本。

这里**只用两样数据**：成交账本（trades，记哪本账由 paper 决定）和建议记录（decisions，final 版）。
不用任何行情数据，所以不可能被“人为生成的行情”美化；期初库存（kind='opening'）不计入成交率和滑点。

口径：
- 建议成交率：窗口内有数量的买入建议里，在有效期内真的有同品种买入记录的比例（写出分子分母）；
  同一笔买入只归给它之前最近的那条建议。对照“理论触线率”（decisions.buy_hit）。
- 滑点：买入 = 成交价 / 建议买入线 − 1（正数 = 买贵了）；卖出 = 成交价 / 当时计划卖出线 − 1（负数 = 卖便宜了），
  卖在止损线及以下的单独计数、不算滑点。
- 持有时间与资金占用：先进先出配对买卖；资金·天 = 买入金额 × 持有天数；效率 = 盈亏 ÷ 资金·天 × 一百万。
- 已实现盈亏：每笔卖出按移动加权平均成本算（与账本、余额一致）；填了实际到手金额（net）就用 net。
  卖出时账上没有持仓的部分（旧版本允许的超卖）没有成本依据，不计盈亏。
"""
from __future__ import annotations

import statistics
from collections import defaultdict, deque
from datetime import datetime, timedelta

from . import fees, timeutil

MOCK_BANNER = "【模拟数据】人为生成的行情，只用于演示，不能说明策略是否赚钱"


def _kind(r) -> str:
    try:
        return r["kind"] or "trade"
    except (IndexError, KeyError):
        return "trade"


def _net(r) -> float | None:
    try:
        v = r["net"]
    except (IndexError, KeyError):
        return None
    return float(v) if v is not None else None


def _rows(db, paper: bool):
    return db.query("SELECT * FROM trades WHERE paper=? ORDER BY ts, id", (1 if paper else 0,))


def _proceeds(r, covered: int, fee: fees.FeeArg) -> float:
    """这笔卖出中有成本依据的 covered 发实际到手多少：有 net 按比例分摊 net，否则按费率估。"""
    net = _net(r)
    if net is not None and r["qty"]:
        return net * covered / r["qty"]
    return fees.net_proceeds(r["price"], fees.rate_for(fee, r["name"])) * covered


def sell_results(db, fee: fees.FeeArg = fees.DEFAULT_FEE, *, paper: bool = False) -> list[dict]:
    """逐笔卖出的已实现盈亏（移动加权平均成本，和 ledger.positions 同一口径），按时间顺序。

    只返回有成本依据（covered > 0）的卖出；每项 {id, ts, name, qty, covered, price, pnl, used_net}。
    """
    pos: dict[str, dict] = {}
    out: list[dict] = []
    for r in _rows(db, paper):
        p = pos.setdefault(r["name"], {"qty": 0, "avg": 0.0})
        if r["side"] == "buy":
            total = p["avg"] * p["qty"] + r["price"] * r["qty"]
            p["qty"] += r["qty"]
            p["avg"] = total / p["qty"] if p["qty"] else 0.0
            continue
        covered = min(r["qty"], p["qty"]) if p["qty"] > 0 else 0
        if covered > 0:
            pnl = _proceeds(r, covered, fee) - p["avg"] * covered
            out.append({"id": r["id"], "ts": r["ts"], "name": r["name"], "qty": r["qty"], "covered": covered,
                        "price": r["price"], "pnl": pnl, "used_net": _net(r) is not None})
        p["qty"] -= covered
        if p["qty"] == 0:
            p["avg"] = 0.0
    return out


def fifo_lots(db, fee: fees.FeeArg = fees.DEFAULT_FEE, *, paper: bool = False,
              now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """先进先出配对：返回 (已平仓的批次, 仍持有的批次)。期初库存的批次带 opening=True。"""
    now = now or timeutil.now()
    lots: dict[str, deque] = defaultdict(deque)
    closed: list[dict] = []
    for r in _rows(db, paper):
        if r["side"] == "buy":
            lots[r["name"]].append({"ts": r["ts"], "qty": r["qty"], "price": r["price"],
                                    "opening": _kind(r) == "opening"})
            continue
        left = r["qty"]
        per_unit = _proceeds(r, 1, fee)          # 每发到手：有 net 按 net 平摊，否则按费率估
        q = lots[r["name"]]
        while left > 0 and q:
            lot = q[0]
            take = min(left, lot["qty"])
            days = max((timeutil.parse_dt(r["ts"]) - timeutil.parse_dt(lot["ts"])).total_seconds() / 86400, 0.0)
            capital = lot["price"] * take
            closed.append({"name": r["name"], "qty": take, "buy_ts": lot["ts"], "sell_ts": r["ts"],
                           "buy_price": lot["price"], "days": days, "capital": capital,
                           "capital_days": capital * days, "pnl": (per_unit - lot["price"]) * take,
                           "opening": lot["opening"]})
            lot["qty"] -= take
            left -= take
            if lot["qty"] == 0:
                q.popleft()
    still = []
    for name, q in lots.items():
        for lot in q:
            days = max((now - timeutil.parse_dt(lot["ts"])).total_seconds() / 86400, 0.0)
            still.append({"name": name, "qty": lot["qty"], "buy_ts": lot["ts"], "buy_price": lot["price"],
                          "days": days, "capital": lot["price"] * lot["qty"], "opening": lot["opening"]})
    return closed, still


def _dist(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {"n": len(vals), "mean": sum(vals) / len(vals), "median": statistics.median(vals),
            "min": min(vals), "max": max(vals)}


def compute(cfg, db, *, days: int = 30, paper: bool = False, now: datetime | None = None) -> dict:
    now = now or timeutil.now()
    since = timeutil.fmt(now - timedelta(days=days))
    fee = cfg.fee_for
    pflag = 1 if paper else 0
    trades = [r for r in _rows(db, paper)]
    real_trades = [r for r in trades if _kind(r) == "trade"]
    out: dict = {"days": days, "paper": paper, "since": since, "now": timeutil.fmt(now),
                 "trades_total": len(real_trades), "trades_in_window": sum(1 for r in real_trades if r["ts"] >= since)}

    # —— 建议成交率 ——
    decs = db.query("SELECT * FROM decisions WHERE variant='final' AND action='buy' AND COALESCE(qty,0)>0 "
                    "AND paper=? AND created_at>=? ORDER BY created_at, id", (pflag, since))
    by_name: dict[str, list] = defaultdict(list)
    for d in decs:
        by_name[d["name"]].append(d)
    filled: dict[int, list] = defaultdict(list)
    buy_slip: list[float] = []
    unmatched_buys = 0
    for r in real_trades:
        if r["side"] != "buy" or r["ts"] < since:
            continue
        cands = [d for d in by_name.get(r["name"], []) if d["valid_from"] <= r["ts"] <= d["valid_until"]]
        if not cands:
            unmatched_buys += 1
            continue
        d = cands[-1]                       # 归给成交之前最近的那条建议
        filled[d["id"]].append(r)
        if d["buy_price"]:
            buy_slip.append(r["price"] / d["buy_price"] - 1)
    scored = [d for d in decs if d["buy_hit"] is not None]
    out["fill"] = {
        "suggestions": len(decs), "filled": sum(1 for d in decs if filled.get(d["id"])),
        "suggested_qty": sum(int(d["qty"] or 0) for d in decs),
        "filled_qty": sum(min(sum(t["qty"] for t in filled.get(d["id"], [])), int(d["qty"] or 0)) for d in decs),
        "theory_scored": len(scored), "theory_hit": sum(1 for d in scored if d["buy_hit"]),
        "unmatched_buys": unmatched_buys,
    }

    # —— 卖出滑点：相对成交时最近一份计划里的卖出线 ——
    all_decs = db.query("SELECT name, created_at, valid_until, sell_price, stop_price FROM decisions "
                        "WHERE variant='final' AND paper=? AND sell_price IS NOT NULL ORDER BY created_at, id", (pflag,))
    sell_ref: dict[str, list] = defaultdict(list)
    for d in all_decs:
        sell_ref[d["name"]].append(d)
    sell_slip: list[float] = []
    stop_sells = 0
    for r in real_trades:
        if r["side"] != "sell" or r["ts"] < since:
            continue
        refs = [d for d in sell_ref.get(r["name"], []) if d["created_at"] <= r["ts"] <= d["valid_until"]]
        if not refs:
            continue
        d = refs[-1]
        if d["stop_price"] and r["price"] <= d["stop_price"]:
            stop_sells += 1
            continue
        sell_slip.append(r["price"] / d["sell_price"] - 1)
    out["slippage"] = {"buy": _dist(buy_slip), "sell": _dist(sell_slip), "stop_sells": stop_sells}

    # —— 持有时间与资金占用（先进先出）——
    closed, still = fifo_lots(db, fee, paper=paper, now=now)
    win = [c for c in closed if c["sell_ts"] >= since and not c["opening"]]
    cap_days = sum(c["capital_days"] for c in win)
    out["holding"] = {
        "lots": len(win), "days": _dist([c["days"] for c in win]),
        "capital_days": cap_days, "pnl": sum(c["pnl"] for c in win),
        "pnl_per_million_day": (sum(c["pnl"] for c in win) / cap_days * 1e6) if cap_days > 0 else None,
        "open_capital": sum(s["capital"] for s in still if not s["opening"]),
        "open_days": _dist([s["days"] for s in still if not s["opening"]]),
        "opening_lots_excluded": sum(1 for c in closed if c["sell_ts"] >= since and c["opening"]),
    }

    # —— 已实现盈亏（移动平均成本，与账本一致）——
    res = [s for s in sell_results(db, fee, paper=paper) if s["ts"] >= since]
    wins = [s["pnl"] for s in res if s["pnl"] > 0]
    losses = [s["pnl"] for s in res if s["pnl"] <= 0]
    out["realized"] = {"sells": len(res), "wins": len(wins), "losses": len(losses),
                       "win_rate": len(wins) / len(res) if res else None,
                       "avg_win": sum(wins) / len(wins) if wins else None,
                       "avg_loss": sum(losses) / len(losses) if losses else None,
                       "total": sum(s["pnl"] for s in res), "with_net": sum(1 for s in res if s["used_net"])}
    return out


def _pct(x) -> str:
    return "—" if x is None else f"{x:+.2%}"


def _rate(a: int, b: int) -> str:
    return f"{a}/{b}" + (f"（{a / b:.0%}）" if b else "（—）")


def format_stats(s: dict, *, mock: bool = False) -> str:
    book = "模拟账本" if s["paper"] else "真实账本"
    L: list[str] = []
    if mock:
        L += [MOCK_BANNER, ""]
    L.append(f"成交统计（{book}，近 {s['days']} 天：{s['since'][:10]} → {s['now'][:10]}）")
    L.append(f"数据来源：{book}的成交记录 + 计划里的建议记录；不使用任何行情数据。")
    f = s["fill"]
    if s["trades_total"] == 0:
        L += ["", (f"{book}还没有成交记录，无法统计{'模拟' if s['paper'] else '真实'}成交率和滑点；"
                   f"下面只有理论触线统计，不能代表真实收益。"),
              (f"  理论触线率（按之后的行情回看，价格碰到买入线就算）：{_rate(f['theory_hit'], f['theory_scored'])}"
               f"　窗口内有数量的买入建议 {f['suggestions']} 条")]
        return "\n".join(L) + "\n"
    L += ["", "【建议成交率】",
          (f"  有效期内真的买到了：{_rate(f['filled'], f['suggestions'])}　成交数量占建议数量 "
           f"{_rate(f['filled_qty'], f['suggested_qty'])}"),
          f"  对照：理论触线率 {_rate(f['theory_hit'], f['theory_scored'])}（按之后的行情回看，不代表真的成交）"]
    if f["unmatched_buys"]:
        L.append(f"  另有 {f['unmatched_buys']} 笔买入不在任何建议的有效期内（自主操作），不计入成交率")
    sl = s["slippage"]
    L += ["", "【滑点】（成交价相对计划价）"]
    for key, label, hint in (("buy", "买入", "正数 = 比买入线买得贵"), ("sell", "卖出", "负数 = 比卖出线卖得便宜")):
        d = sl[key]
        L.append(f"  {label}：" + (f"{d['n']} 笔，均值 {_pct(d['mean'])}，中位数 {_pct(d['median'])}（{hint}）"
                                  if d else "没有可对照的成交"))
    if sl["stop_sells"]:
        L.append(f"  卖在止损线及以下的 {sl['stop_sells']} 笔单独计数，不算滑点")
    h = s["holding"]
    L += ["", "【持有时间与资金占用】（先进先出配对）"]
    if h["lots"]:
        L.append(f"  已平仓 {h['lots']} 批，持有天数中位数 {h['days']['median']:.1f}（最长 {h['days']['max']:.1f}）")
        L.append(f"  资金占用 {h['capital_days'] / 1e6:,.2f} 百万·天，盈亏 {h['pnl']:+,.0f}；"
                 f"每占用一百万一天 {h['pnl_per_million_day']:+,.0f}" if h["pnl_per_million_day"] is not None
                 else f"  盈亏 {h['pnl']:+,.0f}（持有不足一天，无法折算资金效率）")
    else:
        L.append("  窗口内没有已平仓的批次")
    if h["open_capital"]:
        L.append(f"  仍占用资金 {h['open_capital']:,.0f}，最久的已持有 {h['open_days']['max']:.1f} 天")
    if h["opening_lots_excluded"]:
        L.append(f"  期初库存配对出的 {h['opening_lots_excluded']} 批不计入持有时间（它的买入时间是你补录的）")
    r = s["realized"]
    L += ["", "【已实现盈亏】（扣费后；填了实际到手金额的按实际到手算）"]
    if r["sells"]:
        L.append(f"  卖出 {r['sells']} 笔：盈 {r['wins']}、亏 {r['losses']}（胜率 {r['win_rate']:.0%}）；"
                 f"平均盈 {r['avg_win'] or 0:+,.0f}、平均亏 {r['avg_loss'] or 0:+,.0f}；合计 {r['total']:+,.0f}")
        L.append(f"  其中 {r['with_net']} 笔用了实际到手金额，其余按配置的费率估算")
    else:
        L.append("  窗口内没有有成本依据的卖出")
    n = r["sells"]
    if 0 < n < 20:
        L += ["", f"提示：只有 {n} 笔卖出，样本太少，结论不稳；至少攒到 20～30 笔再判断策略好坏。"]
    return "\n".join(L) + "\n"
