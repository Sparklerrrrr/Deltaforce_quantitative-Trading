"""流水线节点：采集 → 资讯 → 复盘 → 特征 → 规则 → 模型 → 护栏 → 计划 → 同步提醒 → 通知 → 反思。

每个节点只做一件事，输入输出通过 Context 传递；失败时怎么办由配置里的 on_fail 决定。
"""
from __future__ import annotations

import logging

from . import collect, features, guardrails, intel, ledger, moligod, plan as planmod
from . import llm as L
from . import reflect, review, rules, timeutil
from .config import secret
from .executors import get_executor, plan_to_instructions
from .log import event, get_logger
from .mail import compose, poll as mailpoll
from .mail.transport import FileTransport, make_transport
from .pipeline import Context, Node, NodeSkip, register
from .sources import xtc as X
from .sources.mock import MockXtcTransport

log = get_logger("nodes")


def xtc_client(ctx: Context) -> X.XtcClient:
    if "xtc" not in ctx.services:
        if ctx.mock:
            client = X.XtcClient(ctx.cfg, ctx.db, "MOCK", transport=MockXtcTransport(ctx.cfg.fee),
                                 run_id=ctx.run_id)
        else:
            client = X.XtcClient(ctx.cfg, ctx.db, secret("DFQ_XTC_TOKEN"), run_id=ctx.run_id,
                                 cache_only=ctx.dry_run)
        ctx.services["xtc"] = client
    return ctx.services["xtc"]


def llm_client(ctx: Context) -> L.LLMClient:
    if "llm" not in ctx.services:
        ctx.services["llm"] = L.LLMClient(ctx.cfg, secret("DFQ_LLM_API_KEY"))
    return ctx.services["llm"]


def mail_transport(ctx: Context):
    if "mail" not in ctx.services:
        ctx.services["mail"] = make_transport(ctx.cfg, dry_run=ctx.dry_run or ctx.mock)
    return ctx.services["mail"]


@register
class MailPoll(Node):
    id, title = "mail_poll", "读取邮件回复并记账"
    provides = ()

    def run(self, ctx):
        if not ctx.cfg.get("mail.enabled") and not (ctx.cfg.data_dir / "inbox").exists():
            raise NodeSkip("邮件未启用")
        if not ctx.cfg.get("mail.owner_address"):
            raise NodeSkip("未配置 mail.owner_address")
        res = mailpoll.poll(ctx.cfg, ctx.db, mail_transport(ctx), dry_run=ctx.dry_run)
        ctx["mail_result"] = res
        return {"real": res["real_recorded"], "paper": res["paper_recorded"],
                "errors": len(res["errors"]), "rejected": len(res["rejected"])}


@register
class CollectMarket(Node):
    id, title = "collect_market", "采集行情"
    provides = ("watch",)

    def run(self, ctx):
        if not ctx.cfg.get("xtc.enabled", True) and not ctx.mock:
            raise RuntimeError("xtc.enabled 为 false")
        client = xtc_client(ctx)
        if not ctx.mock and not ctx.dry_run and not client.token:
            raise X.TokenMissing("未设置 DFQ_XTC_TOKEN（在 DFQ.exe 的“设置”页填写，或写进程序文件夹的 .env）")
        res = collect.collect_market(ctx.cfg, ctx.db, client)
        ctx["watch"] = res["watch"]
        ctx["collect_stats"] = res
        for w in res["warnings"]:
            ctx.warn(w)
        if res["limit_hit"]:
            ctx.warn("【报警】小涛查今日请求次数已到上限，部分数据没拉到，计划可能基于不完整的数据。")
        if not res["stats"]["list_items"] and not res["stats"]["minute_points"]:
            raise RuntimeError("这次没有采到任何新行情")
        return {**res["stats"], "cost": res["cost"]}

    def degrade(self, ctx, err):
        watch, warns = collect.resolve_watchlist(ctx.cfg, ctx.db, None)
        ctx["watch"] = watch
        ctx.warn("行情采集失败，本次计划基于库里已有的旧数据（如果有）。")
        return {"fallback": "db", "items": len(watch)}


@register
class CollectIntel(Node):
    id, title = "collect_intel", "采集资讯与观点"

    def run(self, ctx):
        cfg, db = ctx.cfg, ctx.db
        n_manual = intel.ingest_manual_dir(cfg, db)
        n_news = 0
        if cfg.get("intel.news.enabled") and not ctx.offline and not ctx.dry_run and not ctx.mock:
            try:
                n_news = intel.fetch_news(cfg, db, secret("DFQ_TAVILY_API_KEY"))
            except ValueError as e:
                ctx.warn(f"新闻搜索未执行：{e}")
        n_hints = intel.hints_to_opinions(db, ctx.now)
        client = None if (ctx.offline or ctx.dry_run) else llm_client(ctx)
        stats = intel.process_documents(cfg, db, client)
        titles = stats.pop("quarantined_titles", [])
        if titles:
            ctx.warn(f"有 {len(titles)} 篇资讯疑似含有操纵模型的指令，已隔离、未参与分析："
                     + "；".join(titles[:5]) + "。如果是误判，把原文里对模型说话的句子删掉后重新放入即可。")
        ctx["intel_stats"] = {"manual": n_manual, "news": n_news, "hints": n_hints, **stats}
        return ctx["intel_stats"]


@register
class Review(Node):
    id, title = "review", "复盘打分"

    def run(self, ctx):
        s = review.score_open_decisions(ctx.cfg, ctx.db, ctx.now)
        n_op = review.score_opinions(ctx.cfg, ctx.db, ctx.now)
        ctx["recent"] = review.recent_summary(ctx.db, 7)
        ctx["sources"] = review.source_scores(ctx.db)
        return {**s, "opinions_scored": n_op}


@register
class Features(Node):
    id, title = "features", "计算特征"
    requires = ("watch",)
    provides = ("feats",)

    def run(self, ctx):
        feats = {}
        for w in ctx["watch"]:
            f = features.compute(ctx.cfg, ctx.db, w, ctx.now)
            feats[w["oid"]] = f
        if not feats:
            raise RuntimeError("关注清单里没有任何可用品种")
        ctx["feats"] = feats
        note = features.fee_check(ctx.cfg, list(feats.values()))
        if note:
            ctx.warn(note)
        usable = sum(1 for f in feats.values() if f.get("low_ref"))
        if usable == 0:
            ctx.warn("所有品种的历史数据都不足，今天只能全部观望。连续运行几天后会改善。")
        return {"items": len(feats), "usable": usable}


@register
class RuleBaseline(Node):
    id, title = "rule_baseline", "规则基线"
    requires = ("feats",)
    provides = ("rule",)

    def run(self, ctx):
        ctx["rule"] = {oid: rules.baseline(ctx.cfg, f) for oid, f in ctx["feats"].items()}
        return {"buy": sum(1 for r in ctx["rule"].values() if r["action"] == "buy")}


@register
class LLMAnalyze(Node):
    id, title = "llm_analyze", "模型分析"
    requires = ("feats", "rule")
    provides = ("candidates",)

    def _fallback(self, ctx, why: str):
        ctx["candidates"] = [{**r, "qty": None} for r in ctx["rule"].values()]
        ctx["mode"], ctx["llm_out"] = "offline", None
        ctx["mode_note"] = why

    def run(self, ctx):
        if ctx.offline:
            self._fallback(ctx, "--offline")
            return {"mode": "offline"}
        client = llm_client(ctx)
        if not client.ready:
            self._fallback(ctx, client.why_not_ready())
            if ctx.cfg.get("llm.enabled"):
                ctx.warn(f"模型未就绪（{client.why_not_ready()}），本次只用规则基线。")
            return {"mode": "offline", "reason": client.why_not_ready()}
        if ctx.dry_run:
            self._fallback(ctx, "dry-run 不调用模型")
            return {"mode": "offline", "reason": "dry-run"}
        state = guardrails.portfolio_state(ctx.cfg, ctx.db)
        user = L.build_analysis_input(
            ctx.cfg, ctx["feats"], ctx["rule"], state, ctx.get("recent") or {},
            ctx.get("sources") or [], intel.recent_digests(ctx.db), intel.recent_hints(ctx.db))
        out = L.validate_analysis(client.chat_json(L.SYSTEM_PROMPT, user), ctx["rule"])
        if out["missing"]:
            ctx.warn(f"模型漏掉了 {len(out['missing'])} 个品种，已用规则基线补齐。")
        ctx["candidates"] = out["items"]
        ctx["llm_out"] = out
        ctx["mode"] = "llm"
        return {"mode": "llm", "model": client.model}

    def degrade(self, ctx, err):
        self._fallback(ctx, f"模型失败: {err}")
        ctx.warn("模型分析失败，本次计划只用规则基线。")
        return {"mode": "offline", "fallback": True}


@register
class Guardrails(Node):
    id, title = "guardrails", "护栏"
    requires = ("feats", "rule", "candidates")
    provides = ("guarded", "rule_sized", "state")

    def run(self, ctx):
        state = guardrails.portfolio_state(ctx.cfg, ctx.db)
        if not state["cash_known"]:
            ctx.warn(f"还没有填写哈夫币余额，仓位按假定资金 {state['cash']:,.0f} 估算。"
                     f"填写方法：python -m dfq balance set 你的余额")
        guarded = guardrails.apply(ctx.cfg, ctx["candidates"], ctx["feats"], ctx["rule"], state, ctx.now)
        # 纯规则基线也过一遍同样的护栏和仓位分配，作为对照组
        base = guardrails.apply(ctx.cfg, [{**r, "qty": None} for r in ctx["rule"].values()],
                                ctx["feats"], ctx["rule"], state, ctx.now)
        ctx["guarded"], ctx["state"] = guarded, state
        ctx["rule_sized"] = {i["oid"]: i for i in base["items"]}
        n_guard = sum(1 for i in guarded["items"] if i["guard_notes"])
        return {"buy": sum(1 for i in guarded["items"] if i["action"] == "buy"),
                "adjusted_by_guard": n_guard, "sells": len(guarded["sells"])}


@register
class WritePlan(Node):
    id, title = "write_plan", "生成计划文件"
    requires = ("guarded", "feats", "state")
    provides = ("plan",)

    def run(self, ctx):
        client = ctx.services.get("llm")
        mode = ctx.get("mode", "offline")
        p = planmod.build(
            ctx.cfg, now=ctx.now, guarded=ctx["guarded"], feats=ctx["feats"], state=ctx["state"],
            mode=mode, model=client.model if (client and mode == "llm") else None,
            prompt_ver=L.PROMPT_VER if mode == "llm" else None, llm_out=ctx.get("llm_out"),
            warnings=list(ctx.warnings), run_id=ctx.run_id)
        if ctx.dry_run:
            p["plan_id"] += "-dryrun"
            d = ctx.cfg.data_dir / "plans"
            d.mkdir(parents=True, exist_ok=True)
            import json
            path = d / f"plan_{p['plan_id']}.json"
            path.write_text(json.dumps(p, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        else:
            path = planmod.save(ctx.cfg, p)
            planmod.record_decisions(ctx.db, p, ctx["rule"], ctx["rule_sized"])
            if p.get("next_run"):
                ctx.db.kv_set("next_run_suggestion", {"at": p["next_run"], "reason": p.get("next_run_reason")})
        ctx["plan"] = p
        ctx["plan_path"] = str(path)
        return {"plan_id": p["plan_id"], "path": str(path)}


@register
class SyncAlerts(Node):
    id, title = "sync_alerts", "同步 moligod 提醒"
    requires = ("plan",)
    provides = ("alert_result",)

    def run(self, ctx):
        desired = moligod.desired_alerts(ctx.cfg, ctx["plan"])
        res = moligod.sync(ctx.cfg, ctx.db, desired, dry_run=ctx.dry_run or ctx.mock)
        ctx["alert_result"] = res
        if not res["ok"] and ctx.cfg.get("moligod.enabled"):
            ctx.warn("moligod 提醒没有全部自动设置成功，见邮件里的手动设置清单。")
        return {"synced": len(res["synced"]), "manual": len(res["manual"]),
                "unmatched": len(res["unmatched"])}

    def degrade(self, ctx, err):
        desired = moligod.desired_alerts(ctx.cfg, ctx["plan"])
        ctx["alert_result"] = {"status": f"自动同步出错（{type(err).__name__}），请手动设置。",
                               "synced": [], "manual": desired, "unmatched": [], "ok": False}
        return {"manual": len(desired)}


@register
class Notify(Node):
    id, title = "notify", "发送计划邮件"
    provides = ("notified",)

    def _system_lines(self, ctx) -> list[str]:
        db, cfg = ctx.db, ctx.cfg
        lines = [f"小涛查接口：今日已用 {db.usage_on('xtc')}/{cfg.get('xtc.daily_limit')} 次"
                 + (f"，本次 {ctx.services['xtc'].cost_this_session} 次" if "xtc" in ctx.services else "")]
        mode = ctx.get("mode", "offline")
        lines.append("分析方式：" + ("规则 + 模型" if mode == "llm" else f"仅规则基线（{ctx.get('mode_note') or '未启用模型'}）"))
        if ctx.mock:
            lines.append("数据来源：模拟数据（--mock），价格是假的，仅用于演示流程")
        nr = db.kv_get("next_run_suggestion")
        if nr and mode == "llm":
            lines.append(f"模型建议下次运行时间：{nr.get('at')}")
        pending = db.one("SELECT COUNT(*) AS c FROM reflections WHERE status='pending'")["c"]
        if pending:
            lines.append(f"有 {pending} 条反思建议待你确认：python -m dfq reflect list")
        ups = [u["name"] for u in cfg.get("intel.bilibili.ups") or [] if u.get("name")]
        if ups:
            lines.append(f"资讯：{'、'.join(ups)} 的新视频需要你手动粘贴文字稿（放进 manual_notes/ 或用 note add）")
        return lines

    def _review_text(self, ctx) -> str | None:
        rec = ctx.get("recent")
        if not rec or not rec["final"]["signals"]:
            return None
        f, r = rec["final"], rec["rule"]
        avg = "—" if f["avg_return"] is None else f"{f['avg_return']:+.1%}"
        text = (f"近 {rec['days']} 天：买入信号 {f['signals']} 个，触发 {f['triggered']}，止盈 {f['wins']}，"
                f"止损 {f['stops']}，单笔均收益 {avg}；模拟盈亏 {f['profit']:+,.0f}"
                f"（规则基线 {r['profit']:+,.0f}，什么都不做 0）")
        return text + "\n详情：python -m dfq perf"

    def run(self, ctx):
        cfg = ctx.cfg
        transport = mail_transport(ctx)
        owner = cfg.get("mail.owner_address") or "owner@localhost"
        plan = ctx.get("plan")
        if plan is None:
            subject = "【系统异常】今晚没有生成交易计划"
            body = ("流程在生成计划前中止了，今天请不要按旧计划操作。\n\n原因：\n"
                    + "\n".join(f"· {w}" for w in ctx.warnings) + "\n\n排查：python -m dfq status\n")
            headers = None
        else:
            plan["warnings"] = list(dict.fromkeys(list(plan.get("warnings") or []) + ctx.warnings))
            executor, fallback_note = get_executor(cfg)
            if fallback_note:
                plan["warnings"].append(fallback_note)
            for ins in plan_to_instructions(plan):
                executor.execute(ins)
            alert = ctx.get("alert_result") or {"status": "", "manual": moligod.desired_alerts(cfg, plan)}
            receipts = mailpoll.recent_receipts(ctx.db)
            mr = ctx.get("mail_result")
            if mr and mr["errors"]:
                receipts += [f"没看懂的回复：{e}" for e in mr["errors"]]
            if mr and mr.get("rejected"):
                receipts += [f"未记账的来信：{x}" for x in mailpoll.describe_rejections(mr["rejected"])]
            subject = compose.build_subject(cfg, plan)
            cs = ctx.get("collect_stats")
            if cs is None or cs.get("limit_hit"):
                # 行情没采到或没采全：标题上直接标出来，避免把“没数据”误读成“没机会”
                subject = subject.replace("】", "】⚠行情数据不全｜", 1)
            body = compose.build_body(cfg, plan, manual_alerts=alert["manual"], alert_status=alert["status"],
                                      receipts=receipts, review_text=self._review_text(ctx),
                                      system_lines=self._system_lines(ctx))
            headers = {"X-DFQ-Plan": plan["plan_id"]}
        mid = transport.send(subject, body, to=owner, headers=headers)
        # 记下这封计划邮件属于“模拟”还是“真实”：日后回复它的成交记哪本账，只以这里为准
        ctx.db.execute("INSERT OR IGNORE INTO mail_log(message_id,direction,subject,ts,status,plan_id,paper) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (mid, "out", subject[:300], timeutil.fmt(timeutil.now()),
                        "file" if isinstance(transport, FileTransport) else "sent",
                        plan["plan_id"] if plan else None,
                        (1 if plan["paper"] else 0) if plan else None))
        ctx["notified"] = {"subject": subject, "body": body, "via": transport.name}
        if isinstance(transport, FileTransport) and not (ctx.dry_run or ctx.mock):
            ctx.warn("邮件未启用（mail.enabled: false），计划只写到了 data/outbox/latest.txt。")
        return {"via": transport.name, "subject": subject}

    def degrade(self, ctx, err):
        """发信失败：改为写文件，保证计划不丢。"""
        ft = FileTransport(ctx.cfg.data_dir)
        plan = ctx.get("plan")
        subject = compose.build_subject(ctx.cfg, plan) if plan else "【系统异常】今晚没有生成交易计划"
        alert = ctx.get("alert_result") or {"status": "", "manual": []}
        body = (compose.build_body(ctx.cfg, plan, manual_alerts=alert["manual"], alert_status=alert["status"])
                if plan else "\n".join(ctx.warnings))
        ft.send(subject, f"（邮件发送失败：{err}）\n\n{body}", to="owner@localhost")
        ctx["notified"] = {"subject": subject, "body": body, "via": "file(fallback)"}
        event(log, logging.ERROR, "邮件发送失败，计划已写入 data/outbox/latest.txt", node="notify")
        return {"via": "file(fallback)"}


@register
class Reflect(Node):
    id, title = "reflect", "每周反思"

    def run(self, ctx):
        if ctx.dry_run or not reflect.is_due(ctx.cfg, ctx.db):
            raise NodeSkip("未到反思时间")
        client = None if ctx.offline else llm_client(ctx)
        res = reflect.run(ctx.cfg, ctx.db, client)
        ctx["reflection"] = res
        return {"id": res["id"], "path": res["path"]}
