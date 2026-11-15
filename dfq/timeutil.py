"""时间工具。

游戏和小涛查的数据都是北京时间，所以全系统统一用“北京时间的 naive datetime”。
不依赖 zoneinfo（Windows 上需要额外装 tzdata），中国没有夏令时，固定 UTC+8 即可。

接口返回的时间标签都不带年份（分钟曲线甚至不带月份），这里负责补全：
- 分钟曲线： "25 16:02分"       —— 只有 日 时:分
- 小时曲线： "12-23 16时"       —— 月-日 时
- 天曲线：   "11-27"            —— 月-日
- 高低点：   "12-23 16:57分"    —— 月-日 时:分
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))
FMT = "%Y-%m-%d %H:%M:%S"
FMT_MIN = "%Y-%m-%d %H:%M"

_now_override: datetime | None = None


def set_now(dt: datetime | str | None) -> None:
    """测试 / 模拟用：把“现在”固定到某个时刻。"""
    global _now_override
    _now_override = parse_dt(dt) if isinstance(dt, str) else dt


def now() -> datetime:
    """当前北京时间（naive）。可用 set_now() 或环境变量 DFQ_NOW 覆盖。"""
    if _now_override is not None:
        return _now_override
    env = os.environ.get("DFQ_NOW")
    if env:
        return parse_dt(env)
    return datetime.now(BJ).replace(tzinfo=None, microsecond=0)


def parse_dt(s: str) -> datetime:
    s = s.strip().replace("T", " ")
    for fmt in (FMT, FMT_MIN, "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s!r}")


def fmt(dt: datetime) -> str:
    return dt.strftime(FMT)


def fmt_min(dt: datetime) -> str:
    return dt.strftime(FMT_MIN)


_RE_MINUTE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})[:：](\d{1,2})")
# 实测（2026-11-15 probe）：小时曲线的标签是 "11-15 周六 20时"，日期和小时之间夹着星期几
_RE_MD = re.compile(r"^\s*(\d{1,2})-(\d{1,2})(?:\s+(?:周|星期)[一二三四五六日天])?(?:\s+(\d{1,2})(?:[:：](\d{1,2}))?)?")


def _date_with_day(anchor: date, day: int, max_back: int = 3, forward: int = 0) -> date | None:
    """在 anchor 往前 max_back 天（及往后 forward 天）内，找“日”等于 day 的日期。

    优先取离 anchor 最近且不晚于 anchor 的日期。
    因为是逐日回溯，所以跨月、跨年、闰年都天然正确。
    """
    for delta in range(0, max_back + 1):
        d = anchor - timedelta(days=delta)
        if d.day == day:
            return d
    for delta in range(1, forward + 1):
        d = anchor + timedelta(days=delta)
        if d.day == day:
            return d
    return None


def parse_minute_labels(labels: list[str], ref: datetime) -> list[datetime | None]:
    """把分钟曲线的标签（"25 16:02分"）补全成完整时间。

    思路：序列是按时间升序、最多覆盖最近 24 小时，最后一个点最接近 ref（拉取时刻）。
    先定位最后一个点的日期，再从后往前逐点回溯；当“时:分”比后一个点还大时，
    说明跨过了零点，日期减一天。解析不了的标签返回 None（由调用方丢弃）。
    """
    parsed: list[tuple[int, int, int] | None] = []
    for lab in labels:
        m = _RE_MINUTE.match(str(lab))
        if not m:
            parsed.append(None)
            continue
        d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59):
            parsed.append(None)
            continue
        parsed.append((d, h, mi))

    out: list[datetime | None] = [None] * len(labels)
    nxt: datetime | None = None
    for i in range(len(parsed) - 1, -1, -1):
        p = parsed[i]
        if p is None:
            continue
        d, h, mi = p
        if nxt is None:
            # 最后一个有效点：允许比 ref 晚一点点（时钟误差 / 缓存）
            anchor = (ref + timedelta(minutes=30)).date()
            dt_date = _date_with_day(anchor, d, max_back=3)
            if dt_date is None:
                continue
            cur = datetime(dt_date.year, dt_date.month, dt_date.day, h, mi)
            if cur > ref + timedelta(minutes=30):
                # “日”相同但时间在未来：说明其实是上个月的同一天，继续往前找
                alt = _date_with_day(dt_date - timedelta(days=1), d, max_back=31)
                if alt is None:
                    continue
                cur = datetime(alt.year, alt.month, alt.day, h, mi)
        else:
            dt_date = _date_with_day(nxt.date(), d, max_back=3)
            if dt_date is None:
                continue
            cur = datetime(dt_date.year, dt_date.month, dt_date.day, h, mi)
            if cur > nxt:
                # 同一“日”但时刻更晚 → 不可能在同一天，回退到更早的同日（罕见：数据缺口≥1个月）
                alt = _date_with_day(dt_date - timedelta(days=1), d, max_back=31)
                if alt is None:
                    continue
                cur = datetime(alt.year, alt.month, alt.day, h, mi)
        out[i] = cur
        nxt = cur
    return out


def parse_md_label(label: str, ref: datetime) -> datetime | None:
    """解析带月-日的标签（"12-23 16时" / "11-27" / "12-23 16:57分"），补全年份。

    在 ref 的前一年、当年、后一年三个候选里取离 ref 最近的那个。
    （历史数据最多 45 天、预测最多几天，所以“最近”一定是对的；跨年自然处理。）
    """
    m = _RE_MD.match(str(label))
    if not m:
        return None
    mon, day = int(m.group(1)), int(m.group(2))
    hour = int(m.group(3)) if m.group(3) is not None else 0
    minute = int(m.group(4)) if m.group(4) is not None else 0
    best: datetime | None = None
    for year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cand = datetime(year, mon, day, hour, minute)
        except ValueError:  # 2 月 29 日在非闰年
            continue
        if best is None or abs(cand - ref) < abs(best - ref):
            best = cand
    return best


def day_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def short(dt: datetime | str) -> str:
    """邮件里用的短时间： 11-14 21:00"""
    if isinstance(dt, str):
        dt = parse_dt(dt)
    return dt.strftime("%m-%d %H:%M")
