"""数据库写事务（DB.write_tx）：先拿写锁、读-检查-写一起提交、失败不留痕迹。记账的原子性建立在它上面。"""
import sqlite3
import threading

import pytest

from dfq.db import DB, TransactionAborted


def _count(db, key):
    return db.one("SELECT COUNT(*) c FROM kv WHERE key=?", (key,))["c"]


def test_commit_on_success_and_helpers_do_not_commit_early(tmp_path):
    path = tmp_path / "t.db"
    a, b = DB(path), DB(path)
    try:
        with a.write_tx():
            a.execute("INSERT INTO kv(key,value) VALUES('k1','1')")
            a.kv_set("k2", {"x": 1})
            a.executemany("INSERT INTO kv(key,value) VALUES(?,?)", [("k3", "3")])
            assert a.in_write_tx
            assert _count(a, "k1") == 1                      # 事务里自己能读到
            assert _count(b, "k1") == 0 and _count(b, "k2") == 0 and _count(b, "k3") == 0   # 别的连接还看不到
        assert not a.in_write_tx
        assert _count(b, "k1") == 1 and _count(b, "k2") == 1 and _count(b, "k3") == 1
    finally:
        a.close()
        b.close()


def test_exception_rolls_back_everything(tmp_path):
    db = DB(tmp_path / "t.db")
    try:
        with pytest.raises(RuntimeError):
            with db.write_tx():
                db.execute("INSERT INTO kv(key,value) VALUES('k1','1')")
                db.kv_set("k2", 2)
                raise RuntimeError("boom")
        assert _count(db, "k1") == 0 and _count(db, "k2") == 0
        assert not db.in_write_tx and not db.conn.in_transaction
        db.kv_set("after", 1)                                    # 之后照常能写
        assert db.kv_get("after") == 1
    finally:
        db.close()


def test_nested_inner_failure_dooms_the_whole_transaction_even_if_swallowed(tmp_path):
    db = DB(tmp_path / "t.db")
    try:
        with pytest.raises(TransactionAborted):
            with db.write_tx():
                db.execute("INSERT INTO kv(key,value) VALUES('outer','1')")
                try:
                    with db.write_tx():
                        db.execute("INSERT INTO kv(key,value) VALUES('inner','1')")
                        raise ValueError("inner failed")
                except ValueError:
                    pass                                         # 外层把异常吞了：也不允许提交半笔
                db.execute("INSERT INTO kv(key,value) VALUES('later','1')")
        assert _count(db, "outer") == 0 and _count(db, "inner") == 0 and _count(db, "later") == 0
        with db.write_tx():                                      # 标记已复位，下一次事务正常
            db.execute("INSERT INTO kv(key,value) VALUES('ok','1')")
        assert _count(db, "ok") == 1
    finally:
        db.close()


def test_nested_success_commits_once_at_the_outermost_level(tmp_path):
    path = tmp_path / "t.db"
    a, b = DB(path), DB(path)
    try:
        with a.write_tx():
            with a.write_tx():
                a.execute("INSERT INTO kv(key,value) VALUES('inner','1')")
            assert _count(b, "inner") == 0                       # 内层结束时还没有提交
        assert _count(b, "inner") == 1
    finally:
        a.close()
        b.close()


def test_write_lock_is_held_from_the_start_of_the_transaction(tmp_path):
    """BEGIN IMMEDIATE：事务一开始就拿写锁。另一个连接此时不能开始写事务，但读不受影响。"""
    path = tmp_path / "t.db"
    a = DB(path)
    try:
        with a.write_tx():
            other = sqlite3.connect(str(path), timeout=0.2)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    other.execute("BEGIN IMMEDIATE")
                assert other.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 0   # 读不受影响（WAL）
            finally:
                other.close()
        other = sqlite3.connect(str(path), timeout=0.2)          # 提交之后就能拿到锁了
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        finally:
            other.close()
    finally:
        a.close()


def test_second_writer_waits_then_sees_the_first_writers_result(tmp_path):
    """两个线程（各自的连接）做同一个“读-检查-写”：后来的那个要等前一个提交，读到的是提交后的值。"""
    path = tmp_path / "t.db"
    DB(path).close()
    started, results = threading.Event(), []

    def worker(first: bool):
        db = DB(path)
        try:
            if not first:
                started.wait(5)
            with db.write_tx():
                started.set()
                seen = db.kv_get("counter", 0)
                if first:
                    threading.Event().wait(0.3)                  # 持锁期间让另一个线程来抢
                db.kv_set("counter", seen + 1)
                results.append(seen)
        finally:
            db.close()

    t1, t2 = threading.Thread(target=worker, args=(True,)), threading.Thread(target=worker, args=(False,))
    t1.start()
    t2.start()
    t1.join(15)
    t2.join(15)
    assert sorted(results) == [0, 1]                             # 不会两个都读到 0
    db = DB(path)
    try:
        assert db.kv_get("counter") == 2
    finally:
        db.close()
