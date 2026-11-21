"""账本完整性（发布前审查第 1、2 条）：超卖一律拒绝、按时间线校验、期初库存、实际到手金额、写事务与并发。

修之前的行为（这些测试在旧代码上都会失败）：
- 卖出超过持仓只在备注里写一句，照样 INSERT；positions() 把超卖部分按“零成本”算成利润；
- “查持仓”和“写成交”是两步，两个进程可以同时查到“够卖”，各写一笔。
"""
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dfq import ledger, timeutil
from dfq.db import DB, TransactionAborted

PS12B = {"name": "12.7x55mm PS12B", "oid": 626}
SS190 = {"name": "5.7x28mm SS190", "oid": 640}
T0 = datetime(2026, 11, 13, 21, 30)
REPO = Path(__file__).resolve().parent.parent


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def rows(db) -> int:
    return db.one("SELECT COUNT(*) AS c FROM trades")["c"]


def state(db) -> dict:
    """两本账全部可观察的状态：失败的操作前后必须完全一样。"""
    out = {}
    for paper in (False, True):
        out[paper] = {
            "balance": ledger.balance(db, paper=paper),
            "positions": ledger.positions(db, paper=paper),
            "realized": ledger.realized_summary(db, paper=paper),
            "spent": ledger.spent_today(db, paper=paper),
            "report": ledger.integrity_report(db, paper=paper),
        }
    out["rows"] = [tuple(r) for r in db.query("SELECT * FROM trades ORDER BY id")]
    out["kv"] = db.kv_get("balance_base")
    return out


# ------------------------------------------------------------------ 超卖拒绝
@pytest.mark.parametrize("paper", [False, True])
def test_oversell_is_rejected_and_nothing_is_written(db, paper):
    ledger.set_balance(db, 1_000_000)
    timeutil.set_now(at(1))
    ledger.record_trade(db, "buy", PS12B, 200, 3000, paper=paper)
    before = state(db)
    book = "模拟账本" if paper else "真实账本"
    with pytest.raises(ledger.LedgerError, match=f"卖出 300 发超过{book}里的持仓 200 发") as ei:
        ledger.record_trade(db, "sell", PS12B, 300, 4000, paper=paper)
    assert "inventory add" in str(ei.value) and "这笔没有记账" in str(ei.value)
    assert state(db) == before                                         # 没有新行、余额 / 持仓 / 盈亏都没变
    with pytest.raises(ledger.LedgerError, match="持仓 0 发"):
        ledger.record_trade(db, "sell", SS190, 1, 4000, paper=paper)    # 从来没买过
    assert state(db) == before
    ledger.record_trade(db, "sell", PS12B, 200, 4000, paper=paper)    # 正好卖完可以
    assert ledger.positions(db, paper=paper)[PS12B["name"]]["qty"] == 0


def test_backdated_sell_is_checked_against_the_timeline(db):
    """补记一笔时间更早的卖出：只比总数会放行，但那一刻其实还没有持仓 / 会让后面那笔卖出没有持仓。"""
    timeutil.set_now(at(100))
    ledger.record_trade(db, "buy", PS12B, 100, 3000, ts=at(10))
    later = ledger.record_trade(db, "sell", PS12B, 100, 4000, ts=at(30))
    ledger.record_trade(db, "buy", PS12B, 100, 3000, ts=at(50))        # 现在总持仓 100
    before = state(db)
    with pytest.raises(ledger.LedgerError, match="按成交时间 2026-11-13 21:35 计算"):
        ledger.record_trade(db, "sell", PS12B, 50, 4000, ts=at(5))      # 那时一发都没有
    with pytest.raises(ledger.LedgerError, match=f"#{later}"):
        ledger.record_trade(db, "sell", PS12B, 50, 4000, ts=at(20))     # 那时够，但会让 #later 不够
    assert state(db) == before
    ledger.record_trade(db, "sell", PS12B, 50, 4000, ts=at(60))         # 放在第二次买入之后就没问题
    assert ledger.positions(db)[PS12B["name"]]["qty"] == 50


def test_undo_buy_that_a_later_sell_depends_on_is_rejected(db):
    b1 = ledger.record_trade(db, "buy", PS12B, 100, 3000)
    timeutil.set_now(at(1))
    s1 = ledger.record_trade(db, "sell", PS12B, 60, 4000)
    before = state(db)
    with pytest.raises(ledger.LedgerError, match=f"trade-undo {s1}") as ei:
        ledger.delete_trade(db, b1)
    assert "没有撤销" in str(ei.value)
    assert state(db) == before
    assert ledger.delete_trade(db, s1)["id"] == s1                       # 撤销卖出永远可以
    assert ledger.delete_trade(db, b1)["id"] == b1                       # 然后再撤销买入
    assert ledger.positions(db) == {} and rows(db) == 0
    with pytest.raises(ledger.LedgerError, match="没有编号"):
        ledger.delete_trade(db, b1)


def test_undo_buy_is_fine_when_other_buys_still_cover_the_sells(db):
    b1 = ledger.record_trade(db, "buy", PS12B, 100, 3000)
    timeutil.set_now(at(1))
    ledger.record_trade(db, "buy", PS12B, 100, 3000)
    timeutil.set_now(at(2))
    ledger.record_trade(db, "sell", PS12B, 100, 4000)
    ledger.delete_trade(db, b1)
    assert ledger.positions(db)[PS12B["name"]]["qty"] == 0


# ------------------------------------------------------------------ 期初库存
def test_opening_inventory_gives_cost_but_does_not_touch_cash(db):
    ledger.set_balance(db, 1_000_000)
    timeutil.set_now(at(1))
    oid = ledger.record_opening(db, PS12B, 200, 3000, note="开始用程序前就有")
    assert ledger.balance(db)["amount"] == 1_000_000 and ledger.spent_today(db) == 0
    p = ledger.positions(db)[PS12B["name"]]
    assert p["qty"] == 200 and p["avg_cost"] == 3000 and p["held_since"] == timeutil.fmt(at(1))
    row = db.one("SELECT * FROM trades WHERE id=?", (oid,))
    assert (row["side"], row["kind"], row["source"], row["paper"]) == ("buy", "opening", "inventory", 0)
    assert ledger.realized_summary(db)["trades"] == 0 and ledger.realized_summary(db)["openings"] == 1
    assert [r["id"] for r in ledger.openings(db)] == [oid] and ledger.openings(db, paper=True) == []
    timeutil.set_now(at(2))
    ledger.record_trade(db, "sell", PS12B, 200, 4000)
    assert ledger.positions(db)[PS12B["name"]]["realized"] == pytest.approx((4000 * 0.87 - 3000) * 200)
    assert ledger.balance(db)["amount"] == pytest.approx(1_000_000 + 4000 * 0.87 * 200)


@pytest.mark.parametrize("cost", [0, -1, float("nan"), float("inf"), True, "3000", 200_000_000])
def test_opening_cost_must_be_a_sane_positive_number(db, cost):
    with pytest.raises(ledger.LedgerError):
        ledger.record_opening(db, PS12B, 100, cost)
    assert rows(db) == 0


@pytest.mark.parametrize("qty", [0, -5, 1.5, True, 2_000_000])
def test_opening_qty_must_be_a_positive_integer(db, qty):
    with pytest.raises(ledger.LedgerError):
        ledger.record_opening(db, PS12B, qty, 3000)
    assert rows(db) == 0


def test_opening_inventory_in_paper_book_stays_there(db):
    ledger.record_opening(db, PS12B, 100, 3000, paper=True)
    assert ledger.positions(db) == {}
    with pytest.raises(ledger.LedgerError):
        ledger.record_trade(db, "sell", PS12B, 10, 4000)
    ledger.record_trade(db, "sell", PS12B, 10, 4000, paper=True)


# ------------------------------------------------------------------ 实际到手金额 / 费率参数 / held_since
def test_net_amount_is_used_for_cash_and_profit(db):
    ledger.set_balance(db, 1_000_000)
    timeutil.set_now(at(1))
    ledger.record_trade(db, "buy", PS12B, 100, 3000)
    ledger.record_trade(db, "sell", PS12B, 40, 4000, net=140_000)      # 实际费率 12.5%，不是按 13% 估
    p = ledger.positions(db)[PS12B["name"]]
    assert p["realized"] == pytest.approx(140_000 - 40 * 3000)
    assert ledger.balance(db)["amount"] == pytest.approx(1_000_000 - 300_000 + 140_000)
    assert db.one("SELECT net FROM trades WHERE side='sell'")["net"] == 140_000


@pytest.mark.parametrize("side,net,msg", [
    ("sell", 160_001, "还多"), ("sell", 0, "正数"), ("sell", -1, "正数"), ("sell", float("nan"), "正数"),
    ("sell", True, "正数"), ("buy", 100, "只对卖出"),
])
def test_bad_net_amount_is_rejected(db, side, net, msg):
    ledger.record_trade(db, "buy", PS12B, 100, 3000)
    before = state(db)
    with pytest.raises(ledger.LedgerError, match=msg):
        ledger.record_trade(db, side, PS12B, 40, 4000, net=net)
    assert state(db) == before


def test_fee_can_be_a_per_item_function(db):
    ledger.set_balance(db, 0)
    timeutil.set_now(at(1))
    ledger.record_trade(db, "buy", PS12B, 10, 1000)
    ledger.record_trade(db, "buy", SS190, 10, 1000)
    ledger.record_trade(db, "sell", PS12B, 10, 2000)
    ledger.record_trade(db, "sell", SS190, 10, 2000)
    rate = {PS12B["name"]: 0.10, SS190["name"]: 0.20}.get
    pos = ledger.positions(db, rate)
    assert pos[PS12B["name"]]["realized"] == pytest.approx((1800 - 1000) * 10)
    assert pos[SS190["name"]]["realized"] == pytest.approx((1600 - 1000) * 10)
    assert ledger.balance(db, rate)["amount"] == pytest.approx(-20_000 + 18_000 + 16_000)
    assert ledger.realized_summary(db, rate)["realized"] == pytest.approx(8000 + 6000)
    assert ledger.positions(db, 0.13)[PS12B["name"]]["realized"] == pytest.approx((1740 - 1000) * 10)


def test_held_since_marks_when_the_current_holding_started(db):
    ledger.record_trade(db, "buy", PS12B, 100, 3000, ts=at(0))
    ledger.record_trade(db, "buy", PS12B, 100, 3200, ts=at(10))
    p = ledger.positions(db)[PS12B["name"]]
    assert p["held_since"] == timeutil.fmt(at(0)) and p["last_buy_at"] == timeutil.fmt(at(10))
    ledger.record_trade(db, "sell", PS12B, 150, 4000, ts=at(20))
    assert ledger.positions(db)[PS12B["name"]]["held_since"] == timeutil.fmt(at(0))   # 部分卖出不重置
    ledger.record_trade(db, "sell", PS12B, 50, 4000, ts=at(30))
    p = ledger.positions(db)[PS12B["name"]]
    assert p["qty"] == 0 and p["held_since"] is None and p["uncovered_qty"] == 0
    ledger.record_trade(db, "buy", PS12B, 10, 3000, ts=at(40))
    assert ledger.positions(db)[PS12B["name"]]["held_since"] == timeutil.fmt(at(40))


def test_trade_in_the_same_second_as_balance_base_is_counted(db):
    """余额基准和成交都按“秒”记时间：同一秒里先设基准、后记成交，这笔也要算进余额（以前会被漏掉）。"""
    ledger.record_trade(db, "buy", PS12B, 10, 1000)            # 同一秒、在设基准之前记的：已经包含在基准里
    ledger.set_balance(db, 500_000)
    ledger.record_trade(db, "buy", PS12B, 10, 2000)            # 同一秒、在设基准之后记的
    assert ledger.balance(db)["amount"] == 500_000 - 20_000


def test_old_balance_base_without_after_id_keeps_old_rule(db):
    ledger.record_trade(db, "buy", PS12B, 10, 1000)
    db.kv_set("balance_base", {"amount": 1000.0, "ts": timeutil.fmt(timeutil.now())})
    assert ledger.balance(db)["amount"] == 1000.0


# ------------------------------------------------------------------ 旧版本留下的超卖
def _legacy_sell(db, qty, price, ts, paper=0, name=PS12B["name"]):
    """模拟旧版本直接写进去的超卖记录（新代码不会再产生这种行）。"""
    return db.execute("INSERT INTO trades(ts,side,oid,name,qty,price,source,paper,note) VALUES(?,?,?,?,?,?,?,?,?)",
                      (timeutil.fmt(ts), "sell", 626, name, qty, price, "mail", paper,
                       "卖出数量 100 超过账面持仓 0（可能有未记账的买入）")).lastrowid


def test_legacy_oversell_is_not_profit_and_is_reported(db):
    ledger.set_balance(db, 1_000_000)
    timeutil.set_now(at(60))
    sid = _legacy_sell(db, 100, 5000, at(10))
    p = ledger.positions(db)[PS12B["name"]]
    assert p["realized"] == 0 and p["uncovered_qty"] == 100 and p["qty"] == 0       # 以前这里是 +435,000 的“利润”
    assert ledger.balance(db)["amount"] == pytest.approx(1_000_000 + 5000 * 0.87 * 100)   # 钱确实收到了
    assert ledger.realized_summary(db)["uncovered_qty"] == 100
    report = ledger.integrity_report(db)
    assert len(report) == 1 and f"#{sid}" in report[0] and "100 发在当时没有持仓" in report[0]
    assert 'inventory add "12.7x55mm PS12B" 100 成本单价 --at "2026-11-13 21:39"' in report[0]
    assert ledger.integrity_report(db, paper=True) == []
    # 旧的坏账不会挡住以后正常的买卖
    ledger.record_trade(db, "buy", PS12B, 50, 3000)
    ledger.record_trade(db, "sell", PS12B, 50, 4000)
    # 按提示补一笔更早的期初库存：成本依据有了，提示消失，余额不变
    bal = ledger.balance(db)["amount"]
    ledger.record_opening(db, PS12B, 100, 3500, ts=at(9))
    p = ledger.positions(db)[PS12B["name"]]
    assert p["uncovered_qty"] == 0 and ledger.integrity_report(db) == []
    assert p["realized"] == pytest.approx((4350 - 3500) * 100 + (3480 - 3000) * 50)
    assert ledger.balance(db)["amount"] == bal


def test_legacy_oversell_in_paper_book_is_reported_there(db):
    _legacy_sell(db, 30, 5000, at(10), paper=1)
    assert ledger.integrity_report(db) == []
    rep = ledger.integrity_report(db, paper=True)
    assert len(rep) == 1 and "模拟账本" in rep[0] and rep[0].endswith("--paper")
    assert ledger.positions(db, paper=True)[PS12B["name"]]["realized"] == 0


# ------------------------------------------------------------------ 事务 / 锁 / 和邮件记账的配合
def test_locked_database_turns_into_a_clear_busy_error(tmp_path):
    path = tmp_path / "t.db"
    a, b = DB(path), DB(path)
    try:
        ledger.record_trade(a, "buy", PS12B, 100, 3000)
        a.conn.execute("PRAGMA busy_timeout=200")                      # 测试里不要等 30 秒
        before = state(a)
        with b.write_tx():                                             # 另一个进程正在写
            for call in (lambda: ledger.record_trade(a, "sell", PS12B, 10, 4000),
                         lambda: ledger.record_trade(a, "buy", PS12B, 10, 4000),
                         lambda: ledger.record_opening(a, PS12B, 10, 4000),
                         lambda: ledger.delete_trade(a, 1),
                         lambda: ledger.set_balance(a, 5)):
                with pytest.raises(ledger.LedgerBusy, match="另一个 DFQ 进程占用"):
                    call()
        assert state(a) == before
        assert str(ledger.LedgerBusy(ledger.BUSY_TEXT)) == "数据库正被另一个 DFQ 进程占用，这笔没有记账，请稍后再试"
    finally:
        a.close()
        b.close()


def test_check_and_write_happen_under_the_same_lock(tmp_path):
    """确定性的交错：B 已经拿到写锁、正在卖出全部持仓；A 此时来卖，必须等 B 提交后再查持仓 → 被拒绝。

    旧代码里 A 先查（B 还没提交，看到 100 发）再写，两笔都会记上。
    """
    path = tmp_path / "t.db"
    seed = DB(path)
    ledger.record_trade(seed, "buy", PS12B, 100, 3000)
    seed.close()
    result: dict = {}

    def seller_a():
        a = DB(path)
        try:
            result["a"] = ledger.record_trade(a, "sell", PS12B, 100, 4100)
        except ledger.LedgerError as e:
            result["a"] = e
        finally:
            a.close()

    b = DB(path)
    try:
        with b.write_tx():
            ledger.record_trade(b, "sell", PS12B, 100, 4000)
            t = threading.Thread(target=seller_a)
            t.start()
            time.sleep(0.5)                       # A 正卡在等锁（查持仓也在锁里，查不到 B 未提交的状态之前的“100 发”）
            assert "a" not in result
        t.join(30)
        assert isinstance(result["a"], ledger.LedgerError) and "持仓 0 发" in str(result["a"])
        assert b.one("SELECT COUNT(*) AS c FROM trades WHERE side='sell'")["c"] == 1
    finally:
        b.close()


def test_clean_rejection_inside_callers_transaction_does_not_doom_it(db):
    """邮件记账会在自己的 write_tx 里逐笔调用 record_trade：一次干净的拒绝不留痕迹，由调用者决定整体要不要回滚。"""
    with db.write_tx():
        ledger.record_trade(db, "buy", PS12B, 10, 3000, source="mail", message_id="<m1>", mail_seq=1)
        with pytest.raises(ledger.LedgerError):
            ledger.record_trade(db, "sell", PS12B, 50, 4000, source="mail", message_id="<m1>", mail_seq=2)
    assert [tuple(r) for r in db.query("SELECT message_id, mail_seq FROM trades")] == [("<m1>", 1)]


def test_duplicate_mail_seq_is_rejected_and_dooms_the_callers_transaction(db):
    ledger.record_trade(db, "buy", PS12B, 10, 3000, source="mail", message_id="<m1>", mail_seq=1)
    with pytest.raises(ledger.LedgerError, match="重复"):
        ledger.record_trade(db, "buy", PS12B, 10, 3000, source="mail", message_id="<m1>", mail_seq=1)
    with pytest.raises(TransactionAborted):
        with db.write_tx():
            ledger.record_trade(db, "buy", PS12B, 5, 3000, source="mail", message_id="<m2>", mail_seq=1)
            try:
                ledger.record_trade(db, "buy", PS12B, 10, 3000, source="mail", message_id="<m1>", mail_seq=1)
            except ledger.LedgerError:
                pass                              # 调用者吞掉了也不行：数据库层面的失败让整封信都不记
    assert rows(db) == 1


SELLER = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from dfq import ledger
from dfq.db import DB
db = DB(sys.argv[2])
start = float(sys.argv[3])
while time.time() < start:
    pass
for name in sys.argv[4:]:
    try:
        tid = ledger.record_trade(db, "sell", {"name": name, "oid": None}, 100, 4000, source="cli")
        print("OK", name, tid, flush=True)
    except ledger.LedgerBusy as e:
        print("BUSY", name, flush=True)
    except ledger.LedgerError as e:
        print("REJECT", name, flush=True)
db.close()
'''


def test_two_processes_selling_everything_at_once_exactly_one_wins(tmp_path):
    """两个独立进程（各自打开同一个数据库文件）同时把同一批持仓全部卖出：每个品种恰好一个成功，持仓不为负。"""
    path = tmp_path / "race.db"
    names = [f"TEST-AMMO-{i}" for i in range(12)]
    db = DB(path)
    for n in names:
        ledger.record_trade(db, "buy", {"name": n, "oid": None}, 100, 3000)
    db.close()
    script = tmp_path / "seller.py"
    script.write_text(SELLER, encoding="utf-8")
    start = time.time() + 2.0
    procs = [subprocess.Popen([sys.executable, str(script), str(REPO), str(path), str(start), *names],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
             for _ in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1] for o in outs]
    lines = [ln.split() for o in outs for ln in o[0].splitlines()]
    assert len(lines) == 2 * len(names)
    for n in names:
        res = sorted(ln[0] for ln in lines if ln[1] == n)
        assert res == ["OK", "REJECT"], (n, res)
    db = DB(path)
    try:
        for n, p in ledger.positions(db).items():
            assert p["qty"] == 0 and p["uncovered_qty"] == 0 and p["realized"] == pytest.approx((3480 - 3000) * 100)
        assert db.one("SELECT COUNT(*) AS c FROM trades WHERE side='sell'")["c"] == len(names)
    finally:
        db.close()


# ------------------------------------------------------------------ 随机操作序列：资金一致性
ITEMS = [PS12B, SS190]


def _expected(model: list[dict], base: float, fee: float = 0.13) -> dict:
    """测试自己的参考实现：只用成功写入的操作推算余额、持仓和已实现盈亏。"""
    out: dict = {}
    for paper in (False, True):
        cash, pos = base, {}
        for r in sorted((m for m in model if m["paper"] == paper), key=lambda m: (m["ts"], m["id"])):
            q, c, real = pos.get(r["name"], (0, 0.0, 0.0))
            if r["side"] == "buy":
                if r["kind"] != "opening":
                    cash -= r["price"] * r["qty"]
                c = (c * q + r["price"] * r["qty"]) / (q + r["qty"])
                q += r["qty"]
            else:
                got = r["net"] if r["net"] is not None else r["price"] * (1 - fee) * r["qty"]
                cash += got
                real += got - c * r["qty"]
                q -= r["qty"]
                assert q >= 0, "参考模型里出现了负持仓：说明账本放进了不该放的卖出"
                if q == 0:
                    c = 0.0
            pos[r["name"]] = (q, c, real)
        out[paper] = (cash, pos)
    return out


def _check_invariants(db, model, base):
    for paper in (False, True):
        for item in ITEMS:                                   # 任何前缀（任何时刻）持仓都不为负
            held = 0
            for r in db.query("SELECT side, qty FROM trades WHERE paper=? AND name=? ORDER BY ts, id",
                              (int(paper), item["name"])):
                held += r["qty"] if r["side"] == "buy" else -r["qty"]
                assert held >= 0
        cash, pos = _expected(model, base)[paper]
        assert ledger.balance(db, paper=paper)["amount"] == pytest.approx(cash, abs=1e-6)
        got = ledger.positions(db, paper=paper)
        for name, (q, c, real) in pos.items():
            assert got[name]["qty"] == q and got[name]["avg_cost"] == pytest.approx(c)
            assert got[name]["realized"] == pytest.approx(real, abs=1e-6) and got[name]["uncovered_qty"] == 0
        assert ledger.integrity_report(db, paper=paper) == []
    assert db.one("SELECT COUNT(*) AS c FROM trades")["c"] == len(model)


@pytest.mark.parametrize("seed", range(6))
def test_random_operations_keep_funds_consistent(db, seed):
    rnd = random.Random(seed)
    base = 5_000_000.0
    ledger.set_balance(db, base)
    now = T0
    model: list[dict] = []
    ok = rejected = 0
    for _ in range(160):
        now += timedelta(minutes=rnd.randint(1, 30))
        timeutil.set_now(now)
        paper = rnd.random() < 0.3
        item = rnd.choice(ITEMS)
        held = sum((m["qty"] if m["side"] == "buy" else -m["qty"]) for m in model
                   if m["paper"] == paper and m["name"] == item["name"])
        kind = rnd.choice(["buy", "buy", "sell", "sell", "sell", "backsell", "opening", "undo", "bad"])
        qty = rnd.randint(1, 150)
        price = float(rnd.randint(1000, 6000))
        ts = None
        net = None
        if kind == "backsell":
            ts = T0 + timedelta(seconds=rnd.randint(61, int((now - T0).total_seconds())))
        if kind in ("sell", "backsell"):
            qty = rnd.randint(1, held + 60)                   # 有一部分会超卖
            if rnd.random() < 0.4:
                net = round(price * qty * rnd.uniform(0.8, 1.0), 2)
        before = state(db)
        try:
            if kind == "buy":
                tid = ledger.record_trade(db, "buy", item, qty, price, paper=paper)
                model.append(dict(id=tid, ts=timeutil.fmt(now), side="buy", kind="trade", name=item["name"],
                                  qty=qty, price=price, net=None, paper=paper))
            elif kind in ("sell", "backsell"):
                tid = ledger.record_trade(db, "sell", item, qty, price, paper=paper, ts=ts, net=net)
                model.append(dict(id=tid, ts=timeutil.fmt(ts or now), side="sell", kind="trade", name=item["name"],
                                  qty=qty, price=price, net=net, paper=paper))
            elif kind == "opening":
                ts = T0 + timedelta(seconds=rnd.randint(61, int((now - T0).total_seconds())))
                tid = ledger.record_opening(db, item, qty, price, paper=paper, ts=ts)
                model.append(dict(id=tid, ts=timeutil.fmt(ts), side="buy", kind="opening", name=item["name"],
                                  qty=qty, price=price, net=None, paper=paper))
            elif kind == "undo":
                if not model:
                    continue
                victim = rnd.choice(model)
                ledger.delete_trade(db, victim["id"])
                model.remove(victim)
            else:
                bad = rnd.choice([(0, price, None), (qty, -1.0, None), (qty, price, price * qty * 1.5)])
                ledger.record_trade(db, "sell", item, bad[0], bad[1], paper=paper, net=bad[2])
                raise AssertionError("非法输入居然被接受了")
            ok += 1
        except ledger.LedgerError:
            rejected += 1
            assert state(db) == before                        # 失败的操作：余额、持仓、成交数、盈亏、行内容全都没变
        _check_invariants(db, model, base)
    assert ok > 40 and rejected > 20                          # 两类操作都真的被大量演练到了


# ------------------------------------------------------------------ 命令行
@pytest.fixture
def cli(tmp_path, capsys):
    from dfq.cli import main
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    base = ["--config", str(cfgp)]

    def run(*argv):
        code = main([*argv, *base])
        return code, capsys.readouterr().out
    run.db_path = tmp_path / "data" / "dfq.db"
    return run


def test_cli_inventory_trade_net_and_oversell(cli):
    assert cli("balance", "set", "1000000")[0] == 0
    code, out = cli("inventory", "list")
    assert code == 0 and "还没有期初库存" in out
    code, out = cli("inventory", "add", "PS12B", "200", "3000", "--note", "以前就有")
    assert code == 0 and "已录入期初库存 #1" in out and "不动余额" in out and "trade-undo 1" in out
    code, out = cli("trade", "sell", "PS12B", "300", "4000")
    assert code == 2 and "卖出 300 发超过真实账本里的持仓 200 发，这笔没有记账" in out and "inventory add" in out
    code, out = cli("trade", "sell", "PS12B", "200", "4000", "--net", "700000")
    assert code == 0 and "实际到手 700,000" in out
    code, out = cli("trade", "buy", "PS12B", "10", "3000", "--net", "1")
    assert code == 2 and "只对卖出" in out
    code, out = cli("balance")
    assert code == 0 and "1,700,000" in out and "账本提示" not in out
    code, out = cli("inventory", "list")
    assert "#1" in out and "12.7x55mm PS12B ×200" in out and "以前就有" in out
    code, out = cli("trade-undo", "1")
    assert code == 2 and "没有撤销" in out and "trade-undo 2" in out
    assert cli("inventory", "list", "--paper")[1].startswith("模拟账本里还没有期初库存")
    code, out = cli("inventory", "add", "PS12B", "10", "0")
    assert code == 2 and "没有录入" in out
    code, out = cli("inventory", "add", "PS12B")
    assert code == 2 and "用法" in out
    db = DB(cli.db_path)
    try:
        assert db.one("SELECT COUNT(*) AS c FROM trades")["c"] == 2
    finally:
        db.close()


def test_cli_status_and_balance_show_legacy_oversell(cli):
    assert cli("balance", "set", "1000000")[0] == 0
    db = DB(cli.db_path)
    try:
        _legacy_sell(db, 100, 5000, at(10))
    finally:
        db.close()
    for cmd in (("status",), ("balance",), ("inventory", "list")):
        code, out = cli(*cmd)
        assert code == 0 and "没有成本依据" in out and 'inventory add "12.7x55mm PS12B" 100 成本单价 --at' in out, cmd


def test_cli_busy_database_exits_3(cli, monkeypatch):
    def busy(*a, **k):
        raise ledger.LedgerBusy(ledger.BUSY_TEXT)
    monkeypatch.setattr(ledger, "record_trade", busy)
    monkeypatch.setattr(ledger, "record_opening", busy)
    monkeypatch.setattr(ledger, "delete_trade", busy)
    code, out = cli("trade", "buy", "PS12B", "10", "3000")
    assert code == 3 and "另一个 DFQ 进程占用" in out
    assert cli("inventory", "add", "PS12B", "10", "3000")[0] == 3
    assert cli("trade-undo", "1")[0] == 3
