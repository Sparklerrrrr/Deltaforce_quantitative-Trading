"""邮箱接口：发信 + 收信。默认 QQ 邮箱（SMTP/IMAP），可替换成别的服务商。

- SmtpImapTransport：真实收发。密码（QQ 邮箱是“授权码”）只从环境变量读。
- FileTransport：不联网。发信写成 .eml 存到 <data>/outbox/，收信读 <data>/inbox/ 里的 .eml。
  dry-run、模拟模式和测试都用它。
"""
from __future__ import annotations

import email
import email.policy
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
        except (LookupError, UnicodeDecodeError, KeyError, AttributeError, TypeError):
            # 畸形邮件（空正文、未知编码……）：尽力解码，解不出就当空
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
        if part.get_content_subtype() == "plain" and plain is None:
            plain = content
        elif part.get_content_subtype() == "html" and rich is None:
            rich = content
    if plain and plain.strip():
        return plain
    return html_to_text(rich) if rich else ""


def to_inbound(msg) -> InboundMail:
    date = None
    try:
        d = parsedate_to_datetime(msg.get("Date", ""))
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            date = d.astimezone(timeutil.BJ).replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    refs = re.findall(r"<[^>]+>", str(msg.get("References", "")))
    # 多个 From 头 / 一个 From 里多个地址，是伪造邮件的常见手法：
    # 服务商验证的可能是其中一个，而我们读到的是另一个。数清楚，交给上层拒绝。
    from_headers = [str(h) for h in (msg.get_all("From") or [])]
    from_addrs = [a for _, a in getaddresses(from_headers) if a]
    irt = re.findall(r"<[^>]+>", str(msg.get("In-Reply-To", "")))
    return InboundMail(
        message_id=str(msg.get("Message-ID", "")).strip(),
        from_addr=from_addrs[0].lower() if from_addrs else "",
        subject=str(msg.get("Subject", "")),
        date=date, text=extract_text(msg),
        in_reply_to=irt[0] if irt else "", references=refs,
        auth_results=[str(h) for h in (msg.get_all("Authentication-Results") or [])],
        from_count=len(from_addrs) if len(from_headers) <= 1 else max(len(from_addrs), 2))


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
        out = []
        if not self.inbox.exists():
            return out
        for p in sorted(self.inbox.glob("*.eml")):
            msg = email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
            m = to_inbound(msg)
            if not m.message_id:
                m.message_id = f"<file-{p.name}>"
            out.append(m)
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
                server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
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
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email.message_from_bytes(parts[0][1], policy=email.policy.default)
                out.append(to_inbound(msg))
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
