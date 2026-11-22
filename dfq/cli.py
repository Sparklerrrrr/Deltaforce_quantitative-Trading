"""命令行入口：python -m dfq <子命令>"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__, timeutil
from .config import Config, load_config, secret
from .db import DB
from .log import SECRET_ENV_NAMES, get_logger, setup_logging

log = get_logger("cli")


def _open(args) -> tuple[Config, DB]:
    cfg = load_config(args.config)
    if getattr(args, "mock", False):
        cfg.set("general.data_dir", str(cfg.data_dir / "mock"))
        cfg.set("general.paper_mode", True)
        cfg.set("moligod.enabled", False)
    if getattr(args, "now", None):
        timeutil.set_now(args.now)
    setup_logging(cfg.data_dir / "logs", verbose=args.verbose)
    return cfg, DB(cfg.data_dir / "dfq.db")


def _print(*lines: str) -> None:
    for line in lines:
        print(line)


# ------------------------------------------------------------------ run
def cmd_run(args) -> int:
    from . import nodes  # noqa: F401  注册节点
    from .pipeline import Context, overall_status, run_pipeline, summarize

    cfg, db = _open(args)
    now = timeutil.now()
    every = int(cfg.get("general.run_every_days", 1))
    last = db.kv_get("last_run_at")
    if last and not (args.force or args.dry_run):
        gap = now - timeutil.parse_dt(last)
        if gap < timedelta(days=every) - timedelta(hours=3):
            _print(f"上次运行在 {last}，按 run_every_days={every} 还没到下一次。需要立刻运行请加 --force。")
            return 0

    mode = "mock" if args.mock else ("dry-run" if args.dry_run else ("offline" if args.offline else "normal"))
    run_id = db.execute("INSERT INTO runs(started_at,mode,status) VALUES(?,?,?)",
                        (timeutil.fmt(now), mode, "running")).lastrowid
    ctx = Context(cfg=cfg, db=db, now=now, run_id=run_id, offline=args.offline,
                  dry_run=args.dry_run, mock=args.mock)
    for note in cfg.notes:
        ctx.warn(f"配置提示：{note}")
    results = run_pipeline(ctx, cfg.get("pipeline.nodes"))
    status = overall_status(results)
    cost = ctx.services["xtc"].cost_this_session if "xtc" in ctx.services else 0
    db.execute("UPDATE runs SET finished_at=?, status=?, api_cost=?, summary=? WHERE id=?",
               (timeutil.fmt(timeutil.now()), status, cost,
                json.dumps({"nodes": [r.__dict__ for r in results], "warnings": ctx.warnings},
                           ensure_ascii=False, default=str), run_id))
    if not args.dry_run and ctx.get("plan") is not None:
        db.kv_set("last_run_at", timeutil.fmt(now))

    _print("", "流水线：" + summarize(results))
    _print(f"小涛查接口：本次 {cost} 次，今日累计 {db.usage_on('xtc')}/{cfg.get('xtc.daily_limit')}")
    note = ctx.get("notified")
    if note:
        _print("", "邮件标题：" + note["subject"], f"发送方式：{note['via']}")
        if note["via"].startswith("file"):
            _print(f"正文已写入：{cfg.data_dir / 'outbox' / 'latest.txt'}")
    if ctx.get("plan_path"):
        _print(f"计划文件：{ctx['plan_path']}")
    if ctx.warnings:
        _print("", "提示：")
        _print(*[f"  · {w}" for w in ctx.warnings])
    if args.show and note:
        _print("", "=" * 60, note["subject"], "=" * 60, note["body"])
    return 0 if status != "failed" else 1


# ------------------------------------------------------------------ probe / status
def cmd_probe(args) -> int:
    from . import probe
    from .sources.mock import MockXtcTransport
    cfg, db = _open(args)
    transport = MockXtcTransport(cfg.fee) if args.mock else None
    out_dir = cfg.data_dir / ("probe" if args.mock else "reports")   # 运行产物放 data/，不写进源码的 docs/
    res = probe.run(cfg, db, secret("DFQ_XTC_TOKEN"), out_dir, transport=transport)
    if not res["ok"]:
        _print(*res["lines"])
        return 2
    _print(f"探测完成，消耗 {res['used']} 次。报告：{res['report_path']}")
    _print(*[ln for ln in res["lines"] if ln.startswith("- **") or ln.startswith("- ❌") or ln.startswith("- ⚠")])
    return 0


def cmd_status(args) -> int:
    cfg, db = _open(args)
    from . import funds, ledger
    _print(f"dfq {__version__}　配置：{cfg.path if cfg.path and cfg.path.exists() else '（未找到 config.yaml，使用默认值）'}",
           f"数据目录：{cfg.data_dir}",
           f"模式：{'模拟（paper_mode）' if cfg.paper else '实盘记账'}　执行器：{cfg.get('executor.mode')}", "")
    if cfg.notes:
        _print("配置提示：", *[f"  · {n}" for n in cfg.notes], "")
    from . import envfile
    env_path = envfile.path_for_config(args.config)
    _print(f"密钥（只显示是否已设置，不显示内容）：来自系统环境变量或 {env_path}"
           f"{'' if env_path.exists() else '（文件还不存在）'}")
    used_by = {"DFQ_XTC_TOKEN": "小涛查行情", "DFQ_LLM_API_KEY": "大模型", "DFQ_SMTP_PASSWORD": "发信授权码",
               "DFQ_IMAP_PASSWORD": "收信授权码（可不设，默认同发信）", "DFQ_TAVILY_API_KEY": "新闻搜索"}
    for name in SECRET_ENV_NAMES:
        if name in used_by:
            src = envfile.source(name, env_path)     # 只说来源，不显示值；系统环境变量会盖过 .env，要让用户看得出来
            _print(f"  {name:<20} {envfile.SOURCE_TEXT[src]}　（{used_by[name]}）")
    _print("", "功能开关：",
           f"  大模型 {'开' if cfg.get('llm.enabled') else '关'}　邮件 {'开' if cfg.get('mail.enabled') else '关'}　"
           f"moligod 提醒：{_moligod_mode(cfg)}　新闻搜索 "
           f"{'开' if cfg.get('intel.news.enabled') else '关'}　每小时异动检查 {'开' if cfg.get('watch.enabled') else '关'}")
    _print("", f"小涛查接口：今日 {db.usage_on('xtc')}/{cfg.get('xtc.daily_limit')} 次，累计 {db.usage_total('xtc')} 次")
    for r in db.query("SELECT day, SUM(cost) AS c FROM api_usage WHERE provider='xtc' GROUP BY day "
                      "ORDER BY day DESC LIMIT 7"):
        _print(f"  {r['day']}  {r['c']} 次")
    last = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    if last:
        _print("", f"最近一次运行：{last['started_at']}　模式 {last['mode']}　结果 {last['status']}　接口 {last['api_cost']} 次")
    _print("", "关注品种的数据覆盖：")
    for w in cfg.watchlist:
        row = db.find_item(w["name"])
        oid = row["oid"] if row else w["oid"]
        if oid is None:
            _print(f"  {w['name']:<22} 未知 oid")
            continue
        m = db.one("SELECT COUNT(*) AS c, MIN(ts) AS a, MAX(ts) AS b FROM minute_prices WHERE oid=?", (oid,))
        d = db.one("SELECT COUNT(*) AS c FROM day_prices WHERE oid=?", (oid,))
        _print(f"  {w['name']:<22} oid={oid:<5} 分钟点 {m['c']:<6} 日线 {d['c']:<3} "
               f"{(m['a'] or '—')} → {(m['b'] or '—')}")
    bal = funds.current_balance(cfg, db)
    _print("", "资金：" + (f"{bal['amount']:,.0f}（基准 {bal['base']:,.0f} @ {bal['base_ts']}，其后按成交推算）"
                         if bal["known"] else "未填写（python -m dfq balance set 金额）"))
    for name, p in ledger.open_positions(db, cfg.fee).items():
        _print(f"  持仓 {name} ×{p['qty']}　均价 {p['avg_cost']:.0f}")
    for name, p in ledger.open_positions(db, cfg.fee, paper=True).items():
        _print(f"  模拟持仓 {name} ×{p['qty']}　均价 {p['avg_cost']:.0f}　（模拟账本，不计入真实持仓和余额）")
    pend = db.one("SELECT COUNT(*) AS c FROM reflections WHERE status='pending'")["c"]
    if pend:
        _print("", f"有 {pend} 条反思建议待确认：python -m dfq reflect list")
    _print_integrity(db)
    return 0


def cmd_plan(args) -> int:
    cfg, db = _open(args)
    from . import guardrails, plan as planmod
    from .mail import compose
    p = planmod.load_latest(cfg)
    if p is None:
        _print("还没有计划。先运行：python -m dfq run")
        return 1
    if guardrails.is_expired(p):
        _print(f"※ 这份计划已于 {p['valid_until']} 过期作废，仅供查看。", "")
    _print(compose.build_subject(cfg, p), "")
    from . import moligod
    _print(compose.build_body(cfg, p, manual_alerts=moligod.desired_alerts(cfg, p)))
    return 0


# ------------------------------------------------------------------ 记账
def _fee_arg(cfg):
    """按品种取费率（cfg.fee_for）；没有它时用统一费率。"""
    return getattr(cfg, "fee_for", None) or cfg.fee


def _ledger_fail(e, verb: str = "没有记账") -> int:
    """记账被拒绝时的退出码：2 = 这笔本身有问题（改了再记）；3 = 数据库正被别的 DFQ 进程占用（原样稍后重试即可）。"""
    from . import ledger
    _print(f"{verb}：{e}")
    return 3 if isinstance(e, ledger.LedgerBusy) else 2


def _print_integrity(db) -> None:
    """旧版本允许超卖时留下的、没有成本依据的卖出：两本账分别列出，并说明怎么补。"""
    from . import ledger
    lines = ledger.integrity_report(db) + ledger.integrity_report(db, paper=True)
    if lines:
        _print("", "账本提示（这些卖出没有成本依据，不计盈亏）：", *[f"  · {x}" for x in lines])


def cmd_trade(args) -> int:
    cfg, db = _open(args)
    from . import ledger
    resolver = ledger.ItemResolver(cfg, db)
    item, why = resolver.resolve(args.item)
    if item is None:
        _print(f"没有记账：{why}")
        return 2
    ts = timeutil.parse_dt(args.at) if args.at else None
    try:
        tid = ledger.record_trade(db, args.side, item, args.qty, args.price, source=args.source, ts=ts,
                                  paper=args.paper, note=args.note, net=args.net)
    except ledger.LedgerError as e:
        return _ledger_fail(e)
    _print(f"已记账 #{tid}：{'买' if args.side == 'buy' else '卖'} {item['name']} ×{args.qty} @{args.price:.0f}"
           + (f"，实际到手 {args.net:,.0f}" if args.net is not None else "") + ("（模拟）" if args.paper else ""))
    pos = ledger.open_positions(db, _fee_arg(cfg), paper=args.paper).get(item["name"])
    if pos:
        _print(f"当前{'模拟' if args.paper else ''}持仓 ×{pos['qty']}，均价 {pos['avg_cost']:.0f}")
    if cfg.paper and not args.paper:
        _print(f"注意：现在是模拟模式（paper_mode），但这笔记进了真实账本。如果它只是模拟成交，"
               f"请撤销（python -m dfq trade-undo {tid}）后加 --paper 重记。")
    return 0


def cmd_trade_undo(args) -> int:
    cfg, db = _open(args)
    from . import ledger
    try:
        row = ledger.delete_trade(db, args.id)
    except ledger.LedgerError as e:
        return _ledger_fail(e, "没有撤销")
    what = "期初库存" if row.get("kind") == "opening" else ("买" if row["side"] == "buy" else "卖")
    _print(f"已撤销 #{row['id']}：{row['ts'][:16]} {what} {row['name']} "
           f"×{row['qty']} @{row['price']:.0f}（{'模拟账本' if row['paper'] else '真实账本'}，来源 {row['source']}）")
    if row["source"] == "mail":
        _print("这笔来自邮件回复。同一封邮件不会被再次记账；如需更正，请用 trade 命令手动补记。")
    return 0


def cmd_balance(args) -> int:
    cfg, db = _open(args)
    from . import funds, ledger
    if args.action == "set":
        if args.amount is None:
            _print("用法：python -m dfq balance set 金额")
            return 2
        try:
            ledger.set_balance(db, args.amount)
        except ledger.LedgerError as e:
            return _ledger_fail(e, "没有设置")
        _print(f"余额基准已设为 {args.amount:,.0f}。之后的成交会自动在此基础上加减。")
    bal = funds.current_balance(cfg, db)
    if bal.get("note"):
        _print(bal["note"])
    _print("当前估算余额：" + (f"{bal['amount']:,.0f}" if bal["known"] else "未填写"))
    _print_integrity(db)
    return 0


def cmd_inventory(args) -> int:
    """期初库存：使用本程序之前就有的子弹。只提供成本依据，不动余额；撤销用 trade-undo 编号。"""
    cfg, db = _open(args)
    from . import ledger
    book = "模拟账本" if args.paper else "真实账本"
    if args.action == "add":
        if args.item is None or args.qty is None or args.cost is None:
            _print('用法：python -m dfq inventory add 品种 数量 成本单价 [--paper] [--at "YYYY-MM-DD HH:MM"] [--note 文字]')
            return 2
        item, why = ledger.ItemResolver(cfg, db).resolve(args.item)
        if item is None:
            _print(f"没有录入：{why}")
            return 2
        ts = timeutil.parse_dt(args.at) if args.at else None
        try:
            tid = ledger.record_opening(db, item, args.qty, args.cost, paper=args.paper, ts=ts, note=args.note)
        except ledger.LedgerError as e:
            return _ledger_fail(e, "没有录入")
        _print(f"已录入期初库存 #{tid}：{item['name']} ×{args.qty}，成本 {args.cost:,.0f}/发（{book}；不动余额）。"
               f"录错了可以撤销：python -m dfq trade-undo {tid}")
        pos = ledger.open_positions(db, _fee_arg(cfg), paper=args.paper).get(item["name"])
        if pos:
            _print(f"当前{book}持仓 ×{pos['qty']}，均价 {pos['avg_cost']:.0f}")
        return 0
    rows = ledger.openings(db, paper=args.paper)
    if not rows:
        _print(f"{book}里还没有期初库存。录入：python -m dfq inventory add 品种 数量 成本单价" + (" --paper" if args.paper else ""))
    else:
        _print(f"{book}的期初库存（不动余额；撤销用 trade-undo 编号）：")
        for r in rows:
            _print(f"  #{r['id']}  {r['ts'][:16]}  {r['name']} ×{r['qty']}  成本 {r['price']:,.0f}/发"
                   + (f"  {r['note']}" if r["note"] else ""))
    lines = ledger.integrity_report(db, paper=args.paper)
    if lines:
        _print("", "账本提示（这些卖出没有成本依据，不计盈亏）：", *[f"  · {x}" for x in lines])
    return 0


def cmd_mail_poll(args) -> int:
    cfg, db = _open(args)
    from . import guardrails, moligod, plan as planmod
    from .mail import poll
    from .mail.transport import MailError, make_transport
    transport = make_transport(cfg, dry_run=args.dry_run)
    try:
        res = poll.poll(cfg, db, transport, dry_run=args.dry_run)
    except MailError as e:
        _print(f"读取邮件失败：{e}")
        return 1
    _print(f"检查 {res['checked']} 封，忽略 {res['ignored']} 封；记入真实账本 {res['real_recorded']} 笔、"
           f"模拟账本 {res['paper_recorded']} 笔；没看懂 {len(res['errors'])} 处；"
           f"未通过校验 {len(res['rejected'])} 封" + ("（dry-run：以上都没有真的写入）" if args.dry_run else ""))
    _print(*[f"  ✓ {r}" for r in res["recorded"]], *[f"  ✗ {e}" for e in res["errors"]],
           *[f"  ⊘ {x}" for x in poll.describe_rejections(res["rejected"])])
    if res["errors"]:
        _print("  有问题的回复整封都没有记账（避免只记一半）：改好后把全部成交重新回复一遍，或在界面里记账。")
    if res.get("malformed"):
        _print(f"  另有 {res['malformed']} 封邮件格式异常、无法解析，已跳过（以后不会再处理它们）。")
    if res.get("deferred"):
        _print(f"  另有 {res['deferred']} 封因为数据库正被另一个 DFQ 程序占用暂时没处理，下次读取邮件时会自动再试。")
    # 持仓变了：把 moligod 上对应品种的低于线在“买入线/止损线”之间切换
    p = planmod.load_latest(cfg)
    changed = res["paper_recorded"] if cfg.paper else res["real_recorded"]     # 只有当前模式那本账变了才需要改提醒
    # 只在“真的自动同步”时才去改网页（自动同步停用期间 auto_enabled 恒为 False，不会启动浏览器）
    if changed and not args.dry_run and moligod.auto_enabled(cfg) and p and not guardrails.is_expired(p):
        state = guardrails.portfolio_state(cfg, db)
        p["sells"] = guardrails.build_sells(cfg, state, p["items"])
        sync = moligod.sync(cfg, db, moligod.desired_alerts(cfg, p))
        _print("moligod：" + sync["status"])
        if sync["manual"]:
            lines = [f"· {a['name']}：低于(≤) {a['low']:.0f}（{a['low_role']}） 高于(≥) {a['high']:.0f}（{a['high_role']}）"
                     for a in sync["manual"]]
            try:
                transport.send("【请手动改提醒】持仓变化后 moligod 阈值需要更新",
                               sync["status"] + "\n\n" + "\n".join(lines) + "\n",
                               to=cfg.get("mail.owner_address") or "owner@localhost")
            except MailError as e:
                _print(f"提醒邮件发送失败：{e}")
    return 0


def cmd_mail_test(args) -> int:
    cfg, db = _open(args)
    from .mail.transport import MailError, make_transport
    transport = make_transport(cfg)
    try:
        mid = transport.send(
            "【测试】dfq 邮件发送正常",
            "这是一封测试邮件。能收到说明发信配置正确。\n\n"
            "下一步，确认“回复记账”能用：直接回复本邮件一行成交（格式：方向 品种 数量 成交价），\n"
            "然后运行  python -m dfq mail-poll --dry-run  （只检查，不会真的记账）。\n"
            "· 如果提示“身份验证未通过”，它会列出邮件里出现的验证标识，\n"
            "  把属于你邮箱服务商的那个填进 config.yaml 的 mail.auth.trusted_authserv 再试。\n"
            "· 如果提示“回复的那封邮件不是交易计划”，说明身份验证已经通过，一切正常\n"
            "  （测试邮件不对应任何计划，所以本来就不该记账）。\n",
            to=cfg.get("mail.owner_address") or "owner@localhost")
        # 记下这封信的编号，这样对它的回复才能通过“必须是对本系统邮件的回复”这道校验
        db.execute("INSERT OR IGNORE INTO mail_log(message_id,direction,subject,ts,status) VALUES(?,?,?,?,?)",
                   (mid, "out", "测试邮件", timeutil.fmt(timeutil.now()), "sent"))
    except MailError as e:
        _print(f"发送失败：{e}")
        return 1
    _print(f"已通过 {transport.name} 发送" + ("（邮件未启用，只写了文件）" if transport.name == "file" else "，请查收。"))
    return 0


# ------------------------------------------------------------------ 复盘
def cmd_perf(args) -> int:
    cfg, db = _open(args)
    from . import review
    review.score_open_decisions(cfg, db)
    if args.mock:
        from .execstats import MOCK_BANNER
        _print(MOCK_BANNER, "")
    _print(review.format_perf(review.perf(cfg, db, args.days)))
    src = review.source_scores(db)
    if src:
        _print("", "信源信用分（命中+1）/（样本+2），样本少时接近 0.5：")
        for s in src:
            _print(f"  {s['source_name']:<16} {s['score']:.2f}　已打分 {s['n']}（命中 {s['hits']}）　待打分 {s['pending']}")
    return 0


def cmd_fee_check(args) -> int:
    """用平台的“军需处兑换-子弹收益”接口核对卖出费率（1 次额度，6 小时内重复运行走缓存）。"""
    cfg, db = _open(args)
    from . import fees
    from .execstats import MOCK_BANNER
    from .sources import xtc as X
    from .sources.mock import MockXtcTransport
    if args.mock:
        client = X.XtcClient(cfg, db, "MOCK", transport=MockXtcTransport(cfg.fee))
        _print(MOCK_BANNER, "")
    else:
        client = X.XtcClient(cfg, db, secret("DFQ_XTC_TOKEN"))
    try:
        resp = client.exchange_pay()
        ev = fees.build_evidence(X.parse_exchange_pay(resp), cfg.fee, checked_at=resp.get("_fetched_at"))
    except (X.XtcError, ValueError) as e:
        _print(f"没有核对成功：{e}", "费率依据保持不变（计划里仍会写明“未核对”或上一次核对的结果）。")
        return 1
    ev["from_cache"] = bool(resp.get("_from_cache"))
    ev["mock"] = bool(args.mock)
    fees.save_evidence(db, ev)
    _print(f"平台数据（{ev['checked_at'][:16]}{'，来自 6 小时内的缓存，没有花次数' if ev['from_cache'] else ''}）："
           f"{ev['n']} 种子弹的 手续费 ÷ 出售总价", "")
    for it in ev["items"]:
        _print(f"  {str(it['name'] or '?'):<24} {('%d 级' % it['grade']) if it['grade'] else '    '}  {it['rate']:.3%}")
    _print("", f"最低 {ev['min']:.3%}　中位数 {ev['median']:.3%}　最高 {ev['max']:.3%}　配置 {cfg.fee:.2%}")
    if ev["consistent"]:
        _print(f"结论：与配置一致（相差不超过 {fees.CONSISTENT_TOL:.1%}）。计划里的费率依据会写“已用平台数据核对”。")
    else:
        _print(f"结论：与配置不一致。如果确认平台数据可信，把 config.yaml 的 fees.sell_fee_rate 改成 {ev['median']:.3f}。")
    grades = sorted({it["grade"] for it in ev["items"] if it["grade"]})
    _print("", "说明：这是第三方平台按它的规则算出来的手续费，不是游戏官方公告；"
               + (f"这次只覆盖 {'、'.join(map(str, grades))} 级子弹，" if grades else "")
               + "关注清单里没有覆盖到的子弹按同一费率外推。",
           "最可靠的依据是你自己的成交：卖出记账时填实际到手金额（trade sell ... --net 到手总额），满 3 笔后程序会据此校准。")
    return 0


def cmd_exec_stats(args) -> int:
    """真实成交统计：只用成交账本 + 建议记录，不用行情数据。"""
    cfg, db = _open(args)
    from . import execstats
    paper = bool(args.paper or args.mock)          # --mock 的数据都在模拟账本里
    if args.days is not None and args.days <= 0:
        _print("--days 必须是正整数")
        return 2
    s = execstats.compute(cfg, db, days=args.days or 30, paper=paper)
    _print(execstats.format_stats(s, mock=bool(args.mock)).rstrip())
    if cfg.paper and not paper:
        _print("", "现在是模拟模式（paper_mode），计划和回复记账都在模拟账本里；查看它请加 --paper。")
    return 0


def cmd_risk(args) -> int:
    """风控状态 / 确认恢复买入。"""
    cfg, db = _open(args)
    from . import risk
    paper = bool(args.paper or args.mock)
    book = "模拟账本" if paper else "真实账本"
    if args.action == "resume":
        r = risk.resume(db, paper=paper)
        _print(f"已记录确认（{r['acked_at'][:16]}，{book}）。"
               + (f"之前的暂停原因：{r['previous_reason']}。" if r["was_halted"] else "当时没有处于暂停状态。")
               + "从现在起，只有出现新的亏损才会再次暂停买入。")
    st = risk.status(cfg, db, paper=paper)
    lim = st["limits"]
    _print(f"风控状态（{book}）：" + (f"⚠ 已暂停新的买入——{st['reason']}" if st["halted"] else "正常，可以给出买入建议"),
           f"  近 {st['window_days']} 天已实现盈亏 {st['window_pnl']:+,.0f}；亏损上限 = 资金基准 {st['capital_base']:,.0f}"
           f"{'' if st['capital_known'] else '（未填余额，按 risk.assumed_capital）'} × {lim['max_cumulative_loss_pct']:.0%}",
           f"  最近连续亏损 {st['consecutive_losses']} 笔（上限 {lim['halt_after_losses']} 笔）　"
           f"有成本依据的卖出共 {st['closed_sells']} 笔",
           f"  单笔止损实亏 ≤ 总资产 {lim['max_loss_per_trade_pct']:.0%}；持仓超过 {lim['max_holding_days']} 天降到回本价卖，"
           f"超过 {2 * lim['max_holding_days']} 天建议按现价卖；涨跌或偏离 7 日中位价超过 {lim['abnormal_move_pct']:g}% 当天不买",
           f"  上次确认：{st['acked_at'][:16] if st['acked_at'] else '从未'}")
    if st["halted"]:
        _print("", f"卖出和止损照常。检查过原因之后运行：python -m dfq risk resume{' --paper' if paper else ''}")
    if cfg.paper and not paper:
        _print("", "现在是模拟模式（paper_mode），计划看的是模拟账本；查看它请加 --paper。")
    return 0


def cmd_review(args) -> int:
    cfg, db = _open(args)
    from . import review
    s = review.score_open_decisions(cfg, db)
    n = review.score_opinions(cfg, db)
    _print(f"建议：新定论 {s['finalized']} 条，仍在观察 {s['open']} 条；观点：新打分 {n} 条")
    return 0


def cmd_reflect(args) -> int:
    cfg, db = _open(args)
    from . import llm as L
    from . import reflect
    if args.action == "run":
        client = None if args.offline else L.LLMClient(cfg, secret("DFQ_LLM_API_KEY"))
        res = reflect.run(cfg, db, client, args.days)
        _print(Path(res["path"]).read_text(encoding="utf-8"), f"已存档：{res['path']}")
    elif args.action == "list":
        rows = db.query("SELECT * FROM reflections ORDER BY id DESC LIMIT 20")
        if not rows:
            _print("还没有反思记录。")
        for r in rows:
            c = json.loads(r["content"])
            _print(f"#{r['id']}  {r['created_at'][:16]}  {r['period']}  [{r['status']}]  "
                   f"参数建议 {len(c.get('param_suggestions') or [])} 条")
    elif args.action in ("show", "accept", "reject"):
        if args.id is None:
            _print("需要编号，例如：python -m dfq reflect show 1")
            return 2
        if args.action == "show":
            r = db.one("SELECT * FROM reflections WHERE id=?", (args.id,))
            if r is None:
                _print("没有这个编号")
                return 2
            _print(reflect.to_markdown(r["id"], r["period"], json.loads(r["content"]), r["model"]))
        else:
            _print(reflect.decide(cfg, db, args.id, args.action == "accept"))
    return 0


# ------------------------------------------------------------------ 资讯
def cmd_note(args) -> int:
    cfg, db = _open(args)
    from . import intel
    if args.action == "list":
        for r in db.query("SELECT * FROM documents ORDER BY id DESC LIMIT 20"):
            _print(f"#{r['id']} {r['collected_at'][:16]} [{r['source_type']}/{r['source_name']}] {r['title'] or ''}")
        return 0
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        _print("请粘贴文字，结束后按 Ctrl+Z 回车（Windows）或 Ctrl+D（其他系统）：")
        text = sys.stdin.read()
    doc_id = intel.add_document(db, args.source, args.name, text, title=args.title, url=args.url,
                                published_at=args.date)
    _print(f"已入库 #{doc_id}，下次运行时会提炼观点。" if doc_id else "内容为空或与已有记录重复，没有入库。")
    return 0


def cmd_transcribe(args) -> int:
    cfg, db = _open(args)
    from . import intel
    try:
        text = intel.transcribe(args.file, args.model)
    except RuntimeError as e:
        _print(str(e))
        return 2
    doc_id = intel.add_document(db, "bili", args.name, text, title=args.title or Path(args.file).stem)
    _print(f"转写完成（{len(text)} 字），已入库 #{doc_id}。")
    return 0


# ------------------------------------------------------------------ moligod / watch / init
def _moligod_mode(cfg) -> str:
    from . import moligod
    return moligod.mode_label(cfg)


def cmd_moligod_login(args) -> int:
    cfg, db = _open(args)
    from . import moligod
    if not moligod.AUTO_SYNC_AVAILABLE:            # 自动同步停用：不打开浏览器，明确告诉用户只能手动
        _print(moligod.UNAVAILABLE_TEXT)
        return 3
    _print(moligod.login(cfg))
    return 0


def cmd_moligod_sync(args) -> int:
    cfg, db = _open(args)
    from . import guardrails, moligod, plan as planmod
    p = planmod.load_latest(cfg)
    if p is None or guardrails.is_expired(p):
        _print("没有有效的计划（不存在或已过期），不做同步。")
        return 1
    if not moligod.auto_enabled(cfg):
        _print("moligod 提醒：手动模式（不操作网页），下面是需要你手动设置的提醒清单。")
    res = moligod.sync(cfg, db, moligod.desired_alerts(cfg, p), dry_run=args.dry_run)
    _print(res["status"])
    for a in res["manual"]:
        _print(f"  需手动：{a['name']}　低于(≤) {a['low']:.0f}（{a['low_role']}）　高于(≥) {a['high']:.0f}（{a['high_role']}）")
    # 返回 1 = “提醒没有被自动设置好，需要你手动处理”（手动模式下也是如此，脚本据此知道还有事要做）
    return 0 if res["ok"] else 1


def cmd_watch(args) -> int:
    cfg, db = _open(args)
    from . import watch
    from .mail.transport import make_transport
    from .sources import xtc as X
    if not cfg.get("watch.enabled") and not args.force:
        _print("每小时异动检查默认关闭（watch.enabled: false）。它每天约多花 48 次请求，确认要用再开。")
        return 0
    client = X.XtcClient(cfg, db, secret("DFQ_XTC_TOKEN"))
    try:
        res = watch.check(cfg, db, client, make_transport(cfg, dry_run=args.dry_run), dry_run=args.dry_run)
    except X.XtcError as e:
        _print(f"检查失败：{e}")
        return 1
    _print(f"检查 {res['checked']} 个关注品种，异动 {len(res['alerts'])} 条，发信 {res['sent']} 条")
    _print(*[f"  · {a}" for a in res["alerts"]])
    return 0


def cmd_gui(args) -> int:
    from .webui import app
    return app.main(config=args.config)


def cmd_init(args) -> int:
    dst = Path(args.config)
    from .layout import resource
    src = resource("config.example.yaml") or Path(__file__).resolve().parent.parent / "config.example.yaml"
    if dst.exists():
        _print(f"{dst} 已存在，没有覆盖。")
        return 0
    if not src.exists():
        _print("找不到 config.example.yaml")
        return 1
    shutil.copyfile(src, dst)
    _print(f"已生成 {dst}。请打开它填写邮箱等信息（密钥不要写进去，放在同一文件夹的 .env 里）。")
    return 0


# ------------------------------------------------------------------ argparse
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.yaml", help="配置文件路径（默认 config.yaml）")
    common.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    common.add_argument("--now", help=argparse.SUPPRESS)       # 模拟/测试用：固定“现在”
    common.add_argument("--mock", action="store_true",
                        help="使用模拟数据目录 data/mock/（run/probe 时行情也用模拟的，不花次数）")

    p = argparse.ArgumentParser(prog="python -m dfq", description="三角洲行动子弹交易决策助手",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", metavar="<子命令>")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, parents=[common])
        sp.set_defaults(fn=fn)
        return sp

    sp = add("run", cmd_run, "每晚主流程：采集 → 分析 → 计划 → 同步提醒 → 邮件")
    sp.add_argument("--offline", action="store_true", help="不调用大模型，只用规则基线")
    sp.add_argument("--dry-run", action="store_true", help="不花接口次数、不发邮件、不改 moligod，只看会产生什么")
    sp.add_argument("--force", action="store_true", help="忽略 run_every_days，立刻运行")
    sp.add_argument("--show", action="store_true", help="把邮件正文打印到屏幕")

    add("probe", cmd_probe, "阶段 0：实测小涛查接口（≤15 次），生成探测报告")
    add("status", cmd_status, "查看配置、环境变量、接口用量、数据覆盖")
    add("plan", cmd_plan, "查看最新的交易计划")
    add("init", cmd_init, "从 config.example.yaml 生成 config.yaml")
    add("gui", cmd_gui, "打开图形界面")

    sp = add("trade", cmd_trade, "命令行记账：trade buy PS12B 200 3550")
    sp.add_argument("side", choices=["buy", "sell"])
    sp.add_argument("item")
    sp.add_argument("qty", type=int)
    sp.add_argument("price", type=float)
    sp.add_argument("--paper", action="store_true", help="记为模拟成交（不计入真实持仓）")
    sp.add_argument("--at", help="成交时间，如 '2026-11-14 10:30'（默认现在）")
    sp.add_argument("--note")
    sp.add_argument("--net", type=float, help="卖出时实际到手的总金额（扣完手续费后真正收到的钱；填了就用它算余额和盈亏）")
    sp.add_argument("--source", choices=["cli", "gui"], default="cli", help=argparse.SUPPRESS)
    sp = add("trade-undo", cmd_trade_undo, "撤销一笔记错的成交或期初库存：trade-undo 编号（编号见 status 或界面）")
    sp.add_argument("id", type=int)
    sp = add("inventory", cmd_inventory, "期初库存（用本程序之前就有的子弹）：inventory add PS12B 200 3500 / inventory list")
    sp.add_argument("action", nargs="?", choices=["add", "list"], default="list")
    sp.add_argument("item", nargs="?")
    sp.add_argument("qty", nargs="?", type=int)
    sp.add_argument("cost", nargs="?", type=float, help="成本单价：不知道就填现在的市价（不能填 0）")
    sp.add_argument("--paper", action="store_true", help="录入模拟账本")
    sp.add_argument("--at", help="时间，如 '2026-11-01 10:00'（默认现在；给更早的卖出补成本时要早于那笔卖出）")
    sp.add_argument("--note")

    sp = add("balance", cmd_balance, "查看 / 设置哈夫币余额：balance set 5000000")
    sp.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    sp.add_argument("amount", nargs="?", type=float)

    sp = add("mail-poll", cmd_mail_poll, "读取邮件回复并记账")
    sp.add_argument("--dry-run", action="store_true")
    add("mail-test", cmd_mail_test, "发一封测试邮件")

    sp = add("perf", cmd_perf, "复盘绩效：最终建议 vs 规则基线 vs 什么都不做")
    sp.add_argument("--days", type=int, help="只统计近 N 天")
    add("fee-check", cmd_fee_check, "用平台数据核对卖出费率（花 1 次小涛查额度，6 小时内重复运行走缓存）")
    sp = add("exec-stats", cmd_exec_stats, "真实成交统计：建议成交率、滑点、持有时间与资金占用、已实现盈亏")
    sp.add_argument("--days", type=int, help="统计近 N 天（默认 30）")
    sp.add_argument("--paper", action="store_true", help="看模拟账本（默认看真实账本）")
    sp = add("risk", cmd_risk, "风控状态：risk status / risk resume（确认后恢复买入建议）")
    sp.add_argument("action", nargs="?", choices=["status", "resume"], default="status")
    sp.add_argument("--paper", action="store_true", help="看模拟账本（默认看真实账本）")
    add("review", cmd_review, "立即给到期的建议和观点打分")
    sp = add("reflect", cmd_reflect, "反思：run / list / show N / accept N / reject N")
    sp.add_argument("action", nargs="?", choices=["run", "list", "show", "accept", "reject"], default="list")
    sp.add_argument("id", nargs="?", type=int)
    sp.add_argument("--days", type=int)
    sp.add_argument("--offline", action="store_true")

    sp = add("note", cmd_note, "手动录入资讯：note add --source bili --name moligod --file 文稿.txt")
    sp.add_argument("action", choices=["add", "list"])
    sp.add_argument("--source", choices=["bili", "qq", "news", "other"], default="other")
    sp.add_argument("--name", default="未署名", help="信源名：UP 主名、群名……")
    sp.add_argument("--title")
    sp.add_argument("--url")
    sp.add_argument("--date")
    sp.add_argument("--file", help="文本文件；不给就从键盘粘贴")
    sp = add("transcribe", cmd_transcribe, "（可选）本地语音转写后入库，需要 faster-whisper")
    sp.add_argument("file")
    sp.add_argument("--name", required=True)
    sp.add_argument("--title")
    sp.add_argument("--model", default="small")

    add("moligod-login", cmd_moligod_login, "打开浏览器，手动登录 moligod 一次（自动同步暂时停用，目前不可用）")
    sp = add("moligod-sync", cmd_moligod_sync, "列出 / 同步 moligod 价格提醒（自动同步暂时停用，只列手动清单）")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("watch", cmd_watch, "（可选，默认关）每小时异常波动检查")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK，统一成 UTF-8 防乱码/报错
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    from . import envfile
    envfile.autoload(getattr(args, "config", None))   # 读取配置文件旁边的 .env（已有的环境变量优先）
    if not getattr(args, "fn", None):
        parser.print_help()
        return 0
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("已取消。")
        return 130
    except ValueError as e:                        # 配置错误等：给人话，不甩堆栈
        from .log import redact
        print(f"出错：{redact(e)}")
        return 2
