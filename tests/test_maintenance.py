"""dfq/maintenance.py：占用空间、清理（绝不越界）、数据库备份与恢复。"""
import os
import sqlite3
import sys
import time

import pytest

from dfq import maintenance as M
from dfq.db import DB


def _db(path, rows=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(path)
    for i in range(rows):
        db.execute("INSERT INTO trades (ts, side, name, qty, price) VALUES (?, 'buy', ?, 1, 100)",
                   (f"2026-11-0{i + 1} 10:00:00", f"品种{i}"))
    db.close()


def _count(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        conn.close()


def _write(p, data=b"x" * 10, age_days=0.0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    return p


def _link_dir(link, target):
    """目录联接（Windows，不需要管理员）或符号链接（其他系统）。"""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "app"
    _db(b / "data" / "dfq.db")
    _write(b / "data" / "plans" / "latest.json", b"{}")
    _write(b / "data" / "cache" / "a.json", b"c" * 100)
    _write(b / "data" / "cache" / "sub" / "b.json", b"c" * 50)
    _write(b / "data" / "probe" / "raw.json", b"p" * 20)
    _write(b / "data" / "tmp" / "t.txt", b"t" * 5)
    _write(b / "data" / "exports" / "DFQ-logs.zip", b"z" * 7)
    _write(b / "data" / "logs" / "old.log", b"o" * 30, age_days=40)
    _write(b / "data" / "logs" / "new.log", b"n" * 30, age_days=1)
    _write(b / "data" / "reports" / "r.md", b"r")
    _write(b / ".env", b"DFQ_XTC_TOKEN=dummy-test-token-0000\n")
    _write(b / "config.yaml", b"general:\n  data_dir: data\n")
    return b


def test_storage_lists_every_area(base):
    s = M.storage(base)
    items = {i["id"]: i for i in s["items"]}
    assert set(items) == {"db", "plans", "cache", "logs", "backups", "exports", "reports", "webview", "tmp", "mock"}
    assert items["cache"]["bytes"] == 170 and items["cache"]["files"] == 3      # cache + probe
    assert items["logs"]["files"] == 2 and items["db"]["bytes"] > 0
    assert {k for k, v in items.items() if v["cleanable"]} == {"cache", "tmp", "exports", "logs"}
    assert s["total"] == sum(i["bytes"] for i in s["items"]) and s["base"] == str(base)


@pytest.mark.parametrize("what", ["db", "plans", "backups", "reports", "..", "", "cache/..", None, "CACHE"])
def test_clean_refuses_anything_but_the_four_kinds(base, what):
    with pytest.raises(M.MaintenanceError):
        M.clean(base, what)


def test_clean_cache_tmp_exports_never_touch_data(base):
    freed = M.clean(base, "cache") + M.clean(base, "tmp") + M.clean(base, "exports")
    assert freed == 170 + 5 + 7
    assert not any((base / "data" / "cache").rglob("*.json")) and not (base / "data" / "probe" / "raw.json").exists()
    assert (base / "data" / "cache").is_dir()                                  # 目录本身保留
    for keep in ("data/dfq.db", "data/plans/latest.json", "data/reports/r.md", ".env", "config.yaml",
                 "data/logs/old.log"):
        assert (base / keep).exists(), keep


def test_clean_logs_only_removes_old_files(base):
    assert M.clean(base, "logs", log_keep_days=30) == 30
    assert not (base / "data" / "logs" / "old.log").exists() and (base / "data" / "logs" / "new.log").exists()
    _write(base / "data" / "logs" / "mid.log", b"m", age_days=10)
    assert M.clean(base, "logs", log_keep_days=0) == 1                         # 0 = 手动清理时删 7 天以前的
    with pytest.raises(M.MaintenanceError):
        M.clean(base, "logs", log_keep_days=-1)


def test_clean_logs_only_touches_log_like_files(base):
    """数据目录可以改到任意位置：logs 里用户自己放的其他文件，再旧也不删。"""
    logs = base / "data" / "logs"
    keep = [_write(logs / n, b"k", age_days=400) for n in ("notes.docx", "photo.png", "dfq.db", "sub/readme.md")]
    gone = [_write(logs / n, b"g", age_days=400) for n in ("dfq-20250101.jsonl", "task-20250101.log", "x.TXT", "a.log.1")]
    M.clean(base, "logs", log_keep_days=30)
    assert all(p.exists() for p in keep) and not any(p.exists() for p in gone)


def test_clean_logs_covers_task_logs_when_data_dir_moved(base, tmp_path):
    """计划任务的输出固定在程序文件夹的 data\\logs；数据目录改到别处时两处的旧日志都清理、都计入占用。"""
    data = tmp_path / "moved-data"
    a = _write(data / "logs" / "dfq-20250101.jsonl", b"a" * 5, age_days=100)
    b = _write(base / "data" / "logs" / "task-20250101.log", b"b" * 7, age_days=100)
    logs_item = next(i for i in M.storage(base, data)["items"] if i["id"] == "logs")
    assert logs_item["bytes"] >= 12
    M.clean(base, "logs", data_dir=data, log_keep_days=30)
    assert not a.exists() and not b.exists()


def test_clean_does_not_follow_links_inside_target(base, tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "precious.txt", b"keep me")
    _link_dir(base / "data" / "cache" / "link", outside)
    M.clean(base, "cache")
    assert (outside / "precious.txt").read_bytes() == b"keep me"
    assert os.path.lexists(base / "data" / "cache" / "link")                   # 链接本身也不删
    assert M.storage(base)["items"][2]["bytes"] == 0                           # 统计时也不跟随链接


def test_clean_skips_target_dir_that_is_itself_a_link(base, tmp_path):
    outside = tmp_path / "elsewhere"
    _write(outside / "important.db", b"x" * 10)
    import shutil
    shutil.rmtree(base / "data" / "tmp")
    _link_dir(base / "data" / "tmp", outside)
    assert M.clean(base, "tmp") == 0
    assert (outside / "important.db").exists()


def test_clean_with_custom_data_dir(base, tmp_path):
    data = tmp_path / "elsewhere-data"
    _write(data / "cache" / "x.json", b"x" * 3)
    assert M.clean(base, "cache", data_dir=data) == 3
    assert (base / "data" / "cache" / "a.json").exists()                       # 只清配置里的数据目录


def test_backup_restore_round_trip(base):
    db_path = base / "data" / "dfq.db"
    r = M.backup_db(base, tag="manual")
    assert M.BACKUP_RE.match(r["name"]) and r["name"].endswith("-manual.db") and r["bytes"] > 0
    _db(db_path, rows=3)                                                       # 备份之后又记了 3 笔
    assert _count(db_path) == 4
    items = M.list_backups(base)
    assert [i["name"] for i in items] == [r["name"]] and items[0]["auto"] is False
    out = M.restore(base, r["name"])
    assert _count(db_path) == 1                                                # 回到备份时的样子
    assert out["safety"].endswith("-before-restore.db")
    assert _count(M.backups_dir(base) / out["safety"]) == 4                    # 恢复前的库也留了一份
    assert not list(M.backups_dir(base).glob("*.partial"))


def test_backup_without_database_is_a_clear_error(tmp_path):
    with pytest.raises(M.MaintenanceError, match="还没有数据库"):
        M.backup_db(tmp_path)
    with pytest.raises(M.MaintenanceError):
        M.backup_db(tmp_path, tag="../evil")


@pytest.mark.parametrize("name", ["../dfq.db", "..\\dfq.db", "dfq.db", "dfq-20260101-000000-manual.db",
                                  "/etc/passwd", "C:\\Windows\\x.db", "dfq-20260101-000000-manual.db/../x", 5, None])
def test_restore_rejects_names_not_in_the_list(base, name):
    M.backup_db(base)
    with pytest.raises(M.MaintenanceError):
        M.resolve_backup(base, name)


def test_restore_rejects_corrupt_or_foreign_backup(base):
    d = M.backups_dir(base)
    d.mkdir(parents=True)
    _write(d / "dfq-20260101-000000-manual.db", b"not a database at all" * 100)
    before = (base / "data" / "dfq.db").read_bytes()
    with pytest.raises(M.MaintenanceError):
        M.restore(base, "dfq-20260101-000000-manual.db")
    other = d / "dfq-20260102-000000-manual.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE x (a)")
    conn.commit()
    conn.close()
    with pytest.raises(M.MaintenanceError, match="不是 DFQ"):
        M.restore(base, other.name)
    assert (base / "data" / "dfq.db").read_bytes() == before                  # 当前库没有被动过


def test_prune_keeps_newest_auto_backups_only(base):
    d = M.backups_dir(base)
    for i in range(5):
        r = M.backup_db(base, tag="auto", now=time.time() - (5 - i) * 3600)
        t = time.time() - (5 - i) * 3600
        os.utime(d / r["name"], (t, t))
    manual = M.backup_db(base, tag="manual")["name"]
    assert M.prune_auto(base, 2) == 3
    names = [b["name"] for b in M.list_backups(base)]
    assert manual in names and sum(1 for n in names if n.endswith("-auto.db")) == 2


def test_startup_backs_up_once_per_20_hours_and_cleans_logs(base):
    lines = M.startup(base, {"backup_on_start": True, "backup_keep": 7, "log_keep_days": 30})
    assert any("已自动备份" in x for x in lines) and any("清理" in x for x in lines)
    assert not (base / "data" / "logs" / "old.log").exists()
    assert M.startup(base, {"backup_on_start": True, "backup_keep": 7, "log_keep_days": 30}) == []   # 20 小时内不重复
    later = time.time() + 21 * 3600
    assert any("已自动备份" in x for x in M.startup(base, {"backup_on_start": True, "backup_keep": 7}, now=later))
    assert M.startup(base, {"backup_on_start": False, "log_keep_days": 0}) == []


def test_startup_never_raises(tmp_path):
    assert M.startup(tmp_path / "missing", {"backup_on_start": True, "log_keep_days": 30}) == []
