"""在 YAML 文本里只改一个标量值，其余每个字符（注释、缩进、空行、换行符）原样保留。

为什么不用“读进来 → 改 → yaml.dump 写回”：那样会丢掉 config.yaml 里全部的中文注释，
把用户自己的写法（引号、对齐、行尾注释）统统重排。界面上改一个开关，不能把整份文件改得面目全非。

做法：按行找到目标键（只认最常见的“块状映射”写法），只替换它那一行里“值”的那一段；
键不存在时在父节末尾按同样的缩进插入一行。任何看不懂、可能改错的写法（流式写法 {a: 1}、多文档、
锚点 / 别名、tab 缩进、多行字符串、目标本身是映射或列表……）一律抛 PatchError 拒绝修改，
宁可让用户手动改，也不猜。

调用方必须在落盘前再调用 verify()：用 yaml.safe_load 把新旧文本都读一遍，确认**只有**目标键变了、
而且变成了期望的值——这是最后一道保险，前面按行解析哪怕有疏漏，也不会把错的东西写进文件。
纯函数，不读写任何文件。
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any

import yaml


class PatchError(ValueError):
    """无法安全修改（str(e) 是给人看的中文说明）。"""


_KEY_PART = re.compile(r"^[A-Za-z0-9_\-]+$")
# 一行“键: 值”：键可以是普通写法或带引号；冒号后面必须是空白或行尾（否则 a:b 是一个普通字符串）
_KEY_LINE = re.compile(r"""^(?P<indent>[ ]*)(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|"""
                       r"""[^\s#'"{}\[\],&*!|>%@`:-][^:#]*?|-[^\s:#][^:#]*?)"""
                       r"""[ ]*:(?=[ \t]|$)(?P<rest>.*)$""")
_SEQ_LINE = re.compile(r"^(?P<indent>[ ]*)-(?:[ \t]|$)")
_DOC_MARK = re.compile(r"^(---|\.\.\.)(?:[ \t]|$)")


def _split_lines(text: str) -> list[str]:
    """按行切开并保留每行自己的换行符（\\r\\n 或 \\n），拼回去与原文完全相同。"""
    return text.splitlines(keepends=True) if text else []


def _body_eol(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1], line[-1]
    return line, ""


def _unquote_key(k: str) -> str:
    k = k.strip()
    if len(k) >= 2 and k[0] == k[-1] == '"':
        try:
            v = yaml.safe_load(k)
        except yaml.YAMLError:
            return k
        return v if isinstance(v, str) else k
    if len(k) >= 2 and k[0] == k[-1] == "'":
        return k[1:-1].replace("''", "'")
    return k


def _split_value(rest: str) -> tuple[str, str, str]:
    """把冒号后面的部分拆成 (值前空白, 值, 值后面的全部：空白 + 注释)。

    值是带引号的字符串时按引号规则找结尾（字符串里的 # 和冒号不是注释）；
    普通写法的值到“空白 + #”或行尾为止。"""
    lead_len = len(rest) - len(rest.lstrip(" \t"))
    lead, s = rest[:lead_len], rest[lead_len:]
    if not s or s.startswith("#"):
        return lead, "", s
    if s[0] == '"':
        i = 1
        while i < len(s):
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == '"':
                break
            i += 1
        else:
            raise PatchError("这一行的双引号没有配对（可能是多行字符串），无法安全修改")
        value, tail = s[:i + 1], s[i + 1:]
    elif s[0] == "'":
        i = 1
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            raise PatchError("这一行的单引号没有配对（可能是多行字符串），无法安全修改")
        value, tail = s[:i + 1], s[i + 1:]
    else:
        m = re.search(r"[ \t]#", s)
        value, tail = (s[:m.start()], s[m.start():]) if m else (s, "")
        stripped = value.rstrip(" \t")
        tail = value[len(stripped):] + tail
        value = stripped
    if tail.strip() and not tail.lstrip(" \t").startswith("#"):
        raise PatchError("值后面还有别的内容，无法安全修改")
    return lead, value, tail


class _Line:
    __slots__ = ("idx", "indent", "path", "lead", "value", "tail", "kind")

    def __init__(self, idx: int, indent: int, path: tuple, lead: str, value: str, tail: str, kind: str):
        self.idx, self.indent, self.path = idx, indent, path
        self.lead, self.value, self.tail, self.kind = lead, value, tail, kind


def _scan(lines: list[str]) -> list[_Line]:
    """逐行解析出每个“键”所在的完整路径。列表项里面的键路径中带 None，永远不会被当成目标。"""
    out: list[_Line] = []
    stack: list[tuple[int, Any]] = []            # (缩进, 键名 或 None=列表项)
    for idx, raw in enumerate(lines):
        body, _ = _body_eol(raw)
        if idx == 0 and body.startswith("﻿"):
            body = body[1:]
        stripped = body.strip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        lead_ws = body[:len(body) - len(body.lstrip(" \t"))]
        if "\t" in lead_ws:
            raise PatchError(f"第 {idx + 1} 行用 tab 缩进，无法安全修改（YAML 要求用空格缩进）")
        if _DOC_MARK.match(body):
            raise PatchError("文件里有多个 YAML 文档（--- 分隔），无法安全修改")
        indent = len(lead_ws)
        seq = _SEQ_LINE.match(body)
        if seq:
            while stack and (stack[-1][0] > indent or (stack[-1][0] == indent and stack[-1][1] is None)):
                stack.pop()
            stack.append((indent, None))
            out.append(_Line(idx, indent, tuple(k for _, k in stack), "", "", "", "seq"))
            continue
        m = _KEY_LINE.match(body)
        if not m:
            out.append(_Line(idx, indent, (), "", "", "", "other"))
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = _unquote_key(m.group("key"))
        path = tuple(k for _, k in stack) + (key,)
        stack.append((indent, key))
        try:
            lead, value, tail = _split_value(m.group("rest"))
        except PatchError:
            lead, value, tail = "", "\0unparsable", ""
        out.append(_Line(idx, indent, path, lead, value, tail, "key"))
    return out


def _parse_key(dotted: str) -> tuple[str, ...]:
    if not isinstance(dotted, str) or not dotted:
        raise PatchError("配置项名称不对")
    parts = tuple(dotted.split("."))
    if any(not _KEY_PART.match(p) for p in parts):
        raise PatchError(f"配置项名称不对：{dotted[:60]}")
    return parts


def format_value(value: Any) -> str:
    """把 Python 值写成 YAML 标量（safe_load 读回来类型和值都不变）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PatchError("数值不能是无穷大或 nan")
        r = repr(value)
        if "e" in r or "E" in r:                    # PyYAML 只认 1.0e-05 这种写法（要有小数点、指数带符号）
            mant, exp = r.lower().split("e")
            if "." not in mant:
                mant += ".0"
            if exp[0] not in "+-":
                exp = "+" + exp
            r = f"{mant}e{exp}"
        elif "." not in r:
            r += ".0"
        return r
    if isinstance(value, str):
        out = ['"']
        for ch in value:
            o = ord(ch)
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif o < 0x20 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029, 0xFEFF) or 0xD800 <= o <= 0xDFFF:
                out.append(f"\\u{o:04x}")
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)
    raise PatchError(f"不支持的值类型：{type(value).__name__}")


def _children(lines: list[str], scanned: list[_Line], at: int, indent: int) -> list[_Line]:
    """紧跟在 scanned[at] 后面、缩进更深的那些行（它的子内容）。"""
    kids = []
    for ln in scanned[at + 1:]:
        if ln.indent <= indent and not (ln.kind == "seq" and ln.indent == indent):
            break
        kids.append(ln)
    return kids


def _block_end(lines: list[str], scanned: list[_Line], at: int, indent: int) -> int:
    """父节的最后一行（不含它后面的空行和顶格注释）的行号。"""
    kids = _children(lines, scanned, at, indent)
    return kids[-1].idx if kids else scanned[at].idx


def set_scalar(text: str, dotted_key: str, value: Any) -> str:
    """把 dotted_key（如 "mail.enabled"）的值改成 value，返回新文本。见模块说明。"""
    parts = _parse_key(dotted_key)
    new_val = format_value(value)
    if not isinstance(text, str):
        raise PatchError("配置文件内容不对")
    lines = _split_lines(text)
    scanned = _scan(lines)
    nl = "\r\n" if "\r\n" in text else "\n"

    hits = [i for i, ln in enumerate(scanned) if ln.kind == "key" and ln.path == parts]
    if len(hits) > 1:
        raise PatchError(f"{dotted_key} 在文件里出现了不止一次，请先手动删掉重复的那一行")
    # 祖先链上的每一节：必须是“键:”后面什么都没有（只能有注释）的块状写法
    for depth in range(1, len(parts)):
        anc = [ln for ln in scanned if ln.kind == "key" and ln.path == parts[:depth]]
        if len(anc) > 1:
            raise PatchError(f"{'.'.join(parts[:depth])} 这一节在文件里出现了不止一次，无法安全修改")
        if anc and anc[0].value:
            raise PatchError(f"{'.'.join(parts[:depth])} 这一节不是常见的分行写法（如 {{a: 1}} 或带锚点），无法安全修改")

    if hits:
        pos = hits[0]
        ln = scanned[pos]
        if ln.value == "\0unparsable":
            raise PatchError(f"{dotted_key} 这一行的写法看不懂（可能是多行字符串），无法安全修改")
        if ln.value[:1] in ("{", "[", "|", ">", "&", "*", "!"):
            raise PatchError(f"{dotted_key} 不是一个简单的值（是一组设置、多行文字、锚点或别名），不能在界面里修改")
        kids = _children(lines, scanned, pos, ln.indent)
        if kids:
            if ln.value:
                raise PatchError(f"{dotted_key} 的值跨了多行，无法安全修改")
            raise PatchError(f"{dotted_key} 是一组设置或列表（下面还有子项），不能当成一个值修改")
        body, eol = _body_eol(lines[ln.idx])
        m = _KEY_LINE.match(body[1:] if ln.idx == 0 and body.startswith("﻿") else body)
        assert m is not None
        bom = "﻿" if ln.idx == 0 and body.startswith("﻿") else ""
        head = bom + body[len(bom):len(bom) + m.start("rest")]
        lead = ln.lead if ln.lead and ln.value else " "     # 原来留空（“键:   # 注释”）时，值紧跟冒号后一个空格
        tail = ln.tail
        if tail.startswith((" ", "\t")) and ln.value:
            # 尽量让行尾注释停在原来那一列（值变长变短时调整中间的空格，至少留一个）
            gap = len(tail) - len(tail.lstrip(" "))
            want = max(1, gap - (len(new_val) - len(ln.value)))
            tail = " " * want + tail.lstrip(" ")
        elif tail and not ln.value:
            tail = " " + tail.lstrip(" ")
        lines[ln.idx] = head + lead + new_val + tail + eol
        return "".join(lines)

    # —— 键不存在：找到最深的已有祖先，在它这一节末尾插入 ——
    depth = 0
    anc_line: _Line | None = None
    for d in range(len(parts) - 1, 0, -1):
        found = [ln for ln in scanned if ln.kind == "key" and ln.path == parts[:d]]
        if found:
            depth, anc_line = d, found[0]
            break
    missing = parts[depth:]
    if anc_line is not None:
        a_pos = scanned.index(anc_line)
        kids = _children(lines, scanned, a_pos, anc_line.indent)
        if kids:
            first = min(k.indent for k in kids)
            if any(k.kind != "key" for k in kids if k.indent == first):
                raise PatchError(f"{'.'.join(parts[:depth])} 下面是列表或特殊写法，不能插入设置项")
        step_indent = min(k.indent for k in kids) if kids else anc_line.indent + 2
        step = max(step_indent - anc_line.indent, 1)
        insert_after = _block_end(lines, scanned, a_pos, anc_line.indent)
        # 最后一项后面紧跟着的、缩进更深的注释行（对齐写的续行说明）属于那一项，插在它们后面
        while insert_after + 1 < len(lines):
            nxt = _body_eol(lines[insert_after + 1])[0]
            if nxt.strip().startswith("#") and len(nxt) - len(nxt.lstrip(" ")) > anc_line.indent:
                insert_after += 1
            else:
                break
        base_indent = step_indent
    else:
        top = [ln for ln in scanned if ln.kind == "key" and len(ln.path) == 1]
        if any(ln.indent != 0 for ln in top) or any(ln.kind in ("seq", "other") and ln.indent == 0 for ln in scanned):
            raise PatchError("文件顶层不是常见的“键: 值”写法，无法安全插入")
        kids_all = [ln for ln in scanned if ln.kind == "key" and len(ln.path) == 2]
        step = min((ln.indent for ln in kids_all), default=2) or 2
        insert_after = len(lines) - 1
        base_indent = 0
    new_lines = []
    for i, name in enumerate(missing):
        ind = " " * (base_indent + i * step)
        if i == len(missing) - 1:
            new_lines.append(f"{ind}{name}: {new_val}{nl}")
        else:
            new_lines.append(f"{ind}{name}:{nl}")
    if insert_after >= 0 and lines and not _body_eol(lines[insert_after])[1]:
        lines[insert_after] = lines[insert_after] + nl      # 最后一行没有换行符：先补上
    if anc_line is None and lines and _body_eol(lines[-1])[0].strip():
        new_lines.insert(0, nl)                             # 新的一节和上面的内容隔一个空行
    lines[insert_after + 1:insert_after + 1] = new_lines
    return "".join(lines)


def _strict_equal(a: Any, b: Any) -> bool:
    """比较时连类型一起比：true 和 1、1 和 1.0 在 Python 里相等，但对配置来说是不同的值。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def _load(text: str, what: str) -> Any:
    try:
        return yaml.safe_load(text.lstrip("﻿")) if text else None
    except yaml.YAMLError as e:
        raise PatchError(f"{what}不是合法的 YAML：{getattr(e, 'problem', None) or type(e).__name__}") from None


def verify(old_text: str, new_text: str, dotted_key: str, value: Any) -> None:
    """回读校验：新旧文本读出来的内容，差别必须恰好只有目标键，且新值等于 value（类型也相同）。"""
    parts = _parse_key(dotted_key)
    old = _load(old_text, "原文件")
    new = _load(new_text, "修改后的文件")
    if old is None:
        old = {}
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise PatchError("配置文件的顶层必须是“键: 值”")
    expected = copy.deepcopy(old)
    cur = expected
    for p in parts[:-1]:
        nxt = cur.get(p)
        if nxt is None:
            nxt = cur[p] = {}
        if not isinstance(nxt, dict):
            raise PatchError(f"{p} 不是一组设置，无法在下面放 {dotted_key}")
        cur = nxt
    cur[parts[-1]] = value
    if not _strict_equal(new, expected):
        raise PatchError(f"修改 {dotted_key} 时回读校验没有通过（文件写法特殊，改动可能影响到别的设置），已放弃修改。"
                         "请用记事本手动修改 config.yaml")
