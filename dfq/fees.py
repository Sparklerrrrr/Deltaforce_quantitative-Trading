"""交易成本。

交易行卖出时要扣手续费和上架费，程序按一个“综合费率”计算（默认 13%，可配，也可以按品种单独设置）：
- 卖出净得   = 卖价 × (1 − 费率)
- 回本卖价   = 买入价 ÷ (1 − 费率)       例：3600 ÷ 0.87 ≈ 4138
- 扣费后收益 = 卖价 × (1 − 费率) ÷ 买入价 − 1
费率只在卖出时扣；买入按挂单价全额付款，不另收费。

13% 从哪来、可信到什么程度（这一点必须如实告诉使用者，不能把默认值当成事实）：
- 最初来源：使用者给的数字 + 小涛查文档里“军需处兑换”的一个示例（出售价 308880、手续费 40154，正好 13%）。
- 2026-11-16 开发时用真实 token 调过一次平台接口 exchange_pay（军需处兑换-子弹收益）：返回 9 种 2～4 级子弹
  （单价 133～2926），每种的 手续费 sxf ÷ 出售总价 priceMax 都在 12.998%～13.000%，与等级、单价无关。
  这是第三方平台算出来的数，不是游戏官方公告；关注清单里的 5 级子弹不在这个接口的范围内（是外推）。
- 所以计划里会写明费率依据：用 `python -m dfq fee-check` 在你自己的环境里核对一次（存进 kv fee_evidence），
  或者卖出时填“实际到手金额”（trades.net），程序据此校准（`calibration`）。两者都没有时，计划会说“未核对”。

注意：列表接口里的“秒出价”（ShopSellType 第 3 项）**不是**扣完手续费的到手价。真实数据里 90 种子弹
1 − 秒出价/现价 的中位数只有 2.7%，所以它只是“想立刻卖掉时建议的挂单价”。用它反推费率是错的
（旧版 `implied_fee` 就这么做，导致每份计划都有一条误报），现在它只叫 `quick_sell_discount`。
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Callable

from . import timeutil

DEFAULT_FEE = 0.13

# 费率参数既可以是一个数，也可以是“品种名 → 费率”的函数（Config.fee_for）
FeeArg = float | Callable[[str], float]

EVIDENCE_KEY = "fee_evidence"        # kv 里存 fee-check 结果的键
CONSISTENT_TOL = 0.005               # 平台中位数与配置相差不超过 0.5 个百分点 → 认为一致
CALIBRATION_MIN_N = 3                # 至少 3 笔填了实际到手金额的卖出，才拿来校准
QUICK_SELL_NOTE_PCT = 0.05           # 秒出价比现价低这么多以上才提示（平常约 1%～4%）


def _check(fee: float) -> None:
    if not (0 <= fee < 1):
        raise ValueError(f"费率必须在 [0, 1) 内: {fee}")


def rate_for(fee: FeeArg, name: str | None) -> float:
    """把“一个数或一个函数”的费率参数落到某个品种上。"""
    r = fee(name or "") if callable(fee) else fee
    r = float(r)
    _check(r)
    return r


def net_proceeds(sell_price: float, fee: float = DEFAULT_FEE) -> float:
    """卖出 1 发实际到手的钱。"""
    _check(fee)
    return sell_price * (1 - fee)


def breakeven_price(buy_price: float, fee: float = DEFAULT_FEE) -> int:
    """回本卖价（向上取整：卖价必须 ≥ 它才不亏）。"""
    _check(fee)
    if buy_price <= 0:
        raise ValueError("买入价必须为正")
    # 减一个极小量再取整，避免 3480/0.87=4000.0000000001 这类浮点误差多进一位
    return int(math.ceil(buy_price / (1 - fee) - 1e-9))


def net_return(buy_price: float, sell_price: float, fee: float = DEFAULT_FEE) -> float:
    """扣费后收益率，0.05 表示赚 5%。"""
    _check(fee)
    if buy_price <= 0:
        raise ValueError("买入价必须为正")
    return sell_price * (1 - fee) / buy_price - 1


def net_profit(buy_price: float, sell_price: float, qty: int, fee: float = DEFAULT_FEE) -> float:
    """扣费后总利润（哈夫币）。"""
    return (net_proceeds(sell_price, fee) - buy_price) * qty


def quick_sell_discount(price: float, quick_sell_price: float) -> float | None:
    """“秒出价”比现价低多少：1 − 秒出价 / 现价。

    秒出价 = 平台建议的“想立刻卖掉时的挂单价”，**不是**扣完手续费的到手价
    （真实数据里这个差值中位数只有约 2.7%，远小于 13% 的费率），所以它不能用来反推费率。
    """
    if not price or price <= 0 or quick_sell_price is None or quick_sell_price <= 0:
        return None
    return 1 - quick_sell_price / price


# 旧名字保留为别名（外部脚本可能还在用）；含义以 quick_sell_discount 的说明为准，它不是费率
implied_fee = quick_sell_discount


def parse_shop_sell_type(raw) -> dict:
    """解析 ShopSellType：[推荐出售方式, 推荐等级, 秒出价格]。字段缺失时尽量容错。"""
    out = {"method": None, "level": None, "quick_price": None}
    if not isinstance(raw, (list, tuple)):
        return out
    if len(raw) > 0 and raw[0] is not None:
        out["method"] = str(raw[0])
    if len(raw) > 1:
        try:
            out["level"] = int(float(raw[1]))
        except (TypeError, ValueError):
            pass
    if len(raw) > 2:
        try:
            out["quick_price"] = float(raw[2])
        except (TypeError, ValueError):
            pass
    return out


# ------------------------------------------------------------------ 费率依据：平台核对
def build_evidence(rows: list[dict], configured: float, checked_at: str | None = None,
                   source: str = "xtc.exchange_pay") -> dict:
    """把 exchange_pay 解析出的 [{name, grade, rate}] 汇总成 fee_evidence。没有可用数据时抛 ValueError。"""
    items = [{"name": r.get("name"), "grade": r.get("grade"), "rate": round(float(r["rate"]), 6)}
             for r in rows if r.get("rate") is not None and 0 <= float(r["rate"]) < 1]
    if not items:
        raise ValueError("平台返回里没有可用的“出售总价 / 手续费”数据，无法核对费率")
    rates = sorted(i["rate"] for i in items)
    med = statistics.median(rates)
    return {"checked_at": checked_at or timeutil.fmt(timeutil.now()), "source": source,
            "n": len(items), "min": rates[0], "median": round(med, 6), "max": rates[-1],
            "configured": configured, "consistent": abs(med - configured) <= CONSISTENT_TOL,
            "items": items}


def save_evidence(db, ev: dict) -> None:
    db.kv_set(EVIDENCE_KEY, ev)


def evidence(db) -> dict | None:
    """读取上一次 fee-check 的结果；没有或格式不对返回 None。"""
    ev = db.kv_get(EVIDENCE_KEY)
    if not isinstance(ev, dict) or not isinstance(ev.get("median"), (int, float)):
        return None
    return ev


# ------------------------------------------------------------------ 费率依据：按实际成交校准
def calibration(db, *, paper: bool = False, fee: FeeArg | None = None) -> dict | None:
    """用填了“实际到手金额”（net）的卖出反推真实费率：逐笔 1 − net / (价格 × 数量)。

    返回 {"n","median","min","max"}（再加 "diff_median" = 逐笔 “实测 − 配置” 的中位数，给了 fee 时才有）；
    一笔都没有返回 None。只看指定的那本账。
    """
    try:
        rows = db.query("SELECT name, qty, price, net FROM trades WHERE paper=? AND side='sell' "
                        "AND net IS NOT NULL AND COALESCE(kind,'trade')='trade'", (1 if paper else 0,))
    except sqlite3.OperationalError:          # 旧库还没有 net 列（迁移前）：当作没有校准数据
        return None
    rates, diffs = [], []
    for r in rows:
        gross = float(r["price"]) * int(r["qty"])
        if gross <= 0 or r["net"] is None or not (0 < float(r["net"]) <= gross):
            continue
        rate = 1 - float(r["net"]) / gross
        rates.append(rate)
        if fee is not None:
            diffs.append(rate - rate_for(fee, r["name"]))
    if not rates:
        return None
    rates.sort()
    out = {"n": len(rates), "median": round(statistics.median(rates), 6),
           "min": round(rates[0], 6), "max": round(rates[-1], 6)}
    if diffs:
        out["diff_median"] = round(statistics.median(diffs), 6)
    return out


def basis(cfg, db, *, paper: bool = False) -> dict:
    """费率依据：返回 {"status", "line", "warnings"}。

    status 三选一：calibrated（按你的实际成交校准，至少 3 笔）、platform（已用平台数据核对）、unverified（未核对）。
    line 写进计划邮件的“系统状态”；warnings 写进计划的“提示与降级”。未核对时绝不把 13% 说成事实。
    """
    conf = cfg.fee
    overrides = cfg.get("fees.overrides") or {}
    ov_txt = f"；另有 {len(overrides)} 个品种单独设置了费率" if overrides else ""
    warn_diff = float(cfg.get("fees.warn_diff", 0.03))
    warnings: list[str] = []
    cal = calibration(db, paper=paper, fee=cfg.fee_for)
    ev = evidence(db)
    book = "模拟账本" if paper else "真实账本"

    if cal and cal["n"] >= CALIBRATION_MIN_N:
        status = "calibrated"
        line = (f"费率依据：按你的实际成交校准（{book} {cal['n']} 笔填了到手金额，实际费率中位数约 {cal['median']:.1%}，"
                f"范围 {cal['min']:.1%}～{cal['max']:.1%}；配置 {conf:.1%}{ov_txt}）")
        if abs(cal.get("diff_median", cal["median"] - conf)) > warn_diff:
            warnings.append(f"费率提示：按你记录的实际到手金额反推，卖出费率约 {cal['median']:.1%}，与配置的 {conf:.1%} "
                            f"相差超过 {warn_diff:.0%}。回本价和止损亏损都按配置算，请核对后修改 config.yaml 的 "
                            f"fees.sell_fee_rate（或 fees.overrides 里对应品种）。")
    elif ev is not None:
        status = "platform"
        line = (f"费率依据：已用平台数据核对（{str(ev.get('checked_at', ''))[:10]}，{ev.get('n')} 种子弹，"
                f"手续费/出售价中位数 {ev['median']:.2%}；配置 {conf:.1%}{ov_txt}）。"
                f"平台数据来自第三方，不是游戏官方公告，且只覆盖部分子弹")
        if abs(ev["median"] - conf) > CONSISTENT_TOL:
            warnings.append(f"费率提示：平台数据核对出的费率中位数 {ev['median']:.2%} 与配置的 {conf:.1%} 不一致"
                            f"（核对时间 {str(ev.get('checked_at', ''))[:16]}）。请确认后修改 fees.sell_fee_rate。")
    else:
        status = "unverified"
        line = (f"费率依据：未核对——{conf:.0%} 是默认值，不是你账号上实测的数{ov_txt}。"
                f"运行 python -m dfq fee-check 用平台数据核对一次，或在卖出记账时填实际到手金额（--net）")
        warnings.append(f"费率未核对：回本价、止损亏损都按默认费率 {conf:.0%} 估算。"
                        f"请运行 python -m dfq fee-check，或卖出时记下实际到手金额，让程序按真实成交校准。")
    if cal and status != "calibrated":
        line += f"；另有 {cal['n']} 笔实际到手记录（中位数 {cal['median']:.1%}），满 {CALIBRATION_MIN_N} 笔后用于校准"
    return {"status": status, "line": line, "warnings": warnings, "calibration": cal, "evidence": ev}


def quick_sell_note(discounts: list[float]) -> str | None:
    """秒出价相对现价的折让：平常只有几个点，只有明显偏大时才提示一句（这是挂单策略信息，不是费率）。"""
    vals = [d for d in discounts if d is not None]
    if not vals:
        return None
    med = statistics.median(vals)
    if med > QUICK_SELL_NOTE_PCT:
        return (f"行情提示：想立刻卖出通常要比现价低约 {med:.0%}（平台“秒出价”），比平常（约 1%～4%）明显偏大，"
                f"可能是买盘变少。这不是手续费，手续费另按费率扣。")
    return None
