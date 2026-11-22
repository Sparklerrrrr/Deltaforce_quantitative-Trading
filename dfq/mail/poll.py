"""读取邮件回复并记账。

一封来信要连过五关才会被记账，任何一关不过都不记：

  1. 发件人：恰好一个 From 头、里面恰好一个地址，并且等于 mail.owner_address；
     显示名、注释里藏着别的地址的一律不认（见 transport._parse_from）。
  2. 身份验证：你的邮箱服务商确认它确实来自你的邮箱域（SPF / DKIM / DMARC，见 auth.py）。
     From 和 In-Reply-To 都是发信人自己填的，光凭它们不算数。
  3. 对应计划：它回复的是本系统发出的某封计划邮件（靠邮件头里的引用关系，
     而计划邮件的编号含 128 位随机数、只发给过你），并且那封计划发出还不到
     mail.reply_max_age_days 天 —— 很久以前的计划邮件被翻出来重放，不记账。
  4. 账本归属：那份计划当初是“模拟”还是“真实”，从本地数据库里查，
     **不看邮件里写了什么**。模拟计划的回复只进模拟账本，绝不影响真实持仓、余额和绩效。
     查不到归属的（比如回复的是测试邮件）→ 拒绝记账，而不是默认当真实成交。
  5. 整封一起记：一封信里只要有一行不合法（看不懂、数字离谱、卖出超过持仓……），
     整封都不记，并把每一行的问题告诉你 —— 不会出现“记了一半”的账。

不重复记账：
  - Message-ID 统一写法（大小写、空白、注释、尖括号）后去重；
  - “认领这封信”和“写入它的全部成交”在同一个数据库写事务里：中途崩溃、数据库被占用、
    两个轮询同时跑，结果都只有两种 —— 整封记上了且标成已处理，或者什么都没写、下次再试；
  - 同一份计划下、成交行完全相同的另一封回复（哪怕 Message-ID 不同，比如客户端重发）视为重复，不记账并说明原因。

另外：
  - 邮件内容只解析成成交记录，不执行任何指令；
  - 只有身份已经确认的来信才会收到“没记账”的回信，避免被人利用来乱发邮件；
  - 已经定性过的来信（包括验证失败、无法解析的）不再重复处理、重复提示；
  - 一封畸形邮件只会被标成“无法解析”，不会让整批轮询失败。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .. import ledger, timeutil
from ..db import TransactionAborted
from ..log import event, get_logger
from . import auth, compose, reply
from .transport import MailError, MailTransport, norm_msgid

log = get_logger("mail")

DATE_SKEW = timedelta(minutes=10)      # 邮件 Date 头最多允许比现在晚这么多（两边时钟误差）


class _Handled(Exception):
    """这封信已经被别的轮询（或上一次轮询）处理过了。"""


class _Duplicate(Exception):
    """同一份计划下，已经有一封成交行完全相同的回复记过账了。"""


class _Rejected(Exception):
    """有至少一行不合法：整封回滚，一笔都不记。"""


class _AlreadyBooked(Exception):
    """账上已经有带这个邮件编号的成交。"""


def plan_context(db, m) -> dict | None:
    """这封回复对应哪封我们发出的邮件、那份计划是模拟还是真实。对不上返回 None。

    先看 In-Reply-To，再从近到远看 References；只采信本地 mail_log 里的记录。
    只认带尖括号的标准写法（真实邮件里提取出来的都是这样），比较时忽略大小写。
    """
    for ref in [m.in_reply_to, *reversed(m.references or [])]:
        if not isinstance(ref, str) or not (ref.strip().startswith("<") and ref.strip().endswith(">")):
            continue
        key = norm_msgid(ref)
        if not key:
            continue
        row = db.one("SELECT message_id, plan_id, paper, ts FROM mail_log WHERE direction='out' AND lower(message_id)=?",
                     (key,))
        if row is not None:
            return {"ref": row["message_id"], "plan_id": row["plan_id"], "ref_ts": row["ts"],
                    "paper": None if row["paper"] is None else bool(row["paper"])}
    return None


def _plan_sent_at(db, ctx: dict) -> datetime | None:
    """计划邮件最早发出的时间（回复失败说明的回信也带着同一个 plan_id，以最早那封为准）。"""
    ts = None
    if ctx.get("plan_id") is not None:
        row = db.one("SELECT MIN(ts) AS t FROM mail_log WHERE direction='out' AND plan_id=?", (ctx["plan_id"],))
        ts = row["t"] if row is not None else None
    ts = ts or ctx.get("ref_ts")
    try:
        return timeutil.parse_dt(ts) if ts else None
    except (TypeError, ValueError, AttributeError):
        return None


def _log_in(db, mid: str, m, status: str, note: str = "", *, plan_id=None, paper=None) -> None:
    """记下这封来信的处理结果（同一个编号只有一行；发出的邮件那一行绝不会被覆盖）。"""
    row = db.one("SELECT direction FROM mail_log WHERE message_id=?", (mid,))
    if row is not None and row["direction"] != "in":
        return
    db.execute("INSERT OR REPLACE INTO mail_log(message_id,direction,subject,ts,status,plan_id,note,paper) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (mid, "in", (m.subject or "")[:200], timeutil.fmt(timeutil.now()), status, plan_id, (note or "")[:1000],
                None if paper is None else (1 if paper else 0)))


def _signature(trades: list[dict]) -> list[tuple]:
    return [(t["side"], t["item"]["name"], int(t["qty"]), round(float(t["price"]), 4)) for t in trades]


def _find_duplicate(db, plan_id, paper: bool, sig: list[tuple]) -> str | None:
    """同一份计划下，有没有一封已经记过账、成交行完全相同的回复（之后被撤销了的不算）。"""
    if plan_id is None:
        return None
    for r in db.query("SELECT message_id FROM mail_log WHERE direction='in' AND status='ok' AND plan_id=?", (plan_id,)):
        rows = db.query("SELECT side, name, qty, price FROM trades WHERE message_id=? AND paper=? ORDER BY mail_seq, id",
                        (r["message_id"], 1 if paper else 0))
        if rows and [(x["side"], x["name"], int(x["qty"]), round(float(x["price"]), 4)) for x in rows] == sig:
            return r["message_id"]
    return None


def _trade_time(m, ctx: dict, plan_sent: datetime | None) -> tuple[datetime, str]:
    """成交时间：Date 头落在 [计划邮件发出时间, 现在 + 10 分钟] 之内才用它，否则用收到的时间（现在）。

    Date 是发信端自己写的：写成 2099 年或一年前，会把成交排到持仓时间线上错误的位置。
    """
    now = timeutil.now()
    try:
        lo = timeutil.parse_dt(ctx["ref_ts"]) if ctx.get("ref_ts") else plan_sent
    except (TypeError, ValueError, AttributeError):
        lo = plan_sent
    if m.date is None:
        return now, "邮件没有可用的发送时间，按收到的时间记"
    if (lo is None or m.date >= lo) and m.date <= now + DATE_SKEW:
        return m.date, ""
    return now, f"邮件写的发送时间 {timeutil.fmt(m.date)[:16]} 不合理，按收到的时间记"


def poll(cfg, db, transport: MailTransport, *, dry_run: bool = False) -> dict:
    owner = (cfg.get("mail.owner_address") or "").strip().lower()
    res: dict[str, Any] = {"checked": 0, "ignored": 0, "recorded": [], "errors": [], "failed_replies": 0,
           "rejected": [], "paper_recorded": 0, "real_recorded": 0, "malformed": 0, "deferred": 0, "duplicates": 0}
    if not owner:
        raise MailError("未配置 mail.owner_address，无法判断哪些回复是你本人发的")

    since = timeutil.now() - timedelta(days=int(cfg.get("mail.poll_lookback_days", 3)))
    mails = transport.fetch(since, owner)
    sent_ids = {norm_msgid(r["message_id"]) for r in db.query("SELECT message_id FROM mail_log WHERE direction='out'")}
    state = {"cfg": cfg, "db": db, "transport": transport, "owner": owner, "dry_run": dry_run, "res": res,
             "since": since, "sent_ids": sent_ids, "resolver": ledger.ItemResolver(cfg, db),
             "trusted": cfg.get("mail.auth.trusted_authserv") or [],
             "max_age": float(cfg.get("mail.reply_max_age_days", 3) or 3)}

    for m in mails:
        res["checked"] += 1
        try:
            _handle(state, m)
        except (TransactionAborted, sqlite3.OperationalError) as e:
            # 数据库正被别的 DFQ 进程占用等：什么都没写，下次轮询再试；不打扰你
            res["deferred"] += 1
            event(log, logging.INFO, f"这封来信暂时没处理（{type(e).__name__}），下次轮询再试", node="mail_poll")
        except Exception as e:  # noqa: BLE001 —— 任何一封信出问题都不能让整批失败
            res["malformed"] += 1
            event(log, logging.WARNING, f"处理来信时出错，已跳过：{type(e).__name__}", node="mail_poll")
            if not dry_run:
                try:
                    mid = norm_msgid(getattr(m, "message_id", ""))
                    if mid and mid not in sent_ids:
                        _log_in(db, mid, m, "malformed", f"处理时出错：{type(e).__name__}")
                except Exception:  # noqa: BLE001
                    pass

    event(log, logging.INFO, "邮件回复处理完成", node="mail_poll", checked=res["checked"],
          ignored=res["ignored"], rejected=len(res["rejected"]), real=res["real_recorded"],
          paper=res["paper_recorded"], errors=len(res["errors"]), malformed=res["malformed"],
          deferred=res["deferred"])
    return res


def _handle(st: dict, m) -> None:
    db, res, dry_run, owner = st["db"], st["res"], st["dry_run"], st["owner"]
    mid = norm_msgid(getattr(m, "message_id", ""))
    if not mid or mid in st["sent_ids"]:
        return                                      # 没编号的；或自己发给自己的计划邮件
    prev = db.one("SELECT status, direction FROM mail_log WHERE message_id=?", (mid,))
    if prev is not None and (prev["direction"] != "in" or prev["status"] != "unverified"):
        return                                      # 处理过了（含验证失败、无法解析的）：不再处理、不再提示
    recheck = prev is not None                      # 上次“无法验证”：改好配置后可以重来，但不重复提示

    def reject(status: str, reason: str, seen: list[str] | None = None, **kw) -> None:
        res["rejected"].append({"subject": (m.subject or "")[:80], "reason": reason, "status": status,
                                "authserv_seen": seen or []})
        if not dry_run:
            _log_in(db, mid, m, status, reason, **kw)
        event(log, logging.WARNING, f"来信未记账：{reason}", node="mail_poll", status=status)

    if getattr(m, "malformed", ""):
        res["malformed"] += 1
        if not dry_run:
            _log_in(db, mid, m, "malformed", m.malformed)
        event(log, logging.WARNING, f"来信无法解析，已跳过：{m.malformed}", node="mail_poll")
        return
    if m.date and m.date < st["since"]:
        return

    # —— 第 1 关：发件人 ——
    if m.from_addr != owner or m.from_count != 1 or getattr(m, "from_problem", ""):
        res["ignored"] += 1
        return                                      # 不是本人（或 From 有猫腻）：不记录、不回复

    ctx = plan_context(db, m)

    # —— 第 2 关：身份验证 ——
    verdict = auth.verify_sender(m.auth_results, owner, st["trusted"])
    if not verdict.ok:
        if ctx is None or (recheck and verdict.status == auth.MISSING):
            res["ignored"] += 1                     # 与记账无关；或上次已经提示过“无法验证”，不再重复提示
            return
        reject("auth_fail" if verdict.status == auth.FAIL else "unverified",
               f"身份验证未通过：{verdict.detail}", verdict.seen_authserv)
        return                                      # 身份没确认：不记账，也绝不回信

    trades, errors = reply.parse_reply(m.text, st["resolver"], _last_price(db))

    # —— 第 3 关：对应哪份计划 ——
    if ctx is None or ctx["paper"] is None:
        if not trades and not errors:
            return                                  # 你发给自己的其他邮件，和记账无关：别打扰
        why = ("这封邮件不是对计划邮件的回复" if ctx is None else
               "你回复的那封邮件不是交易计划（比如测试邮件）")
        reason = (f"{why}，无法判断这些成交属于模拟还是真实，所以没有记账。"
                  f"请直接回复计划邮件；或用命令行记账：python -m dfq trade buy 品种 数量 价格"
                  f"（模拟成交加 --paper）")
        reject("no_plan", reason)
        if not dry_run:
            _reply(db, st["transport"], owner, m, [], [reason], None)
            res["failed_replies"] += 1
        return

    paper = ctx["paper"]
    plan_sent = _plan_sent_at(db, ctx)
    if trades or errors:
        age = None if plan_sent is None else timeutil.now() - plan_sent
        if age is None or age > timedelta(days=st["max_age"]):
            days = f"{age.total_seconds() / 86400:.1f} 天前" if age is not None else "很久以前"
            reason = (f"你回复的计划邮件是 {days}发出的，超过了 {st['max_age']:g} 天的有效期"
                      f"（mail.reply_max_age_days），为防止旧邮件被重放，这封没有记账。"
                      f"如果这些成交是真的，请在界面里记账，或用命令行：python -m dfq trade buy/sell 品种 数量 价格"
                      + ("（模拟成交加 --paper）" if paper else ""))
            reject("expired", reason, plan_id=ctx["plan_id"], paper=paper)
            if not dry_run:
                _reply(db, st["transport"], owner, m, [], [reason], ctx)
                res["failed_replies"] += 1
            return

    # —— 第 4 关：逐行检查，整封一起记 ——
    ts, ts_note = _trade_time(m, ctx, plan_sent)
    labels: list[str] = []
    bad: set[int] = set()                           # 有问题的成交行（序号），回信时和“本身没问题”的分开列
    for i, t in enumerate(trades):
        label = (f"{'买' if t['side'] == 'buy' else '卖'} {t['item']['name']} ×{t['qty']} @{t['price']:.0f}"
                 + ("【模拟】" if paper else ""))
        notes = [x for x in (t["note"], ts_note) if x]
        t["full_note"] = "；".join(notes)
        if notes:
            label += f"（{t['full_note']}）"
        if not (0 < t["qty"] <= ledger.MAX_QTY and 0 < t["price"] <= ledger.MAX_PRICE):
            errors.append(f"「{t['line']}」数字大得不合理")
            bad.add(i)
        labels.append(label)
    if not trades and not errors:
        if not dry_run:
            _log_in(db, mid, m, "empty", plan_id=ctx["plan_id"], paper=paper)
        return
    if dry_run:
        if not errors:
            res["recorded"] += labels
            res["paper_recorded" if paper else "real_recorded"] += len(labels)
        res["errors"] += errors
        return
    if errors:
        _fail(st, m, mid, ctx, errors, [x for i, x in enumerate(labels) if i not in bad])
        return

    try:
        with db.write_tx():
            row = db.one("SELECT status, direction FROM mail_log WHERE message_id=?", (mid,))
            if row is not None and not (row["direction"] == "in" and row["status"] == "unverified"):
                raise _Handled()                    # 另一个轮询刚刚处理了它
            if db.one("SELECT 1 FROM trades WHERE lower(message_id)=? LIMIT 1", (mid,)) is not None:
                raise _AlreadyBooked()              # 账上已经有这封信的成交（例如旧版本记过、却没留下处理记录）
            dup = _find_duplicate(db, ctx["plan_id"], paper, _signature(trades))
            if dup is not None:
                raise _Duplicate(dup)
            _log_in(db, mid, m, "processing", plan_id=ctx["plan_id"], paper=paper)     # 先占位，再记账
            line_errors: list[str] = []
            for seq, t in enumerate(trades):
                try:
                    ledger.record_trade(db, t["side"], t["item"], t["qty"], t["price"], source="mail",
                                        ts=ts, message_id=mid, paper=paper, note=t["full_note"] or None, mail_seq=seq)
                except ledger.LedgerError as e:
                    line_errors.append(f"「{t['line']}」{e}")
                    bad.add(seq)
            if line_errors:
                errors += line_errors
                raise _Rejected()
            _log_in(db, mid, m, "ok", "；".join(labels), plan_id=ctx["plan_id"], paper=paper)
    except _Handled:
        return
    except _Duplicate as d:
        reason = ("这封回复和之前已经记过账的另一封回复（同一份计划）成交内容完全一样，看起来是重复发送，没有再记一遍。"
                  "如果你确实又做了一模一样的成交，请在界面里记账，或用命令行 python -m dfq trade 记账。")
        res["duplicates"] += 1
        reject("duplicate", reason, plan_id=ctx["plan_id"], paper=paper)
        event(log, logging.INFO, "重复的回复没有再记账", node="mail_poll", same_as=str(d)[:80])
        return
    except _Rejected:
        _fail(st, m, mid, ctx, errors, [x for i, x in enumerate(labels) if i not in bad])
        return
    except (_AlreadyBooked, sqlite3.IntegrityError):
        # 这封信的成交已经在账上（例如旧版本记过但没留下处理记录）：不再记第二遍
        res["duplicates"] += 1
        _log_in(db, mid, m, "duplicate", "账上已经有这封信的成交", plan_id=ctx["plan_id"], paper=paper)
        return
    res["recorded"] += labels
    res["paper_recorded" if paper else "real_recorded"] += len(labels)
    if st["cfg"].get("mail.confirm_success"):
        _reply(db, st["transport"], owner, m, labels, [], ctx)


def _last_price(db):
    def last_price(oid):
        snap = db.latest_snapshot(oid)
        return snap["price"] if snap is not None else None
    return last_price


def _fail(st: dict, m, mid: str, ctx: dict, errors: list[str], fine: list[str]) -> None:
    """整封没有记账：记下原因（以后不再重复处理），回信把每一行的问题说清楚。fine = 本身没问题的那些行。"""
    db, res = st["db"], st["res"]
    res["errors"] += errors
    _log_in(db, mid, m, "error", "；".join(errors), plan_id=ctx["plan_id"], paper=ctx["paper"])
    _reply(db, st["transport"], st["owner"], m, [], errors, ctx, not_booked=fine)
    res["failed_replies"] += 1


def _reply(db, transport, owner: str, m, ok_lines: list[str], errors: list[str], ctx: dict | None,
           not_booked: list[str] | None = None) -> None:
    """给已确认身份的来信回一封说明。回信沿用原计划的归属，这样你再回复它时仍能对上账本。"""
    if errors and ctx is not None:
        subject = "【没有记账】这封回复有问题，请改好后整封重新回复"
        lines = ["这封回复一笔都没有记账：一封回复里只要有一行有问题，整封都不记，以免只记一半。", "", "问题："]
        lines += [f"· {e}" for e in errors]
        if not_booked:
            lines += ["", "下面这些行本身没问题，但因为同一封里有问题，也没有记账（重新回复时请一起写上）："]
            lines += [f"· {x}" for x in not_booked]
        lines += ["", "格式（一行一笔）：方向 品种 数量 成交价      方向写 买 或 卖",
                  "如果是卖出超过持仓：先在界面或命令行里补记之前的买入（或期初库存），再重新回复。"]
        body = "\n".join(lines) + "\n"
    elif errors:
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
