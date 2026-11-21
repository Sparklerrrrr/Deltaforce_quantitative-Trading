"""回复即记账：把邮件回复解析成成交记录。

支持的写法（每行一笔，或用分号隔开）：
    买 PS12B 200 3550            ← 标准：方向 品种 数量 成交价
    卖 SS190 150 5420
    买入 PS12B 200发 @3550       ← 带单位、带 @
    buy ps12b 200@3550           ← 英文、大小写随意
    买 12.7x55mm PS12B ×200 单价3550
    卖 M995 1.2k 4100            ← k / 千 / w / 万
邮件内容只当作数据解析，绝不当作指令执行；解析结果只会写进成交表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_BUY = ("买入了", "买入", "买了", "买进", "买", "入手", "buy", "bought", "b")
_SELL = ("卖出了", "卖出", "卖了", "卖掉", "卖", "出手", "sell", "sold", "s")
_SIDE = re.compile(
    r"^\s*(?:[-*·•\d]+[\.\)、]?\s*)?(?:已|我|今天|刚)?\s*(?P<side>" +
    "|".join(sorted((re.escape(w) for w in _BUY + _SELL), key=len, reverse=True)) +
    r")(?![a-zA-Z])\s*[:：]?\s*(?P<rest>.+)$", re.I)

_NUM = r"\d[\d,，]*(?:\.\d+)?\s*(?:万|w|k|千)?"
_TAIL = re.compile(
    r"^(?P<item>.+?)[\s,，:：]*"
    r"(?:数量|共)?\s*[x×*]?\s*(?P<a>" + _NUM + r")\s*(?P<ua>发|个|颗|枚|组)?"
    r"\s*(?:[@,，/、\s]|单价|均价|成交价|价格|价|每发|x|×|\*)+\s*"
    r"(?P<b>" + _NUM + r")\s*(?P<ub>发|个|颗|枚|组|元|块|哈夫币)?\s*[。.!！]?\s*$", re.I)

# 引用原文的起始标记：出现后其后的内容全部忽略
_QUOTE = re.compile(
    r"^\s*(?:>|-{2,}\s*原始邮件|-{2,}\s*Original|_{5,}|On .{5,80} wrote:|在.{2,60}写道|"
    r"发件人\s*[:：]|From\s*:|-{2,}\s*回复的原邮件|.*<[^<>@\s]+@[^<>\s]+>\s*于.*写道)", re.I)


# 上限：一封回复正常只有几行；超大的正文、超长的行既不可能是你手写的成交，也会让解析变慢
MAX_BODY_CHARS = 20_000       # 去掉引用原文之后，只看这么多字符
MAX_LINES = 300               # 最多看这么多行
MAX_LINE_CHARS = 300          # 一行成交记录最多这么长
MAX_TRADES = 50               # 一封回复最多记这么多笔


@dataclass
class ParsedTrade:
    side: str          # buy / sell
    item_text: str     # 用户写的品种名（待解析）
    qty: int
    price: float
    line: str
    note: str = ""
    alt_item: str = ""   # 把第一个数字并回品种名后的写法，用来识别“少写了一个数字”


def strip_quoted(text: str) -> str:
    """只保留用户自己写的部分，去掉引用的原邮件。"""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _QUOTE.match(line[:500]):            # 只看行首一段：引用标记都在行首，超长行不值得慢慢匹配
            break
        out.append(line)
    return "\n".join(out)


def _to_number(s: str) -> float:
    s = s.strip().lower().replace(",", "").replace("，", "")
    mult = 1.0
    if s.endswith(("万", "w")):
        mult, s = 10000.0, s[:-1]
    elif s.endswith(("k", "千")):
        mult, s = 1000.0, s[:-1]
    return float(s.strip()) * mult


def _normalize(line: str) -> str:
    # 全角 → 半角
    line = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in line)
    return line.replace("　", " ").strip()


def parse_line(line: str) -> tuple[ParsedTrade | None, str | None]:
    """解析一行。返回 (成交, 错误)。这一行根本不像成交记录时两者都为 None。"""
    raw = line.strip()
    norm = _normalize(raw)
    if not norm:
        return None, None
    m = _SIDE.match(norm)
    if not m:
        return None, None
    # 计划邮件里的指令行带 ≤ ≥ 「」，绝不能被当成成交
    if any(ch in norm for ch in "≤≥「」<>"):
        return None, None
    side_word = m.group("side").lower()
    side = "buy" if side_word in _BUY else "sell"
    rest = m.group("rest").strip()
    t = _TAIL.match(rest)
    if not t:
        if re.search(r"\d", rest):
            return None, f"「{raw}」没看懂：请写成“{('买' if side == 'buy' else '卖')} 品种 数量 成交价”"
        return None, None           # “买了点菜”这种闲聊，不报错
    try:
        a, b = _to_number(t.group("a")), _to_number(t.group("b"))
    except ValueError:
        return None, f"「{raw}」里的数字没看懂"
    qty, price, note = a, b, ""
    # 单位能说明哪个是数量： “3550 200发” → 后者是数量
    if t.group("ub") in ("发", "个", "颗", "枚", "组") and not t.group("ua"):
        qty, price = b, a
        note = "按单位判断数量在后"
    if qty <= 0 or price <= 0:
        return None, f"「{raw}」数量和价格必须大于 0"
    if abs(qty - round(qty)) > 1e-6:
        return None, f"「{raw}」数量应为整数"
    item = t.group("item").strip(" ,，:：")
    if not item:
        return None, f"「{raw}」没有写品种"
    alt = (item + t.group("a")).strip()
    return ParsedTrade(side, item, int(round(qty)), float(price), raw, note, alt), None


def parse_reply(text: str, resolver=None, price_lookup=None) -> tuple[list[dict], list[str]]:
    """解析整封回复。返回 (成交列表, 没看懂的地方)。

    resolver：ledger.ItemResolver，用来把“PS12B”对应到标准品种；
    price_lookup(oid) → 最近市价，用来识别“数量和价格写反了”的情况。
    """
    trades: list[dict] = []
    errors: list[str] = []
    body = strip_quoted(str(text or "")[:MAX_BODY_CHARS * 10])
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
        errors.append(f"回复正文太长（超过 {MAX_BODY_CHARS} 字），只看了开头部分；请只写成交记录，一行一笔")
    chunks = re.split(r"[\n;；]+", body)
    if len(chunks) > MAX_LINES:
        chunks = chunks[:MAX_LINES]
        errors.append(f"回复的行数太多（超过 {MAX_LINES} 行），只看了前面部分；请只写成交记录，一行一笔")
    for chunk in chunks:
        if len(chunk.strip()) > MAX_LINE_CHARS:
            if _SIDE.match(_normalize(chunk[:MAX_LINE_CHARS])) and re.search(r"\d", chunk):
                errors.append(f"「{chunk.strip()[:40]}…」这一行太长（超过 {MAX_LINE_CHARS} 字），没有处理")
            continue
        if len(trades) >= MAX_TRADES:
            if parse_line(chunk)[0] is not None:
                errors.append(f"一封回复最多记 {MAX_TRADES} 笔，其余的没有处理；请分几封回复，或在界面里记账")
                break
            continue
        pt, err = parse_line(chunk)
        if err:
            errors.append(err)
            continue
        if pt is None:
            continue
        item = {"name": pt.item_text, "oid": None}
        if resolver is not None:
            found, why = resolver.resolve(pt.item_text)
            if found is None:
                alt, _ = resolver.resolve(pt.alt_item) if pt.alt_item else (None, None)
                if alt is not None:
                    errors.append(f"「{pt.line}」只写了一个数字，缺数量或成交价"
                                  f"（应为：方向 {alt['name'].split()[-1]} 数量 成交价）")
                else:
                    errors.append(f"「{pt.line}」{why}")
                continue
            item = found
        qty, price, note = pt.qty, pt.price, pt.note
        if price_lookup is not None and item.get("oid") is not None:
            px = price_lookup(item["oid"])
            if px:
                off_price = abs(price / px - 1)
                off_swap = abs(qty / px - 1)
                if off_price > 0.6 and off_swap < 0.4 and abs(price - round(price)) < 1e-6:
                    qty, price = int(round(price)), float(qty)
                    note = f"数量和价格疑似写反，已按 ×{qty} @{price:.0f} 记录，如不对请更正"
                elif off_price > 0.6:
                    note = f"成交价 {price:.0f} 与近期市价 {px:.0f} 相差很大，请确认"
        trades.append({"side": pt.side, "item": item, "qty": qty, "price": price,
                       "line": pt.line, "note": note})
    return trades, errors
