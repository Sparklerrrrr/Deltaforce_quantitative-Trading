"""特征计算（纯代码，不调用模型）。

每个品种算出：
- 当前价在近 N 天价格分布中的分位（0 = 最低，1 = 最高）
- 日内波动幅度、低点/高点通常出现在几点
- 7 天、30 天趋势
- 按候选买卖线扣费后的预期收益
数据不足时（冷启动）退化为只用天曲线，并在 basis / data_days 里如实标注。
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from . import fees, timeutil


def quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数。sorted_vals 必须已升序。"""
    if not sorted_vals:
        raise ValueError("空样本")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    q = min(max(q, 0.0), 1.0)
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def pct_rank(sorted_vals: list[float], x: float) -> float:
    """x 在样本中的分位：有多少比例的样本 ≤ x。"""
    if not sorted_vals:
        return 0.5
    import bisect
    return bisect.bisect_right(sorted_vals, x) / len(sorted_vals)


def daily_stats(minutes: list[tuple[datetime, float]], day_rows: list) -> dict[str, dict]:
    """合并两种来源的日线：自己攒的分钟数据优先，其余用接口的天曲线补。"""
    out: dict[str, dict] = {}
    for r in day_rows:
        if r["pmin"] is None or r["pmax"] is None:
            continue
        out[r["d"]] = {"min": r["pmin"], "max": r["pmax"], "avg": r["price"],
                       "min_hour": _hour_of(r["min_at"]), "max_hour": _hour_of(r["max_at"]),
                       "src": "day"}
    by_day: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for t, p in minutes:
        by_day[timeutil.day_str(t)].append((t, p))
    for d, pts in by_day.items():
        if len(pts) < 60:       # 不足 60 个点的残缺日不单独成日线
            continue
        lo = min(pts, key=lambda x: x[1])
        hi = max(pts, key=lambda x: x[1])
        out[d] = {"min": lo[1], "max": hi[1], "avg": sum(p for _, p in pts) / len(pts),
                  "min_hour": lo[0].hour, "max_hour": hi[0].hour, "src": "minute"}
    return dict(sorted(out.items()))


def _hour_of(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return timeutil.parse_dt(ts).hour
    except ValueError:
        return None


def _top_hours(hours: list[int], k: int = 3) -> list[int]:
    cnt: dict[int, int] = defaultdict(int)
    for h in hours:
        cnt[h] += 1
    return [h for h, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def compute(cfg, db, item: dict, now: datetime | None = None) -> dict:
    now = now or timeutil.now()
    oid = item["oid"]
    window = int(cfg.get("features.window_days", 7))
    fee = cfg.fee_for(item["name"])
    start = now - timedelta(days=window)

    minutes = db.minutes(oid, start, now)
    day_rows = db.days(oid, now - timedelta(days=45), now)
    snap = db.latest_snapshot(oid)
    daily_all = daily_stats(db.minutes(oid, now - timedelta(days=45), now), day_rows)
    win_start = timeutil.day_str(start)
    daily = {d: v for d, v in daily_all.items() if d >= win_start}

    prices = sorted(p for _, p in minutes)
    current = None
    current_ts = None
    if snap is not None and snap["price"]:
        current, current_ts = snap["price"], snap["ts"]
    if minutes and (current_ts is None or timeutil.fmt_min(minutes[-1][0]) > current_ts):
        current, current_ts = minutes[-1][1], timeutil.fmt_min(minutes[-1][0])

    f: dict = {
        "oid": oid, "name": item["name"], "grade": item.get("grade"),
        "current": current, "current_ts": current_ts,
        "window_days": window, "minute_points": len(prices),
        "data_days": len(daily), "history_days": len(daily_all),
        "basis": None, "low_ref": None, "high_ref": None,
        "warnings": [],
    }
    if current is None:
        f["warnings"].append("没有任何价格数据")
        return f

    stale_h = (now - timeutil.parse_dt(current_ts)).total_seconds() / 3600
    f["stale_hours"] = round(stale_h, 1)
    if stale_h > 6:
        f["warnings"].append(f"最新价格是 {stale_h:.0f} 小时前的，数据可能过期")

    bq, sq = float(cfg.get("rules.buy_quantile")), float(cfg.get("rules.sell_quantile"))
    minute_days = sum(1 for v in daily.values() if v["src"] == "minute")
    f["minute_days"] = minute_days

    if len(prices) >= int(cfg.get("features.min_minute_points", 200)) and minute_days >= 2:
        # 分钟样本足够：直接取分位
        f["basis"] = "minute"
        f["low_ref"], f["high_ref"] = quantile(prices, bq), quantile(prices, sq)
        f["q"] = {k: round(quantile(prices, v)) for k, v in
                  (("q05", .05), ("q10", .10), ("q20", .20), ("q50", .50),
                   ("q80", .80), ("q90", .90), ("q95", .95))}
        f["pct_rank"] = round(pct_rank(prices, current), 3)
    elif len(daily) >= 3:
        # 冷启动：只有日线。用“日低点的中位数 / 日高点的中位数”，再向均价收缩一点保守处理
        f["basis"] = "daily"
        mins = sorted(v["min"] for v in daily.values())
        maxs = sorted(v["max"] for v in daily.values())
        avgs = [v["avg"] for v in daily.values() if v["avg"]]
        mid = statistics.mean(avgs) if avgs else (mins[len(mins) // 2] + maxs[len(maxs) // 2]) / 2
        shrink = float(cfg.get("rules.cold_shrink", 0.15))
        lo_med, hi_med = quantile(mins, 0.5), quantile(maxs, 0.5)
        f["low_ref"] = lo_med + shrink * (mid - lo_med)
        f["high_ref"] = hi_med - shrink * (hi_med - mid)
        span = (maxs[-1] - mins[0]) or 1
        f["pct_rank"] = round(min(max((current - mins[0]) / span, 0), 1), 3)
        f["warnings"].append(f"分钟历史不足（{minute_days} 天），阈值按日线估算，可信度较低")
    else:
        f["warnings"].append(f"历史数据太少（日线 {len(daily)} 天），暂不给买卖线")

    if daily:
        f["window_min"] = min(v["min"] for v in daily.values())
        f["window_max"] = max(v["max"] for v in daily.values())
        amps = [(v["max"] - v["min"]) / v["min"] for v in daily.values() if v["min"] > 0]
        f["amp_avg"] = round(statistics.mean(amps), 4) if amps else None
        f["low_hours"] = _top_hours([v["min_hour"] for v in daily_all.values() if v["min_hour"] is not None])
        f["high_hours"] = _top_hours([v["max_hour"] for v in daily_all.values() if v["max_hour"] is not None])

    # 趋势：优先用接口给的 7/30 日涨跌，没有再用日均价自己算
    f["trend_7d"] = snap["day7_bl"] if snap is not None else None
    f["trend_30d"] = snap["day30_bl"] if snap is not None else None
    f["change_today"] = snap["bl"] if snap is not None else None
    avg_series = [(d, v["avg"]) for d, v in daily_all.items() if v["avg"]]
    if f["trend_7d"] is None and len(avg_series) >= 7:
        f["trend_7d"] = round((avg_series[-1][1] / avg_series[-7][1] - 1) * 100, 2)
    if f["trend_30d"] is None and len(avg_series) >= 25:
        f["trend_30d"] = round((avg_series[-1][1] / avg_series[0][1] - 1) * 100, 2)

    # 近 7 日中位价与现价的偏离：给“异常波动保护”用（固定 7 天，不随 window_days 变）
    p7 = sorted(p for _, p in db.minutes(oid, now - timedelta(days=7), now))
    if len(p7) < 60:
        p7 = sorted(v["avg"] for d, v in daily_all.items()
                    if d >= timeutil.day_str(now - timedelta(days=7)) and v["avg"])
    if p7:
        med7 = statistics.median(p7)
        f["median_7d"] = round(med7, 1)
        f["dev_from_median_pct"] = round((current / med7 - 1) * 100, 2) if med7 else None

    # 秒出价比现价低多少：这是“想立刻卖掉要让多少价”，不是手续费（手续费见 fees.basis）
    if snap is not None and snap["quick_price"] and snap["price"]:
        qd = fees.quick_sell_discount(snap["price"], snap["quick_price"])
        f["quick_sell_discount"] = round(qd, 4) if qd is not None else None

    if f["low_ref"] and f["high_ref"]:
        f["low_ref"], f["high_ref"] = round(f["low_ref"]), round(f["high_ref"])
        f["breakeven_at_low"] = fees.breakeven_price(f["low_ref"], fee)
        f["exp_net_return"] = round(fees.net_return(f["low_ref"], f["high_ref"], fee), 4)
    return f


def fee_check(cfg, feats: list[dict]) -> str | None:
    """秒出价折让的中性提示（只在明显偏大时给一句）。

    旧版在这里把“1 − 秒出价/现价”当成费率和配置比较，真实数据里这个值只有约 2%，于是每份计划都误报
    “与配置的 13% 相差较大”。秒出价不是扣费后的到手价，所以这里不再和费率比较；费率依据见 fees.basis。
    """
    return fees.quick_sell_note([f.get("quick_sell_discount") for f in feats])
