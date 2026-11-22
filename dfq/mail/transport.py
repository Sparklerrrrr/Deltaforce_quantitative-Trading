"""邮箱接口：发信 + 收信。默认 QQ 邮箱（SMTP/IMAP），可替换成别的服务商。

- SmtpImapTransport：真实收发。密码（QQ 邮箱是“授权码”）只从环境变量读。
- FileTransport：不联网。发信写成 .eml 存到 <data>/outbox/，收信读 <data>/inbox/ 里的 .eml。
  dry-run、模拟模式和测试都用它。
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import html
import imaplib
import re
import secrets
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path

from .. import timeutil


class MailError(Exception):
    pass


@dataclass
class InboundMail:
    message_id: str
    from_addr: str
    subject: str
    date: datetime | None
    text: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    auth_results: list[str] = field(default_factory=list)   # 原始 Authentication-Results 头（自上而下）
    from_count: int = 1                                     # From 头里一共有几个地址；不是 1 就可疑
    from_problem: str = ""                                  # From 头写法可疑的原因（空 = 没发现问题）
    malformed: str = ""                                     # 整封信解析失败的原因（空 = 正常）


MAX_TEXT_CHARS = 200_000          # 正文最多取这么多字符：回复记账只需要开头几行，超大正文只会拖慢解析
MAX_RAW_BYTES = 20_000_000        # 整封信（含附件）超过这么大就不解析，直接标成“无法处理”
MAX_ID_CHARS = 500


class MailTooLarge(ValueError):
    """整封邮件超过 MAX_RAW_BYTES。"""
_ADDRLIKE = re.compile(r"[^\s<>()\",;:@\[\]\\]+@[^\s<>()\",;:@\[\]\\]+")


def norm_msgid(value) -> str:
    """把 Message-ID 规范成唯一的写法：去掉注释和空白、去掉尖括号、统一小写，再统一加回尖括号。

    同一封信的编号可能被写成 `<A@B>`、`<a@b>`、`<a@b> (resent)`、`a@b`；不统一的话，
    去重会把它们当成不同的信，重复记账。写法太离谱（超长、没有内容）返回空串。
    """
    if not isinstance(value, str):
        return ""
    s = re.sub(r"\([^()]{0,200}\)", " ", value[:2000])        # 注释（不处理嵌套：嵌套的注释本来就不正常）
    m = re.search(r"<([^<>]*)>", s)
    inner = m.group(1) if m else s
    inner = re.sub(r"\s+", "", inner).lower()
    if not inner or len(inner) > MAX_ID_CHARS or any(ord(c) < 33 or ord(c) == 127 for c in inner):
        return ""
    return f"<{inner}>"


def build_message(subject: str, body: str, *, sender: str, to: str, in_reply_to: str | None = None,
                  extra_headers: dict | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    domain = sender.split("@")[-1] if "@" in sender else "dfq.local"
    # 邮件编号里放 128 位随机数：回复时带回的这个编号是“回的确实是我发的那封”的凭据之一，必须猜不出来
    msg["Message-ID"] = make_msgid(idstring=secrets.token_hex(16), domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg.set_content(body, charset="utf-8")
    return msg


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?is)<blockquote.*", "", s)                      # 引用的原文整段丢弃
    s = re.sub(r"(?i)<br\s*/?>|</div>|</p>|</li>|</tr>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    return html.unescape(s)


def extract_text(msg) -> str:
    """取邮件正文的纯文本（优先 text/plain，没有再把 HTML 转成文本）。"""
    plain, rich = None, None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError, KeyError, AttributeError, TypeError, ValueError):
            # 畸形邮件（空正文、未知编码……）：尽力解码，解不出就当空
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
        if not isinstance(content, str):
            content = ""
        content = content[:MAX_TEXT_CHARS]
        if part.get_content_subtype() == "plain" and plain is None:
            plain = content
        elif part.get_content_subtype() == "html" and rich is None:
            rich = content
    if plain and plain.strip():
        return plain
    return html_to_text(rich) if rich else ""


def _raw_headers(msg) -> list[tuple[str, str]]:
    """原样的邮件头（不解码 =?utf-8?...?= 编码字、只把折行接回一行）。读不出来的头跳过。"""
    out: list[tuple[str, str]] = []
    try:
        items = list(msg.raw_items())
    except Exception:  # noqa: BLE001 —— 畸形邮件：当没有头
        return out
    for k, v in items:
        try:
            out.append((str(k).strip().lower(), re.sub(r"\r?\n(?=[ \t])", "", str(v))))
        except Exception:  # noqa: BLE001
            continue
    return out


def _count_raw_from_lines(raw: bytes) -> int:
    """直接数原始字节里的 From 头行：`From : x`（冒号前有空格）、放在第一行的 `From :` 等写法，
    Python 的解析器会漏掉或改名，而某些邮箱服务商可能认它 —— 两边读到的发件人不一样就是伪造的机会。"""
    head = re.split(rb"\r?\n\r?\n", raw[:200_000], maxsplit=1)[0]
    return len(re.findall(rb"(?mi)^from[ \t]*:", head))


def _parse_from(msg, headers: list[tuple[str, str]], raw: bytes | None) -> tuple[str, int, str]:
    """(地址, 地址个数, 可疑原因)。要求：恰好一个 From 头、里面恰好一个地址、显示名和注释里不出现 @。

    多个 From 头 / 一个 From 里多个地址 / 把你的地址写进显示名（`me@example.com <attacker@evil.com>`），
    都是伪造邮件的常见手法：服务商验证的可能是其中一个，而我们读到的是另一个。
    """
    raw_vals = [v for k, v in headers if k == "from"]
    n_hdr = len(raw_vals)
    if raw is not None:
        n_hdr = max(n_hdr, _count_raw_from_lines(raw))
    try:
        hdrs = msg.get_all("From") or []
        addrs: list[str] = []
        named_group = False
        for h in hdrs:
            for g in getattr(h, "groups", ()):
                named_group = named_group or g.display_name is not None
            addrs += [str(a.addr_spec) for a in getattr(h, "addresses", ())]
        decoded = [str(h) for h in hdrs]
        if not hdrs and raw_vals:                       # 非 EmailPolicy 的旧式对象：退回到地址列表解析
            addrs = [a for _, a in getaddresses(raw_vals) if a]
            decoded = raw_vals
    except Exception as e:  # noqa: BLE001 —— 例如编码字解出了换行：按可疑处理，绝不让整批轮询失败
        return "", max(n_hdr, 1), f"From 头无法解析（{type(e).__name__}）"
    first = addrs[0].lower() if addrs and "@" in addrs[0] else ""
    count = len(addrs) if n_hdr <= 1 else max(len(addrs), 2)
    if n_hdr == 0:
        return first, count, "没有 From 头"
    if n_hdr > 1:
        return first, count, "有多个 From 头"
    if len(addrs) != 1 or not first:
        return first, count, "From 里不是恰好一个地址"
    if named_group:
        return first, count, "From 用了群组写法"
    for text in raw_vals + decoded:
        if len(text) > 998:
            return first, count, "From 头太长"
        if any((ord(c) < 32 and c not in "\t") or ord(c) == 127 for c in text):
            return first, count, "From 头里有控制字符"
        like = [a.lower().rstrip(".") for a in _ADDRLIKE.findall(text)]
        if text.count("@") != len(like) or any(a != first for a in like):
            return first, count, "From 的显示名或注释里出现了别的地址或多余的 @（把别人的地址伪装成你的常见手法）"
        if text.count("<") > 1 or text.count(">") > 1 or ":" in text or ";" in text:
            return first, count, "From 的写法不正常（多个尖括号、群组或路由写法）"
    return first, count, ""


def _parse_date(msg) -> datetime | None:
    """Date 头 → 北京时间。写错、溢出、离谱的年份一律当没有（成交时间另有规则兜底，见 poll.py）。"""
    try:
        d = parsedate_to_datetime(str(msg.get("Date", "") or ""))
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d = d.astimezone(timeutil.BJ).replace(tzinfo=None)
        return d if 2000 <= d.year <= 2100 else None
    except Exception:  # noqa: BLE001 —— OverflowError、ValueError、TypeError……
        return None


def to_inbound(msg, raw: bytes | None = None) -> InboundMail:
    """把一封邮件转成 InboundMail。任何一个头写坏了都不抛异常：对应字段留空或标为可疑，交给上层拒绝。

    raw：原始字节（有的话），用来核对 From 头的行数。
    """
    headers = _raw_headers(msg)

    def first(name: str) -> str:
        return next((v for k, v in headers if k == name), "")

    from_addr, from_count, from_problem = _parse_from(msg, headers, raw)
    refs = [i for i in (norm_msgid(x) for x in re.findall(r"<[^<>]{1,500}>", first("references"))[-50:]) if i]
    irt = [i for i in (norm_msgid(x) for x in re.findall(r"<[^<>]{1,500}>", first("in-reply-to"))) if i]
    try:
        subject = str(msg.get("Subject", "") or "")[:300]
    except Exception:  # noqa: BLE001
        subject = ""
    malformed = ""
    try:
        text = extract_text(msg)
    except Exception as e:  # noqa: BLE001
        text, malformed = "", f"正文无法解析（{type(e).__name__}）"
    return InboundMail(
        message_id=norm_msgid(first("message-id")),
        from_addr=from_addr, subject=subject, date=_parse_date(msg), text=text,
        in_reply_to=irt[0] if irt else "", references=refs,
        auth_results=[v for k, v in headers if k == "authentication-results"],
        from_count=from_count, from_problem=from_problem, malformed=malformed)


def parse_raw(raw: bytes, fallback_id: str = "") -> InboundMail:
    """从原始字节得到 InboundMail；整封都解析不了时返回一条标了 malformed 的记录（不抛异常）。

    这样一封畸形邮件只会被记一次“无法解析”，不会让整批轮询失败、也不会每次都重新处理。
    """
    try:
        if len(raw) > MAX_RAW_BYTES:
            # 回复记账只需要几行字；几十 MB 的信（大附件）解析起来很慢，又不可能是成交回复
            raise MailTooLarge(f"邮件太大（{len(raw) // 1_000_000} MB，上限 {MAX_RAW_BYTES // 1_000_000} MB）")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        m = to_inbound(msg, raw)
    except Exception as e:  # noqa: BLE001
        mid = re.search(rb"(?mi)^message-id[ \t]*:[ \t]*(<[^<>\r\n]{1,500}>)", raw[:200_000])
        why = (f"{e}，没有处理；记账回复请只写成交记录、不要带附件" if isinstance(e, MailTooLarge)
               else f"整封邮件无法解析（{type(e).__name__}）")
        m = InboundMail(message_id=norm_msgid(mid.group(1).decode("ascii", "replace")) if mid else "",
                        from_addr="", subject="", date=None, text="", from_count=0, malformed=why)
    if not m.message_id:
        # 没有编号的信：用内容的指纹当编号，保证同一封只处理一次
        m.message_id = fallback_id or f"<noid-{hashlib.sha256(raw).hexdigest()[:32]}@dfq.local>"
    return m


class MailTransport(ABC):
    name = "base"

    @abstractmethod
    def send(self, subject: str, body: str, *, to: str, in_reply_to: str | None = None,
             headers: dict | None = None) -> str:
        """发送并返回 Message-ID。"""

    @abstractmethod
    def fetch(self, since: datetime, from_addr: str | None = None) -> list[InboundMail]:
        """读取 since 之后的来信（不改变已读状态）。"""


class FileTransport(MailTransport):
    name = "file"

    def __init__(self, base_dir: Path, sender: str = "dfq@localhost"):
        self.outbox = Path(base_dir) / "outbox"
        self.inbox = Path(base_dir) / "inbox"
        self.sender = sender

    def send(self, subject, body, *, to, in_reply_to=None, headers=None) -> str:
        self.outbox.mkdir(parents=True, exist_ok=True)
        msg = build_message(subject, body, sender=self.sender, to=to or "owner@localhost",
                            in_reply_to=in_reply_to, extra_headers=headers)
        name = f"{timeutil.now():%Y%m%d-%H%M%S}-{len(list(self.outbox.glob('*.eml'))):03d}.eml"
        (self.outbox / name).write_bytes(msg.as_bytes(policy=email.policy.SMTPUTF8))
        (self.outbox / "latest.txt").write_text(f"Subject: {subject}\n\n{body}", encoding="utf-8")
        return str(msg["Message-ID"])

    def fetch(self, since, from_addr=None) -> list[InboundMail]:
        out: list[InboundMail] = []
        if not self.inbox.exists():
            return out
        for p in sorted(self.inbox.glob("*.eml")):
            try:
                raw = p.read_bytes()
            except OSError:
                continue                     # 文件正被别的程序占用：下次再读
            out.append(parse_raw(raw, fallback_id=norm_msgid(f"<file-{p.name}>")))   # 畸形的也返回（标了 malformed）
        return out


class SmtpImapTransport(MailTransport):
    name = "smtp_imap"

    def __init__(self, cfg, smtp_password: str | None, imap_password: str | None):
        self.cfg = cfg
        self.smtp_password = smtp_password
        self.imap_password = imap_password or smtp_password
        self.sender = cfg.get("mail.from_address") or cfg.get("mail.smtp_user") or ""

    def send(self, subject, body, *, to, in_reply_to=None, headers=None) -> str:
        host, port = self.cfg.get("mail.smtp_host"), int(self.cfg.get("mail.smtp_port", 465))
        user = self.cfg.get("mail.smtp_user") or self.sender
        if not (host and user and self.sender and to):
            raise MailError("邮件配置不完整：需要 mail.smtp_host / smtp_user / from_address / owner_address")
        if not self.smtp_password:
            raise MailError("未设置 DFQ_SMTP_PASSWORD（QQ 邮箱填“授权码”，不是登录密码；在“设置”页或 .env 里填写）")
        msg = build_message(subject, body, sender=self.sender, to=to, in_reply_to=in_reply_to,
                            extra_headers=headers)
        ctx = ssl.create_default_context()
        try:
            if port == 465:
                server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls(context=ctx)
            with server:
                server.login(user, self.smtp_password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"发信失败: {type(e).__name__}: {e}") from None
        return str(msg["Message-ID"])

    def fetch(self, since, from_addr=None) -> list[InboundMail]:
        host, port = self.cfg.get("mail.imap_host"), int(self.cfg.get("mail.imap_port", 993))
        user = self.cfg.get("mail.imap_user") or self.cfg.get("mail.smtp_user")
        if not (host and user):
            raise MailError("邮件配置不完整：需要 mail.imap_host / imap_user")
        if not self.imap_password:
            raise MailError("未设置 DFQ_IMAP_PASSWORD 或 DFQ_SMTP_PASSWORD（在 DFQ.exe 的“设置”页填写，或写进程序文件夹的 .env）")
        out: list[InboundMail] = []
        try:
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(f"连接 IMAP 失败: {type(e).__name__}: {e}") from None
        try:
            conn.login(user, self.imap_password)
            conn.select(self.cfg.get("mail.imap_folder", "INBOX"), readonly=True)
            # IMAP 的 SINCE 只精确到天，且按服务器时区；多取一天再在本地过滤
            crit = ["SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y")]
            if from_addr:
                crit += ["FROM", f'"{from_addr}"']
            typ, data = conn.search(None, *crit)
            if typ != "OK":
                raise MailError("IMAP 搜索失败")
            ids = data[0].split()[-200:]
            for num in ids:
                typ, parts = conn.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple) or not isinstance(parts[0][1], bytes):
                    continue
                out.append(parse_raw(parts[0][1]))      # 一封畸形邮件不会让整批失败（标成 malformed 交给上层）
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailError(f"读取邮件失败: {type(e).__name__}: {e}") from None
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out


def make_transport(cfg, *, dry_run: bool = False) -> MailTransport:
    """根据配置选择邮箱实现。dry-run 或未启用邮件时一律写文件，不联网。"""
    from ..config import secret
    backend = cfg.get("mail.backend", "smtp_imap")
    if dry_run or not cfg.get("mail.enabled") or backend == "file":
        return FileTransport(cfg.data_dir, cfg.get("mail.from_address") or "dfq@localhost")
    if backend == "smtp_imap":
        return SmtpImapTransport(cfg, secret("DFQ_SMTP_PASSWORD"), secret("DFQ_IMAP_PASSWORD"))
    raise MailError(f"未知的邮箱后端: {backend}（可选 smtp_imap / file，或在 mail/transport.py 里扩展）")
