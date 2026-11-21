"""复盘：用真实价格给每条建议和每个信源打分。

建议的打分规则（对 final 和 rule 两个版本一视同仁）：
  1. 有效期内价格是否触及买入线？没触及 → 未成交（miss），收益记 0。
  2. 触及后，在“持有观察期”（默认 48 小时）内：
       先到卖出线 → win，  收益 = 卖出线×(1−费率)/买入线 − 1
       先到止损线 → stop， 收益 = 止损线×(1−费率)/买入线 − 1
       都没到     → hold， 按观察期末的价格×(1−费率) 估值
  3. 同一根 K 线里既到止损又到卖出时，保守地算止损。
注意：这是“理论触线统计”（按之后的行情回看，价格碰到买入线就当成交），不代表你真的挂了单、
也不代表能按这个价成交足量。真实成交率、滑点、资金占用和实际盈亏看 execstats（python -m dfq exec-stats）。

信源的打分：观点给出后 N 天，价格方向对了记命中；信用分 = (命中+1)/(总数+2)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import fees, timeutil
from .log import event, get_logger

log = get_logger("review")


def _path(db, oid: int, start: datetime, end: datetime) -> tuple[list[tuple[datetime, float, float]], str]:
    """返回 [(时间, 低, 高)] 的价格路径和所用粒度。优先分钟，缺失的日子用日线补。"""
    mins = db.minutes(oid, start, end)
    pts = [(t, p, p) for t, p in mins]
    covered = {timeutil.day_str(t) for t, _ in mins}
    gran = "minute" if mins else "day"
    d = start.replace(hour=0, minute=0, second=0, microsecond=0)
    used_day = False
    while d <= end:
        ds = timeutil.day_str(d)
        day_end = d + timedelta(hours=23, minutes=59)
        # 只有整天都落在窗口内、且没有分钟数据时才用日线，否则会把窗口外的高低点算进来
        if ds not in covered and d >= start and day_end <= end:
            r = db.one("SELECT pmin,pmax FROM day_prices WHERE oid=? AND d=?", (oid, ds))
            if r and r["pmin"] is not None and r["pmax"] is not None:
                pts.append((d + timedelta(hours=12), r["pmin"], r["pmax"]))
                used_day = True
        d += timedelta(days=1)
    pts.sort(key=lambda x: x[0])
    if used_day and mins:
        gran = "mixed"
    return pts, gran


def score_decision(db, dec, fee: float, horizon_h: float, now: datetime) -> dict | None:
    """给一条 buy 建议打分。数据还不够下结论时返回 status='open'。"""
    vf, vu = timeutil.parse_dt(dec["valid_from"]), timeutil.parse_dt(dec["valid_until"])
    buy, sell, stop = dec["buy_price"], dec["sell_price"], dec["stop_price"]
    qty = dec["qty"] or 0
    res = {"status": "open", "buy_hit": 0, "buy_hit_at": None, "outcome": None,
           "exit_price": None, "exit_at": None, "net_return": None, "net_profit": None, "note": ""}

    if dec["action"] != "buy" or not buy or not sell:
        res.update(status="final", outcome="watch", net_return=0.0, net_profit=0.0)
        return res

    path, gran = _path(db, dec["oid"], vf, min(vu + timedelta(hours=horizon_h), now))
    res["note"] = f"粒度={gran}"
    hit_at = None
    for t, lo, hi in path:
        if t > vu:
            break
        if lo <= buy:
            hit_at = t
            break
    if hit_at is None:
        if now >= vu:
            if not any(t <= vu for t, _, _ in path):
                res["note"] += "；有效期内没有价格数据，无法判断"
                res.update(status="final", outcome="nodata", net_return=None, net_profit=None)
            else:
                res.update(status="final", outcome="miss", net_return=0.0, net_profit=0.0)
        return res

    res.update(buy_hit=1, buy_hit_at=timeutil.fmt_min(hit_at))
    deadline = hit_at + timedelta(hours=horizon_h)
    last_px = None
    for t, lo, hi in path:
        if t <= hit_at:
            continue
        if t > deadline:
            break
        last_px = (lo + hi) / 2
        if stop and lo <= stop:
            res.update(status="final", outcome="stop", exit_price=stop, exit_at=timeutil.fmt_min(t))
            break
        if hi >= sell:
            res.update(status="final", outcome="win", exit_price=sell, exit_at=timeutil.fmt_min(t))
            break
    if res["status"] != "final":
        if now >= deadline:
            px = last_px if last_px is not None else db.price_at(dec["oid"], deadline, 12)
            if px is None:
                res.update(status="final", outcome="nodata")
                res["note"] += "；观察期末没有价格数据"
                return res
            res.update(status="final", outcome="hold", exit_price=px, exit_at=timeutil.fmt_min(deadline))
        else:
            return res          # 观察期还没走完
    res["net_return"] = round(fees.net_return(buy, res["exit_price"], fee), 5)
    res["net_profit"] = round(fees.net_profit(buy, res["exit_price"], qty, fee), 1)
    return res


def score_open_decisions(cfg, db, now: datetime | None = None) -> dict:
    now = now or timeutil.now()
    horizon = float(cfg.get("review.sell_horizon_hours", 48))
    n_final = n_open = 0
    for dec in db.query("SELECT * FROM decisions WHERE status='open' ORDER BY id"):
        r = score_decision(db, dec, cfg.fee_for(dec["name"]), horizon, now)    # 按品种取费率
        if r is None:
            continue
        db.execute(
            "UPDATE decisions SET status=?, buy_hit=?, buy_hit_at=?, outcome=?, exit_price=?, exit_at=?,"
            " net_return=?, net_profit=?, scored_at=?, score_note=? WHERE id=?",
            (r["status"], r["buy_hit"], r["buy_hit_at"], r["outcome"], r["exit_price"], r["exit_at"],
             r["net_return"], r["net_profit"], timeutil.fmt(now), r["note"], dec["id"]))
        if r["status"] == "final":
            n_final += 1
        else:
            n_open += 1
    event(log, logging.INFO, "建议打分完成", node="review", finalized=n_final, still_open=n_open)
    return {"finalized": n_final, "open": n_open}


def score_opinions(cfg, db, now: datetime | None = None) -> int:
    now = now or timeutil.now()
    min_move = float(cfg.get("intel.opinion_min_move_pct", 2.0))
    n = 0
    for op in db.query("SELECT * FROM opinions WHERE status='open' AND oid IS NOT NULL "
                       "AND direction IN ('up','down','low') AND ref_price IS NOT NULL"):
        due = timeutil.parse_dt(op["collected_at"]) + timedelta(days=op["horizon_days"] or 3)
        if op["direction"] == "low":
            # 平台的“今日低价预测”：等这一天过完，看真实最低价离预测价是否在 ±3% 内
            d0 = timeutil.parse_dt(op["collected_at"]).replace(hour=0, minute=0, second=0)
            if now < d0 + timedelta(days=1):
                continue
            mins = db.minutes(op["oid"], d0, d0 + timedelta(hours=23, minutes=59))
            actual = min((p for _, p in mins), default=None) if len(mins) >= 60 else None
            if actual is None:
                r = db.one("SELECT pmin FROM day_prices WHERE oid=? AND d=?",
                           (op["oid"], timeutil.day_str(d0)))
                actual = r["pmin"] if r and r["pmin"] is not None else None
            if actual is None:
                if now > d0 + timedelta(days=4):
                    db.execute("UPDATE opinions SET status='nodata', scored_at=? WHERE id=?",
                               (timeutil.fmt(now), op["id"]))
                continue
            dev = (actual / op["ref_price"] - 1) * 100
            db.execute("UPDATE opinions SET status='final', hit=?, move_pct=?, scored_at=? WHERE id=?",
                       (1 if abs(dev) <= 3.0 else 0, round(dev, 2), timeutil.fmt(now), op["id"]))
            n += 1
            continue
        if now < due:
            continue
        px = db.price_at(op["oid"], due, 12)
        if px is None:
            if now > due + timedelta(days=3):       # 一直没数据，放弃
                db.execute("UPDATE opinions SET status='nodata', scored_at=? WHERE id=?",
                           (timeutil.fmt(now), op["id"]))
            continue
        move = (px / op["ref_price"] - 1) * 100
        if abs(move) < min_move:
            hit = 0                                  # 没怎么动：方向性观点算没说中
        else:
            hit = 1 if (move > 0) == (op["direction"] == "up") else 0
        db.execute("UPDATE opinions SET status='final', hit=?, move_pct=?, scored_at=? WHERE id=?",
                   (hit, round(move, 2), timeutil.fmt(now), op["id"]))
        n += 1
    return n


def source_scores(db) -> list[dict]:
    """各信源的信用分。样本少时分数会靠近 0.5（拉普拉斯平滑），不会因为一两次就定性。"""
    rows = db.query(
        "SELECT source_type, source_name, COUNT(*) AS n, COALESCE(SUM(hit),0) AS hits "
        "FROM opinions WHERE status='final' GROUP BY source_type, source_name")
    out = []
    for r in rows:
        out.append({"source_type": r["source_type"], "source_name": r["source_name"],
                    "n": r["n"], "hits": r["hits"],
                    "score": round((r["hits"] + 1) / (r["n"] + 2), 3)})
    pending = {(r["source_type"], r["source_name"]): r["c"] for r in db.query(
        "SELECT source_type, source_name, COUNT(*) AS c FROM opinions WHERE status='open' "
        "GROUP BY source_type, source_name")}
    known = {(o["source_type"], o["source_name"]) for o in out}
    for (st, sn), c in pending.items():
        if (st, sn) not in known:
            out.append({"source_type": st, "source_name": sn, "n": 0, "hits": 0, "score": 0.5})
    for o in out:
        o["pending"] = pending.get((o["source_type"], o["source_name"]), 0)
    return sorted(out, key=lambda o: -o["score"])


def _agg(rows) -> dict:
    sig = [r for r in rows if r["action"] == "buy" and r["outcome"] != "nodata"]
    trig = [r for r in sig if r["buy_hit"]]
    wins = [r for r in trig if r["outcome"] == "win"]
    stops = [r for r in trig if r["outcome"] == "stop"]
    rets = [r["net_return"] for r in trig if r["net_return"] is not None]
    closed = [r["net_return"] for r in wins + stops if r["net_return"] is not None]
    profit = sum(r["net_profit"] or 0 for r in trig)
    invested = sum((r["buy_price"] or 0) * (r["qty"] or 0) for r in trig)
    return {
        "signals": len(sig), "triggered": len(trig), "wins": len(wins), "stops": len(stops),
        "holds": len(trig) - len(wins) - len(stops),
        "trigger_rate": len(trig) / len(sig) if sig else None,
        "win_rate": len(wins) / len(trig) if trig else None,
        "avg_return": sum(rets) / len(rets) if rets else None,
        "avg_closed": sum(closed) / len(closed) if closed else None,
        "profit": profit, "invested": invested,
        "roi": profit / invested if invested else None,
    }


def perf(cfg, db, days: int | None = None) -> dict:
    """系统是否有用的唯一标准：最终建议 vs 纯规则基线 vs 什么都不做（收益恒为 0）。"""
    from . import ledger
    where, params = "status='final'", []
    if days:
        where += " AND created_at>=?"
        params.append(timeutil.fmt(timeutil.now() - timedelta(days=days)))
    rows = db.query(f"SELECT * FROM decisions WHERE {where}", params)
    out = {v: _agg([r for r in rows if r["variant"] == v]) for v in ("final", "rule")}
    out["open"] = db.one("SELECT COUNT(*) AS c FROM decisions WHERE status='open'")["c"]
    out["plans"] = db.one("SELECT COUNT(DISTINCT plan_id) AS c FROM decisions")["c"]
    out["real"] = ledger.realized_summary(db, cfg.fee_for)
    out["paper_book"] = ledger.realized_summary(db, cfg.fee_for, paper=True)
    out["days"] = days
    return out


def format_perf(p: dict) -> str:
    def pc(x):
        return "—" if x is None else f"{x:+.2%}"

    def rt(x):
        return "—" if x is None else f"{x:.0%}"

    lines = ["复盘绩效" + (f"（近 {p['days']} 天）" if p.get("days") else "（全部历史）"),
             THEORY_LABEL,
             f"已出计划 {p['plans']} 份，尚在观察期的建议 {p['open']} 条", ""]
    for key, label in (("final", "最终建议"), ("rule", "规则基线")):
        a = p[key]
        lines += [
            f"【{label}】",
            f"  买入信号 {a['signals']} 个 → 触发 {a['triggered']} 个（触发率 {rt(a['trigger_rate'])}）",
            f"  触发后：止盈 {a['wins']}　止损 {a['stops']}　到期仍持有 {a['holds']}　（止盈占比 {rt(a['win_rate'])}）",
            f"  扣费后单笔均收益：已平仓 {pc(a['avg_closed'])}　全部（未平仓按清算价估值）{pc(a['avg_return'])}",
            f"  理论总盈亏 {a['profit']:+,.0f}　投入资金收益率 {pc(a['roi'])}", ""]
    lines += ["【什么都不做】", "  盈亏 0　收益率 +0.00%", ""]
    f, r = p["final"], p["rule"]
    if f["triggered"] == 0 and r["triggered"] == 0:
        lines.append("结论：还没有触发并走完观察期的建议，暂时无法判断。继续积累数据。")
    else:
        diff = f["profit"] - r["profit"]
        verdict = "优于" if diff > 0 else ("等于" if diff == 0 else "劣于")
        lines.append(f"结论：最终建议{verdict}规则基线（理论盈亏差 {diff:+,.0f}）；"
                     f"相对什么都不做 {f['profit']:+,.0f}。")
        if f["triggered"] < 20:
            lines.append(f"提示：样本只有 {f['triggered']} 笔触发，结论还不稳，至少攒到 20～30 笔再下判断。")
    real = p["real"]
    lines.append("")
    lines.append(f"真实成交：{real['trades']} 笔，已实现盈亏（扣费后）{real['realized']:+,.0f}")
    for name, pos in real["open"].items():
        lines.append(f"  持仓 {name} ×{pos['qty']}，均价 {pos['avg_cost']:.0f}")
    pb = p.get("paper_book")
    if pb and pb["trades"]:
        lines.append(f"模拟账本（与真实成交完全分开）：{pb['trades']} 笔，已实现盈亏 {pb['realized']:+,.0f}")
        for name, pos in pb["open"].items():
            lines.append(f"  模拟持仓 {name} ×{pos['qty']}，均价 {pos['avg_cost']:.0f}")
    lines.append("")
    lines.append("口径说明：理论盈亏按“参考价触线即成交”计算，偏乐观；到期仍持有的按“当时立刻卖出、扣费”估值，偏悲观。")
    lines.append("真实盈亏以你记账的成交为准；真实成交率、滑点和资金占用：python -m dfq exec-stats")
    return "\n".join(lines)


THEORY_LABEL = "【理论触线统计】按之后的行情回看，价格碰到买入线就算成交——不代表你真的成交，也不代表真实收益"


def recent_summary(db, days: int = 7) -> dict:
    """给模型和邮件看的近期复盘摘要。"""
    since = timeutil.fmt(timeutil.now() - timedelta(days=days))
    rows = db.query("SELECT * FROM decisions WHERE status='final' AND variant='final' "
                    "AND created_at>=? ORDER BY created_at DESC", (since,))
    items = [{"date": r["created_at"][:10], "name": r["name"], "action": r["action"],
              "buy": r["buy_price"], "sell": r["sell_price"], "outcome": r["outcome"],
              "net_return": r["net_return"]} for r in rows][:40]
    rule_rows = db.query("SELECT * FROM decisions WHERE status='final' AND variant='rule' "
                         "AND created_at>=?", (since,))
    return {"days": days, "final": _agg(rows), "rule": _agg(rule_rows), "items": items}
