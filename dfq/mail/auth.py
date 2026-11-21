"""发件人身份验证：读取邮箱服务商写入的 SPF / DKIM / DMARC 验证结果。

为什么需要：`From` 地址和 `In-Reply-To` 都是发信人自己填的，任何人都能伪造，
单凭它们不能证明“这封信真是你发的”。真正做了密码学验证的是收信服务器，
它会把结论写进 `Authentication-Results` 邮件头（RFC 8601），例如：

    Authentication-Results: mx.example.com;
        spf=pass smtp.mailfrom=me@example.com;
        dkim=pass header.d=example.com;
        dmarc=pass header.from=example.com

本模块不自己做 DNS 查询或验签，只采信**你的邮箱服务商**写的结论。要点：

1. 这个邮件头本身也能被发信人伪造塞进邮件里，所以只认开头标识（authserv-id）
   等于 `mail.auth.trusted_authserv` 的，并且只认**从上往下第一条**这样的头
   （收信服务器把自己的结论加在最前面）；下面再有同名标识的头一律不看。
2. 解析是严格的、失败即拒绝：按 RFC 8601 分词（认得注释和引号字符串，
   发件人能控制的值里藏的 `;`、括号都不会把后面的结果“吃掉”或“伪造”出来）；
   写法不规范、属性重复、值里有多个 @、DKIM 的 header.i 不在 header.d 之内、
   头太长、头太多——一律判不通过，**绝不**跳过这条去看下一条。
3. 通过的条件（满足任一）：
     dmarc=pass，且服务商写明了它验证的 From 域（header.from），并且就是你的邮箱域；
     dkim=pass， 且签名域（header.d）就是你的邮箱域；
     spf=pass，  且信封发件域（smtp.mailfrom）就是你的邮箱域。
   “就是”指完全相等（忽略大小写和结尾的一个点），子域、形似域名都不算。
   只要出现 dmarc=fail，或 dmarc=pass 却写着别的域，直接判不通过。
4. 没有可信的验证结果（服务商没写，或你还没配置 trusted_authserv）→ 身份无法确认 →
   **不自动记账**。此时请用界面或命令行记账。

局限（单靠验证头无法排除，所以 poll.py 另外要求“必须回复一封未过期的计划邮件”，ledger 拒绝超卖）：
- 共享域名的邮箱（如 qq.com）：同一服务商的其他用户发来的信，域级验证同样会通过；
- 服务商既没写验证结果、也没清掉发信人伪造的同名头时，代码分辨不出真假。

未实测：各家邮箱（包括 QQ 邮箱）到底写不写这个头、标识是什么，需要你用自己的邮箱确认。
`python -m dfq mail-poll --dry-run` 会把看到的标识打印出来。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PASS, FAIL, MISSING = "pass", "fail", "missing"

MAX_HEADER_CHARS = 4096      # 一条验证结果头的长度上限：正常的不到 1000 字符；超长的多半是攻击或垃圾
MAX_HEADERS = 20             # 同一封信里验证结果头的条数上限
MAX_RESULTS = 32             # 一条头里的结果条数上限

_TOKEN = re.compile(r"[A-Za-z0-9!#$%&'*+\-^_`{|}~.]+")      # RFC 2045 的 token 字符
_SYMBOLS = set(";=/@:<>,[]?")
_ID = re.compile(r"[A-Za-z0-9._\-]{1,255}")
_KEYWORD = re.compile(r"[A-Za-z0-9\-]{1,64}")
_PROPNAME = re.compile(r"[A-Za-z0-9_\-]{1,64}(?:\.[A-Za-z0-9_\-]{1,64}){0,3}")
_LABEL = r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+")


class Malformed(ValueError):
    """验证结果头写法不规范（说明里是给人看的原因）。"""


@dataclass
class AuthVerdict:
    status: str                      # pass / fail / missing
    detail: str
    seen_authserv: list[str] = field(default_factory=list)   # 邮件里出现过的全部标识（诊断用）

    @property
    def ok(self) -> bool:
        return self.status == PASS


# ------------------------------------------------------------------ 分词
@dataclass
class _Tok:
    kind: str        # tok / quoted / sym
    text: str
    gap: bool        # 前面有没有空白或注释（属性值必须是连在一起的一段）


def _skip_comment(s: str, i: int) -> int:
    """s[i] == '('：跳过一个（可嵌套、可转义的）注释，返回注释之后的位置。没闭合抛 Malformed。"""
    depth, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise Malformed("括号注释没有闭合")


def _lex(s: str) -> list[_Tok]:
    """一遍扫描（耗时与长度成正比）。引号字符串和注释先于一切识别，里面的 ; = 都只是普通字符。"""
    out: list[_Tok] = []
    i, n, gap = 0, len(s), False
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i, gap = i + 1, True
        elif c == "(":
            i, gap = _skip_comment(s, i), True
        elif c == '"':
            j, buf = i + 1, []
            while j < n and s[j] != '"':
                if s[j] == "\\":
                    j += 1
                    if j >= n:
                        break
                buf.append(s[j])
                j += 1
            if j >= n:
                raise Malformed("引号没有闭合")
            out.append(_Tok("quoted", "".join(buf), gap))
            i, gap = j + 1, False
        elif c in _SYMBOLS:
            out.append(_Tok("sym", c, gap))
            i, gap = i + 1, False
        else:
            m = _TOKEN.match(s, i)
            if not m:
                raise Malformed(f"出现了不该有的字符 {c!r}")
            out.append(_Tok("tok", m.group(0), gap))
            i, gap = m.end(), False
    return out


def _check_chars(s: str) -> None:
    for ch in s:
        o = ord(ch)
        if (o < 32 and ch not in "\t\r\n") or o == 127 or 0xD800 <= o <= 0xDFFF:
            raise Malformed("含有控制字符或无法解码的字节")


# ------------------------------------------------------------------ 解析
def _parse_strict(value: str) -> dict:
    s = value if isinstance(value, str) else ""
    if len(s) > MAX_HEADER_CHARS:
        raise Malformed(f"长度 {len(s)} 超过 {MAX_HEADER_CHARS} 字符")
    _check_chars(s)
    groups: list[list[_Tok]] = [[]]
    for t in _lex(s):
        if t.kind == "sym" and t.text == ";":
            groups.append([])
        else:
            groups[-1].append(t)
    head = groups[0]
    if not head or head[0].kind != "tok" or not _ID.fullmatch(head[0].text):
        raise Malformed("开头的服务商标识（authserv-id）写法不规范")
    if len(head) > 2 or (len(head) == 2 and not (head[1].kind == "tok" and head[1].text.isdigit() and head[1].gap)):
        raise Malformed("服务商标识后面多出了内容")
    body = [g for g in groups[1:] if g]          # 结尾多一个分号很常见，空段忽略
    if len(body) > MAX_RESULTS:
        raise Malformed("结果条数太多")
    results = []
    for g in body:
        if len(g) == 1 and g[0].kind == "tok" and g[0].text.lower() == "none":
            continue                              # “none” = 服务商没做任何验证
        results.append(_parse_resinfo(g))
    return {"authserv": head[0].text.lower().rstrip("."), "results": results}


def _parse_resinfo(g: list[_Tok]) -> dict:
    """method [/version] = result  然后若干个  name = value。"""
    if g[0].kind != "tok" or not _KEYWORD.fullmatch(g[0].text):
        raise Malformed("验证方法的名字写法不规范")
    method = g[0].text.lower()
    k = 1
    if k < len(g) and g[k].kind == "sym" and g[k].text == "/":
        if k + 1 >= len(g) or g[k + 1].kind != "tok" or not g[k + 1].text.isdigit():
            raise Malformed("验证方法的版本号写法不规范")
        k += 2
    if not (k + 1 < len(g) and g[k].kind == "sym" and g[k].text == "=" and g[k + 1].kind == "tok"
            and _KEYWORD.fullmatch(g[k + 1].text)):
        raise Malformed(f"{method} 的结果写法不规范")
    result = g[k + 1].text.lower()
    k += 2
    props: dict[str, str] = {}
    while k < len(g):
        name_tok = g[k]
        if name_tok.kind != "tok" or not name_tok.gap or not _PROPNAME.fullmatch(name_tok.text):
            raise Malformed(f"{method} 的属性写法不规范")
        name = name_tok.text.lower()
        k += 1
        if not (k < len(g) and g[k].kind == "sym" and g[k].text == "="):
            raise Malformed(f"属性 {name} 没有写值")
        k += 1
        if k >= len(g):
            raise Malformed(f"属性 {name} 的值是空的")
        parts = [g[k].text]
        k += 1
        while k < len(g) and not g[k].gap:      # 值是连在一起的一段（引号串 + @ + 域名 这样的拼接）
            parts.append(g[k].text)
            k += 1
        if name in props:
            raise Malformed(f"属性 {name} 出现了两次")
        val = "".join(parts).strip().lower()
        if val.count("@") > 1:
            raise Malformed(f"属性 {name} 的值里有多个 @")
        props[name] = val
    return {"method": method, "result": result, "props": props}


def parse_auth_results(value: str) -> dict | None:
    """解析一个 Authentication-Results 头。写法不规范返回 None（要知道原因用 _parse_strict）。"""
    try:
        return _parse_strict(value)
    except Malformed:
        return None


def _leading_id(value) -> str | None:
    """取一条头开头的服务商标识（只看开头，花费有上限），用来挑出“你的服务商写的”那一条。

    只取到第一个不能出现在域名里的字符为止：`mx.example.com/1`、`mx.example.com:25` 都算以
    mx.example.com 开头（随后在严格解析时判为不规范，而不是被当成别人的头跳过去）。
    """
    if not isinstance(value, str):
        return None
    s = value[:MAX_HEADER_CHARS]
    i, n = 0, len(s)
    while i < n:
        if s[i] in " \t\r\n":
            i += 1
        elif s[i] == "(":
            try:
                i = _skip_comment(s, i)
            except Malformed:
                return None
        else:
            break
    m = _ID.match(s, i)
    return m.group(0).lower().rstrip(".") if m else None


# ------------------------------------------------------------------ 判定
def _norm_domain(v: str | None) -> str | None:
    """一个“纯域名”值：合法域名才返回（小写、去掉结尾一个点），否则 None —— 绝不猜。"""
    if not v:
        return None
    d = v.strip().lower()
    if d.endswith("."):
        d = d[:-1]
    return d if _DOMAIN.fullmatch(d) else None


def _addr_domain(v: str | None, *, allow_bare: bool) -> str | None:
    """“地址”值（local@domain、@domain，allow_bare 时也接受纯域名）的域；写法异常返回 None。"""
    if not v:
        return None
    if v.count("@") == 1:
        return _norm_domain(v.split("@", 1)[1])
    if "@" not in v and allow_bare:
        return _norm_domain(v)
    return None


def _owner_domain(owner_address: str) -> str | None:
    s = (owner_address or "").strip().lower()
    if s.count("@") != 1 or not s.split("@")[0]:
        return None
    return _norm_domain(s.split("@")[1])


def _trusted_set(trusted_authserv) -> set[str]:
    if isinstance(trusted_authserv, str):
        trusted_authserv = [trusted_authserv]          # 配置里写成了字符串而不是列表：当成一项，不能按字符拆开
    if not isinstance(trusted_authserv, (list, tuple, set)):
        return set()
    out = set()
    for t in trusted_authserv:
        if isinstance(t, str):
            t = t.strip().lower().rstrip(".")
            if _ID.fullmatch(t) and "." in t:
                out.add(t)
    return out


def verify_sender(auth_headers: list[str], owner_address: str, trusted_authserv: list[str]) -> AuthVerdict:
    """判断这封信是否经可信的服务商验证为确实来自 owner_address 的域。"""
    headers = [h for h in (auth_headers or []) if isinstance(h, str)]
    seen = list(dict.fromkeys(i for i in (_leading_id(h) for h in headers[:MAX_HEADERS * 2]) if i))
    owner_domain = _owner_domain(owner_address)
    if not owner_domain:
        return AuthVerdict(FAIL, "mail.owner_address 不是合法的邮箱地址", seen)
    trusted = _trusted_set(trusted_authserv)
    if not trusted:
        return AuthVerdict(MISSING, "还没有配置 mail.auth.trusted_authserv，无法采信任何验证结果", seen)
    if len(headers) > MAX_HEADERS:
        return AuthVerdict(FAIL, f"邮件里有 {len(headers)} 条验证结果头，多得不正常，不予采信", seen)
    if any(len(h) > MAX_HEADER_CHARS for h in headers):
        return AuthVerdict(FAIL, f"邮件里有超过 {MAX_HEADER_CHARS} 字符的验证结果头，不予采信", seen)
    mine = next((h for h in headers if _leading_id(h) in trusted), None)
    if mine is None:
        return AuthVerdict(MISSING, "邮件里没有你的邮箱服务商写入的验证结果（Authentication-Results）", seen)
    # 只采信从上往下第一条：收信服务器总是把自己的结论加在最前面，
    # 下面即使还有同名标识的头，也可能是发信人预先塞进邮件里冒充的。写法不规范也不能跳过它去看下面的。
    try:
        parsed = _parse_strict(mine)
    except Malformed as e:
        return AuthVerdict(FAIL, f"你的邮箱服务商写的验证结果格式不规范（{e}），为安全起见不予采信", seen)
    return _judge(parsed["results"], owner_domain, seen)


def _judge(results: list[dict], d: str, seen: list[str]) -> AuthVerdict:
    dmarc = [r for r in results if r["method"] == "dmarc"]
    if any(r["result"] == "fail" for r in dmarc):
        return AuthVerdict(FAIL, "DMARC 验证失败（dmarc=fail）：发件人很可能是伪造的", seen)
    if len(dmarc) > 1:
        return AuthVerdict(FAIL, "验证结果里出现了多条 DMARC 结论，不予采信", seen)
    passed: list[str] = []
    notes: list[str] = []
    for r in results:
        method, result, props = r["method"], r["result"], r["props"]
        if method == "dkim" and "header.d" in props and "header.i" in props:
            dd, di = _norm_domain(props["header.d"]), _addr_domain(props["header.i"], allow_bare=False)
            if not dd or not di or not (di == dd or di.endswith("." + dd)):
                return AuthVerdict(FAIL, "DKIM 结果里签名身份（header.i）不在签名域（header.d）之内，不予采信", seen)
        if result != "pass":
            continue
        if method == "dmarc":
            if "header.from" not in props:
                notes.append("dmarc=pass 但服务商没有写明它验证的是哪个域（header.from），这一项不算数")
                continue
            hf = _norm_domain(props["header.from"])
            if hf != d:
                return AuthVerdict(FAIL, f"DMARC 验证的发件域是 {hf or '（写法异常）'}，不是你的 {d}", seen)
            passed.append("dmarc")
        elif method == "dkim":
            sd = _norm_domain(props["header.d"]) if "header.d" in props else \
                _addr_domain(props.get("header.i"), allow_bare=False)
            if sd == d:
                passed.append("dkim")
        elif method == "spf":
            mf = _addr_domain(props.get("smtp.mailfrom"), allow_bare=True)   # 只认信封发件域；仅 HELO 通过不算
            if mf == d:
                passed.append("spf")
    if passed:
        return AuthVerdict(PASS, "已通过：" + "、".join(dict.fromkeys(passed)), seen)
    why = f"没有任何一项验证（SPF/DKIM/DMARC）证明这封信来自 {d}"
    return AuthVerdict(FAIL, why + ("（" + "；".join(notes) + "）" if notes else ""), seen)
