"""规则基线：不调用模型也能出计划（`--offline`）。

逻辑故意保持简单、可解释、可事后打分：
  买入线 = 近 N 天价格的低分位（默认 15%）
  卖出线 = 近 N 天价格的高分位（默认 85%）
  止损线 = min(买入线 × (1 − 10%), 近 N 天最低价 × (1 − 2%))   ← 只在跌破近期所有低点时触发
  卖出线必须 ≥ 回本价 × (1 + 最低利润率)，否则只观望
  7 日跌幅过大（默认 >15%）也只观望 —— 不接飞刀
"""
from __future__ import annotations

import math

from . import fees


def stop_line(cfg, buy: float, window_min: float | None) -> int:
    """止损线 = min(买入线 × (1 − stop_pct), 近期最低价 × (1 − stop_buffer))。

    为什么要取两者中更低的：卖出要扣约 13% 的费，止损一次的实际亏损 ≈ 跌幅 + 费率
    （在买入线下方 10% 止损 ≈ 实亏 22%），而一次止盈通常只赚几个点。
    所以止损不能被日常波动碰到，只应该在“跌破近期所有低点”时触发 ——
    那意味着行情结构变了（例如版本削弱），而不是普通回调。
    """
    stop = buy * (1 - float(cfg.get("rules.stop_pct", 0.10)))
    if window_min:
        stop = min(stop, window_min * (1 - float(cfg.get("rules.stop_buffer", 0.02))))
    return int(math.floor(stop))


def baseline(cfg, f: dict) -> dict:
    fee = cfg.fee
    out = {
        "oid": f["oid"], "name": f["name"], "action": "watch",
        "buy_price": None, "sell_price": None, "stop_price": None,
        "confidence": 0.0, "reason": "", "risk": "",
    }
    if not f.get("low_ref") or not f.get("high_ref"):
        out["reason"] = "；".join(f.get("warnings") or ["数据不足"])
        return out

    buy = int(round(f["low_ref"]))
    sell = int(round(f["high_ref"]))
    stop = stop_line(cfg, buy, f.get("window_min"))
    be = fees.breakeven_price(buy, fee)
    need = int(math.ceil(be * (1 + float(cfg.get("rules.min_margin", 0.02)))))
    out.update(buy_price=buy, sell_price=sell, stop_price=stop, breakeven=be)

    basis = "近%d天分钟价" % f["window_days"] if f["basis"] == "minute" else "日线（冷启动）"
    ret = fees.net_return(buy, sell, fee)
    rank = f.get("pct_rank")
    rank_txt = f"，现价处于{rank:.0%}分位" if rank is not None else ""

    if sell < need:
        out["reason"] = (f"空间不足：卖出线 {sell} 低于要求的 {need}"
                         f"（回本价 {be}，扣费后仅 {ret:+.1%}）{rank_txt}")
        return out

    t7 = f.get("trend_7d")
    block = float(cfg.get("rules.trend_block_pct", 15.0))
    if t7 is not None and t7 <= -block:
        out["reason"] = f"7 日跌幅 {t7:.1f}% 超过 {block:.0f}%，下跌趋势中不接"
        return out

    out["action"] = "buy"
    hours = f.get("low_hours") or []
    hour_txt = f"，低点多见于 {'/'.join(str(h) for h in hours)} 点" if hours else ""
    out["reason"] = (f"{basis}低/高分位 {buy}/{sell}，扣费后预期 {ret:+.1%}"
                     f"{rank_txt}{hour_txt}")
    risks = []
    if f["basis"] != "minute":
        risks.append("历史数据少，阈值不稳")
    if t7 is not None and t7 < -5:
        risks.append(f"7 日走弱 {t7:.1f}%")
    if f.get("amp_avg") is not None and f["amp_avg"] < fee / (1 - fee):
        risks.append("日内振幅不够覆盖手续费，需隔日持有")
    stop_loss = fees.net_return(buy, stop, fee)
    risks.append(f"触发止损实亏约 {stop_loss:.0%}")
    out["risk"] = "；".join(risks)

    # 置信度：数据越多、利润空间越大越高；仅用于排序和展示
    conf = 0.35
    conf += min(f.get("minute_days", 0), 7) * 0.04
    conf += min(max(ret, 0), 0.15) * 1.5
    if f["basis"] != "minute":
        conf -= 0.15
    out["confidence"] = round(min(max(conf, 0.05), 0.9), 2)
    return out
