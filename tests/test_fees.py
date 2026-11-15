import pytest

from dfq import fees


def test_breakeven_matches_spec_formula():
    # 回本卖价 = 买入价 ÷ 0.87
    assert fees.breakeven_price(3600) == 4138          # 3600/0.87 = 4137.93 → 向上取整
    assert fees.breakeven_price(87) == 100             # 恰好整除时不多进一位
    assert fees.breakeven_price(3480) == 4000


def test_selling_at_breakeven_never_loses():
    for buy in (1, 99, 455, 3600, 5467, 21139):
        be = fees.breakeven_price(buy)
        assert fees.net_return(buy, be) >= 0
        assert fees.net_return(buy, be - 1) < 0        # 再低 1 就亏：说明取整没有多留余量


def test_net_return_and_profit():
    assert fees.net_proceeds(1000) == pytest.approx(870)
    assert fees.net_return(3600, 5400) == pytest.approx(5400 * 0.87 / 3600 - 1)
    assert fees.net_profit(3600, 5400, 200) == pytest.approx((5400 * 0.87 - 3600) * 200)
    assert fees.net_return(1000, 1000) == pytest.approx(-0.13)   # 原价卖出亏掉整个费率


def test_custom_fee_rate():
    assert fees.breakeven_price(900, fee=0.10) == 1000
    assert fees.net_return(900, 1000, fee=0.10) == pytest.approx(0)


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        fees.breakeven_price(0)
    with pytest.raises(ValueError):
        fees.breakeven_price(100, fee=1.0)
    with pytest.raises(ValueError):
        fees.net_return(-5, 100)


def test_implied_fee_from_quick_sell_price():
    # 文档里“军需处兑换”示例：出售价 308880、手续费 40154 → 正好 13%
    assert fees.implied_fee(308880, 308880 - 40154) == pytest.approx(0.13, abs=1e-4)
    assert fees.implied_fee(0, 100) is None
    assert fees.implied_fee(100, None) is None


def test_parse_shop_sell_type_tolerates_garbage():
    assert fees.parse_shop_sell_type(["交易行上架", "3", "493794"]) == {
        "method": "交易行上架", "level": 3, "quick_price": 493794.0}
    assert fees.parse_shop_sell_type(None)["quick_price"] is None
    assert fees.parse_shop_sell_type(["x"])["level"] is None
    assert fees.parse_shop_sell_type(["x", "abc", "?"]) == {"method": "x", "level": None, "quick_price": None}
