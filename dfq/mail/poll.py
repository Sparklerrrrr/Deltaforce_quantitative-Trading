"""读取邮件回复并记账。

一封来信要连过四关才会被记账，任何一关不过都不记：

  1. 发件人：From 里有且只有一个地址，并且等于 mail.owner_address。
  2. 身份验证：你的邮箱服务商确认它确实来自你的邮箱域（SPF / DKIM / DMARC，见 auth.py）。
     From 和 In-Reply-To 都是发信人自己填的，光凭它们不算数。
  3. 对应计划：它回复的是本系统发出的某封计划邮件（靠邮件头里的引用关系，
     而计划邮件的编号是随机生成、只发给过你的）。
  4. 账本归属：那份计划当初是“模拟”还是“真实”，从本地数据库里查，
     **不看邮件里写了什么**。模拟计划的回复只进模拟账本，绝不影响真实持仓、余额和绩效。
     查不到归属的（比如回复的是测试邮件）→ 拒绝记账，而不是默认当真实成交。

另外：
  - 邮件内容只解析成成交记录，不执行任何指令；
  - 每封邮件只处理一次（按 Message-ID 去重）；
  - 只有身份已经确认的来信才会收到“没看懂/没记账”的回信，避免被人利用来乱发邮件。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import ledger, timeutil
from ..log import event, get_logger
from . import auth, compose, reply
from .transport import MailError, MailTransport

log = get_logger("mail")


def plan_context(db, m) -> dict | None:
    """这封回复对应哪封我们发出的邮件、那份计划是模拟还是真实。对不上返回 None。

    先看 In-Reply-To，再从近到远看 References；只采信本地 mail_log 里的记录。
    """
    for ref in [m.in_reply_to, *reversed(m.references or [])]:
        if not ref:
            continue
        row = db.one("SELECT message_id, plan_id, paper FROM mail_log WHERE message_id=? AND direction='out'",
                     (ref,))
        if row is not None:
            return {"ref": row["message_id"], "plan_id": row["plan_id"],
                    "paper": None if row["paper"] is None else bool(row["paper"])}
    return None


def _log_in(db, m, status: str, note: str = "") -> None:
    db.execute("INSERT OR REPLACE INTO mail_log(message_id,direction,subject,ts,status,note) VALUES(?,?,?,?,?,?)",
               (m.message_id, "in", (m.subject or "")[:200], timeutil.fmt(timeutil.now()), status, note[:1000]))


def poll(cfg, db, transport: MailTransport, *, dry_run: bool = False) -> dict:
    owner = (cfg.get("mail.owner_address") or "").strip().lower()
    res = {"checked": 0, "ignored": 0, "recorded": [], "errors": [], "failed_replies": 0,
           "rejected": [], "paper_recorded": 0, "real_recorded": 0}
    if not owner:
        raise MailError("未配置 mail.owner_address，无法判断哪些回复是你本人发的")
    trusted = cfg.get("mail.auth.trusted_authserv") or []

    since = timeutil.now() - timedelta(days=int(cfg.get("mail.poll_lookback_days", 3)))
    mails = transport.fetch(since, owner)
    sent_ids = {r["message_id"] for r in db.query("SELECT message_id FROM mail_log WHERE direction='out'")}
    resolver = ledger.ItemResolver(cfg, db)

    def last_price(oid):
        snap = db.latest_snapshot(oid)
        return snap["price"] if snap is not None else None

    def reject(m, status: str, reason: str, seen: list[str] | None = None) -> None:
        res["rejected"].append({"subject": (m.subject or "")[:80], "reason": reason, "status": status,
                                "authserv_seen": seen or []})
        if not dry_run:
            _log_in(db, m, status, reason)
        event(log, logging.WARNING, f"来信未记账：{reason}", node="mail_poll", status=status)

    for m in mails:
        res["checked"] += 1
        if not m.message_id or m.message_id in sent_ids:
            continue                                    # 没编号的；或自己发给自己的计划邮件
        prev = db.one("SELECT status FROM mail_log WHERE message_id=?", (m.message_id,))
        if prev is not None and prev["status"] != "unverified":
            continue                                    # 处理过了（“无法验证”的除外：改好配置后可以重来）
        if m.date and m.date < since:
            continue

        # —— 第 1 关：发件人 ——
        if m.from_addr != owner or m.from_count != 1:
            res["ignored"] += 1
            continue                                    # 不是本人（或 From 有猫腻）：不记录、不回复

        ctx = plan_context(db, m)

        # —— 第 2 关：身份验证 ——
        verdict = auth.verify_sender(m.auth_results, owner, trusted)
        if not verdict.ok:
            if ctx is None:
                res["ignored"] += 1                     # 既没验证通过、也不是回复我们的邮件：与记账无关
                continue
            reject(m, "auth_fail" if verdict.status == auth.FAIL else "unverified",
                   f"身份验证未通过：{verdict.detail}", verdict.seen_authserv)
            continue                                    # 身份没确认：不记账，也绝不回信

        trades, errors = reply.parse_reply(m.text, resolver, last_price)

        # —— 第 3、4 关：对应哪份计划、记哪本账 ——
        if ctx is None or ctx["paper"] is None:
            if not trades and not errors:
                continue                                # 你发给自己的其他邮件，和记账无关：别打扰
            why = ("这封邮件不是对计划邮件的回复" if ctx is None else
                   "你回复的那封邮件不是交易计划（比如测试邮件）")
            reason = (f"{why}，无法判断这些成交属于模拟还是真实，所以没有记账。"
                      f"请直接回复计划邮件；或用命令行记账：python -m dfq trade buy 品种 数量 价格"
                      f"（模拟成交加 --paper）")
            reject(m, "no_plan", reason)
            if not dry_run:
                _reply(db, transport, owner, m, [], [reason], None)
                res["failed_replies"] += 1
            continue

        paper = ctx["paper"]
        ok_lines = []
        for t in trades:
            label = (f"{'买' if t['side'] == 'buy' else '卖'} {t['item']['name']} ×{t['qty']} @{t['price']:.0f}"
                     + ("【模拟】" if paper else ""))
            if t["note"]:
                label += f"（{t['note']}）"
            if not dry_run:
                ledger.record_trade(db, t["side"], t["item"], t["qty"], t["price"], source="mail",
                                    ts=m.date, message_id=m.message_id, paper=paper,
                                    note=t["note"] or None)
            ok_lines.append(label)
        res["recorded"] += ok_lines
        res["paper_recorded" if paper else "real_recorded"] += len(ok_lines)
        res["errors"] += errors
        status = "ok" if trades and not errors else ("partial" if trades else ("error" if errors else "empty"))
        if dry_run:
            continue
        _log_in(db, m, status, "；".join(ok_lines + errors))
        if errors:
            _reply(db, transport, owner, m, ok_lines, errors, ctx)
            res["failed_replies"] += 1
        elif ok_lines and cfg.get("mail.confirm_success"):
            _reply(db, transport, owner, m, ok_lines, [], ctx)

    event(log, logging.INFO, "邮件回复处理完成", node="mail_poll", checked=res["checked"],
          ignored=res["ignored"], rejected=len(res["rejected"]), real=res["real_recorded"],
          paper=res["paper_recorded"], errors=len(res["errors"]))
    return res


def _reply(db, transport, owner: str, m, ok_lines: list[str], errors: list[str], ctx: dict | None) -> None:
    """给已确认身份的来信回一封说明。回信沿用原计划的归属，这样你再回复它时仍能对上账本。"""
    if errors:
        subject, body = compose.build_failure_reply(ok_lines, errors)
    else:
        subject = "【记账回执】已记录 %d 笔" % len(ok_lines)
        body = "已记账：\n" + "\n".join(f"· {x}" for x in ok_lines) + "\n"
    try:
        mid = transport.send(subject, body, to=owner, in_reply_to=m.message_id)
    except MailError as e:
        event(log, logging.WARNING, f"回信没发出去: {e}", node="mail_poll")
        return
    paper = None if ctx is None or ctx["paper"] is None else (1 if ctx["paper"] else 0)
    db.execute("INSERT OR IGNORE INTO mail_log(message_id,direction,subject,ts,status,plan_id,paper) "
               "VALUES(?,?,?,?,?,?,?)",
               (mid, "out", subject, timeutil.fmt(timeutil.now()), "sent",
                ctx["plan_id"] if ctx else None, paper))


def recent_receipts(db, hours: int = 30) -> list[str]:
    """最近记到账上的成交（放进当晚的计划邮件里当回执）。两本账都列出，模拟的会标明。"""
    since = timeutil.fmt(timeutil.now() - timedelta(hours=hours))
    out = []
    for r in db.query("SELECT * FROM trades WHERE ts>=? ORDER BY ts", (since,)):
        s = (f"{r['ts'][5:16]} {'买' if r['side'] == 'buy' else '卖'}入账 {r['name']} ×{r['qty']} @{r['price']:.0f}"
             + ("【模拟账本】" if r["paper"] else ""))
        if r["note"]:
            s += f"（{r['note']}）"
        out.append(s)
    return out


def describe_rejections(rejected: list[dict]) -> list[str]:
    """把被拒绝的来信整理成给人看的几行（命令行和当晚邮件共用）。"""
    lines = []
    for r in rejected:
        line = f"《{r['subject']}》{r['reason']}"
        if r["status"] == "unverified" and r.get("authserv_seen"):
            line += (f"　〔邮件里出现的验证标识：{'、'.join(r['authserv_seen'])}。"
                     f"确认哪个是你邮箱服务商的之后，填进 config.yaml 的 mail.auth.trusted_authserv〕")
        lines.append(line)
    return lines
