"""配置校验：任何一项不合法都明确报错并拒绝运行。

为什么要这么严：这是一个会给出“买多少、什么价买”的程序。配置里把比例 5% 写成 5、
把键名拼错一个字母，都可能让风控形同虚设，而且不会有任何报错。所以这里：

  - 每个参数都有类型和取值范围，超出就报错（不会悄悄改成默认值）；
  - 不认识的键名直接报错（多半是拼写错误，否则你以为设了限额，其实没生效）；
  - 把所有问题一次列全，而不是改一个报一个；
  - 布尔值不能冒充数字（YAML 里 yes/no/on/off 会被解析成布尔值）。
"""
from __future__ import annotations

import difflib
import math
import re
from typing import Any

from .http import InsecureUrl, check_secure_url


class ConfigError(ValueError):
    """配置不合法。str(e) 是给人看的多行说明。"""

    def __init__(self, problems: list[str], source: str = "config.yaml"):
        self.problems = problems
        super().__init__(f"{source} 有 {len(problems)} 处问题，已拒绝运行：\n"
                         + "\n".join(f"  - {p}" for p in problems))


# ---- 规则描述 ----
def B():
    return ("bool",)


def I(lo, hi):                       # noqa: E743  整数，闭区间
    return ("int", lo, hi)


def N(lo, hi, lo_open=False, hi_open=False, ratio=False):   # 数值
    return ("num", lo, hi, lo_open, hi_open, ratio)


def R(lo, hi, lo_open=False, hi_open=False):                # 比例（小数），报错时会提示“5% 写 0.05”
    return N(lo, hi, lo_open, hi_open, ratio=True)


def S(allow_empty=False):
    return ("str", allow_empty)


def E(*choices):
    return ("enum", choices)


def OPT(rule):                       # 允许 null
    return ("opt", rule)


FREE = ("free",)                     # 结构另行检查

# llm.extra_body 不能覆盖的字段：模型、消息、流式（会破坏解析）、以及已有专门配置项的参数
LLM_RESERVED_KEYS = {"model", "messages", "stream", "max_tokens", "temperature", "response_format"}

SPEC: dict[str, tuple] = {
    "general.data_dir": S(),
    "general.paper_mode": B(),
    "general.run_every_days": I(1, 30),
    "general.plan_valid_hours": N(1, 168),

    "fees.sell_fee_rate": R(0, 0.5, hi_open=True),
    "fees.warn_diff": R(0, 0.5, lo_open=True),
    "fees.overrides": FREE,

    "xtc.enabled": B(),
    "xtc.base_url": ("url",),
    "xtc.daily_limit": I(1, 10000),
    "xtc.timeout": N(1, 120),
    "xtc.retries": I(0, 5),
    "xtc.ammo_key1": S(),
    "xtc.ammo_key2": S(),
    "xtc.list_limit": I(10, 20),
    "xtc.list_max_pages": I(1, 50),
    "xtc.grades": FREE,
    "xtc.fetch_minute": B(),
    "xtc.fetch_day_bootstrap": B(),
    "xtc.fetch_hour_forecast": B(),
    "xtc.fetch_ammo_day": B(),
    "xtc.fetch_ammo_yesterday": B(),
    "xtc.cost": FREE,
    "xtc.cache_ttl": FREE,

    "features.window_days": I(2, 30),
    "features.min_minute_points": I(10, 100000),

    "rules.buy_quantile": R(0, 0.5, lo_open=True),
    "rules.sell_quantile": R(0.5, 1, hi_open=True),
    "rules.min_margin": R(0, 0.5),
    "rules.stop_pct": R(0.01, 0.5),
    "rules.stop_buffer": R(0, 0.3),
    "rules.trend_block_pct": N(1, 100),
    "rules.cold_shrink": R(0, 0.5),

    "risk.assumed_capital": N(1, 1e12),
    "risk.max_daily_spend_pct": R(0, 1, lo_open=True),
    "risk.max_position_pct": R(0, 1, lo_open=True),
    "risk.max_total_exposure_pct": R(0, 1, lo_open=True),
    "risk.lot_size": I(1, 10000),
    "risk.min_qty": I(1, 1000000),
    "risk.llm_max_adjust_pct": R(0, 0.5),
    "risk.max_valid_hours": N(1, 720),
    "risk.max_loss_per_trade_pct": R(0, 0.5, lo_open=True),
    "risk.max_holding_days": I(1, 365),
    "risk.max_cumulative_loss_pct": R(0, 1, lo_open=True),
    "risk.loss_window_days": I(1, 365),
    "risk.halt_after_losses": I(1, 100),
    "risk.abnormal_move_pct": N(1, 100),

    "llm.enabled": B(),
    "llm.base_url": S(allow_empty=True),
    "llm.model": S(allow_empty=True),
    "llm.timeout": N(1, 600),
    "llm.retries": I(0, 5),
    "llm.max_tokens": I(100, 200000),
    "llm.temperature": OPT(N(0, 2)),
    "llm.json_mode": B(),
    "llm.allow_http_local": B(),
    "llm.extra_body": FREE,

    "intel.bilibili.enabled": B(),
    "intel.bilibili.ups": FREE,
    "intel.bilibili.max_videos_per_up": I(1, 20),
    "intel.bilibili.min_interval_sec": N(1, 3600),
    "intel.news.enabled": B(),
    "intel.news.provider": E("tavily"),
    "intel.news.queries": FREE,
    "intel.news.max_results": I(1, 20),
    "intel.news.days": I(1, 30),
    "intel.manual_dir": S(),
    "intel.opinion_horizon_days": I(1, 14),
    "intel.opinion_min_move_pct": N(0, 50),

    "mail.enabled": B(),
    "mail.backend": E("smtp_imap", "file"),
    "mail.owner_address": S(allow_empty=True),
    "mail.from_address": S(allow_empty=True),
    "mail.smtp_host": S(),
    "mail.smtp_port": I(1, 65535),
    "mail.smtp_user": S(allow_empty=True),
    "mail.imap_host": S(),
    "mail.imap_port": I(1, 65535),
    "mail.imap_user": S(allow_empty=True),
    "mail.imap_folder": S(),
    "mail.confirm_success": B(),
    "mail.poll_lookback_days": I(1, 30),
    "mail.reply_max_age_days": N(0.5, 30),
    "mail.auth.trusted_authserv": FREE,

    "executor.mode": E("manual", "auto"),

    "moligod.enabled": B(),
    "moligod.url": ("url",),
    "moligod.profile_dir": S(allow_empty=True),
    "moligod.headless": B(),
    "moligod.min_gap_sec": N(1, 120),
    "moligod.max_gap_sec": N(1, 300),
    "moligod.max_ops_per_run": I(1, 100),
    "moligod.timeout_sec": N(1, 120),

    "funds.official_enabled": B(),

    "review.sell_horizon_hours": N(1, 720),
    "review.reflect_every_days": I(1, 90),

    "watch.enabled": B(),
    "watch.move_pct": N(0.1, 100),

    "watchlist": FREE,
    "pipeline.nodes": FREE,
}

# 流水线节点名（与 nodes.py 的注册表保持一致，有测试盯着）
KNOWN_NODES = ("mail_poll", "collect_market", "collect_intel", "review", "reflect", "features",
               "rule_baseline", "llm_analyze", "guardrails", "write_plan", "sync_alerts", "notify")
ON_FAIL = ("degrade", "skip", "abort")
_EMAIL = re.compile(r"^[^@\s<>\"',;]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _fmt(v: Any) -> str:
    s = repr(v)
    return s if len(s) <= 40 else s[:37] + "…"


def _check(path: str, v: Any, rule: tuple, out: list[str]) -> None:
    kind = rule[0]
    if kind == "opt":
        if v is not None:
            _check(path, v, rule[1], out)
    elif kind == "bool":
        if not isinstance(v, bool):
            out.append(f"{path} 应为 true 或 false，现在是 {_fmt(v)}")
    elif kind == "int":
        _, lo, hi = rule
        if isinstance(v, bool) or not isinstance(v, int):
            if isinstance(v, float) and math.isfinite(v) and v == int(v):
                out.append(f"{path} 应为整数，请去掉小数点：{_fmt(v)}")
            else:
                out.append(f"{path} 应为整数，现在是 {_fmt(v)}")
        elif not (lo <= v <= hi):
            out.append(f"{path} 应在 {lo}～{hi} 之间，现在是 {v}")
    elif kind == "num":
        _, lo, hi, lo_open, hi_open, ratio = rule
        if not _is_num(v):
            hint = "（数字不要加引号；也不能是 nan/inf）" if isinstance(v, (str, float)) else ""
            out.append(f"{path} 应为数字，现在是 {_fmt(v)}{hint}")
            return
        ok = (v > lo if lo_open else v >= lo) and (v < hi if hi_open else v <= hi)
        if not ok:
            rng = f"{'大于' if lo_open else '不小于'} {lo:g} 且{'小于' if hi_open else '不大于'} {hi:g}"
            hint = ""
            if ratio and v > hi and lo <= v / 100 <= hi:
                hint = f"　〔这是比例，要写成小数：{v:g}% 应写 {v / 100:g}〕"
            out.append(f"{path} 应{rng}，现在是 {v:g}{hint}")
    elif kind == "str":
        if not isinstance(v, str):
            out.append(f"{path} 应为文字，现在是 {_fmt(v)}")
        elif not rule[1] and not v.strip():
            out.append(f"{path} 不能为空")
    elif kind == "enum":
        if v not in rule[1]:
            out.append(f"{path} 只能是 {' / '.join(map(str, rule[1]))}，现在是 {_fmt(v)}")
    elif kind == "url":
        if not isinstance(v, str):
            out.append(f"{path} 应为网址，现在是 {_fmt(v)}")
            return
        try:
            check_secure_url(v, what=path)
        except InsecureUrl as e:
            out.append(str(e))


def _walk_unknown(node: Any, prefix: str, out: list[str]) -> None:
    """找出不认识的键。到达规则里的叶子或自由结构就不再往下。"""
    if not isinstance(node, dict):
        return
    for key, val in node.items():
        path = f"{prefix}{key}"
        if path in SPEC:
            continue
        children = [p for p in SPEC if p.startswith(path + ".")]
        if children:
            if isinstance(val, dict):
                _walk_unknown(val, path + ".", out)
            else:
                out.append(f"{path} 应该是一组设置（下面还有子项），现在是 {_fmt(val)}")
            continue
        siblings = sorted({p[len(prefix):].split(".")[0] for p in SPEC if p.startswith(prefix)})
        guess = difflib.get_close_matches(str(key), siblings, n=1, cutoff=0.6)
        out.append(f"不认识的配置项 {path}" + (f"，是不是想写 {prefix}{guess[0]}？" if guess else
                                                "（拼写错误？程序不会使用它，所以直接报错）"))


def _get(data: dict, dotted: str) -> tuple[bool, Any]:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _check_free(data: dict, out: list[str]) -> None:
    # —— 大模型附加参数：只能补充，不能改掉程序自己控制的字段 ——
    ok, extra = _get(data, "llm.extra_body")
    if ok and extra is not None:
        if not isinstance(extra, dict):
            out.append(f"llm.extra_body 应该是一组键值（例如 {{thinking: {{type: disabled}}}}），现在是 {_fmt(extra)}")
        else:
            reserved = sorted(set(map(str, extra)) & LLM_RESERVED_KEYS)
            if reserved:
                out.append(f"llm.extra_body 里不能设置 {', '.join(reserved)}：这些由程序或 llm 的其他配置项控制")

    # —— 关注清单 ——
    wl = data.get("watchlist")
    if not isinstance(wl, list) or not wl:
        out.append("watchlist 不能为空，至少要有一个品种")
    else:
        seen: set[str] = set()
        aliases: dict[str, str] = {}
        for i, w in enumerate(wl, 1):
            where = f"watchlist 第 {i} 项"
            if isinstance(w, str):
                w = {"name": w}
            if not isinstance(w, dict):
                out.append(f"{where} 格式不对，应为 {{name: 品种名, ...}}")
                continue
            name = w.get("name")
            if not isinstance(name, str) or not name.strip():
                out.append(f"{where} 缺少 name（品种名）")
                continue
            for k in w:
                if k not in ("name", "oid", "alias", "moligod_name", "search"):
                    out.append(f"{where}（{name}）有不认识的键 {k}")
            if name.strip().lower() in seen:
                out.append(f"{where}：品种 {name} 重复出现")
            seen.add(name.strip().lower())
            oid = w.get("oid")
            if oid is not None and (isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0):
                out.append(f"{where}（{name}）的 oid 应为正整数或留空，现在是 {_fmt(oid)}")
            al = w.get("alias") or []
            if not isinstance(al, list) or any(not isinstance(a, (str, int)) or isinstance(a, bool) for a in al):
                out.append(f"{where}（{name}）的 alias 应为一组文字，如 [PS12B]")
            else:
                for a in al:
                    key = str(a).strip().lower()
                    if key in aliases and aliases[key] != name:
                        out.append(f"简称 {a} 同时属于 {aliases[key]} 和 {name}，记账时会分不清")
                    aliases[key] = name
            for k in ("moligod_name", "search"):
                if w.get(k) is not None and not isinstance(w.get(k), str):
                    out.append(f"{where}（{name}）的 {k} 应为文字")

    # —— 小涛查 ——
    ok, grades = _get(data, "xtc.grades")
    if ok and grades != "auto":
        if not isinstance(grades, list) or not grades or any(
                isinstance(g, bool) or not isinstance(g, int) or not (0 <= g <= 6) for g in grades):
            out.append(f"xtc.grades 应为 auto 或一组 0～6 的整数（如 [5]），现在是 {_fmt(grades)}")
    for key, lo, hi, unit in (("xtc.cost", 0, 100, "次"), ("xtc.cache_ttl", 0, 90 * 86400, "秒")):
        ok, d = _get(data, key)
        if ok:
            if not isinstance(d, dict):
                out.append(f"{key} 应为“接口名: 数值”的对照表")
            else:
                for k, v in d.items():
                    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
                        out.append(f"{key}.{k} 应为 {lo}～{hi} 的整数（{unit}），现在是 {_fmt(v)}")

    # —— 资讯 ——
    ok, q = _get(data, "intel.news.queries")
    if ok and (not isinstance(q, list) or any(not isinstance(x, str) or not x.strip() for x in q)):
        out.append("intel.news.queries 应为一组非空的搜索词")
    ok, ups = _get(data, "intel.bilibili.ups")
    if ok and (not isinstance(ups, list) or any(not isinstance(u, dict) or not u.get("name") for u in ups)):
        out.append("intel.bilibili.ups 的每一项都应为 {name: UP主名}")

    # —— 邮件身份验证 ——
    ok, ta = _get(data, "mail.auth.trusted_authserv")
    if ok:
        if not isinstance(ta, list):
            out.append("mail.auth.trusted_authserv 应为一组标识，如 [mx.example.com]；没有就写 []")
        else:
            for t in ta:
                if not isinstance(t, str) or not _HOST.match(t.strip()):
                    out.append(f"mail.auth.trusted_authserv 里的 {_fmt(t)} 不像一个服务器标识")

    # —— 流水线 ——
    ok, nodes = _get(data, "pipeline.nodes")
    if ok:
        if not isinstance(nodes, list) or not nodes:
            out.append("pipeline.nodes 应为非空的节点列表")
        else:
            ids = [n.get("id") for n in nodes if isinstance(n, dict)]
            for i, n in enumerate(nodes, 1):
                where = f"pipeline.nodes 第 {i} 项"
                if not isinstance(n, dict) or not isinstance(n.get("id"), str):
                    out.append(f"{where} 应为 {{id: 节点名, ...}}")
                    continue
                if n["id"] not in KNOWN_NODES:
                    guess = difflib.get_close_matches(n["id"], KNOWN_NODES, n=1)
                    out.append(f"{where}：没有叫 {n['id']} 的节点" + (f"，是不是 {guess[0]}？" if guess else ""))
                for k, v in n.items():
                    if k == "id":
                        continue
                    if k == "on_fail":
                        good = v in ON_FAIL or (isinstance(v, str) and v.startswith("goto:") and v[5:] in ids)
                        if not good:
                            out.append(f"{where}（{n['id']}）的 on_fail 只能是 degrade / skip / abort / goto:已有节点名，"
                                       f"现在是 {_fmt(v)}")
                    elif k == "retries":
                        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 5):
                            out.append(f"{where}（{n['id']}）的 retries 应为 0～5 的整数")
                    elif k in ("always", "enabled"):
                        if not isinstance(v, bool):
                            out.append(f"{where}（{n['id']}）的 {k} 应为 true 或 false")
                    else:
                        out.append(f"{where}（{n['id']}）有不认识的键 {k}")
            if len(ids) != len(set(ids)):
                out.append("pipeline.nodes 里有重复的节点")
            for must in ("guardrails", "write_plan", "notify"):
                en = [n for n in nodes if isinstance(n, dict) and n.get("id") == must and n.get("enabled", True)]
                if not en:
                    out.append(f"pipeline.nodes 里不能去掉或关闭 {must}"
                               + ("（风控护栏是硬性要求）" if must == "guardrails" else ""))
            order = {nid: i for i, nid in enumerate(ids)}
            if all(k in order for k in ("llm_analyze", "guardrails", "write_plan")) and not (
                    order["llm_analyze"] < order["guardrails"] < order["write_plan"]):
                out.append("pipeline.nodes 的顺序必须是 llm_analyze → guardrails → write_plan（模型的建议必须先过护栏）")


def _check_cross(data: dict, out: list[str], notes: list[str]) -> None:
    def num(path):
        ok, v = _get(data, path)
        return v if ok and _is_num(v) else None

    bq, sq = num("rules.buy_quantile"), num("rules.sell_quantile")
    if bq is not None and sq is not None and not bq < sq:
        out.append(f"rules.buy_quantile（{bq:g}）必须小于 rules.sell_quantile（{sq:g}）")
    pv, mv = num("general.plan_valid_hours"), num("risk.max_valid_hours")
    if pv is not None and mv is not None and pv > mv:
        out.append(f"general.plan_valid_hours（{pv:g}）不能超过 risk.max_valid_hours（{mv:g}）")
    g1, g2 = num("moligod.min_gap_sec"), num("moligod.max_gap_sec")
    if g1 is not None and g2 is not None and g1 > g2:
        out.append(f"moligod.min_gap_sec（{g1:g}）不能大于 max_gap_sec（{g2:g}）")
    if _get(data, "moligod.enabled")[1] is True:
        from .moligod import AUTO_SYNC_AVAILABLE     # 用到时再导入：配置检查本身不该依赖账本等模块的加载顺序
        if not AUTO_SYNC_AVAILABLE:
            # 只提示、不报错：用户以前写了 true 的配置要照常能打开
            notes.append("moligod.enabled 设成了 true，但自动同步功能暂时停用，按手动模式运行"
                         "（计划邮件里会列出提醒价位，请到 moligod 网页上手动设置）")
    lot, mq = num("risk.lot_size"), num("risk.min_qty")
    if lot is not None and mq is not None and mq < lot:
        out.append(f"risk.min_qty（{mq:g}）不能小于 risk.lot_size（{lot:g}）")
    pp, te = num("risk.max_position_pct"), num("risk.max_total_exposure_pct")
    if pp is not None and te is not None and pp > te:
        notes.append(f"单品种上限（{pp:g}）大于总仓位上限（{te:g}），实际起作用的是总仓位上限")
    fee = num("fees.sell_fee_rate")
    if fee is not None and fee == 0:
        notes.append("fees.sell_fee_rate 是 0：回本价将等于买入价。确认交易行真的不收费再这样设")

    # 按品种单独设置的费率：{品种名: 费率}，0 ≤ 费率 < 1；品种不在关注清单里只提示（可能是还没加进清单）
    ok, ov = _get(data, "fees.overrides")
    if ok and ov is not None:
        if not isinstance(ov, dict):
            out.append(f"fees.overrides 应为“品种名: 费率”的对照表（例如 {{\"12.7x55mm PS12B\": 0.13}}），现在是 {_fmt(ov)}")
        else:
            wl = _get(data, "watchlist")[1]
            names = {str(w.get("name") if isinstance(w, dict) else w).strip().lower()
                     for w in (wl if isinstance(wl, list) else [])}
            for k, v in ov.items():
                if not _is_num(v) or not (0 <= v < 1):
                    hint = f"　〔这是比例，要写成小数：{v:g}% 应写 {v / 100:g}〕" if _is_num(v) and 1 <= v < 100 else ""
                    out.append(f"fees.overrides.{k} 应为 0～1 之间的小数（不含 1），现在是 {_fmt(v)}{hint}")
                elif str(k).strip().lower() not in names:
                    notes.append(f"fees.overrides 里的 {k} 不在关注清单里，这个费率只在记账、持仓估值时用到")

    ok, wl = _get(data, "watchlist")
    if ok and isinstance(wl, list) and len(wl) > 10:
        notes.append(f"关注清单有 {len(wl)} 个品种，建议控制在 5～10 个")

    # 大模型地址：填了就必须安全（不管开没开）
    ok, url = _get(data, "llm.base_url")
    allow = _get(data, "llm.allow_http_local")[1]
    if ok and isinstance(url, str) and url.strip():
        try:
            check_secure_url(url, allow_http_local=bool(allow) if isinstance(allow, bool) else False,
                             what="llm.base_url")
        except InsecureUrl as e:
            out.append(str(e))
    if _get(data, "llm.enabled")[1] is True:
        if not (isinstance(url, str) and url.strip()):
            out.append("llm.enabled 为 true 时必须填写 llm.base_url")
        m = _get(data, "llm.model")[1]
        if not (isinstance(m, str) and m.strip()):
            out.append("llm.enabled 为 true 时必须填写 llm.model")

    # 邮件：开了就必须填全
    if _get(data, "mail.enabled")[1] is True and _get(data, "mail.backend")[1] == "smtp_imap":
        owner = _get(data, "mail.owner_address")[1]
        if not (isinstance(owner, str) and _EMAIL.match(owner.strip())):
            out.append(f"mail.enabled 为 true 时 mail.owner_address 必须是你的邮箱地址，现在是 {_fmt(owner)}")
        sender = _get(data, "mail.from_address")[1] or _get(data, "mail.smtp_user")[1]
        if not (isinstance(sender, str) and _EMAIL.match(sender.strip())):
            out.append("mail.enabled 为 true 时必须填写 mail.from_address（发件邮箱地址）")
        ta = _get(data, "mail.auth.trusted_authserv")[1]
        if not ta:
            notes.append("mail.auth.trusted_authserv 为空：邮件回复无法验证身份，一律不会记账（计划邮件照常发送）。"
                         "运行 python -m dfq mail-poll --dry-run 可以看到你的邮箱服务商写的验证标识")
    elif _get(data, "mail.owner_address")[1]:
        owner = _get(data, "mail.owner_address")[1]
        if isinstance(owner, str) and owner.strip() and not _EMAIL.match(owner.strip()):
            out.append(f"mail.owner_address 不是合法的邮箱地址：{_fmt(owner)}")


def check(data: dict, source: str = "config.yaml") -> list[str]:
    """校验合并后的完整配置。有问题抛 ConfigError；返回不致命的提示。"""
    problems: list[str] = []
    notes: list[str] = []
    if not isinstance(data, dict):
        raise ConfigError(["顶层必须是键值对"], source)
    _walk_unknown(data, "", problems)
    for path, rule in SPEC.items():
        if rule is FREE:
            continue
        found, value = _get(data, path)
        if not found:
            problems.append(f"缺少配置项 {path}")
            continue
        _check(path, value, rule, problems)
    _check_free(data, problems)
    _check_cross(data, problems, notes)
    if problems:
        raise ConfigError(list(dict.fromkeys(problems)), source)
    return notes


def check_overrides(extra: Any, whitelist: dict[str, tuple[float, float]], source: str) -> dict[str, float]:
    """校验 overrides.yaml（你确认过的反思调整）。只允许白名单内、范围内的数值。"""
    problems: list[str] = []
    out: dict[str, float] = {}
    if extra is None:
        return out
    if not isinstance(extra, dict):
        raise ConfigError(["内容必须是键值对"], source)

    def flat(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                flat(v, key + ".")
            elif key not in whitelist:
                problems.append(f"{key} 不允许通过这个文件修改（只允许：{'、'.join(whitelist)}）。请删掉这一行；"
                                f"要改它请改 config.yaml")
            elif not _is_num(v):
                problems.append(f"{key} 应为数字，现在是 {_fmt(v)}")
            else:
                lo, hi = whitelist[key]
                if not (lo <= v <= hi):
                    problems.append(f"{key} 应在 {lo:g}～{hi:g} 之间，现在是 {v:g}")
                else:
                    out[key] = v

    flat(extra)
    if problems:
        raise ConfigError(problems, source)
    return out
