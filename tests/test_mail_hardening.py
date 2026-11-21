"""审查第 6 条：邮件记账的边界检查（伪造验证头、发件人伪装、重复投递、重放、中途崩溃、并发、畸形邮件、超大输入）。

每个测试都对应一个在旧版本上能复现的问题（复现脚本见修复记录）：旧版本要么放行了伪造的来信，
要么把同一笔成交记了两遍，要么被一封畸形邮件拖垮整批轮询。
"""
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from dfq import ledger
from dfq.db import DB
from dfq.mail import auth, poll, reply, transport
from dfq.mail.auth import verify_sender
from dfq.mail.transport import FileTransport, InboundMail, MailTransport, build_message, norm_msgid, parse_raw

OWNER = "me@example.com"
TRUSTED = ["mx.example.com"]
GOOD = ("mx.example.com; spf=pass smtp.mailfrom=me@example.com; dkim=pass header.d=example.com; "
        "dmarc=pass header.from=example.com")
FORGED = GOOD                    # 伪造的头和真的长得一模一样：区别只在于它在哪个位置
REPO = Path(__file__).resolve().parents[1]


def verdict(headers):
    return verify_sender(headers, OWNER, TRUSTED)


# ====================================================================== 验证结果头
def test_dmarc_pass_without_header_from_is_not_accepted():
    """旧版本：dmarc=pass 不写 header.from 也放行 —— 根本不知道服务商验证的是哪个域。"""
    for h in ["mx.example.com; dmarc=pass", "mx.example.com; spf=fail smtp.mailfrom=me@example.com; dkim=fail; dmarc=pass"]:
        v = verdict([h])
        assert v.status == auth.FAIL and "header.from" in v.detail


def test_dmarc_pass_for_another_domain_fails_even_if_dkim_passes():
    v = verdict(["mx.example.com; dkim=pass header.d=example.com; dmarc=pass header.from=evil.com"])
    assert v.status == auth.FAIL and "evil.com" in v.detail


@pytest.mark.parametrize("header", [
    "mx.example.com; dmarc=pass header.from=evil.com@example.com",            # 多个 @ / 地址冒充域名
    "mx.example.com; dmarc=pass header.from=mail.example.com",                # 子域不算
    "mx.example.com; dmarc=pass header.from=example.com.evil.com",            # 形似域名
    'mx.example.com; dmarc=pass header.from=""',                              # 空值
    "mx.example.com; dkim=pass header.i=x@evil.com@example.com",              # header.i 多个 @
    "mx.example.com; dkim=pass header.d=example.com header.i=@evil.com",      # header.i 不在 header.d 之内
    "mx.example.com; dkim=pass header.d=evil.com header.i=@example.com",
    "mx.example.com; dmarc=pass header.from=evil.com header.from=example.com",  # 属性重复（旧版本取最后一个）
    "mx.example.com; dmarc=pass header.from=example.com; dmarc=pass header.from=example.com",  # 多条 DMARC 结论
    # 发件人可控的引号串里藏 “; dkim=pass …”：旧版本把它当成一条真的结果
    'mx.example.com; spf=pass smtp.mailfrom="x; dkim=pass header.d=example.com; y"@evil.com; dkim=none; dmarc=none',
    # 引号串里一个左括号把后面真正的 dmarc=fail “吃掉”
    'mx.example.com; spf=pass smtp.mailfrom="x; dkim=pass header.d=example.com; ("@evil.com; dkim=none; '
    'dmarc=fail header.from=example.com',
    # 转义的引号
    r'mx.example.com; spf=pass smtp.mailfrom="a\"; dkim=pass header.d=example.com; \"b"@evil.com; dmarc=none',
    "mx.example.com; spf=pass smtp.mailfrom=a@example.com (没闭合的注释; dmarc=fail",
    "mx.example.com; dmarc＝pass header.from=example.com",                    # 全角等号
    "mx.example.com; dmarc=pass header.from=exаmple.com",                     # 西里尔字母 а
    "mx.example.com; dmarc=pass header.from=example.com\x00",                 # 控制字符
    "mx.example.com; dmarc=pass header.from=example.com; dkim=pass header.d=example.com " + "x" * 5000,  # 超长
])
def test_forged_or_malformed_results_never_pass(header):
    assert verdict([header]).status == auth.FAIL


@pytest.mark.parametrize("top", [
    "mx.example.com; dmarc=fail header.from=example.com",            # 服务商自己的结论：失败
    "mx.example.com/1; dmarc=fail header.from=example.com",          # 写法稍有不同：旧版本跳过它，采信下面伪造的
    "mx.example.com:25; dmarc=fail header.from=example.com",
    "MX.Example.COM.; dmarc=pass header.from=example.com; ???",       # 以受信任标识开头但写坏了
    "mx.example.com; ",                                               # 没有任何结果
])
def test_only_the_topmost_trusted_header_counts(top):
    """服务商把自己的结论加在最前面；下面同名的头可能是发信人预先塞进来的，一律不看。"""
    assert verdict([top, FORGED]).status == auth.FAIL


def test_headers_of_other_servers_above_are_skipped_but_not_trusted():
    v = verdict(["mx.other-hop.net; dmarc=pass header.from=example.com", "mx.example.com; dmarc=fail"])
    assert v.status == auth.FAIL and v.seen_authserv == ["mx.other-hop.net", "mx.example.com"]
    assert verdict(["mx.example.com.attacker.net; " + FORGED.split("; ", 1)[1]]).status == auth.MISSING


def test_too_many_auth_headers_fail():
    assert verdict([GOOD] * (auth.MAX_HEADERS + 1)).status == auth.FAIL
    assert verdict([GOOD] * 3).ok                                     # 正常数量：采信第一条


@pytest.mark.parametrize("header", [
    GOOD,
    "MX.EXAMPLE.COM.; DMARC=PASS header.from=EXAMPLE.COM.",           # 大小写、结尾的点
    "mx.example.com 1; dmarc=pass header.from=example.com",           # 带版本号
    "(收信服务器) mx.example.com; dkim=pass (签名有效) header.d=example.com header.i=@example.com",
    "mx.example.com; dmarc/1=pass header.from=example.com",
    "mx.example.com; spf=pass smtp.mailfrom=example.com",             # 只写域名的 mailfrom
    "mx.example.com;\r\n\tdkim=pass header.d=example.com;\r\n\tdmarc=none",
    'mx.example.com; dkim=pass reason="good signature; ok" header.d=example.com',   # 引号串里的分号不切段
])
def test_genuine_results_still_pass(header):
    assert verdict([header]).ok, verdict([header]).detail


def test_huge_headers_are_rejected_fast():
    """旧版本：10 万字符的属性名解析 6.5 秒（平方增长）。现在超长直接拒绝，长度上限内也是线性的。"""
    cases = [
        ["mx.example.com; dkim=pass " + "a" * 100_000 + "=b"],
        ["mx.example.com; " + "spf=pass smtp.mailfrom=me@example.com; " * 100],       # 刚好不超长、结果很多
        ["mx.example.com; dkim=pass " + "a." * 2000 + "=b"],
        ["(" * 500_000],
        [GOOD + " " * 100_000] * 30,
    ]
    for headers in cases:
        t = time.perf_counter()
        v = verdict(headers)
        assert time.perf_counter() - t < 0.5 and not v.ok


@pytest.mark.parametrize("headers", [None, [None], [b"bytes"], [123], [";;;;"], ["  \t"]])
def test_garbage_input_never_raises(headers):
    assert not verdict(headers).ok


def test_owner_or_trusted_misconfigured_never_passes():
    assert verify_sender([GOOD], "a@evil.com@example.com", TRUSTED).status == auth.FAIL
    assert verify_sender([GOOD], "Me <me@example.com>", TRUSTED).status == auth.FAIL
    assert verify_sender([GOOD], OWNER, "m").status == auth.MISSING              # 1 个字符的标识不算
    assert verify_sender([GOOD], OWNER, "mx.example.com").ok                    # 写成字符串而不是列表：当成一项


# ====================================================================== From 头
TAIL = (b"Message-ID: <p@x>\r\nSubject: Re\r\nDate: Fri, 13 Nov 2026 12:00:00 +0800\r\n"
        b"In-Reply-To: <plan-1@dfq>\r\n\r\nhi\r\n")


@pytest.mark.parametrize("head", [
    b'From: "me@example.com" <attacker@evil.com>\r\n',               # 把你的地址写进显示名
    b"From: me@example.com <attacker@evil.com>\r\n",
    b"From: Me <me@example.com> (attacker@evil.com)\r\n",            # 注释里另一个地址
    b'From: "attacker@evil.com" <me@example.com>\r\n',               # 显示名里有 @：一律可疑
    b"From: me@example.com\r\nFrom: attacker@evil.com\r\n",           # 两个 From 头
    b"From : attacker@evil.com\r\nFrom: me@example.com\r\n",          # 冒号前有空格的写法
    b"From: me@example.com, attacker@evil.com\r\n",                  # 一个头里两个地址
    b"From: grp: me@example.com;\r\n",                               # 群组写法
    b"From: =?utf-8?q?me@example.com=0D=0AFrom:?= <attacker@evil.com>\r\n",   # 编码字解出换行
    b"From: =?utf-8?q?x=0D=0A?= <me@example.com>\r\n",
    b"From: =?utf-8?b?bWVAZXhhbXBsZS5jb20gKA==?= <attacker@evil.com>\r\n",    # 解码成 'me@example.com ('
    b"From: Me <me@example.com>" + b"x" * 2000 + b"\r\n",
    b"Subject: no from at all\r\n",
])
def test_from_tricks_are_not_accepted_as_owner(head):
    m = parse_raw(head + TAIL)
    assert not (m.from_addr == OWNER and m.from_count == 1 and not m.from_problem)


def test_plain_from_is_accepted():
    for head in (b"From: me@example.com\r\n", b"From: =?utf-8?b?5oiR?= <ME@Example.com>\r\n"):
        m = parse_raw(head + TAIL)
        assert m.from_addr == OWNER and m.from_count == 1 and m.from_problem == ""


# ====================================================================== Message-ID
def test_message_id_is_normalized():
    assert {norm_msgid(x) for x in ["<R1@X>", "<r1@x> (resent)", "  <r1@x>  ", "r1@x", "<r1 @x>"]} == {"<r1@x>"}
    for bad in ["", "<>", None, 123, "<" + "a" * 600 + "@x>", "<a\x01@x>"]:
        assert norm_msgid(bad) == ""


def test_outgoing_message_id_is_unpredictable():
    a = str(build_message("s", "b", sender="dfq@example.com", to=OWNER)["Message-ID"])
    b = str(build_message("s", "b", sender="dfq@example.com", to=OWNER)["Message-ID"])
    assert a != b and re.search(r"[0-9a-f]{32}", a)


# ====================================================================== 轮询与记账
class Box(MailTransport):
    name = "fake"

    def __init__(self, inbox=None):
        self.inbox, self.sent = inbox or [], []

    def send(self, subject, body, *, to, in_reply_to=None, headers=None):
        self.sent.append({"subject": subject, "body": body, "to": to, "in_reply_to": in_reply_to})
        return f"<out-{len(self.sent)}-{id(self)}@dfq>"

    def fetch(self, since, from_addr=None):
        return list(self.inbox)


def mail(text="买 PS12B 200 3550", *, mid="<r1@x>", frm=OWNER, reply_to="<plan-1@dfq>", auth_=(GOOD,),
         date=datetime(2026, 11, 13, 12, 0), from_count=1, from_problem=""):
    return InboundMail(message_id=mid, from_addr=frm, subject="Re: 计划", date=date, text=text,
                       in_reply_to=reply_to, references=[reply_to] if reply_to else [],
                       auth_results=list(auth_), from_count=from_count,
                       **({"from_problem": from_problem} if from_problem else {}))


PLAN_TS = "2026-11-12 21:30:00"          # 现在是 2026-11-13 21:30（conftest）：计划是前一天晚上发的


def _prepare(cfg, db, plan_ts=PLAN_TS):
    cfg.set("mail.owner_address", OWNER)
    cfg.set("mail.auth.trusted_authserv", TRUSTED)
    for oid, name in ((626, "12.7x55mm PS12B"), (640, "5.7x28mm SS190")):
        db.upsert_item(oid, name, 5, 0)
    db.execute("INSERT INTO mail_log(message_id,direction,subject,ts,status,plan_id,paper) "
               "VALUES('<plan-1@dfq>','out','计划',?,'sent','p1',0)", (plan_ts,))


@pytest.fixture
def setup(cfg, db):
    _prepare(cfg, db)
    return cfg, db


def trades(db):
    return [(r["side"], r["name"], r["qty"], r["price"]) for r in db.query("SELECT * FROM trades ORDER BY id")]


def inbound_rows(db):
    return {r["message_id"]: r["status"] for r in db.query("SELECT * FROM mail_log WHERE direction='in'")}


def test_message_id_variants_are_the_same_mail(setup):
    cfg, db = setup
    res = poll.poll(cfg, db, Box([mail(mid="<R1@X>"), mail(mid="<r1@x> (resent)"), mail(mid=" <r1@x> ")]))
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0)] and res["rejected"] == []
    assert inbound_rows(db) == {"<r1@x>": "ok"}


def test_resend_with_new_message_id_is_not_booked_twice(setup):
    """旧版本：客户端重发（换了 Message-ID）会记两遍。"""
    cfg, db = setup
    box = Box([mail(mid="<r1@x>"), mail(mid="<r1-resend@x>")])
    res = poll.poll(cfg, db, box)
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0)]
    assert res["duplicates"] == 1 and res["rejected"][0]["status"] == "duplicate" and "重复" in res["rejected"][0]["reason"]
    again = poll.poll(cfg, db, box)
    assert again["rejected"] == [] and len(trades(db)) == 1                       # 不再重复提示


def test_different_content_for_the_same_plan_is_booked(setup):
    cfg, db = setup
    poll.poll(cfg, db, Box([mail(mid="<r1@x>"), mail("买 PS12B 100 3500", mid="<r2@x>")]))
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0), ("buy", "12.7x55mm PS12B", 100, 3500.0)]


def test_resend_after_undo_is_accepted(setup):
    cfg, db = setup
    poll.poll(cfg, db, Box([mail(mid="<r1@x>")]))
    ledger.delete_trade(db, db.one("SELECT id FROM trades")["id"])                # 你撤销了那一笔
    poll.poll(cfg, db, Box([mail(mid="<r1-again@x>")]))
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0)]


def test_reply_to_an_old_plan_is_not_booked(cfg, db):
    """旧版本：一年前的计划邮件现在回复（或被人翻出来重放）照样记账。"""
    _prepare(cfg, db, plan_ts="2026-11-09 21:30:00")                              # 4 天前，默认有效期 3 天
    box = Box([mail()])
    res = poll.poll(cfg, db, box)
    assert trades(db) == [] and res["rejected"][0]["status"] == "expired"
    assert len(box.sent) == 1 and "reply_max_age_days" in box.sent[0]["body"] and "python -m dfq trade" in box.sent[0]["body"]
    poll.poll(cfg, db, box)
    assert len(box.sent) == 1                                                     # 不重复回信


def test_replying_to_a_recent_failure_notice_does_not_renew_an_old_plan(cfg, db):
    _prepare(cfg, db, plan_ts="2026-11-09 21:30:00")
    db.execute("INSERT INTO mail_log(message_id,direction,subject,ts,status,plan_id,paper) "
               "VALUES('<notice@dfq>','out','没记账',?,'sent','p1',0)", ("2026-11-13 10:00:00",))
    res = poll.poll(cfg, db, Box([mail(reply_to="<notice@dfq>")]))
    assert trades(db) == [] and res["rejected"][0]["status"] == "expired"


def test_reply_max_age_is_configurable(cfg, db):
    _prepare(cfg, db, plan_ts="2026-11-09 21:30:00")
    cfg.set("mail.reply_max_age_days", 5)
    poll.poll(cfg, db, Box([mail()]))
    assert len(trades(db)) == 1


def test_crash_in_the_middle_books_nothing_and_retry_books_once(setup, monkeypatch):
    """旧版本：记了第一笔后崩溃，这封信没标成已处理，下次轮询把第一笔再记一遍。"""
    cfg, db = setup
    real, calls = ledger.record_trade, {"n": 0}

    def crash_on_second(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("模拟断电 / 进程被杀")
        return real(*a, **k)

    monkeypatch.setattr(ledger, "record_trade", crash_on_second)
    box = Box([mail("买 PS12B 200 3550\n买 SS190 150 5420")])
    with pytest.raises(KeyboardInterrupt):
        poll.poll(cfg, db, box)
    assert trades(db) == [] and inbound_rows(db) == {}
    monkeypatch.setattr(ledger, "record_trade", real)
    poll.poll(cfg, db, box)
    poll.poll(cfg, db, box)
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0), ("buy", "5.7x28mm SS190", 150, 5420.0)]
    assert [r["mail_seq"] for r in db.query("SELECT mail_seq FROM trades ORDER BY id")] == [0, 1]


def test_database_error_in_the_middle_is_retried_silently(setup, monkeypatch):
    cfg, db = setup
    real, calls = ledger.record_trade, {"n": 0}

    def locked_on_second(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(ledger, "record_trade", locked_on_second)
    box = Box([mail("买 PS12B 200 3550\n买 SS190 150 5420")])
    res = poll.poll(cfg, db, box)
    assert res["deferred"] == 1 and trades(db) == [] and inbound_rows(db) == {} and box.sent == []
    monkeypatch.setattr(ledger, "record_trade", real)
    poll.poll(cfg, db, box)
    assert len(trades(db)) == 2


def test_database_locked_by_another_process_defers_the_mail(setup):
    cfg, db = setup
    other = sqlite3.connect(db.path, timeout=0.1, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")                                              # 另一个 DFQ 进程正在写
    db.conn.execute("PRAGMA busy_timeout=100")
    box = Box([mail()])
    try:
        res = poll.poll(cfg, db, box)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert res["deferred"] == 1 and trades(db) == [] and inbound_rows(db) == {} and box.sent == [] and res["rejected"] == []
    poll.poll(cfg, db, box)
    assert len(trades(db)) == 1


def test_overlapping_polls_book_once(setup, monkeypatch):
    """旧版本：界面按钮和计划任务同时轮询，两边都“查到没处理过”，各记一遍。"""
    cfg, db = setup
    real_parse, state = reply.parse_reply, {"first": True}

    def parse_while_other_poll_runs(*a, **k):
        if state["first"]:
            state["first"] = False
            other = DB(db.path)                                                   # 另一个进程 = 另一条连接
            try:
                poll.poll(cfg, other, Box([mail()]))
            finally:
                other.close()
        return real_parse(*a, **k)

    monkeypatch.setattr(reply, "parse_reply", parse_while_other_poll_runs)
    poll.poll(cfg, db, Box([mail()]))
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0)]


_CHILD = r"""
import copy, sys
sys.path.insert(0, sys.argv[1])
from dfq import timeutil
from dfq.config import DEFAULTS, Config
from dfq.db import DB
from dfq.mail import poll
from dfq.mail.transport import FileTransport
timeutil.set_now("2026-11-13 21:30:00")
data = copy.deepcopy(DEFAULTS)
data["general"]["data_dir"] = sys.argv[2]
cfg = Config(data, sys.argv[2] + "/config.yaml")
cfg.set("mail.owner_address", "me@example.com")
cfg.set("mail.auth.trusted_authserv", ["mx.example.com"])
db = DB(sys.argv[2] + "/dfq.db")
res = poll.poll(cfg, db, FileTransport(sys.argv[2]))
print(res["real_recorded"], res["deferred"])
"""


def raw_mail(*, mid="<r1@x>", frm=b"me@example.com", body="买 PS12B 200 3550", date=b"Fri, 13 Nov 2026 12:00:00 +0800",
             irt=b"<plan-1@dfq>", ar=None):
    ar = GOOD.encode() if ar is None else ar
    head = (b"Authentication-Results: " + ar + b"\r\nFrom: " + frm + b"\r\nTo: me@example.com\r\nSubject: Re\r\n"
            b"Date: " + date + b"\r\nMessage-ID: " + mid.encode() + b"\r\n")
    if irt:
        head += b"In-Reply-To: " + irt + b"\r\nReferences: " + irt + b"\r\n"
    return head + b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body.encode() + b"\r\n"


def test_concurrent_poll_processes_book_once(cfg, db):
    """四个进程同时读同一个收件箱、写同一个数据库：每封信恰好记一次。"""
    _prepare(cfg, db)
    inbox = cfg.data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.eml").write_bytes(raw_mail(mid="<a@x>"))
    (inbox / "b.eml").write_bytes(raw_mail(mid="<b@x>", body="买 SS190 150 5420"))
    env = {**os.environ, "PYTHONUTF8": "1", "DFQ_DOTENV": "0"}
    procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(REPO), str(cfg.data_dir)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
    assert sum(int(o[0].split()[0]) for o in outs) == 2
    assert sorted(trades(db)) == [("buy", "12.7x55mm PS12B", 200, 3550.0), ("buy", "5.7x28mm SS190", 150, 5420.0)]


def test_one_bad_line_or_oversell_books_nothing(setup):
    """一封信里有一行不合法（这里是卖出超过持仓）：整封都不记，回信逐行说明。"""
    cfg, db = setup
    box = Box([mail("买 PS12B 200 3550\n卖 SS190 150 5420")])                    # 没有 SS190 持仓
    res = poll.poll(cfg, db, box)
    assert trades(db) == [] and inbound_rows(db) == {"<r1@x>": "error"}
    assert len(res["errors"]) == 1 and "SS190" in res["errors"][0]
    assert len(box.sent) == 1 and "一笔都没有记账" in box.sent[0]["body"] and "PS12B ×200" in box.sent[0]["body"]
    poll.poll(cfg, db, box)
    assert len(box.sent) == 1 and trades(db) == []


def test_buy_then_sell_in_the_same_mail_is_fine(setup):
    cfg, db = setup
    poll.poll(cfg, db, Box([mail("买 SS190 150 4600\n卖 SS190 150 5420")]))
    assert trades(db) == [("buy", "5.7x28mm SS190", 150, 4600.0), ("sell", "5.7x28mm SS190", 150, 5420.0)]


@pytest.mark.parametrize("date,expected,noted", [
    (datetime(2026, 11, 13, 12, 0), "2026-11-13 12:00:00", False),                # 计划发出之后、现在之前：用它
    (datetime(2026, 11, 13, 21, 39), "2026-11-13 21:39:00", False),               # 比现在晚不到 10 分钟：时钟误差
    (datetime(2099, 1, 1), "2026-11-13 21:30:00", True),                          # 未来
    (datetime(2026, 11, 12, 8, 0), "2026-11-13 21:30:00", True),                  # 早于计划发出
    (None, "2026-11-13 21:30:00", True),
])
def test_date_header_is_only_trusted_inside_the_plan_window(setup, date, expected, noted):
    cfg, db = setup
    poll.poll(cfg, db, Box([mail(date=date)]))
    row = db.one("SELECT ts, note FROM trades")
    assert row["ts"] == expected and bool(row["note"]) == noted


def test_unverified_and_failed_mails_are_reported_once_and_never_answered(setup):
    cfg, db = setup
    box = Box([mail(mid="<a1@x>", auth_=()), mail(mid="<a2@x>", auth_=("mx.example.com; dmarc=fail",)),
               mail(mid="<a3@x>", auth_=("mx.example.com; dmarc=pass",))])
    res = poll.poll(cfg, db, box)
    assert [r["status"] for r in res["rejected"]] == ["unverified", "auth_fail", "auth_fail"] and box.sent == []
    again = poll.poll(cfg, db, box)
    assert again["rejected"] == [] and box.sent == [] and trades(db) == []


def test_suspicious_from_is_ignored_without_reply(setup):
    cfg, db = setup
    box = Box([mail(mid="<a1@x>", from_problem="From 的显示名里出现了别的地址"), mail(mid="<a2@x>", from_count=2)])
    res = poll.poll(cfg, db, box)
    assert res["ignored"] == 2 and trades(db) == [] and box.sent == []


def test_trades_already_on_the_books_are_not_booked_again(setup):
    """旧版本记过账、却没留下处理记录的信（mail_seq 为空的旧数据）：不再记第二遍。"""
    cfg, db = setup
    db.execute("INSERT INTO trades(ts,side,oid,name,qty,price,source,message_id,paper) "
               "VALUES('2026-11-13 12:00:00','buy',626,'12.7x55mm PS12B',200,3550,'mail','<R1@X>',0)")
    res = poll.poll(cfg, db, Box([mail()]))
    assert len(trades(db)) == 1 and res["duplicates"] == 1 and inbound_rows(db) == {"<r1@x>": "duplicate"}


def test_inbound_mail_cannot_take_over_an_outgoing_id(setup):
    cfg, db = setup
    poll.poll(cfg, db, Box([mail(mid="<PLAN-1@DFQ>")]))
    row = db.one("SELECT direction, status FROM mail_log WHERE message_id='<plan-1@dfq>'")
    assert (row["direction"], row["status"]) == ("out", "sent") and trades(db) == []


def test_malformed_mails_do_not_break_the_batch(setup):
    """旧版本：一封 From 里藏换行、或日期溢出的信让整批轮询抛异常，后面的信都处理不了。"""
    cfg, db = setup
    inbox = cfg.data_dir / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "1.eml").write_bytes(raw_mail(mid="<bad1@x>", frm=b"=?utf-8?q?me@example.com=0D=0AFrom:?= <a@evil.com>"))
    (inbox / "2.eml").write_bytes(raw_mail(mid="<bad2@x>", date=b"Fri, 31 Dec 9999 23:59:59 -1400", irt=b"", body="hi"))
    (inbox / "3.eml").write_bytes(b"\x00\xff\xfe\r\n\r\n\x00")
    (inbox / "4.eml").write_bytes(raw_mail(mid="<good@x>"))
    ft = FileTransport(cfg.data_dir)
    res = poll.poll(cfg, db, ft)
    assert res["checked"] == 4 and trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0)]
    assert inbound_rows(db)["<good@x>"] == "ok"
    again = poll.poll(cfg, db, ft)
    assert again["recorded"] == [] and again["rejected"] == [] and len(trades(db)) == 1


def test_unexpected_error_in_one_mail_is_contained(setup, monkeypatch):
    cfg, db = setup
    real = reply.parse_reply

    def boom_on_first(text, *a, **k):
        if "PS12B" in text:
            raise ValueError("意外")
        return real(text, *a, **k)

    monkeypatch.setattr(reply, "parse_reply", boom_on_first)
    box = Box([mail(mid="<x1@x>"), mail("买 SS190 150 5420", mid="<x2@x>")])
    res = poll.poll(cfg, db, box)
    assert res["malformed"] == 1 and trades(db) == [("buy", "5.7x28mm SS190", 150, 5420.0)]
    assert inbound_rows(db)["<x1@x>"] == "malformed"
    monkeypatch.setattr(reply, "parse_reply", real)
    poll.poll(cfg, db, box)
    assert len(trades(db)) == 1                                                   # 定性过的信不再处理


def test_oversized_mail_is_marked_malformed_without_parsing(setup, monkeypatch):
    cfg, db = setup
    monkeypatch.setattr(transport, "MAX_RAW_BYTES", 2000)
    m = parse_raw(raw_mail(body="买 PS12B 200 3550\n" + "x" * 5000))
    assert m.malformed and "太大" in m.malformed and m.message_id == "<r1@x>"
    res = poll.poll(cfg, db, Box([m]))
    assert res["malformed"] == 1 and trades(db) == [] and inbound_rows(db) == {"<r1@x>": "malformed"}


def test_huge_bodies_are_bounded(setup):
    cfg, db = setup
    t = time.perf_counter()
    res = poll.poll(cfg, db, Box([mail("买 PS12B 200 3550\n" + "闲聊" * 2_000_000, mid="<big@x>"),
                                  mail("\n".join(["买 PS12B 1 3550"] * 60), mid="<many@x>"),
                                  mail("买 PS12B 200 3550 " + "9" * 5000, mid="<long@x>")]))
    assert time.perf_counter() - t < 5
    assert trades(db) == [] and len(res["errors"]) >= 3                          # 每封都有一处超限 → 整封不记


def test_dry_run_writes_nothing_and_sends_nothing(setup):
    cfg, db = setup
    box = Box([mail(), mail(mid="<a1@x>", auth_=()), mail(mid="<a2@x>", reply_to="")])
    poll.poll(cfg, db, box, dry_run=True)
    assert trades(db) == [] and inbound_rows(db) == {} and box.sent == []
