"""可选：每小时异常波动检查（默认关闭）。

每次只花 1～2 次请求：拉子弹列表的“今日跌幅榜第一页”和“今日涨幅榜第一页”，
关注品种出现大幅波动（默认 ±8%）或跌破持仓止损线时发一封提醒邮件。
注意：每小时跑一次 ≈ 每天 48 次，加上每晚的 ~15 次会超过默认日上限 60，
开启前请先把 xtc.daily_limit 调高，并确认资源包够用。
"""
from __future__ import annotations

from . import ledger, timeutil
from .sources import xtc as X


def check(cfg, db, client: X.XtcClient, transport, *, dry_run: bool = False) -> dict:
    move = float(cfg.get("watch.move_pct", 8.0))
    stop_pct = float(cfg.get("rules.stop_pct", 0.10))
    names = {w["name"].lower() for w in cfg.watchlist}
    key1, key2 = str(cfg.get("xtc.ammo_key1")), str(cfg.get("xtc.ammo_key2"))
    limit = int(cfg.get("xtc.list_limit", 20))
    seen: dict[str, dict] = {}
    for sort in ("1-1", "1-2"):          # 今日涨跌：从小到大 / 从大到小
        resp = client.call("item_list_pro", {"p": 1, "limit": limit, "key1": key1, "key2": key2,
                                             "sort": sort}, ttl=1800)
        for d in resp.get("data") or []:
            it = X.parse_list_item(d)
            if it and it["name"].lower() in names:
                seen[it["name"]] = it
    pos = ledger.open_positions(db, cfg.fee)
    alerts = []
    for name, it in seen.items():
        if it["bl"] is not None and abs(it["bl"]) >= move:
            alerts.append(f"{name} 今日 {it['bl']:+.1f}%，现价 {it['price']:.0f}")
        p = pos.get(name)
        if p and it["price"] and it["price"] <= p["avg_cost"] * (1 - stop_pct):
            alerts.append(f"{name} 现价 {it['price']:.0f} 已跌破止损线 "
                          f"{p['avg_cost'] * (1 - stop_pct):.0f}（持仓 ×{p['qty']}，均价 {p['avg_cost']:.0f}）")
    # 同一条提醒 6 小时内不重复发
    last = db.kv_get("watch_last", {}) or {}
    now = timeutil.now()
    fresh = [a for a in alerts if a.split(" ")[0] not in last
             or (now - timeutil.parse_dt(last[a.split(" ")[0]])).total_seconds() > 6 * 3600]
    if fresh and not dry_run:
        subject = "【异动】" + "｜".join(fresh)[:120]
        transport.send(subject, "\n".join(f"· {a}" for a in fresh) + "\n\n（每小时异常波动检查）\n",
                       to=cfg.get("mail.owner_address") or "owner@localhost")
        for a in fresh:
            last[a.split(" ")[0]] = timeutil.fmt(now)
        db.kv_set("watch_last", last)
    return {"checked": len(seen), "alerts": alerts, "sent": len(fresh) if not dry_run else 0}
