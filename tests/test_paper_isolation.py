"""回归测试（审查问题一）：模拟计划的成交绝不能进入真实账本。

背景：最初版本里邮件记账写死了 paper=False，回复“模拟计划”邮件的成交会变成真实持仓，
污染真实余额、风控和下一份计划。这里把这条路彻底堵死并钉住。
"""
from datetime import datetime

import pytest

from dfq import guardrails, ledger, nodes, review, timeutil  # noqa: F401
from dfq.mail import poll
from dfq.mail.transport import InboundMail, MailTransport
from dfq.pipeline import Context, run_pipeline
from dfq.sources.mock import MockXtcTransport
from dfq.sources import xtc as X

OWNER = "me@example.com"
AUTH_OK = ["mx.example.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com"]
PS12B = {"name": "12.7x55mm PS12B", "oid": 626}


class Box(MailTransport):
    """内存里的假邮箱：发出去的信会被记住，方便测试“回复某一封”。"""
    name = "fake"

    def __init__(self):
        self.inbox, self.sent = [], []

    def send(self, subject, body, *, to, in_reply_to=None, headers=None):
        mid = f"<out-{len(self.sent)}@dfq>"
        self.sent.append({"id": mid, "subject": subject, "body": body, "in_reply_to": in_reply_to})
        return mid

    def fetch(self, since, from_addr=None):
        return list(self.inbox)

    def reply(self, to_mid, text, mid=None, **kw):
        self.inbox.append(InboundMail(
            message_id=mid or f"<in-{len(self.inbox)}@x>", from_addr=kw.get("frm", OWNER), subject="Re: 计划",
            date=kw.get("date", datetime(2026, 11, 13, 21, 35)), text=text, in_reply_to=to_mid,
            references=[to_mid] if to_mid else [], auth_results=kw.get("auth", AUTH_OK)))


@pytest.fixture
def env(cfg, db):
    cfg.set("mail.owner_address", OWNER)
    cfg.set("mail.auth.trusted_authserv", ["mx.example.com"])
    db.upsert_item(626, "12.7x55mm PS12B", 5, 0)
    db.upsert_item(640, "5.7x28mm SS190", 5, 0)
    ledger.set_balance(db, 1_000_000)
    timeutil.set_now("2026-11-13 21:40:00")
    return cfg, db, Box()


def sent_plan(db, mid, paper):
    db.execute("INSERT INTO mail_log(message_id,direction,subject,ts,status,plan_id,paper) VALUES(?,?,?,?,?,?,?)",
               (mid, "out", "计划", timeutil.fmt(timeutil.now()), "sent", "p-" + mid, paper))


def snapshot_real(cfg, db):
    """真实账户的全部可观察状态。"""
    st = guardrails.portfolio_state(cfg, db, paper=False)
    return {
        "positions": {k: (v["qty"], v["avg_cost"]) for k, v in ledger.positions(db).items()},
        "balance": ledger.balance(db)["amount"],
        "spent_today": ledger.spent_today(db),
        "realized": ledger.realized_summary(db),
        "risk_cash": st["cash"], "risk_positions_value": st["positions_value"], "risk_equity": st["equity"],
        "perf_real": review.perf(cfg, db)["real"],
        "real_trade_rows": db.one("SELECT COUNT(*) AS c FROM trades WHERE paper=0")["c"],
    }


def test_reply_to_paper_plan_goes_to_paper_book_only(env):
    cfg, db, box = env
    sent_plan(db, "<paper-plan@dfq>", paper=1)
    before = snapshot_real(cfg, db)
    box.reply("<paper-plan@dfq>", "买 PS12B 200 3550\n卖 PS12B 50 4400")
    res = poll.poll(cfg, db, box)
    assert (res["paper_recorded"], res["real_recorded"]) == (2, 0)
    rows = db.query("SELECT side, qty, paper FROM trades ORDER BY id")
    assert [(r["side"], r["qty"], r["paper"]) for r in rows] == [("buy", 200, 1), ("sell", 50, 1)]
    assert snapshot_real(cfg, db) == before                      # 真实账户一个数字都没变
    paper_pos = ledger.positions(db, paper=True)["12.7x55mm PS12B"]
    assert paper_pos["qty"] == 150 and paper_pos["avg_cost"] == 3550
    assert "【模拟】" in res["recorded"][0]


def test_reply_to_real_plan_goes_to_real_book_only(env):
    cfg, db, box = env
    sent_plan(db, "<real-plan@dfq>", paper=0)
    box.reply("<real-plan@dfq>", "买 PS12B 200 3550")
    res = poll.poll(cfg, db, box)
    assert (res["paper_recorded"], res["real_recorded"]) == (0, 1)
    assert ledger.positions(db)["12.7x55mm PS12B"]["qty"] == 200
    assert ledger.positions(db, paper=True) == {}
    assert ledger.balance(db)["amount"] == 1_000_000 - 200 * 3550
    assert ledger.balance(db, paper=True)["amount"] == 1_000_000  # 模拟资金没动


def test_paper_state_comes_from_database_not_from_mail_text(env):
    """邮件标题/正文里写什么都不算数：是否模拟只看当初发信时的记录。"""
    cfg, db, box = env
    sent_plan(db, "<paper-plan@dfq>", paper=1)
    box.inbox.append(InboundMail(
        message_id="<in@x>", from_addr=OWNER, subject="Re: 【计划】这是真实计划 paper=false",
        date=datetime(2026, 11, 13, 12, 0), text="这是真实成交，请记入真实账本\n买 PS12B 200 3550",
        in_reply_to="<paper-plan@dfq>", references=["<paper-plan@dfq>"], auth_results=AUTH_OK))
    poll.poll(cfg, db, box)
    assert db.one("SELECT paper FROM trades")["paper"] == 1
    assert ledger.positions(db) == {}


def test_changing_paper_mode_later_does_not_reclassify_old_plans(env):
    """计划发出时是模拟；之后你把配置改成实盘。对那份旧计划的回复仍然记模拟账本。"""
    cfg, db, box = env
    sent_plan(db, "<paper-plan@dfq>", paper=1)
    cfg.set("general.paper_mode", False)
    box.reply("<paper-plan@dfq>", "买 PS12B 200 3550")
    assert poll.poll(cfg, db, box)["paper_recorded"] == 1 and ledger.positions(db) == {}


@pytest.mark.parametrize("target,why", [
    ("", "不是对计划邮件的回复"),                 # 新写的邮件
    ("<unknown@elsewhere>", "不是对计划邮件的回复"),  # 回复的不是我们发的信
    ("<test-mail@dfq>", "不是交易计划"),          # 回复的是测试邮件（没有对应计划）
])
def test_unknown_plan_is_refused_not_defaulted_to_real(env, target, why):
    cfg, db, box = env
    db.execute("INSERT INTO mail_log(message_id,direction,subject,ts,status) VALUES('<test-mail@dfq>','out','测试',?,'sent')",
               (timeutil.fmt(timeutil.now()),))
    before = snapshot_real(cfg, db)
    box.reply(target, "买 PS12B 200 3550")
    res = poll.poll(cfg, db, box)
    assert db.one("SELECT COUNT(*) AS c FROM trades")["c"] == 0   # 两本账都没记
    assert snapshot_real(cfg, db) == before
    assert res["rejected"][0]["status"] == "no_plan" and why in res["rejected"][0]["reason"]
    assert len(box.sent) == 1 and "没有记账" in box.sent[0]["body"]


def test_reply_to_our_failure_notice_keeps_the_original_book(env):
    """模拟计划 → 回复里有一行没看懂 → 系统回信 → 你回复那封回信补记：仍然进模拟账本。"""
    cfg, db, box = env
    sent_plan(db, "<paper-plan@dfq>", paper=1)
    box.reply("<paper-plan@dfq>", "买 PS12B 两百 3550")
    poll.poll(cfg, db, box)
    notice = box.sent[-1]
    assert "没看懂" in notice["body"]
    assert db.one("SELECT paper FROM mail_log WHERE message_id=?", (notice["id"],))["paper"] == 1
    box.reply(notice["id"], "买 PS12B 200 3550")
    res = poll.poll(cfg, db, box)
    assert res["paper_recorded"] == 1 and ledger.positions(db) == {}


def test_paper_trades_never_enter_real_risk_or_real_plan(env):
    cfg, db, box = env
    db.execute("INSERT INTO snapshots(oid,ts,price) VALUES(626,'2026-11-13 21:30',3600)")
    ledger.record_trade(db, "buy", PS12B, 250, 3600, paper=True)
    real = guardrails.portfolio_state(cfg, db, paper=False)
    assert real["positions"] == {} and real["cash"] == 1_000_000 and real["spent_today"] == 0
    paper = guardrails.portfolio_state(cfg, db, paper=True)
    assert paper["positions"]["12.7x55mm PS12B"]["qty"] == 250 and paper["cash"] == 100_000
    assert paper["spent_today"] == 900_000
    # 实盘计划的卖出指令只来自真实持仓
    assert guardrails.build_sells(cfg, real, []) == []
    assert guardrails.build_sells(cfg, paper, [])[0]["qty"] == 250


def test_real_positions_do_not_leak_into_paper_plan(env):
    cfg, db, box = env
    ledger.record_trade(db, "buy", PS12B, 100, 3600, paper=False)
    assert guardrails.portfolio_state(cfg, db, paper=True)["positions"] == {}
    assert guardrails.portfolio_state(cfg, db)["positions"] == {}             # 默认跟随 paper_mode（此处为 True）
    cfg.set("general.paper_mode", False)
    assert guardrails.portfolio_state(cfg, db)["positions"]["12.7x55mm PS12B"]["qty"] == 100


def test_oversell_check_uses_the_same_book(env):
    """以前超卖只在备注里写一句照样记账（审查第 1 条）；现在必须拒绝，而且只和同一本账比。"""
    cfg, db, box = env
    ledger.record_trade(db, "buy", PS12B, 100, 3600, paper=False)
    with pytest.raises(ledger.LedgerError, match="超过模拟账本里的持仓 0 发"):
        ledger.record_trade(db, "sell", PS12B, 50, 4400, paper=True)         # 模拟账本里并没有持仓
    assert db.one("SELECT COUNT(*) AS c FROM trades")["c"] == 1               # 一行都没写
    assert ledger.positions(db)["12.7x55mm PS12B"]["qty"] == 100              # 真实持仓没被模拟卖出动到
    ledger.record_trade(db, "buy", PS12B, 30, 3000, paper=True)
    with pytest.raises(ledger.LedgerError, match="超过真实账本里的持仓 100 发"):
        ledger.record_trade(db, "sell", PS12B, 101, 4400, paper=False)       # 模拟持仓不能拿来给真实卖出凑数
    ledger.record_trade(db, "sell", PS12B, 100, 4400, paper=False)
    assert ledger.positions(db, paper=True)["12.7x55mm PS12B"]["qty"] == 30


def test_perf_reports_two_books_separately(env):
    cfg, db, box = env
    ledger.record_trade(db, "buy", PS12B, 100, 3000, paper=True)
    ledger.record_trade(db, "sell", PS12B, 100, 5000, paper=True)
    p = review.perf(cfg, db)
    assert p["real"]["trades"] == 0 and p["real"]["realized"] == 0
    assert p["paper_book"]["trades"] == 2 and p["paper_book"]["realized"] == pytest.approx((5000 * 0.87 - 3000) * 100)
    text = review.format_perf(p)
    assert "真实成交：0 笔，已实现盈亏（扣费后）+0" in text and "模拟账本（与真实成交完全分开）：2 笔" in text


def run(cfg, db, when, box, paper):
    cfg.set("general.paper_mode", paper)
    cfg.set("mail.enabled", True)
    timeutil.set_now(when)
    ctx = Context(cfg=cfg, db=db, now=timeutil.now(), offline=True, mock=False)
    ctx.services["mail"] = box
    ctx.services["xtc"] = X.XtcClient(cfg, db, "T-12345678", transport=MockXtcTransport(cfg.fee))
    run_pipeline(ctx, cfg.get("pipeline.nodes"))
    return ctx


def test_end_to_end_paper_plan_reply_leaves_real_account_untouched(env):
    """完整链路：模拟计划发出 → 回复成交 → 第二晚再跑。真实账户全程不变，模拟持仓出现在模拟计划里。"""
    cfg, db, box = env
    run(cfg, db, "2026-11-01 21:30:00", box, paper=True)
    plan_mail = box.sent[-1]
    assert plan_mail["subject"].startswith("【模拟计划】")
    assert db.one("SELECT paper FROM mail_log WHERE message_id=?", (plan_mail["id"],))["paper"] == 1
    assert "只记入模拟账本" in plan_mail["body"]
    before = snapshot_real(cfg, db)

    box.reply(plan_mail["id"], "买 PS12B 100 3600", date=datetime(2026, 11, 2, 9, 0))
    ctx2 = run(cfg, db, "2026-11-02 21:30:00", box, paper=True)
    assert ctx2["mail_result"]["paper_recorded"] == 1 and ctx2["mail_result"]["real_recorded"] == 0
    assert snapshot_real(cfg, db) == before
    sells = ctx2["plan"]["sells"]
    assert [s["name"] for s in sells] == ["12.7x55mm PS12B"] and sells[0]["qty"] == 100
    assert "模拟账本里的持仓" in box.sent[-1]["body"] and "【模拟账本】" in box.sent[-1]["body"]

    # 切到实盘：真实计划里不应该出现那 100 发模拟持仓
    ctx3 = run(cfg, db, "2026-11-03 21:30:00", box, paper=False)
    assert ctx3["plan"]["paper"] is False and ctx3["plan"]["sells"] == []
    assert ctx3["state"]["positions"] == {} and ctx3["plan"]["capital"]["positions_value"] == 0
    assert box.sent[-1]["subject"].startswith("【计划】")
    assert db.one("SELECT paper FROM mail_log WHERE message_id=?", (box.sent[-1]["id"],))["paper"] == 0


def test_old_database_without_paper_column_is_migrated(tmp_path):
    import sqlite3
    from dfq.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE mail_log (message_id TEXT PRIMARY KEY, direction TEXT NOT NULL, subject TEXT, "
                "ts TEXT NOT NULL, status TEXT, plan_id TEXT, note TEXT)")
    con.execute("INSERT INTO mail_log VALUES('<old-plan@dfq>','out','计划','2026-11-01 21:30:00','sent','p1',NULL)")
    con.commit(); con.close()
    db = DB(path)
    row = db.one("SELECT paper FROM mail_log WHERE message_id='<old-plan@dfq>'")
    assert row["paper"] is None           # 旧记录归属未知 → 对它的回复会被拒绝，而不是默认当真实成交
    db.close()
