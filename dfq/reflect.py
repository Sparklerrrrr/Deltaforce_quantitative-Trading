"""每周反思：总结错在哪，提出对规则参数和提示词的修改建议。

建议只存档（数据库 + data/reflections/*.md），**不会自动生效**。
你看过后用 `python -m dfq reflect accept <编号>` 确认，白名单内的参数建议才会写进
data/overrides.yaml 并在下次运行时生效；提示词类建议永远只是文字，需要人工改代码。
"""
from __future__ import annotations

import json
import math
from datetime import timedelta
from pathlib import Path

import yaml

from . import llm as L
from . import review, timeutil
from .config import OVERRIDE_WHITELIST
from .untrusted import clean


def is_due(cfg, db) -> bool:
    every = int(cfg.get("review.reflect_every_days", 7))
    last = db.one("SELECT created_at FROM reflections ORDER BY id DESC LIMIT 1")
    first = db.one("SELECT MIN(created_at) AS t FROM decisions")
    if first is None or first["t"] is None:
        return False
    anchor = last["created_at"] if last else first["t"]
    return timeutil.now() - timeutil.parse_dt(anchor) >= timedelta(days=every)


def gather(cfg, db, days: int) -> dict:
    perf = review.perf(cfg, db, days)
    recent = review.recent_summary(db, days)
    per_item: dict[str, dict] = {}
    for it in recent["items"]:
        s = per_item.setdefault(it["name"], {"signals": 0, "win": 0, "stop": 0, "hold": 0, "miss": 0, "watch": 0})
        if it["action"] == "buy":
            s["signals"] += 1
        if it["outcome"] in s:
            s[it["outcome"]] += 1
    return {
        "period_days": days,
        "params": {k: cfg.get(k) for k in OVERRIDE_WHITELIST},
        "fee_rate": cfg.fee,
        "perf_final": perf["final"], "perf_rule": perf["rule"],
        "per_item": per_item,
        "decisions": recent["items"],
        "sources": review.source_scores(db),
        "real_trades": {"count": perf["real"]["trades"], "realized": perf["real"]["realized"]},
    }


def rule_based_reflection(data: dict) -> dict:
    """没有模型时的统计版反思：只陈述事实，给最保守的参数建议。"""
    f, r = data["perf_final"], data["perf_rule"]
    mistakes, sugg = [], []
    n = f["triggered"]
    if f["signals"] == 0:
        summary = "这段时间没有发出任何买入信号。"
    else:
        tr = f["trigger_rate"] or 0
        summary = (f"共 {f['signals']} 个买入信号，触发 {n} 个（{tr:.0%}），止盈 {f['wins']}、止损 {f['stops']}、"
                   f"持有到期 {f['holds']}；模拟盈亏 {f['profit']:+,.0f}，规则基线 {r['profit']:+,.0f}。")
    if n < 10:
        mistakes.append(f"触发样本只有 {n} 笔，不足以判断参数好坏，不建议修改。")
    else:
        p = data["params"]
        if (f["trigger_rate"] or 0) < 0.25:
            mistakes.append("买入线设得太低，多数信号没有成交机会。")
            sugg.append({"key": "rules.buy_quantile", "current": p["rules.buy_quantile"],
                         "suggested": round(min(p["rules.buy_quantile"] + 0.05, 0.45), 2),
                         "reason": f"触发率仅 {f['trigger_rate']:.0%}"})
        if n and f["stops"] / n > 0.35:
            mistakes.append("止损比例偏高，买入后继续下跌的情况多。")
            sugg.append({"key": "rules.trend_block_pct", "current": p["rules.trend_block_pct"],
                         "suggested": max(p["rules.trend_block_pct"] - 3, 3.0),
                         "reason": f"止损占触发的 {f['stops'] / n:.0%}，收紧下跌趋势过滤"})
        if n and f["holds"] / n > 0.5:
            mistakes.append("过半的买入在观察期内没等到卖出线，卖出线可能偏高。")
            sugg.append({"key": "rules.sell_quantile", "current": p["rules.sell_quantile"],
                         "suggested": round(max(p["rules.sell_quantile"] - 0.05, 0.55), 2),
                         "reason": f"持有到期占 {f['holds'] / n:.0%}"})
        if f["profit"] < r["profit"]:
            mistakes.append("最终建议的模拟盈亏不如纯规则基线：模型的调整这段时间是负贡献。")
    return {"summary": summary, "mistakes": mistakes, "param_suggestions": sugg,
            "prompt_suggestions": [], "watchlist_suggestions": [], "generated_by": "rules"}


def _clean_suggestions(raw) -> list[dict]:
    out = []
    for s in _as_list(raw):
        if not isinstance(s, dict) or s.get("key") not in OVERRIDE_WHITELIST:
            continue
        try:
            val = float(s.get("suggested"))
            if isinstance(s.get("suggested"), bool) or not math.isfinite(val):
                continue
        except (TypeError, ValueError):
            continue
        lo, hi = OVERRIDE_WHITELIST[s["key"]]
        if not (lo <= val <= hi):
            continue
        out.append({"key": s["key"], "current": s.get("current"), "suggested": val,
                    "reason": clean(s.get("reason"), 200)})
    return out


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def run(cfg, db, client: L.LLMClient | None = None, days: int | None = None) -> dict:
    days = days or int(cfg.get("review.reflect_every_days", 7))
    data = gather(cfg, db, days)
    content, model = None, None
    if client is not None and client.ready:
        try:
            obj = client.chat_json(L.REFLECT_PROMPT, json.dumps(data, ensure_ascii=False, default=str))
            content = {
                "summary": clean(obj.get("summary"), 1000),
                "mistakes": [clean(x, 300) for x in _as_list(obj.get("mistakes"))][:10],
                "param_suggestions": _clean_suggestions(obj.get("param_suggestions")),
                "prompt_suggestions": [clean(x, 300) for x in _as_list(obj.get("prompt_suggestions"))][:10],
                "watchlist_suggestions": [clean(x, 300) for x in _as_list(obj.get("watchlist_suggestions"))][:10],
                "generated_by": "llm",
            }
            model = client.model
        except L.LLMError as e:
            content = rule_based_reflection(data)
            content["mistakes"].append(f"（模型反思失败，改用统计版：{e}）")
    if content is None:
        content = rule_based_reflection(data)
    now = timeutil.now()
    period = f"{timeutil.day_str(now - timedelta(days=days))}~{timeutil.day_str(now)}"
    cur = db.execute("INSERT INTO reflections(created_at,period,content,status,model) VALUES(?,?,?,?,?)",
                     (timeutil.fmt(now), period, json.dumps(content, ensure_ascii=False), "pending", model))
    rid = cur.lastrowid
    path = cfg.data_dir / "reflections" / f"reflection_{rid:04d}_{timeutil.day_str(now)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_markdown(rid, period, content, model), encoding="utf-8")
    return {"id": rid, "period": period, "content": content, "path": str(path)}


def to_markdown(rid: int, period: str, c: dict, model: str | None) -> str:
    L_ = [f"# 反思 #{rid}（{period}）", "",
          f"生成方式：{'模型 ' + (model or '') if c.get('generated_by') == 'llm' else '统计规则（未调用模型）'}",
          "状态：待确认（不会自动生效）", "", "## 总结", c.get("summary") or "—", "", "## 错在哪"]
    L_ += [f"- {m}" for m in c.get("mistakes") or ["—"]]
    L_ += ["", "## 参数修改建议"]
    if c.get("param_suggestions"):
        for s in c["param_suggestions"]:
            L_.append(f"- `{s['key']}`：{s.get('current')} → **{s['suggested']}**（{s.get('reason', '')}）")
        L_ += ["", f"确认生效：`python -m dfq reflect accept {rid}`　　不采纳：`python -m dfq reflect reject {rid}`"]
    else:
        L_.append("- 无")
    for title, key in (("提示词修改建议（仅存档，需人工修改）", "prompt_suggestions"),
                       ("关注清单建议", "watchlist_suggestions")):
        L_ += ["", f"## {title}"]
        L_ += [f"- {m}" for m in c.get(key) or ["无"]]
    return "\n".join(L_) + "\n"


def decide(cfg, db, rid: int, accept: bool) -> str:
    row = db.one("SELECT * FROM reflections WHERE id=?", (rid,))
    if row is None:
        return f"没有编号为 {rid} 的反思"
    if row["status"] != "pending":
        return f"反思 #{rid} 已经是 {row['status']} 状态"
    applied = []
    if accept:
        content = json.loads(row["content"])
        sugg = _clean_suggestions(content.get("param_suggestions"))
        ov_path: Path = cfg.data_dir / "overrides.yaml"
        ov = {}
        if ov_path.exists():
            ov = yaml.safe_load(ov_path.read_text(encoding="utf-8")) or {}
        for s in sugg:
            sec, key = s["key"].split(".", 1)
            val = int(s["suggested"]) if key == "window_days" else s["suggested"]
            ov.setdefault(sec, {})[key] = val
            applied.append(f"{s['key']} = {val}")
        if applied:
            ov_path.write_text(
                "# 由 `python -m dfq reflect accept` 写入：你确认过的参数调整。删掉某一行即可撤销。\n"
                + yaml.safe_dump(ov, allow_unicode=True), encoding="utf-8")
    db.execute("UPDATE reflections SET status=?, decided_at=? WHERE id=?",
               ("accepted" if accept else "rejected", timeutil.fmt(timeutil.now()), rid))
    if not accept:
        return f"反思 #{rid} 已标记为不采纳。"
    if applied:
        return f"反思 #{rid} 已采纳，下次运行生效：{'；'.join(applied)}"
    return f"反思 #{rid} 已采纳（其中没有可自动应用的参数建议）。"
