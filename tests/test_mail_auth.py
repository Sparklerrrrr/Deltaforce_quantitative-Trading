"""审查问题二：邮件身份验证。From 和 In-Reply-To 都能伪造，必须看服务商的 SPF/DKIM/DMARC 结论。"""
import email
import email.policy
from datetime import datetime

import pytest

from dfq import ledger, timeutil
from dfq.mail import auth as A
from dfq.mail import poll
from dfq.mail.transport import InboundMail, MailTransport, build_message, to_inbound

OWNER = "me@example.com"
TRUSTED = ["mx.example.com"]
GOOD = "mx.example.com; spf=pass smtp.mailfrom=me@example.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com"


# ------------------------------------------------------------------ 解析
def test_parse_typical_header():
    p = A.parse_auth_results(GOOD)
    assert p["authserv"] == "mx.example.com"
    assert [(r["method"], r["result"]) for r in p["results"]] == [("spf", "pass"), ("dkim", "pass"), ("dmarc", "pass")]
    assert p["results"][1]["props"]["header.d"] == "example.com"


def test_parse_folded_header_with_comments_and_quotes():
    raw = ('MX.Example.COM (some server) 1;\r\n\tspf=pass (sender IP is 1.2.3.4) smtp.mailfrom=me@example.com;\r\n'
           '\tdkim=pass (2048-bit key) header.d="Example.com" header.i=@example.com;\r\n\tdmarc=pass (p=reject) header.from=example.com')
    p = A.parse_auth_results(raw)
    assert p["authserv"] == "mx.example.com" and len(p["results"]) == 3
    assert p["results"][1]["props"] == {"header.d": "example.com", "header.i": "@example.com"}


@pytest.mark.parametrize("bad", ["", ";", "   ", "<script>; dkim=pass", "mx.example.com"])
def test_parse_garbage(bad):
    p = A.parse_auth_results(bad)
    assert p is None or p["results"] == []


# ------------------------------------------------------------------ 判定
@pytest.mark.parametrize("header", [
    GOOD,
    "mx.example.com; dmarc=pass header.from=example.com",
    "mx.example.com; dmarc=pass",                                   # 没写 header.from：From 已另行核对
    "mx.example.com; dkim=pass header.d=example.com",
    "mx.example.com; dkim=pass header.i=@example.com",
    "mx.example.com; spf=pass smtp.mailfrom=bounce@example.com",
    "mx.example.com; spf=fail smtp.mailfrom=x@example.com; dkim=pass header.d=example.com",   # 转发场景：SPF 挂但 DKIM 过
])
def test_verified(header):
    v = A.verify_sender([header], OWNER, TRUSTED)
    assert v.ok and v.status == A.PASS, v.detail


@pytest.mark.parametrize("header,why", [
    ("mx.example.com; dmarc=fail header.from=example.com", "DMARC 验证失败"),
    ("mx.example.com; dkim=pass header.d=example.com; dmarc=fail header.from=example.com", "DMARC 验证失败"),
    ("mx.example.com; dkim=pass header.d=evil.com", "没有任何一项验证"),             # 别人的域签的名
    ("mx.example.com; spf=pass smtp.mailfrom=a@evil.com", "没有任何一项验证"),       # 别人的域过了 SPF
    ("mx.example.com; dkim=pass header.d=example.com.evil.com", "没有任何一项验证"),  # 形似域名
    ("mx.example.com; dkim=pass header.d=notexample.com", "没有任何一项验证"),
    ("mx.example.com; dmarc=pass header.from=evil.com", "不是你的"),
    ("mx.example.com; spf=softfail smtp.mailfrom=me@example.com; dkim=none; dmarc=none", "没有任何一项验证"),
    ("mx.example.com; spf=pass smtp.helo=example.com", "没有任何一项验证"),          # 只有 HELO 通过不算
    ("mx.example.com; none", "没有任何一项验证"),
])
def test_rejected(header, why):
    v = A.verify_sender([header], OWNER, TRUSTED)
    assert v.status == A.FAIL and why in v.detail


def test_missing_when_provider_wrote_nothing():
    assert A.verify_sender([], OWNER, TRUSTED).status == A.MISSING


def test_missing_when_trusted_authserv_not_configured():
    v = A.verify_sender([GOOD], OWNER, [])
    assert v.status == A.MISSING and v.seen_authserv == ["mx.example.com"]          # 把看到的标识报出来，方便配置


def test_forged_header_from_untrusted_authserv_is_ignored():
    """发信人自己往邮件里塞一个“验证通过”的头：标识不是你的服务商的，一律不认。"""
    forged = "mx.attacker.net; spf=pass smtp.mailfrom=me@example.com; dkim=pass header.d=example.com; dmarc=pass"
    v = A.verify_sender([forged], OWNER, TRUSTED)
    assert v.status == A.MISSING and v.seen_authserv == ["mx.attacker.net"]


def test_forged_header_reusing_trusted_name_loses_to_the_real_one_on_top():
    """发信人冒用你服务商的标识塞了个 pass；但服务商自己的结论在最上面，只认最上面那个。"""
    real = "mx.example.com; spf=fail smtp.mailfrom=x@evil.com; dkim=none; dmarc=none"
    forged = "mx.example.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com"
    assert A.verify_sender([real, forged], OWNER, TRUSTED).status == A.FAIL
    assert A.verify_sender([forged, real], OWNER, TRUSTED).status == A.PASS         # 反过来说明确实只看第一个


def test_owner_address_must_be_valid():
    assert A.verify_sender([GOOD], "", TRUSTED).status == A.FAIL
    assert A.verify_sender([GOOD], "not-an-address", TRUSTED).status == A.FAIL


# ------------------------------------------------------------------ 与记账流程的结合
class Box(MailTransport):
    name = "fake"

    def __init__(self, inbox):
        self.inbox, self.sent = inbox, []

    def send(self, subject, body, *, to, in_reply_to=None, headers=None):
        self.sent.append({"subject": subject, "body": body, "to": to})
        return f"<out-{len(self.sent)}@dfq>"

    def fetch(self, since, from_addr=None):
        return list(self.inbox)


def mail(text="买 PS12B 200 3550", *, mid="<r1@x>", frm=OWNER, reply_to="<plan-1@dfq>", auth=(GOOD,), from_count=1):
    return InboundMail(message_id=mid, from_addr=frm, subject="Re: 计划", date=datetime(2026, 11, 13, 21, 35),
                       text=text, in_reply_to=reply_to, references=[reply_to] if reply_to else [],
                       auth_results=list(auth), from_count=from_count)


@pytest.fixture
def setup(cfg, db):
    cfg.set("mail.owner_address", OWNER)
    cfg.set("mail.auth.trusted_authserv", TRUSTED)
    db.upsert_item(626, "12.7x55mm PS12B", 5, 0)
    db.execute("INSERT INTO mail_log(message_id,direction,subject,ts,status,plan_id,paper) "
               "VALUES('<plan-1@dfq>','out','计划',?,'sent','p1',0)", (timeutil.fmt(timeutil.now()),))
    return cfg, db


def n_trades(db):
    return db.one("SELECT COUNT(*) AS c FROM trades")["c"]


def test_spoofed_from_and_thread_without_authentication_is_not_recorded(setup):
    """最坏情况：攻击者既伪造了你的发件地址，又拿到了计划邮件的编号。没有服务商的验证结论 → 不记账。"""
    cfg, db = setup
    box = Box([mail(auth=())])
    res = poll.poll(cfg, db, box)
    assert n_trades(db) == 0 and box.sent == []                    # 不记账，也不回信（避免被利用来乱发邮件）
    assert res["rejected"][0]["status"] == "unverified" and "身份验证未通过" in res["rejected"][0]["reason"]


def test_authentication_failure_is_not_recorded_and_not_retried(setup):
    cfg, db = setup
    box = Box([mail(auth=("mx.example.com; spf=fail; dkim=fail; dmarc=fail header.from=example.com",))])
    res = poll.poll(cfg, db, box)
    assert n_trades(db) == 0 and box.sent == [] and res["rejected"][0]["status"] == "auth_fail"
    assert poll.poll(cfg, db, box)["rejected"] == []               # 已定性为伪造，不再反复处理
    assert db.one("SELECT status FROM mail_log WHERE message_id='<r1@x>'")["status"] == "auth_fail"


def test_authenticated_for_another_domain_is_not_recorded(setup):
    """攻击者用自己的域名通过了 SPF/DKIM，但 From 写的是你：域对不上 → 不认。"""
    cfg, db = setup
    box = Box([mail(auth=("mx.example.com; spf=pass smtp.mailfrom=a@evil.com; dkim=pass header.d=evil.com; dmarc=fail header.from=example.com",))])
    assert poll.poll(cfg, db, box)["rejected"][0]["status"] == "auth_fail" and n_trades(db) == 0


def test_self_asserted_authentication_header_is_not_trusted(setup):
    cfg, db = setup
    box = Box([mail(auth=("mx.attacker.net; dkim=pass header.d=example.com; dmarc=pass header.from=example.com",))])
    res = poll.poll(cfg, db, box)
    assert n_trades(db) == 0 and res["rejected"][0]["status"] == "unverified"
    assert res["rejected"][0]["authserv_seen"] == ["mx.attacker.net"]
    assert "mail.auth.trusted_authserv" in poll.describe_rejections(res["rejected"])[0]


def test_unconfigured_trusted_authserv_blocks_everything_until_configured(setup):
    """默认配置（没填服务商标识）下邮件一律不记账；填好之后，同一封邮件可以重新处理。"""
    cfg, db = setup
    cfg.set("mail.auth.trusted_authserv", [])
    box = Box([mail()])
    res = poll.poll(cfg, db, box)
    assert n_trades(db) == 0 and res["rejected"][0]["status"] == "unverified"
    cfg.set("mail.auth.trusted_authserv", TRUSTED)
    assert poll.poll(cfg, db, box)["real_recorded"] == 1 and n_trades(db) == 1
    assert poll.poll(cfg, db, box)["real_recorded"] == 0 and n_trades(db) == 1      # 之后照常去重


def test_multiple_from_addresses_are_rejected(setup):
    cfg, db = setup
    box = Box([mail(from_count=2)])
    res = poll.poll(cfg, db, box)
    assert n_trades(db) == 0 and res["ignored"] == 1 and box.sent == []


@pytest.mark.parametrize("frm", ["attacker@evil.com", "me@example.com.evil.com", "me+x@example.com", ""])
def test_wrong_sender_is_ignored_even_if_authenticated(setup, frm):
    cfg, db = setup
    box = Box([mail(frm=frm)])
    assert poll.poll(cfg, db, box)["ignored"] == 1 and n_trades(db) == 0 and box.sent == []


@pytest.mark.parametrize("reply_to", ["", "<plan-2@dfq>", "<plan-1@dfq.evil>", "plan-1@dfq"])
def test_invalid_thread_is_not_recorded(setup, reply_to):
    cfg, db = setup
    box = Box([mail(reply_to=reply_to)])
    assert poll.poll(cfg, db, box)["real_recorded"] == 0 and n_trades(db) == 0


def test_unauthenticated_mail_that_is_not_even_a_reply_is_silently_ignored(setup):
    cfg, db = setup
    box = Box([mail(reply_to="", auth=())])
    res = poll.poll(cfg, db, box)
    assert res["ignored"] == 1 and res["rejected"] == [] and n_trades(db) == 0 and box.sent == []


def test_all_four_checks_pass(setup):
    cfg, db = setup
    box = Box([mail()])
    res = poll.poll(cfg, db, box)
    assert res["real_recorded"] == 1 and res["rejected"] == []
    assert ledger.positions(db)["12.7x55mm PS12B"]["qty"] == 200


def test_missing_owner_address_is_an_error(cfg, db):
    from dfq.mail.transport import MailError
    with pytest.raises(MailError):
        poll.poll(cfg, db, Box([]))


# ------------------------------------------------------------------ 邮件头的提取
def raw_mail(extra_headers: str) -> InboundMail:
    raw = (f"{extra_headers}Subject: Re: plan\r\nMessage-ID: <a@b>\r\nIn-Reply-To: <plan-1@dfq>\r\n"
           f"Date: Fri, 13 Nov 2026 12:00:00 +0800\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhello\r\n")
    return to_inbound(email.message_from_bytes(raw.encode(), policy=email.policy.default))


def test_headers_are_extracted_in_order():
    m = raw_mail("Authentication-Results: mx.example.com; dmarc=fail\r\n"
                 "Authentication-Results: mx.example.com; dmarc=pass\r\nFrom: Me <ME@Example.com>\r\n")
    assert m.from_addr == OWNER and m.from_count == 1 and len(m.auth_results) == 2
    assert A.verify_sender(m.auth_results, OWNER, TRUSTED).status == A.FAIL         # 最上面的是 fail


def test_duplicate_from_headers_and_address_lists_are_counted():
    assert raw_mail("From: me@example.com\r\nFrom: attacker@evil.com\r\n").from_count >= 2
    assert raw_mail("From: me@example.com, attacker@evil.com\r\n").from_count == 2
    assert raw_mail("").from_addr == "" and raw_mail("").from_count == 0


def test_outgoing_message_ids_are_unguessable():
    ids = {str(build_message("s", "b", sender="a@example.com", to="b@example.com")["Message-ID"]) for _ in range(50)}
    assert len(ids) == 50
    import re
    assert all(re.search(r"\.[0-9a-f]{32}@example\.com>$", i) for i in ids)         # 含 128 位随机数
