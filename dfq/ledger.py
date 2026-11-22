"""记账：成交、持仓、资金。

两本账，互不影响：
  真实账本（paper=False）：你在游戏里真的成交了的。风控、真实余额、真实盈亏只看这一本。
  模拟账本（paper=True） ：模拟计划（paper_mode）下回复的成交。只用于模拟模式下的计划和复盘。
所有查询函数都必须显式指明看哪一本（默认真实），任何一处都不会把两本账混在一起算。

- 成交来自三处：邮件回复（mail-poll）、命令行（python -m dfq trade ...）和界面（内部也是调用命令行）。
- 持仓 = 该账本所有成交按 (时间, 编号) 顺序、用“移动加权平均成本”累计出来的结果。
- 资金 = 你手动填的余额基准 + 基准之后的成交推算（卖出按实际到手金额计；没填到手金额时按扣费后估算）。
- 期初库存（kind='opening'）：使用本程序之前就有的子弹。只给持仓提供成本依据，不动余额（那笔钱是以前花的）。

记账的两条铁律（发布前审查第 1、2 条）：
1. 卖出不能超过同一本账在那个时刻的持仓。超了就拒绝，一行都不写 —— 否则超卖部分没有成本，会被算成“白赚”的利润，
   风控和计划都会建立在一本假账上。以前就有的库存请用期初库存（record_opening）录入。
2. “查持仓”和“写成交”在同一个数据库写事务里完成（db.write_tx = BEGIN IMMEDIATE）。界面、计划任务、命令行是不同的进程，
   不放在一个事务里，两个进程可以同时查到“够卖”，然后各写一笔 —— 结果超卖。被拒绝或出错时数据库里不留任何痕迹。
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Union

from . import fees, timeutil


class LedgerError(Exception):
    """记账被拒绝或失败。出现它时数据库没有任何改动，错误文字可以直接给用户看。"""


class LedgerBusy(LedgerError):
    """数据库正被另一个 DFQ 进程写入，等了一会儿还没轮到。什么都没写，稍后重试即可。"""


BUSY_TEXT = "数据库正被另一个 DFQ 进程占用，这笔没有记账，请稍后再试"

# fee 参数可以是一个费率，也可以是“品种名 → 费率”的函数（不同品种费率不同时用，如 cfg.fee_for）
FeeArg = Union[float, Callable[[str], float]]

# 合理性上限：超过它几乎肯定是输入错误（比如把数量和总价写在了一起）
MAX_QTY = 1_000_000
MAX_PRICE = 100_000_000


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

# ------------------------------------------------------------------ 内部工具
def _book(paper: bool) -> str:
    return "模拟账本" if paper else "真实账本"


def _fee_fn(fee: FeeArg) -> Callable[[str], float]:
    """把 fee 参数统一成“品种名 → 费率”的函数。"""
    if callable(fee):
        return fee
    rate = float(fee)
    return lambda _name: rate


def _is_num(x) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(float(x))


def _check_qty_price(qty, price, what: str = "价格") -> None:
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in (qty, price)):
        raise LedgerError(f"数量和{what}必须是数字")
    try:
        ok = math.isfinite(float(qty)) and math.isfinite(float(price)) and float(qty) == int(qty)
    except (TypeError, ValueError, OverflowError):
        ok = False
    if not ok or qty <= 0 or price <= 0:
        raise LedgerError(f"数量必须是正整数，{what}必须是正数")
    if qty > MAX_QTY or price > MAX_PRICE:
        raise LedgerError(f"数字大得不合理（数量上限 {MAX_QTY:,}，单价上限 {MAX_PRICE:,}），多半是写错了，没有记账")


def _check_item(item) -> None:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
        raise LedgerError("没有指明品种，没有记账")


def _run_tx(db, fn, undone: str = "这笔没有记账"):
    """在一个写事务里执行 fn()：fn 里的“检查”和“写入”要么一起生效，要么什么都不留下。

    fn 返回 (结果, 拒绝原因)。有拒绝原因时 fn 不应写任何东西；这里在事务**正常结束之后**再抛 LedgerError，
    这样外层调用者自己的事务（比如邮件一封信记多笔）不会因为一次“干净的拒绝”被整体判死，由它自己决定要不要整体回滚。
    数据库出错（拿不到锁、约束冲突）则在事务里抛出 → 整个事务回滚，再转成给人看的 LedgerError。
    """
    try:
        with db.write_tx():
            result, problem = fn()
    except sqlite3.IntegrityError as e:
        raise LedgerError(f"这笔和已有记录重复（同一封邮件里的同一笔只能记一次），{undone}") from e
    except sqlite3.OperationalError as e:
        text = str(e).lower()
        if "locked" in text or "busy" in text:
            raise LedgerBusy(BUSY_TEXT if undone == "这笔没有记账" else f"数据库正被另一个 DFQ 进程占用，{undone}，请稍后再试") from e
        raise LedgerError(f"数据库写入失败（{e}），{undone}，请稍后再试") from e
    if problem:
        raise LedgerError(problem)
    return result


_NEW_ID = 1 << 62      # 还没写进去的那一笔：编号比任何已有记录都大（真正写入时它拿到的就是最大的编号）


def _book_rows(db, name: str, paper: bool) -> list[dict]:
    """某本账里某个品种的全部成交，按 (时间, 编号) 排好 —— 和 positions() 累计的顺序完全一致。"""
    return [dict(r) for r in db.query(
        "SELECT id, ts, side, qty, kind FROM trades WHERE paper=? AND name=? ORDER BY ts, id",
        (1 if paper else 0, name))]


def _simulate(rows: list[dict]) -> tuple[dict[int, int], dict[int, int]]:
    """按时间线走一遍，返回 ({卖出编号: 没有持仓可卖的数量}, {卖出编号: 卖出前一刻的持仓})。

    和 positions() 的口径一致：持仓不会被算成负数，卖出时不够的部分记为“没有成本依据”（旧版本留下的超卖）。
    """
    held = 0
    uncovered: dict[int, int] = {}
    before: dict[int, int] = {}
    for r in rows:
        if r["side"] == "buy":
            held += int(r["qty"])
        else:
            sold = min(int(r["qty"]), held)
            before[r["id"]] = held
            uncovered[r["id"]] = int(r["qty"]) - sold
            held -= sold
    return uncovered, before


def _desc(r: dict) -> str:
    what = "期初库存" if r.get("kind") == "opening" else ("买入" if r["side"] == "buy" else "卖出")
    return f"#{r['id']}（{str(r['ts'])[:16]} {what} ×{r['qty']}）"


def _inventory_hint(name: str, qty: int | None, paper: bool, before_ts: str | None = None) -> str:
    """告诉用户怎么录入期初库存的那一句命令。before_ts：期初库存必须早于这个时间才能给那笔卖出提供成本。"""
    cmd = f'python -m dfq inventory add "{name}" {qty if qty else "数量"} 成本单价'
    if before_ts:
        at = timeutil.parse_dt(before_ts) - timedelta(minutes=1)
        cmd += f' --at "{timeutil.fmt_min(at)}"'
    return cmd + (" --paper" if paper else "")


def _sell_problem(db, name: str, paper: bool, when: str, qty: int, backdated: bool) -> str | None:
    """把这笔卖出放进时间线后，任何时刻都必须有足够的持仓。返回拒绝原因（None = 可以记）。"""
    rows = _book_rows(db, name, paper)
    unc_before, _ = _simulate(rows)
    new = {"id": _NEW_ID, "ts": when, "side": "sell", "qty": int(qty), "kind": "trade"}
    timeline = sorted(rows + [new], key=lambda r: (str(r["ts"]), r["id"]))
    unc_after, held_before = _simulate(timeline)
    book = _book(paper)
    if unc_after[_NEW_ID] > 0:
        held = held_before[_NEW_ID]
        at = f"（按成交时间 {when[:16]} 计算）" if backdated else ""
        return (f"卖出 {qty} 发超过{book}里的持仓 {held} 发{at}，这笔没有记账。"
                f"如果这些子弹是使用本程序之前就有的库存，请先录入期初库存"
                f"（{_inventory_hint(name, qty - held, paper, when if backdated else None)}），再记这笔卖出。")
    broken = [r for r in rows if r["side"] == "sell" and unc_after[r["id"]] > unc_before[r["id"]]]
    if broken:
        return (f"按成交时间 {when[:16]} 补记这笔卖出后，后面的卖出 {'、'.join(_desc(r) for r in broken[:5])} "
                f"在{book}里就没有足够的持仓了，这笔没有记账。请核对成交时间和数量。")
    return None


# ------------------------------------------------------------------ 写入
def record_trade(db, side: str, item: dict, qty: int, price: float, *, source: str = "cli",
                 ts: datetime | None = None, message_id: str | None = None,
                 paper: bool = False, note: str | None = None, net: float | None = None,
                 mail_seq: int | None = None) -> int:
    """记一笔成交，返回编号。被拒绝时抛 LedgerError，数据库没有任何改动。

    net：卖出时实际到手的总金额（游戏里扣完手续费后真正收到的钱）。填了就用它算余额和盈亏，不再按费率估算。
    mail_seq：来自邮件时，这是那封信里的第几笔（和 message_id 一起保证同一封信不会记两遍）。
    """
    if side not in ("buy", "sell"):
        raise LedgerError("方向只能是 buy 或 sell")
    _check_item(item)
    _check_qty_price(qty, price)
    if net is not None:
        if side != "sell":
            raise LedgerError("实际到手金额只对卖出有意义，买入请不要填")
        if not _is_num(net) or net <= 0:
            raise LedgerError("实际到手金额必须是正数")
        if net > float(price) * int(qty) + 1e-6:
            raise LedgerError(f"实际到手金额 {net:,.0f} 比 卖价×数量 = {float(price) * int(qty):,.0f} 还多，多半是写错了，没有记账")
    when = timeutil.fmt(ts or timeutil.now())
    name = item["name"]

    def write():
        if side == "sell":
            problem = _sell_problem(db, name, paper, when, int(qty), backdated=ts is not None)
            if problem:
                return None, problem
        cur = db.execute(
            "INSERT INTO trades(ts,side,oid,name,qty,price,source,message_id,paper,note,kind,net,mail_seq) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (when, side, item.get("oid"), name, int(qty), float(price), source, message_id,
             1 if paper else 0, note, "trade", None if net is None else float(net), mail_seq))
        return cur.lastrowid, None
    return _run_tx(db, write)


def record_opening(db, item: dict, qty: int, cost: float, *, paper: bool = False,
                   ts: datetime | None = None, note: str | None = None) -> int:
    """录入期初库存（使用本程序之前就有的子弹），返回编号。

    - 只给持仓提供成本依据，不动余额、不算今天的花费：那笔钱是用本程序之前花的，余额基准里早已不含它。
    - cost 必须是正数。不知道当初的成本就填录入时的市价 —— 不能填 0，否则以后卖出的全部收入都会被算成“利润”。
    - ts 可以填更早的时间：用来给“旧版本记下的、当时没有持仓的卖出”补上成本依据（见 integrity_report）。
    撤销用 delete_trade（命令行 trade-undo 编号）。
    """
    _check_item(item)
    _check_qty_price(qty, cost, what="成本单价")
    when = timeutil.fmt(ts or timeutil.now())

    def write():
        cur = db.execute(
            "INSERT INTO trades(ts,side,oid,name,qty,price,source,message_id,paper,note,kind) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (when, "buy", item.get("oid"), item["name"], int(qty), float(cost), "inventory", None,
             1 if paper else 0, note, "opening"))
        return cur.lastrowid, None
    return _run_tx(db, write)


def delete_trade(db, trade_id: int) -> dict:
    """撤销一笔记错的成交（或期初库存），返回被删掉的那条记录。找不到或不能撤销时抛 LedgerError。

    持仓、余额都是由成交记录现算出来的，所以删掉这一条，账面就恢复到没记它之前。
    但撤销一笔买入后，后面依赖它的卖出就“卖了不存在的子弹” —— 这种撤销会被拒绝，并指出要先撤销哪几笔卖出。
    """
    if isinstance(trade_id, bool) or not isinstance(trade_id, int) or trade_id <= 0:
        raise LedgerError("成交编号必须是正整数")

    def write():
        row = db.one("SELECT * FROM trades WHERE id=?", (trade_id,))
        if row is None:
            return None, f"没有编号为 {trade_id} 的成交"
        row = dict(row)
        if row["side"] == "buy":
            paper = bool(row["paper"])
            rows = _book_rows(db, row["name"], paper)
            unc_before, _ = _simulate(rows)
            unc_after, _ = _simulate([r for r in rows if r["id"] != trade_id])
            broken = [r for r in rows if r["side"] == "sell" and unc_after[r["id"]] > unc_before[r["id"]]]
            if broken:
                ids = " ".join(str(r["id"]) for r in broken)
                return None, (f"撤销 {_desc(row)} 后，后面的卖出 {'、'.join(_desc(r) for r in broken[:5])} 在{_book(paper)}里"
                              f"就没有足够的持仓了，所以没有撤销。如果确实要撤销它，请先撤销这几笔卖出"
                              f"（python -m dfq trade-undo {ids}，一次一笔），再撤销 #{trade_id}。")
        db.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        return row, None
    return _run_tx(db, write, undone="没有撤销")


# ------------------------------------------------------------------ 查询
def positions(db, fee: FeeArg = fees.DEFAULT_FEE, *, paper: bool = False) -> dict[str, dict]:
    """某一本账的持仓（默认真实账本）：

    {名称: {oid, qty, avg_cost, realized, last_buy_at, held_since, uncovered_qty}}
    - held_since：当前这段持仓从哪一刻开始（持仓从 0 变成正数的那笔买入的时间）；清仓后为 None。
    - uncovered_qty：旧版本记下的、当时账本里没有持仓可卖的卖出数量。这部分没有成本依据，**不计盈亏**
      （以前把它当“零成本”算成了利润）；录入时间更早的期初库存即可补上（见 integrity_report）。
    - 卖出填了实际到手金额（net）的，盈亏用它算；没填的按“卖价 ×(1 − 费率)”估算。fee 可以是数或“品种名 → 费率”的函数。
    """
    fee_of = _fee_fn(fee)
    pos: dict[str, dict] = {}
    for r in db.query("SELECT * FROM trades WHERE paper=? ORDER BY ts, id", (1 if paper else 0,)):
        p = pos.setdefault(r["name"], {"oid": r["oid"], "qty": 0, "avg_cost": 0.0, "realized": 0.0,
                                       "last_buy_at": None, "held_since": None, "uncovered_qty": 0})
        if r["side"] == "buy":                      # 普通买入和期初库存都提供成本
            if p["qty"] == 0:
                p["held_since"] = r["ts"]
            total = p["avg_cost"] * p["qty"] + r["price"] * r["qty"]
            p["qty"] += r["qty"]
            p["avg_cost"] = total / p["qty"]
            p["last_buy_at"] = r["ts"]
        else:
            sold = min(r["qty"], p["qty"])
            if r["net"] is not None:
                per = float(r["net"]) / r["qty"]
            else:
                per = fees.net_proceeds(r["price"], fee_of(r["name"]))
            p["realized"] += (per - p["avg_cost"]) * sold
            p["uncovered_qty"] += r["qty"] - sold      # 没有成本依据的部分：不计盈亏（钱照样进余额，见 balance）
            p["qty"] -= sold
            if p["qty"] == 0:
                p["avg_cost"] = 0.0
                p["held_since"] = None
    return pos


def open_positions(db, fee: FeeArg = fees.DEFAULT_FEE, *, paper: bool = False) -> dict[str, dict]:
    return {k: v for k, v in positions(db, fee, paper=paper).items() if v["qty"] > 0}


def openings(db, *, paper: bool = False) -> list[dict]:
    """某本账里录入过的期初库存（新的在前）。"""
    return [dict(r) for r in db.query(
        "SELECT * FROM trades WHERE paper=? AND kind='opening' ORDER BY ts DESC, id DESC", (1 if paper else 0,))]


def integrity_report(db, *, paper: bool = False) -> list[str]:
    """逐条说明这本账里哪一笔卖出有多少发没有成本依据（旧版本允许超卖时记下的），以及怎么补。没有问题时返回空列表。"""
    out: list[str] = []
    names = [r["name"] for r in db.query(
        "SELECT DISTINCT name FROM trades WHERE paper=? AND side='sell' ORDER BY name", (1 if paper else 0,))]
    for name in names:
        rows = _book_rows(db, name, paper)
        unc, _ = _simulate(rows)
        for r in rows:
            u = unc.get(r["id"], 0) if r["side"] == "sell" else 0
            if u > 0:
                out.append(f"{_book(paper)} #{r['id']}（{str(r['ts'])[:16]} 卖出 {name} ×{r['qty']}）里有 {u} 发在当时没有持仓，"
                           f"没有成本依据，这部分不计盈亏（卖出款照常计入余额）。如果它们是使用本程序之前就有的库存，"
                           f"请录入一笔时间更早的期初库存：{_inventory_hint(name, u, paper, str(r['ts']))}")
    return out


def set_balance(db, amount: float) -> None:
    """设置余额基准：此刻游戏里的哈夫币余额。之后的成交在它的基础上加减。"""
    if not _is_num(amount) or amount < 0:
        raise LedgerError("余额必须是不小于 0 的有限数字")

    def write():
        # after_id：设置这一刻已经存在的最大成交编号。和基准同一秒、但在它之后才记的成交仍然要算进余额
        last = db.one("SELECT COALESCE(MAX(id), 0) AS m FROM trades")["m"]
        db.kv_set("balance_base", {"amount": float(amount), "ts": timeutil.fmt(timeutil.now()), "after_id": int(last)})
        return None, None
    _run_tx(db, write, undone="余额没有设置")


def _after_base(rows, base: dict):
    """只留下余额基准之后的成交：时间晚于基准；或同一秒、编号大于设置基准时已有的最大编号（旧数据没有编号的按时间严格晚于）。"""
    after_id = base.get("after_id")
    for r in rows:
        if r["ts"] > base["ts"] or (r["ts"] == base["ts"] and after_id is not None and r["id"] > after_id):
            yield r


def balance(db, fee: FeeArg = fees.DEFAULT_FEE, *, paper: bool = False) -> dict:
    """返回 {known, amount, base, base_ts}。amount = 基准 + 基准之后“该账本”成交的现金流。

    paper=False：真实余额，只算真实成交 —— 模拟成交永远不会改变它。
    paper=True ：模拟资金，从同一个基准出发，只算模拟成交。
    期初库存不动余额；卖出填了实际到手金额的按它计，没填的按“卖价 ×(1 − 费率)”估算。
    旧版本留下的超卖：钱确实收到了，所以照常计入余额（只是不计盈亏，见 positions）。
    """
    base = db.kv_get("balance_base")
    if not base:
        return {"known": False, "amount": None, "base": None, "base_ts": None}
    fee_of = _fee_fn(fee)
    amount = float(base["amount"])
    rows = db.query("SELECT * FROM trades WHERE paper=? AND kind!='opening' AND ts>=? ORDER BY ts, id",
                    (1 if paper else 0, base["ts"]))
    for r in _after_base(rows, base):
        if r["side"] == "buy":
            amount -= r["price"] * r["qty"]
        elif r["net"] is not None:
            amount += float(r["net"])
        else:
            amount += fees.net_proceeds(r["price"], fee_of(r["name"])) * r["qty"]
    return {"known": True, "amount": amount, "base": float(base["amount"]), "base_ts": base["ts"]}


def spent_today(db, *, paper: bool = False) -> float:
    """今天买入花掉的钱（期初库存不算：那不是今天花的）。"""
    day = timeutil.day_str(timeutil.now())
    row = db.one("SELECT COALESCE(SUM(price*qty),0) AS s FROM trades "
                 "WHERE paper=? AND side='buy' AND kind!='opening' AND substr(ts,1,10)=?", (1 if paper else 0, day))
    return float(row["s"])


def realized_summary(db, fee: FeeArg = fees.DEFAULT_FEE, *, paper: bool = False) -> dict:
    """{trades: 成交笔数（不含期初库存）, openings: 期初库存条数, realized: 已实现盈亏, uncovered_qty: 没有成本依据的卖出数量, open}"""
    pos = positions(db, fee, paper=paper)
    p = 1 if paper else 0
    n = db.one("SELECT COUNT(*) AS c FROM trades WHERE paper=? AND kind!='opening'", (p,))["c"]
    n_open = db.one("SELECT COUNT(*) AS c FROM trades WHERE paper=? AND kind='opening'", (p,))["c"]
    return {"trades": n, "openings": n_open, "realized": sum(v["realized"] for v in pos.values()),
            "uncovered_qty": sum(v["uncovered_qty"] for v in pos.values()),
            "open": {k: v for k, v in pos.items() if v["qty"] > 0}}
