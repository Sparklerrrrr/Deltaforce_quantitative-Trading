"""资金损失约束：在护栏（guardrails）里执行，位置在模型输出之后，所以模型绕不过。

五条约束（配置键都在 risk.*）：
1. 单笔最大亏损 max_loss_per_trade_pct：一笔买入如果打到止损线，实亏（含卖出费）不得超过总资产的这个比例，
   超了就削减数量，削到不足 min_qty 就观望。                         → max_qty_for_loss()
2. 最大持仓时间 max_holding_days：超过 → 卖出线降到回本价；超过两倍 → 建议按现价卖出（时间止损）。 → holding_days()
3. 累计亏损上限 max_cumulative_loss_pct / loss_window_days：窗口内已实现亏损超过资金基准 × 比例 → 暂停新的买入。
4. 策略失效检测 halt_after_losses：最近连续 N 笔已平仓交易都亏 → 暂停新的买入。
5. 异常波动保护 abnormal_move_pct：今日涨跌或现价偏离近 7 日中位价超过它 → 当天不买（涨跌都算）。 → abnormal_reason()

3、4 触发后会“锁住”（kv risk_halt_<账本>），卖出 / 止损照常，新的买入一直暂停到你确认（`dfq risk resume`）；
确认之后只有出现**新的**亏损才会再次触发。真实账本和模拟账本各算各的；--mock 的数据在另一个数据库里，三者绝不混算。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from . import fees, timeutil


@dataclass(frozen=True)
class RiskLimits:
    max_loss_per_trade_pct: float
    max_holding_days: int
    max_cumulative_loss_pct: float
    loss_window_days: int
    halt_after_losses: int
    abnormal_move_pct: float
    min_qty: int


@dataclass(frozen=True)
class RiskSnapshot:
    paper: bool
    capital_base: float          # 资金基准：填过余额用余额基准，否则用 risk.assumed_capital
    capital_known: bool
    window_days: int
    window_pnl: float            # 窗口内已实现盈亏合计（扣费后）
    window_loss: float           # = max(−window_pnl, 0)
    consecutive_losses: int      # 最近连续亏损的平仓笔数（只数上次确认之后的）
    losses_since_ack: int        # 上次确认之后出现的亏损平仓笔数（从没确认过 = 全部）
    acked_at: str | None         # 上次 `risk resume` 的时间
    latched: dict | None         # 之前触发、还没确认的暂停 {"since", "reason"}
    closed_sells: int            # 有成本依据的卖出总笔数
    last_loss_at: str | None


def _book(paper: bool) -> str:
    return "paper" if paper else "real"


def _ack_key(paper: bool) -> str:
    return f"risk_ack_{_book(paper)}"


def _halt_key(paper: bool) -> str:
    return f"risk_halt_{_book(paper)}"


def limits(cfg) -> RiskLimits:
    R = lambda k: cfg.get(f"risk.{k}")  # noqa: E731
    return RiskLimits(
        max_loss_per_trade_pct=float(R("max_loss_per_trade_pct")),
        max_holding_days=int(R("max_holding_days")),
        max_cumulative_loss_pct=float(R("max_cumulative_loss_pct")),
        loss_window_days=int(R("loss_window_days")),
        halt_after_losses=int(R("halt_after_losses")),
        abnormal_move_pct=float(R("abnormal_move_pct")),
        min_qty=int(R("min_qty")),
    )


def snapshot(cfg, db, *, paper: bool, now: datetime | None = None) -> RiskSnapshot:
    from . import execstats
    now = now or timeutil.now()
    lim = limits(cfg)
    base = db.kv_get("balance_base")
    known = isinstance(base, dict) and isinstance(base.get("amount"), (int, float))
    capital = float(base["amount"]) if known else float(cfg.get("risk.assumed_capital"))
    sells = execstats.sell_results(db, cfg.fee_for, paper=paper)
    since = timeutil.fmt(now - timedelta(days=lim.loss_window_days))
    window_pnl = sum(s["pnl"] for s in sells if s["ts"] >= since)
    ack = db.kv_get(_ack_key(paper))
    acked_at = ack.get("at") if isinstance(ack, dict) else None
    after = [s for s in sells if acked_at is None or s["ts"] > acked_at]
    streak = 0
    for s in reversed(after):
        if s["pnl"] < 0:
            streak += 1
        else:
            break
    latched = db.kv_get(_halt_key(paper))
    losses = [s for s in sells if s["pnl"] < 0]
    return RiskSnapshot(
        paper=paper, capital_base=capital, capital_known=known, window_days=lim.loss_window_days,
        window_pnl=window_pnl, window_loss=max(-window_pnl, 0.0), consecutive_losses=streak,
        losses_since_ack=sum(1 for s in after if s["pnl"] < 0), acked_at=acked_at,
        latched=latched if isinstance(latched, dict) and latched.get("reason") else None,
        closed_sells=len(sells), last_loss_at=losses[-1]["ts"] if losses else None)


def buy_halt_reason(snap: RiskSnapshot, lim: RiskLimits) -> str | None:
    """是否暂停新的买入：返回写进计划的中文原因；None = 不暂停。

    保守实现：任何一条上限被突破就暂停；你确认（risk resume）之后，只有出现新的亏损才会再次触发——
      · 连续亏损只数确认之后的平仓（确认 = 你看过了，重新开始计数）；
      · 累计亏损仍按整个窗口算，但确认之后必须再出现至少一笔新的亏损才重新暂停；
      · 之前触发过、还没确认的暂停一直保持（即使亏损已经滚出窗口）。
    """
    # TODO(human): 这里的判定规则和优先级由使用者最终确认（见 docs/发布前审查报告.md 的“待你决定”）
    reasons: list[str] = []
    if snap.consecutive_losses >= lim.halt_after_losses:
        reasons.append(f"最近连续 {snap.consecutive_losses} 笔平仓都是亏损（上限 {lim.halt_after_losses} 笔），"
                       f"策略可能已经失效")
    cap = snap.capital_base * lim.max_cumulative_loss_pct
    if snap.window_loss > cap and (snap.acked_at is None or snap.losses_since_ack > 0):
        reasons.append(f"近 {snap.window_days} 天已实现亏损 {snap.window_loss:,.0f}，超过资金基准 "
                       f"{snap.capital_base:,.0f} 的 {lim.max_cumulative_loss_pct:.0%}（{cap:,.0f}）")
    if not reasons and snap.latched:
        reasons.append(f"{str(snap.latched.get('since', ''))[:16]} 触发的暂停还没有确认："
                       f"{snap.latched.get('reason')}")
    return "；".join(reasons) or None


def status(cfg, db, *, paper: bool, now: datetime | None = None, persist: bool = True) -> dict:
    """当前风控状态。persist=True 时，新触发的暂停会被锁住（写进 kv），直到 resume。"""
    now = now or timeutil.now()
    lim = limits(cfg)
    snap = snapshot(cfg, db, paper=paper, now=now)
    reason = buy_halt_reason(snap, lim)
    if reason and persist and snap.latched is None:
        db.kv_set(_halt_key(paper), {"since": timeutil.fmt(now), "reason": reason})
    return {"halted": reason is not None, "reason": reason, "limits": asdict(lim),
            "cum_loss": round(snap.window_loss, 1),
            "cum_loss_pct": round(snap.window_loss / snap.capital_base, 4) if snap.capital_base else None,
            "consecutive_losses": snap.consecutive_losses, "acked_at": snap.acked_at,
            "capital_base": snap.capital_base, "capital_known": snap.capital_known,
            "window_days": snap.window_days, "window_pnl": round(snap.window_pnl, 1),
            "closed_sells": snap.closed_sells, "paper": paper}


def resume(db, *, paper: bool, now: datetime | None = None) -> dict:
    """你确认看过了：解除锁住的暂停，并记下确认时间（之后只有新的亏损才会再次触发）。"""
    now = now or timeutil.now()
    prev = db.kv_get(_halt_key(paper))
    at = timeutil.fmt(now)
    db.kv_set(_ack_key(paper), {"at": at})
    db.kv_set(_halt_key(paper), None)
    return {"acked_at": at, "was_halted": isinstance(prev, dict) and bool(prev.get("reason")),
            "previous_reason": prev.get("reason") if isinstance(prev, dict) else None}


def describe(st: dict) -> str:
    """一行风控状态，写进计划邮件的“系统状态”。"""
    book = "模拟账本" if st["paper"] else "真实账本"
    lim = st["limits"]
    head = (f"风控（{book}）：近 {st['window_days']} 天已实现盈亏 {st['window_pnl']:+,.0f}"
            f"（亏损上限 {lim['max_cumulative_loss_pct']:.0%} × 资金基准 {st['capital_base']:,.0f}），"
            f"连续亏损 {st['consecutive_losses']}/{lim['halt_after_losses']} 笔；"
            f"单笔止损实亏 ≤ 总资产 {lim['max_loss_per_trade_pct']:.0%}，持仓超过 {lim['max_holding_days']} 天降到回本价卖")
    if st["halted"]:
        return head + f"。⚠ 已暂停新的买入：{st['reason']}"
    return head + "。正常"


# ------------------------------------------------------------------ 给护栏用的小工具（纯函数）
def max_qty_for_loss(equity: float, buy: float, stop: float, fee: float, pct: float) -> int | None:
    """单笔最大亏损对应的数量上限 = 总资产 × 比例 ÷ 每发在止损线卖出的实亏；止损不亏（不可能）时返回 None。"""
    loss_each = buy - fees.net_proceeds(stop, fee)
    if loss_each <= 0:
        return None
    return int(max(equity, 0.0) * pct // loss_each)


def holding_days(held_since: str | None, now: datetime) -> float | None:
    if not held_since:
        return None
    try:
        return (now - timeutil.parse_dt(held_since)).total_seconds() / 86400
    except ValueError:
        return None


def abnormal_reason(f: dict, lim: RiskLimits) -> str | None:
    """异常波动保护：今日涨跌（快照 bl）或现价相对近 7 日中位价的偏离超过阈值（涨跌都算）。"""
    th = lim.abnormal_move_pct
    parts = []
    ct = f.get("change_today")
    if isinstance(ct, (int, float)) and abs(ct) > th:
        parts.append(f"今日涨跌 {ct:+.1f}%")
    dev = f.get("dev_from_median_pct")
    if isinstance(dev, (int, float)) and abs(dev) > th:
        parts.append(f"现价偏离近 7 日中位价 {f.get('median_7d') or 0:.0f} 达 {dev:+.1f}%")
    if not parts:
        return None
    return f"异常波动保护：{'，'.join(parts)}，超过 {th:g}%，当天不买"
