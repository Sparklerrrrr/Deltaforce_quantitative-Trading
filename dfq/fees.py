"""交易成本。

交易行卖出时要扣手续费和上架费，默认按综合费率 13% 计算（可配）。
- 卖出净得   = 卖价 × (1 − 费率)
- 回本卖价   = 买入价 ÷ (1 − 费率)       例：3600 ÷ 0.87 ≈ 4138
- 扣费后收益 = 卖价 × (1 − 费率) ÷ 买入价 − 1
"""
from __future__ import annotations

import math

DEFAULT_FEE = 0.13


def _check(fee: float) -> None:
    if not (0 <= fee < 1):
        raise ValueError(f"费率必须在 [0, 1) 内: {fee}")


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


def implied_fee(price: float, quick_sell_price: float) -> float | None:
    """用 ShopSellType 里的“秒出价格”反推实际费率。

    秒出价格 = 已扣手续费、上架费，并且往前压了一格价位后的到手价，
    所以反推出的费率会略高于真实费率（多出来的是压价的那一格）。
    """
    if not price or price <= 0 or quick_sell_price is None or quick_sell_price <= 0:
        return None
    return 1 - quick_sell_price / price


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
