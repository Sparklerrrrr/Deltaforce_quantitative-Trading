"""行情采集入库。

每晚一次的请求预算（8 个关注品种为例）：
  子弹列表翻页 3～5 次 + 分钟曲线 8 次 + 低价预测 1 次 + 昨日收益 1 次 ≈ 13～15 次
  首次运行另加天曲线 8 次（一次性，用来补 30 天历史）。
按“越重要越先拉”的顺序请求；额度用完就停，后面的环节用库里已有的数据继续。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from . import timeutil
from .log import event, get_logger
from .sources import xtc as X

log = get_logger("collect")


def resolve_watchlist(cfg, db, live_items: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """把关注清单里的名称对应到 oid。优先用接口实时数据，其次用库里的，最后用配置里的。"""
    by_name = {}
    for it in live_items or []:
        by_name[it["name"].lower()] = it
    resolved, warnings = [], []
    for w in cfg.watchlist:
        live = by_name.get(w["name"].lower())
        row = db.find_item(w["name"]) if live is None else None
        oid = live["oid"] if live else (row["oid"] if row else w["oid"])
        grade = live["grade"] if live else (row["grade"] if row else None)
        if oid is None:
            warnings.append(f"关注品种「{w['name']}」在接口返回里找不到，也没有配置 oid，已跳过（请核对名称）")
            continue
        if w["oid"] is not None and oid != w["oid"]:
            warnings.append(f"「{w['name']}」配置的 oid={w['oid']}，接口实际为 {oid}，已按接口为准")
        if live is None and live_items:
            warnings.append(f"「{w['name']}」不在本次接口返回的子弹列表里，沿用已知 oid={oid}")
        resolved.append({**w, "oid": int(oid), "grade": grade})
    return resolved, warnings


def collect_market(cfg, db, client: X.XtcClient) -> dict:
    """返回 {"watch": [...], "warnings": [...], "stats": {...}, "limit_hit": bool}"""
    now = timeutil.now()
    warnings: list[str] = []
    stats = {"list_items": 0, "minute_points": 0, "day_rows": 0, "hints": 0}
    limit_hit = False
    live: list[dict] = []

    # 1) 子弹列表：当前价、涨跌、秒出价
    try:
        raw = client.list_all(str(cfg.get("xtc.ammo_key1")), str(cfg.get("xtc.ammo_key2")))
        ts = timeutil.fmt_min(now)
        for d in raw:
            it = X.parse_list_item(d)
            if not it:
                continue
            live.append(it)
            db.upsert_item(it["oid"], it["name"], it["grade"], it["legacy_id"])
            db.execute(
                "INSERT OR REPLACE INTO snapshots(oid,ts,price,bl,day7_price,day7_bl,day30_price,"
                "day30_bl,sell_method,sell_level,quick_price) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (it["oid"], ts, it["price"], it["bl"], it["day7_price"], it["day7_bl"],
                 it["day30_price"], it["day30_bl"], it["sell_method"], it["sell_level"],
                 it["quick_price"]))
        stats["list_items"] = len(live)
        if not live:
            warnings.append("子弹列表返回为空：可能分类索引 key1/key2 不对，请运行 python -m dfq probe 确认")
    except X.DailyLimitExceeded as e:
        limit_hit = True
        warnings.append(str(e))
    except X.XtcError as e:
        warnings.append(f"子弹列表获取失败，沿用库内数据：{e}")

    watch, w2 = resolve_watchlist(cfg, db, live)
    warnings.extend(w2)

    # 2) 分钟曲线（只有 24 小时，必须每晚拉）
    if cfg.get("xtc.fetch_minute", True) and not limit_hit:
        for w in watch:
            try:
                resp = client.minute(w["oid"])
                ref = timeutil.parse_dt(resp.get("_fetched_at")) if resp.get("_fetched_at") else now
                pts = X.parse_minute_curve(resp, ref)
                stats["minute_points"] += db.add_minutes(w["oid"], pts)
                if not pts:
                    warnings.append(f"「{w['name']}」分钟曲线为空")
            except X.DailyLimitExceeded as e:
                limit_hit = True
                warnings.append(str(e))
                break
            except X.XtcError as e:
                warnings.append(f"「{w['name']}」分钟曲线失败：{e}")

    # 3) 天曲线：每个品种首次拉一次，之后 20 天刷新一次（自己攒的分钟数据会接上）
    if cfg.get("xtc.fetch_day_bootstrap", True) and not limit_hit:
        for w in watch:
            key = f"day_bootstrap:{w['oid']}"
            last = db.kv_get(key)
            if last and (now - timeutil.parse_dt(last)) < timedelta(days=20):
                continue
            try:
                resp = client.day(w["oid"])
                rows = X.parse_day_curve(resp, now)
                for r in rows:
                    db.execute(
                        "INSERT OR REPLACE INTO day_prices(oid,d,price,pmin,pmax,min_at,max_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (w["oid"], r["d"], r["price"], r["pmin"], r["pmax"], r["min_at"], r["max_at"]))
                stats["day_rows"] += len(rows)
                db.kv_set(key, timeutil.fmt(now))
            except X.DailyLimitExceeded as e:
                limit_hit = True
                warnings.append(str(e))
                break
            except X.XtcError as e:
                warnings.append(f"「{w['name']}」天曲线失败：{e}")

    # 4) 小时曲线 + 未来预测（可选，默认关）
    if cfg.get("xtc.fetch_hour_forecast", False) and not limit_hit:
        for w in watch:
            try:
                resp = client.hour(w["oid"], forecast=True)
                for r in X.parse_hour_curve(resp, now):
                    db.execute(
                        "INSERT OR REPLACE INTO hour_prices(oid,ts,price,pmin,pmax,forecast,fetched_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (w["oid"], timeutil.fmt_min(r["ts"]), r["price"], r["pmin"], r["pmax"],
                         r["forecast"], timeutil.fmt(now)))
            except X.DailyLimitExceeded as e:
                limit_hit = True
                warnings.append(str(e))
                break
            except X.XtcError as e:
                warnings.append(f"「{w['name']}」小时曲线失败：{e}")

    # 5) 平台自己的分析结论：当作一个信源入库，日后打分
    grades = cfg.get("xtc.grades")
    if grades in (None, "auto", []):
        grades = sorted({w["grade"] for w in watch if w.get("grade")}) or [5]
    watch_oids = {w["oid"] for w in watch}
    watch_names = {w["name"].lower() for w in watch}
    for flag, kind, fn, parser in (
        ("xtc.fetch_ammo_day", "ammo_day", client.ammo_day, X.parse_ammo_day),
        ("xtc.fetch_ammo_yesterday", "ammo_zr_yc", client.ammo_yesterday, X.parse_generic_items),
    ):
        if not cfg.get(flag, True) or limit_hit:
            continue
        for g in grades:
            try:
                resp = fn(int(g))
                if resp.get("_from_cache"):
                    continue  # 同一份数据不重复入库
                for row in parser(resp):
                    oid = int(row.get("oid") or 0)
                    name = str(row.get("name", ""))
                    if oid not in watch_oids and name.lower() not in watch_names:
                        continue
                    db.execute(
                        "INSERT INTO xtc_hints(ts,kind,oid,name,hour,price,extra) VALUES(?,?,?,?,?,?,?)",
                        (timeutil.fmt(now), kind, oid, name, row.get("hour"),
                         row.get("price"), json.dumps(row, ensure_ascii=False, default=str)))
                    stats["hints"] += 1
            except X.DailyLimitExceeded as e:
                limit_hit = True
                warnings.append(str(e))
                break
            except X.XtcError as e:
                warnings.append(f"{kind}(grade={g}) 失败：{e}")

    event(log, logging.INFO, "行情采集完成", node="collect_market", **stats,
          cost=client.cost_this_session, cache_hits=client.cache_hits, limit_hit=limit_hit)
    return {"watch": watch, "warnings": warnings, "stats": stats, "limit_hit": limit_hit,
            "cost": client.cost_this_session}
