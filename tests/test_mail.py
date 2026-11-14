import email
import email.policy
from datetime import datetime
from email.message import EmailMessage

import pytest

from dfq import ledger, timeutil
from dfq.mail import compose, poll
from dfq.mail.transport import FileTransport, InboundMail, MailTransport, extract_text, html_to_text, to_inbound

OWNER = "me@example.com"


class FakeTransport(MailTransport):
    name = "fake"

    def __init__(self, inbox=None):
        self.inbox, self.sent = inbox or [], []

    def send(self, subject, body, *, to, in_reply_to=None, headers=None):
        mid = f"<out-{len(self.sent)}@dfq>"
        self.sent.append({"subject": subject, "body": body, "to": to, "in_reply_to": in_reply_to, "id": mid})
        return mid

    def fetch(self, since, from_addr=None):
        return list(self.inbox)


def inbound(text, *, mid="<r1@x>", frm=OWNER, reply_to="<plan-1@dfq>", date=datetime(2026, 11, 13, 12, 0)):
    return InboundMail(message_id=mid, from_addr=frm, subject="Re: 计划", date=date, text=text,
                       in_reply_to=reply_to, references=[reply_to] if reply_to else [])


@pytest.fixture
def setup(cfg, db):
    cfg.set("mail.owner_address", OWNER)
    for oid, name in ((626, "12.7x55mm PS12B"), (640, "5.7x28mm SS190")):
        db.upsert_item(oid, name, 5, 0)
    db.execute("INSERT INTO mail_log(message_id,direction,subject,ts,status) VALUES('<plan-1@dfq>','out','计划',?, 'sent')",
               (timeutil.fmt(timeutil.now()),))
    return cfg, db


def trades(db):
    return [(r["side"], r["name"], r["qty"], r["price"], r["source"]) for r in db.query("SELECT * FROM trades ORDER BY id")]


def test_owner_reply_is_recorded(setup):
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550\n卖 SS190 150 5420")])
    res = poll.poll(cfg, db, t)
    assert trades(db) == [("buy", "12.7x55mm PS12B", 200, 3550.0, "mail"), ("sell", "5.7x28mm SS190", 150, 5420.0, "mail")]
    assert len(res["recorded"]) == 2 and not res["errors"] and t.sent == []     # 成功默认不回信，避免轰炸
    assert db.one("SELECT ts FROM trades")["ts"] == "2026-11-13 12:00:00"        # 用邮件时间作成交时间


def test_other_senders_are_ignored_completely(setup):
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550", frm="attacker@evil.com"),
                       inbound("买 PS12B 200 3550", mid="<r2@x>", frm="me@example.com.evil.com")])
    res = poll.poll(cfg, db, t)
    assert trades(db) == [] and res["ignored"] == 2 and t.sent == []            # 不记账，也不回信


def test_spoofed_sender_without_thread_reference_is_ignored(setup):
    """只伪造发件人地址还不够：必须是对本系统发出的邮件的回复。"""
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550", reply_to=""),
                       inbound("买 PS12B 200 3550", mid="<r2@x>", reply_to="<not-ours@x>")])
    assert poll.poll(cfg, db, t)["ignored"] == 2 and trades(db) == []
    cfg.set("mail.require_thread_match", False)                                  # 可以关掉，但默认开
    poll.poll(cfg, db, t)
    assert len(trades(db)) == 2


def test_same_mail_is_processed_once(setup):
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550")])
    poll.poll(cfg, db, t)
    poll.poll(cfg, db, t)
    assert len(trades(db)) == 1


def test_parse_failure_gets_one_explaining_reply(setup):
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550\n卖 不存在 10 100")])
    res = poll.poll(cfg, db, t)
    assert len(trades(db)) == 1 and len(res["errors"]) == 1 and len(t.sent) == 1
    m = t.sent[0]
    assert m["to"] == OWNER and m["in_reply_to"] == "<r1@x>" and "没看懂" in m["body"] and "不存在" in m["body"]
    assert "已记账" in m["body"]
    poll.poll(cfg, db, t)
    assert len(t.sent) == 1                                                      # 不重复回信


def test_no_reply_means_nothing_executed(setup):
    cfg, db = setup
    assert poll.poll(cfg, db, FakeTransport([]))["recorded"] == [] and trades(db) == []


def test_dry_run_records_nothing(setup):
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550\n卖 不存在 1 1")])
    res = poll.poll(cfg, db, t, dry_run=True)
    assert len(res["recorded"]) == 1 and trades(db) == [] and t.sent == []
    assert db.one("SELECT COUNT(*) AS c FROM mail_log WHERE direction='in'")["c"] == 0


def test_own_plan_mail_in_inbox_is_not_parsed(setup):
    cfg, db = setup
    t = FakeTransport([inbound("买 PS12B 200 3550", mid="<plan-1@dfq>")])        # 自己发给自己的那封
    poll.poll(cfg, db, t)
    assert trades(db) == []


PLAN = {
    "plan_id": "20261113-2130", "paper": False, "valid_from": "2026-11-13 21:30:00",
    "valid_until": "2026-11-14 21:00:00", "market_view": None,
    "items": [
        {"oid": 626, "name": "12.7x55mm PS12B", "search": "12.7x55mm PS12B", "action": "buy", "buy_price": 3600,
         "sell_price": 4400, "stop_price": 3240, "qty": 200, "budget": 720000, "breakeven": 4138,
         "exp_net_return": 0.063, "stop_net_return": -0.217, "reason": "低分位", "risk": "波动", "guard_notes": [],
         "current": 3900, "low_hours": [3, 4]},
        {"oid": 635, "name": "5.56x45mm M995", "action": "watch", "buy_price": 5000, "sell_price": 5500,
         "reason": "空间不足", "guard_notes": []},
    ],
    "sells": [{"oid": 640, "name": "5.7x28mm SS190", "qty": 150, "avg_cost": 4600, "breakeven": 5288,
               "sell_price": 5400, "stop_price": 4140, "note": ""}],
    "warnings": ["测试提示"], "guard_notes": [], "short_term": [], "long_term": [],
}


def test_subject_is_the_instruction(cfg):
    s = compose.build_subject(cfg, PLAN)
    assert s == "【计划】买 PS12B ≤3600 ×200｜卖 SS190 ≥5400｜有效至 11-14 21:00"
    assert compose.build_subject(cfg, {**PLAN, "paper": True}).startswith("【模拟计划】")
    empty = {**PLAN, "items": [PLAN["items"][1]], "sells": []}
    assert "今日无操作" in compose.build_subject(cfg, empty)


def test_subject_stays_short_with_many_items(cfg):
    many = {**PLAN, "items": [dict(PLAN["items"][0]) for _ in range(6)]}
    s = compose.build_subject(cfg, many)
    assert s.count("买 ") == 3 and "另 3 条见正文" in s and len(s) < 110


def test_body_follows_game_operation_order(cfg):
    alerts = [{"name": "12.7x55mm PS12B", "low": 3600, "high": 4400, "low_role": "买入线", "high_role": "卖出线"}]
    b = compose.build_body(cfg, PLAN, manual_alerts=alerts, alert_status="自动同步未开启。")
    assert b.index("先卖") < b.index("再买") < b.index("观望") < b.index("moligod") < b.index("回复本邮件")
    assert "搜索「12.7x55mm PS12B」 买入价 ≤3600  数量 200" in b
    assert "回本价 4138" in b and "实亏约 -22%" in b and "低于(≤) 3600（买入线）" in b
    assert "测试提示" in b


def test_plan_email_body_never_parses_as_trades(setup):
    """最坏情况：邮件客户端把整封计划原样贴进回复且不加引用标记。"""
    cfg, db = setup
    alerts = [{"name": "12.7x55mm PS12B", "low": 3600, "high": 4400, "low_role": "买入线", "high_role": "卖出线"}]
    body = compose.build_subject(cfg, PLAN) + "\n" + compose.build_body(cfg, PLAN, manual_alerts=alerts)
    t = FakeTransport([inbound(body)])
    res = poll.poll(cfg, db, t)
    assert trades(db) == [] and res["errors"] == []


def test_file_transport_roundtrip(tmp_path):
    ft = FileTransport(tmp_path)
    mid = ft.send("【计划】买 PS12B ≤3600 ×200", "正文：中文 ≤ ≥", to=OWNER)
    eml = list((tmp_path / "outbox").glob("*.eml"))
    assert len(eml) == 1 and mid.startswith("<")
    msg = email.message_from_bytes(eml[0].read_bytes(), policy=email.policy.default)
    assert msg["Subject"] == "【计划】买 PS12B ≤3600 ×200" and "中文 ≤ ≥" in msg.get_content()
    assert "≤3600" in (tmp_path / "outbox" / "latest.txt").read_text(encoding="utf-8")


def test_inbound_parsing_html_only_and_timezone():
    m = EmailMessage()
    m["From"] = "我 <ME@Example.com>"; m["Subject"] = "Re: 计划"; m["Message-ID"] = "<a@b>"
    m["Date"] = "Fri, 13 Nov 2026 04:00:00 +0000"; m["References"] = "<p1@dfq> <p2@dfq>"
    m.set_content("<div>买 PS12B 200 3550</div><div>卖 SS190 150 5420<br></div>"
                  "<blockquote>买 M995 999 9999</blockquote>", subtype="html")
    im = to_inbound(email.message_from_bytes(m.as_bytes(), policy=email.policy.default))
    assert im.from_addr == "me@example.com" and im.references == ["<p1@dfq>", "<p2@dfq>"]
    assert im.date == datetime(2026, 11, 13, 12, 0)                              # 转成北京时间
    assert "买 PS12B 200 3550" in im.text and "999" not in im.text               # 引用块被丢弃


def test_html_to_text_strips_scripts():
    assert "alert" not in html_to_text("<p>你好</p><script>alert(1)</script><style>p{}</style>")
    assert extract_text(EmailMessage()) == ""


def test_ledger_positions_and_balance(cfg, db):
    item = {"name": "12.7x55mm PS12B", "oid": 626}
    ledger.set_balance(db, 1_000_000)
    timeutil.set_now("2026-11-13 22:00:00")
    ledger.record_trade(db, "buy", item, 100, 3000)
    ledger.record_trade(db, "buy", item, 100, 4000)
    p = ledger.positions(db)["12.7x55mm PS12B"]
    assert p["qty"] == 200 and p["avg_cost"] == 3500                             # 移动加权平均
    ledger.record_trade(db, "sell", item, 150, 5000)
    p = ledger.positions(db)["12.7x55mm PS12B"]
    assert p["qty"] == 50 and p["realized"] == pytest.approx((5000 * 0.87 - 3500) * 150)
    assert ledger.balance(db)["amount"] == pytest.approx(1_000_000 - 700_000 + 150 * 5000 * 0.87)
    ledger.record_trade(db, "buy", item, 999, 1, paper=True)                     # 模拟成交不计入持仓
    assert ledger.positions(db)["12.7x55mm PS12B"]["qty"] == 50
    with pytest.raises(ledger.LedgerError):
        ledger.record_trade(db, "buy", item, 0, 100)
