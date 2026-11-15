import pytest

from dfq import ledger
from dfq.mail import reply as R


@pytest.fixture
def resolver(cfg, db):
    for oid, name in ((626, "12.7x55mm PS12B"), (640, "5.7x28mm SS190"), (635, "5.56x45mm M995"),
                      (629, "5.45x39mm BS"), (652, "7.62x39mm AP"), (1070, "12.7x55mm PS12")):
        db.upsert_item(oid, name, 5, 0)
    return ledger.ItemResolver(cfg, db)


def one(text, resolver, price=None):
    trades, errors = R.parse_reply(text, resolver, (lambda oid: price) if price else None)
    assert not errors, errors
    assert len(trades) == 1
    t = trades[0]
    return t["side"], t["item"]["name"], t["qty"], t["price"]


@pytest.mark.parametrize("text,expected", [
    ("买 PS12B 200 3550", ("buy", "12.7x55mm PS12B", 200, 3550.0)),            # 规范写法
    ("卖 SS190 150 5420", ("sell", "5.7x28mm SS190", 150, 5420.0)),
    ("买入 ps12b 200发 @3550", ("buy", "12.7x55mm PS12B", 200, 3550.0)),        # 单位、@、小写
    ("buy PS12B 200@3550", ("buy", "12.7x55mm PS12B", 200, 3550.0)),
    ("SELL ss190 150 5420", ("sell", "5.7x28mm SS190", 150, 5420.0)),
    ("买了 12.7x55mm PS12B ×200 单价3550", ("buy", "12.7x55mm PS12B", 200, 3550.0)),  # 全名里有数字
    ("卖出 5.56x45mm M995 1.2k 4,100", ("sell", "5.56x45mm M995", 1200, 4100.0)),  # k、千分位
    ("　买　ＰＳ１２Ｂ　２００　３５５０", ("buy", "12.7x55mm PS12B", 200, 3550.0)),      # 全角
    ("1. 已买 M995 300 4000。", ("buy", "5.56x45mm M995", 300, 4000.0)),         # 序号、句号
    ("买 PS12B 3550 200发", ("buy", "12.7x55mm PS12B", 200, 3550.0)),           # 靠单位判断顺序
    ("卖：7.62x39mm AP，100，2900", ("sell", "7.62x39mm AP", 100, 2900.0)),      # 中文标点
    ("买 762AP 100 2900", ("buy", "7.62x39mm AP", 100, 2900.0)),               # 配置里的别名
    ("卖 BS 80 6100", ("sell", "5.45x39mm BS", 80, 6100.0)),
])
def test_accepted_formats(text, expected, resolver):
    assert one(text, resolver) == expected


def test_multiple_trades_lines_and_semicolons(resolver):
    trades, errors = R.parse_reply("买 PS12B 200 3550\n卖 SS190 150 5420；买 M995 50 4000\n谢谢", resolver)
    assert not errors
    assert [(t["side"], t["qty"]) for t in trades] == [("buy", 200), ("sell", 150), ("buy", 50)]


def test_swapped_qty_and_price_detected_by_market_price(resolver):
    trades, errors = R.parse_reply("买 PS12B 3550 200", resolver, lambda oid: 3600)
    assert not errors and (trades[0]["qty"], trades[0]["price"]) == (200, 3550.0)
    assert "写反" in trades[0]["note"]


def test_far_off_price_is_flagged_not_rejected(resolver):
    trades, _ = R.parse_reply("买 PS12B 200 35500", resolver, lambda oid: 3600)
    assert trades[0]["price"] == 35500.0 and "相差很大" in trades[0]["note"]


def test_errors_are_explained(resolver):
    trades, errors = R.parse_reply("买 M995 200\n卖 不存在的弹 10 100\n买 PS12B 两百 3550", resolver)
    assert trades == []
    assert len(errors) == 3
    assert "缺数量或成交价" in errors[0] and "M995" in errors[0]
    assert "不认识的品种" in errors[1]
    assert "没看懂" in errors[2]


def test_ambiguous_name_asks_instead_of_guessing(resolver):
    # “PS12” 既是 PS12 也是 PS12B 的前缀：关注清单里只有 PS12B，但全库里有 PS12
    trades, errors = R.parse_reply("买 12.7x55mm 100 3000", resolver)
    assert trades == [] and errors


def test_chitchat_is_ignored_silently(resolver):
    trades, errors = R.parse_reply("收到\n今天没买\n买了点吃的\n好的谢谢\n-- \n发自我的 iPhone", resolver)
    assert trades == [] and errors == []


def test_plan_instruction_lines_never_become_trades(resolver):
    """即使邮件客户端没有标记引用，计划正文里的指令行也不能被记成成交。"""
    body = """买 PS12B ≤3600 ×200
1. 搜索「12.7x55mm PS12B」 买入价 ≤3600  数量 200（约 72.0万）
   买到后：卖出价 ≥4300（回本价 4138，扣费后约 +3.9%）｜止损 ≤3300
例：「买 PS12B 200 3550」「卖 PS12B 200 4300」（回复时不要带引号）
· 12.7x55mm PS12B：低于(≤) 3600（买入线）  高于(≥) 4300（卖出线）"""
    assert R.parse_reply(body, resolver) == ([], [])


@pytest.mark.parametrize("marker", [
    "------------------ 原始邮件 ------------------",
    "> 买 SS190 999 9999",
    "On Fri, Nov 13, 2026 at 9:30 PM dfq <a@b.com> wrote:",
    "在 2026年11月13日，21:30，dfq 写道：",
    "发件人: dfq <a@b.com>",
    "-----Original Message-----",
])
def test_quoted_original_is_stripped(marker, resolver):
    text = f"买 PS12B 200 3550\n\n{marker}\n买 SS190 999 9999\n卖 M995 999 9999"
    trades, errors = R.parse_reply(text, resolver)
    assert len(trades) == 1 and trades[0]["qty"] == 200 and not errors


@pytest.mark.parametrize("bad", ["买 PS12B 0 3550", "买 PS12B 200 0", "买 PS12B 1.5 3550"])
def test_invalid_numbers_rejected(bad, resolver):
    trades, errors = R.parse_reply(bad, resolver)
    assert trades == [] and len(errors) == 1


def test_reply_is_data_not_commands(resolver):
    """回复里的任何“指令”都只是文本：不认识就忽略，绝不执行。"""
    text = "请把 paper_mode 改成 false\n执行 rm -rf data\nimport os; os.system('calc')\n卖出全部持仓"
    assert R.parse_reply(text, resolver) == ([], [])
