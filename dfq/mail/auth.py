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
   等于 `mail.auth.trusted_authserv` 的，并且只认其中最靠上的那一个
   （收信服务器把自己的结论加在最前面）；其余一律当不存在。
2. 通过的条件（满足任一）：
     dmarc=pass，且它验证的 From 域就是你的邮箱域；
     dkim=pass， 且签名域（header.d）等于你的邮箱域；
     spf=pass，  且信封发件域（smtp.mailfrom）等于你的邮箱域。
   只要出现 dmarc=fail，直接判不通过。
3. 没有可信的验证结果（服务商没写，或你还没配置 trusted_authserv）→ 身份无法确认 →
   **不自动记账**。此时请用命令行记账。

未实测：各家邮箱（包括 QQ 邮箱）到底写不写这个头、标识是什么，需要你用自己的邮箱确认。
`python -m dfq mail-poll --dry-run` 会把看到的标识打印出来。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PASS, FAIL, MISSING = "pass", "fail", "missing"


@dataclass
class AuthVerdict:
    status: str                      # pass / fail / missing
    detail: str
    seen_authserv: list[str] = field(default_factory=list)   # 邮件里出现过的全部标识（诊断用）

    @property
    def ok(self) -> bool:
        return self.status == PASS


def _strip_comments(s: str) -> str:
    """去掉 RFC 5322 的括号注释（可嵌套）。"""
    out, depth = [], 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def parse_auth_results(value: str) -> dict | None:
    """解析一个 Authentication-Results 头。格式不对返回 None。"""
    text = _strip_comments(str(value or "")).replace("\r", " ").replace("\n", " ")
    parts = [p.strip() for p in text.split(";")]
    if not parts or not parts[0]:
        return None
    authserv = parts[0].split()[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9._\-]+", authserv):
        return None
    results = []
    for part in parts[1:]:
        m = re.match(r"^([a-zA-Z0-9\-]+)\s*=\s*([a-zA-Z]+)\b(.*)$", part)
        if not m:
            continue
        props = {k.lower(): v.strip().strip('"').lower()
                 for k, v in re.findall(r"([a-zA-Z0-9\-]+\.[a-zA-Z0-9\-]+)\s*=\s*(\"[^\"]*\"|\S+)", m.group(3))}
        results.append({"method": m.group(1).lower(), "result": m.group(2).lower(), "props": props})
    return {"authserv": authserv, "results": results}


def _domain(addr_or_domain: str) -> str:
    s = (addr_or_domain or "").strip().lower().strip("<>")
    return s.rsplit("@", 1)[-1].lstrip("@").rstrip(".")


def verify_sender(auth_headers: list[str], owner_address: str, trusted_authserv: list[str]) -> AuthVerdict:
    """判断这封信是否经可信的服务商验证为确实来自 owner_address 的域。"""
    owner_domain = _domain(owner_address)
    trusted = {str(t).strip().lower() for t in (trusted_authserv or []) if str(t).strip()}
    parsed = [p for p in (parse_auth_results(h) for h in auth_headers or []) if p]
    seen = list(dict.fromkeys(p["authserv"] for p in parsed))
    if not owner_domain or "." not in owner_domain:
        return AuthVerdict(FAIL, "mail.owner_address 不是合法的邮箱地址", seen)
    if not trusted:
        return AuthVerdict(MISSING, "还没有配置 mail.auth.trusted_authserv，无法采信任何验证结果", seen)
    mine = [p for p in parsed if p["authserv"] in trusted]
    if not mine:
        return AuthVerdict(MISSING, "邮件里没有你的邮箱服务商写入的验证结果（Authentication-Results）", seen)
    # 只采信最上面那一个：收信服务器总是把自己的结论加在最前面，
    # 下面即使还有同名标识的头，也可能是发信人预先塞进邮件里冒充的。
    mine = mine[:1]

    passed: list[str] = []
    for p in mine:
        for r in p["results"]:
            method, result, props = r["method"], r["result"], r["props"]
            if method == "dmarc":
                if result == "fail":
                    return AuthVerdict(FAIL, "DMARC 验证失败（dmarc=fail）：发件人很可能是伪造的", seen)
                if result == "pass":
                    hf = props.get("header.from")
                    if hf is not None and _domain(hf) != owner_domain:
                        return AuthVerdict(FAIL, f"DMARC 验证的发件域是 {_domain(hf)}，不是你的 {owner_domain}", seen)
                    passed.append("dmarc")
            elif method == "dkim" and result == "pass":
                d = props.get("header.d") or _domain(props.get("header.i", ""))
                if d and _domain(d) == owner_domain:
                    passed.append("dkim")
            elif method == "spf" and result == "pass":
                mf = props.get("smtp.mailfrom")      # 只认信封发件域；仅 HELO 通过不算
                if mf and _domain(mf) == owner_domain:
                    passed.append("spf")
    if passed:
        return AuthVerdict(PASS, "已通过：" + "、".join(dict.fromkeys(passed)), seen)
    return AuthVerdict(FAIL, f"没有任何一项验证（SPF/DKIM/DMARC）证明这封信来自 {owner_domain}", seen)
