"""读取邮件回复并记账。

安全规则：
1. 只处理发件人地址 == mail.owner_address 的邮件，其他一律忽略；
2. 默认还要求它是对本系统发出的邮件的回复（In-Reply-To / References 能对上），
   这样别人单靠伪造发件人地址也记不进账；
3. 邮件内容只解析成成交记录，不执行任何指令；
4. 每封邮件只处理一次（按 Message-ID 去重）。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import ledger, timeutil
from ..log import event, get_logger
from . import compose, reply
from .transport import MailError, MailTransport

log = get_logger("mail")


def poll(cfg, db, transport: MailTransport, *, dry_run: bool = False) -> dict:
    owner = (cfg.get("mail.owner_address") or "").strip().lower()
    res = {"checked": 0, "ignored": 0, "recorded": [], "errors": [], "failed_replies": 0}
    if not owner and transport.name != "file":
        raise MailError("未配置 mail.owner_address，无法判断哪些回复是你本人发的")

    since = timeutil.now() - timedelta(days=int(cfg.get("mail.poll_lookback_days", 3)))
    mails = transport.fetch(since, owner or None)
    sent_ids = {r["message_id"] for r in db.query("SELECT message_id FROM mail_log WHERE direction='out'")}
    resolver = ledger.ItemResolver(cfg, db)

    def last_price(oid):
        snap = db.latest_snapshot(oid)
        return snap["price"] if snap is not None else None

    for m in mails:
        res["checked"] += 1
        if not m.message_id or db.one("SELECT 1 FROM mail_log WHERE message_id=?", (m.message_id,)):
            continue
        if owner and m.from_addr != owner:
            res["ignored"] += 1
            continue                    # 不是本人：不记录、不回复
        if m.message_id in sent_ids:
            continue                    # 自己发给自己的计划邮件
        if cfg.get("mail.require_thread_match", True) and transport.name != "file":
            refs = {m.in_reply_to, *m.references} - {""}
            if not (refs & sent_ids):
                res["ignored"] += 1
                continue                # 不是对计划邮件的回复
        if m.date and m.date < since:
            continue

        trades, errors = reply.parse_reply(m.text, resolver, last_price)
        ok_lines = []
        for t in trades:
            label = f"{'买' if t['side'] == 'buy' else '卖'} {t['item']['name']} ×{t['qty']} @{t['price']:.0f}"
            if t["note"]:
                label += f"（{t['note']}）"
            if not dry_run:
                ledger.record_trade(db, t["side"], t["item"], t["qty"], t["price"], source="mail",
                                    ts=m.date, message_id=m.message_id, paper=False,
                                    note=t["note"] or None)
            ok_lines.append(label)
        res["recorded"] += ok_lines
        res["errors"] += errors
        status = "ok" if trades and not errors else ("partial" if trades else ("error" if errors else "empty"))
        if not dry_run:
            db.execute("INSERT OR IGNORE INTO mail_log(message_id,direction,subject,ts,status,note) "
                       "VALUES(?,?,?,?,?,?)",
                       (m.message_id, "in", m.subject[:200], timeutil.fmt(timeutil.now()), status,
                        "；".join(ok_lines + errors)[:1000]))
        if errors and not dry_run:
            subj, body = compose.build_failure_reply(ok_lines, errors)
            try:
                mid = transport.send(subj, body, to=owner or "owner@localhost", in_reply_to=m.message_id)
                db.execute("INSERT OR IGNORE INTO mail_log(message_id,direction,subject,ts,status) "
                           "VALUES(?,?,?,?,?)", (mid, "out", subj, timeutil.fmt(timeutil.now()), "sent"))
                res["failed_replies"] += 1
            except MailError as e:
                event(log, logging.WARNING, f"解析失败的回信没发出去: {e}", node="mail_poll")
        elif ok_lines and cfg.get("mail.confirm_success") and not dry_run:
            try:
                mid = transport.send("【记账回执】已记录 %d 笔" % len(ok_lines),
                                     "已记账：\n" + "\n".join(f"· {x}" for x in ok_lines) + "\n",
                                     to=owner or "owner@localhost", in_reply_to=m.message_id)
                db.execute("INSERT OR IGNORE INTO mail_log(message_id,direction,subject,ts,status) "
                           "VALUES(?,?,?,?,?)", (mid, "out", "记账回执", timeutil.fmt(timeutil.now()), "sent"))
            except MailError:
                pass
    event(log, logging.INFO, "邮件回复处理完成", node="mail_poll", checked=res["checked"],
          ignored=res["ignored"], recorded=len(res["recorded"]), errors=len(res["errors"]))
    return res


def recent_receipts(db, hours: int = 30) -> list[str]:
    """最近记到账上的成交（放进当晚的计划邮件里当回执）。"""
    since = timeutil.fmt(timeutil.now() - timedelta(hours=hours))
    out = []
    for r in db.query("SELECT * FROM trades WHERE paper=0 AND ts>=? ORDER BY ts", (since,)):
        s = f"{r['ts'][5:16]} {'买' if r['side'] == 'buy' else '卖'}入账 {r['name']} ×{r['qty']} @{r['price']:.0f}"
        if r["note"]:
            s += f"（{r['note']}）"
        out.append(s)
    return out
