"""记账：成交、持仓、资金。

- 成交来自两处：邮件回复（mail-poll）和命令行（python -m dfq trade ...）。
- 持仓 = 所有真实成交按“移动加权平均成本”累计出来的结果。
- 资金 = 你手动填的余额基准 + 基准之后的成交推算（卖出按扣费后到手计）。
"""
from __future__ import annotations

import re
from datetime import datetime

from . import fees, timeutil


class LedgerError(Exception):
    pass


def _norm(s: str) -> str:
    """名称归一化：去空白和常见分隔符、全角转半角、统一小写。"""
    s = s.strip().lower()
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    return re.sub(r"[\s\-_·\.\"'“”‘’]+", "", s).replace("×", "x")


class ItemResolver:
    """把用户写的品种名（PS12B / ss190 / 12.7x55mm PS12B）对应到标准名和 oid。"""

    def __init__(self, cfg, db):
        self.entries: list[tuple[str, dict]] = []     # (归一化的写法, 品种)
        seen = set()
        for w in cfg.watchlist:
            row = db.find_item(w["name"])
            item = {"name": w["name"], "oid": row["oid"] if row else w["oid"]}
            keys = [w["name"], *w["alias"], w["name"].split()[-1]]
            if w.get("moligod_name"):
                keys.append(w["moligod_name"])
            for k in keys:
                nk = _norm(k)
                if nk and (nk, item["name"]) not in seen:
                    seen.add((nk, item["name"]))
                    self.entries.append((nk, item))
        self.db_items = [{"name": r["name"], "oid": r["oid"]}
                         for r in db.query("SELECT oid,name FROM items")]

    def resolve(self, text: str) -> tuple[dict | None, str | None]:
        """返回 (品种, 错误信息)。匹配不到或有歧义时品种为 None —— 记账宁可多问一句，不能猜。

        优先级：① 关注清单的名称/别名完全一致 → ② 全部子弹里名称或末段完全一致
               → ③ 部分匹配（至少 3 个字符），且在全部子弹里只对应唯一一个品种。
        """
        q = _norm(text)
        if not q:
            return None, "没有写品种名"
        exact = {e[1]["name"]: e[1] for e in self.entries if e[0] == q}
        if len(exact) == 1:
            return next(iter(exact.values())), None
        if len(exact) > 1:
            return None, f"「{text}」同时对应 {'、'.join(exact)}，请写全名"
        exact_db = {i["name"]: i for i in self.db_items
                    if _norm(i["name"]) == q or _norm(i["name"].split()[-1]) == q}
        if len(exact_db) == 1:
            return next(iter(exact_db.values())), None
        if len(exact_db) > 1:
            return None, f"「{text}」同时对应 {'、'.join(list(exact_db)[:5])}，请写全名"
        if len(q) < 3:
            return None, f"不认识的品种「{text}」"
        part: dict[str, dict] = {}
        for key, item in self.entries:
            if q in key or (len(key) >= 3 and key in q):
                part[item["name"]] = item
        for i in self.db_items:
            if q in _norm(i["name"]):
                part.setdefault(i["name"], i)
        if len(part) == 1:
            return next(iter(part.values())), None
        if len(part) > 1:
            return None, f"「{text}」可能是 {'、'.join(list(part)[:5])}，请写清楚"
        return None, f"不认识的品种「{text}」"

def record_trade(db, side: str, item: dict, qty: int, price: float, *, source: str = "cli",
                 ts: datetime | None = None, message_id: str | None = None,
                 paper: bool = False, note: str | None = None) -> int:
    if side not in ("buy", "sell"):
        raise LedgerError("方向只能是 buy 或 sell")
    if qty <= 0 or price <= 0:
        raise LedgerError("数量和价格必须为正数")
    if side == "sell" and not paper:
        held = positions(db).get(item["name"], {}).get("qty", 0)
        if qty > held:
            note = (note + "；" if note else "") + f"卖出数量 {qty} 超过账面持仓 {held}（可能有未记账的买入）"
    cur = db.execute(
        "INSERT INTO trades(ts,side,oid,name,qty,price,source,message_id,paper,note) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (timeutil.fmt(ts or timeutil.now()), side, item.get("oid"), item["name"], int(qty),
         float(price), source, message_id, 1 if paper else 0, note))
    return cur.lastrowid


def positions(db, fee: float = fees.DEFAULT_FEE) -> dict[str, dict]:
    """真实持仓：{名称: {oid, qty, avg_cost, realized}}。只统计非模拟成交。"""
    pos: dict[str, dict] = {}
    for r in db.query("SELECT * FROM trades WHERE paper=0 ORDER BY ts, id"):
        p = pos.setdefault(r["name"], {"oid": r["oid"], "qty": 0, "avg_cost": 0.0, "realized": 0.0,
                                       "last_buy_at": None})
        if r["side"] == "buy":
            total = p["avg_cost"] * p["qty"] + r["price"] * r["qty"]
            p["qty"] += r["qty"]
            p["avg_cost"] = total / p["qty"]
            p["last_buy_at"] = r["ts"]
        else:
            sold = min(r["qty"], p["qty"]) if p["qty"] > 0 else 0
            p["realized"] += (fees.net_proceeds(r["price"], fee) - p["avg_cost"]) * sold
            # 超卖部分没有成本依据，只计到手金额
            p["realized"] += fees.net_proceeds(r["price"], fee) * (r["qty"] - sold)
            p["qty"] -= sold
            if p["qty"] == 0:
                p["avg_cost"] = 0.0
    return pos


def open_positions(db, fee: float = fees.DEFAULT_FEE) -> dict[str, dict]:
    return {k: v for k, v in positions(db, fee).items() if v["qty"] > 0}


def set_balance(db, amount: float) -> None:
    if amount < 0:
        raise LedgerError("余额不能为负")
    db.kv_set("balance_base", {"amount": float(amount), "ts": timeutil.fmt(timeutil.now())})


def balance(db, fee: float = fees.DEFAULT_FEE) -> dict:
    """返回 {known, amount, base, base_ts}。amount = 基准 + 基准之后成交的现金流。"""
    base = db.kv_get("balance_base")
    if not base:
        return {"known": False, "amount": None, "base": None, "base_ts": None}
    amount = float(base["amount"])
    for r in db.query("SELECT * FROM trades WHERE paper=0 AND ts>? ORDER BY ts", (base["ts"],)):
        if r["side"] == "buy":
            amount -= r["price"] * r["qty"]
        else:
            amount += fees.net_proceeds(r["price"], fee) * r["qty"]
    return {"known": True, "amount": amount, "base": float(base["amount"]), "base_ts": base["ts"]}


def spent_today(db) -> float:
    day = timeutil.day_str(timeutil.now())
    row = db.one("SELECT COALESCE(SUM(price*qty),0) AS s FROM trades "
                 "WHERE paper=0 AND side='buy' AND substr(ts,1,10)=?", (day,))
    return float(row["s"])


def realized_summary(db, fee: float = fees.DEFAULT_FEE) -> dict:
    pos = positions(db, fee)
    n = db.one("SELECT COUNT(*) AS c FROM trades WHERE paper=0")["c"]
    return {"trades": n, "realized": sum(p["realized"] for p in pos.values()),
            "open": {k: v for k, v in pos.items() if v["qty"] > 0}}
