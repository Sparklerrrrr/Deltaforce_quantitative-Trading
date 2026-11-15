"""交易计划文件：data/plans/plan_<id>.json，另存一份 latest.json。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import timeutil


def new_plan_id(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M")


def build(cfg, *, now: datetime, guarded: dict, feats: dict[int, dict], state: dict,
          mode: str, model: str | None, prompt_ver: str | None, llm_out: dict | None,
          warnings: list[str], run_id: int | None) -> dict:
    watch = {w["name"]: w for w in cfg.watchlist}
    items = []
    for it in guarded["items"]:
        f = feats.get(it["oid"], {})
        w = watch.get(it["name"], {})
        items.append({
            **it,
            "search": w.get("search") or it["name"],
            "pct_rank": f.get("pct_rank"), "trend_7d": f.get("trend_7d"),
            "low_hours": f.get("low_hours"), "high_hours": f.get("high_hours"),
            "basis": f.get("basis"), "data_days": f.get("data_days"),
        })
    items.sort(key=lambda i: (i["action"] != "buy", -(i.get("confidence") or 0)))
    return {
        "plan_id": new_plan_id(now),
        "run_id": run_id,
        "created_at": timeutil.fmt(now),
        "valid_from": timeutil.fmt(now),
        "valid_until": timeutil.fmt(guarded["valid_until"]),
        "paper": cfg.paper,
        "mode": mode,                      # offline = 纯规则；llm = 规则 + 模型调整
        "model": model, "prompt_ver": prompt_ver,
        "fee_rate": cfg.fee,
        "risk": {
            "max_daily_spend_pct": cfg.get("risk.max_daily_spend_pct"),
            "max_position_pct": cfg.get("risk.max_position_pct"),
            "max_total_exposure_pct": cfg.get("risk.max_total_exposure_pct"),
            "caps": guarded.get("caps"),
        },
        "capital": {"cash": round(state["cash"]), "cash_known": state["cash_known"],
                    "positions_value": round(state["positions_value"]),
                    "equity": round(state["equity"])},
        "items": items,
        "sells": guarded["sells"],
        "market_view": (llm_out or {}).get("market_view"),
        "short_term": (llm_out or {}).get("short_term") or [],
        "long_term": (llm_out or {}).get("long_term") or [],
        "next_run": (llm_out or {}).get("next_run"),
        "next_run_reason": (llm_out or {}).get("next_run_reason"),
        "guard_notes": guarded.get("notes") or [],
        "warnings": warnings,
    }


def save(cfg, plan: dict) -> Path:
    d = cfg.data_dir / "plans"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"plan_{plan['plan_id']}.json"
    text = json.dumps(plan, ensure_ascii=False, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    (d / "latest.json").write_text(text, encoding="utf-8")
    return path


def load_latest(cfg) -> dict | None:
    p = cfg.data_dir / "plans" / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def record_decisions(db, plan: dict, rule: dict[int, dict], rule_sized: dict[int, dict]) -> None:
    """把建议写进 decisions 表：final（最终建议）和 rule（纯规则基线）各一份，供日后对比打分。"""
    rows = []
    common = (plan.get("run_id"), plan["plan_id"])
    for it in plan["items"]:
        rows.append((*common, "final", plan["created_at"], it["oid"], it["name"], it["action"],
                     it.get("buy_price"), it.get("sell_price"), it.get("stop_price"),
                     it.get("qty") or 0, it.get("budget") or 0, plan["valid_from"], plan["valid_until"],
                     it.get("confidence"), it.get("reason"), plan.get("model"), plan.get("prompt_ver"),
                     1 if plan["paper"] else 0))
    for oid, r in rule_sized.items():
        rows.append((*common, "rule", plan["created_at"], oid, r["name"], r["action"],
                     r.get("buy_price"), r.get("sell_price"), r.get("stop_price"),
                     r.get("qty") or 0, r.get("budget") or 0, plan["valid_from"], plan["valid_until"],
                     r.get("confidence"), r.get("reason"), None, None, 1 if plan["paper"] else 0))
    db.executemany(
        "INSERT INTO decisions(run_id,plan_id,variant,created_at,oid,name,action,buy_price,sell_price,"
        "stop_price,qty,budget,valid_from,valid_until,confidence,reason,model,prompt_ver,paper) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
