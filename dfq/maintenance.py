"""数据维护：占用空间、清理、数据库备份与恢复（“设置 → 数据与备份”页和启动时的自动备份用）。

纯函数，参数是程序文件夹 base 和数据目录 data_dir（config.yaml 的 general.data_dir，默认 base/data），不依赖界面。

安全原则（这里删的是用户电脑上的文件，宁可少删，绝不多删）：
  - 能清理的只有四类：cache（接口缓存、实测原始返回）、tmp（临时文件）、exports（导出的日志包）、
    logs（只删超过保留天数的旧日志）。交易数据库、计划、备份、报告、.env、config.yaml 永远不在清理范围里；
  - 不跟随符号链接 / 目录联接（junction）：遇到就跳过，既不进去删，也不删链接本身——
    链接可能是用户自己建的，指向别处的重要文件；
  - 要删的每一个文件都先确认它真的在目标目录里面（解析后的真实路径），越界的一律跳过。
数据库备份用 sqlite3 的在线备份 API：别的进程（计划任务）正在写库时也能得到一份一致的副本，
不会像直接复制文件那样复制到写了一半的状态（WAL 模式下直接复制 dfq.db 还会漏掉 -wal 里的最新成交）。
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

CLEANABLE = ("cache", "tmp", "exports", "logs")
# 清理旧日志时只删看起来是日志的文件：数据目录可以由用户改到任意位置，它下面的 logs 文件夹里
# 也许还有用户自己放的东西，宁可少删
LOG_FILE_RE = re.compile(r"^[\w.\-]+\.(?:jsonl|log|txt)(?:\.\d{1,3})?$", re.IGNORECASE)
LOG_DEFAULT_DAYS = 7                       # log_keep_days 为 0（启动时不自动清理）时，手动清理删早于 7 天的
AUTO_BACKUP_GAP_HOURS = 20                 # 距上次自动备份超过这么久才再做一次
BACKUP_RE = re.compile(r"^dfq-\d{8}-\d{6}(?:-\d{1,3})?-(manual|auto|before-restore)\.db$")
LABELS = {
    "db": "交易数据库（成交、行情历史、建议）", "plans": "历次计划", "cache": "接口缓存",
    "logs": "运行日志", "backups": "备份", "exports": "导出的日志包", "reports": "接口实测报告",
    "webview": "界面浏览器组件数据", "tmp": "临时文件", "mock": "演示用的模拟数据",
}


class MaintenanceError(Exception):
    """给用户看的中文说明。"""


# ------------------------------------------------------------------ 路径
def _dirs(base: Path, data_dir: Path | None) -> dict[str, list[Path]]:
    base = Path(base)
    data = Path(data_dir) if data_dir else base / "data"
    ui = base / "data"                       # 界面自己的文件（tmp、exports、backups、webview）固定在程序文件夹的 data 下
    return {
        "db": [data / "dfq.db", data / "dfq.db-wal", data / "dfq.db-shm"],
        "plans": [data / "plans"],
        "cache": [data / "cache", data / "probe"],
        # 计划任务的输出（task-日期.log）固定写在程序文件夹的 data\logs；数据目录改到别处时两处都算
        "logs": list(dict.fromkeys([data / "logs", ui / "logs"])),
        "backups": [ui / "backups"],
        "exports": [ui / "exports"],
        "reports": [data / "reports"],
        "webview": [ui / "webview"],
        "tmp": [ui / "tmp"],
        "mock": [data / "mock"],
    }


def backups_dir(base: Path) -> Path:
    return Path(base) / "data" / "backups" / "db"


def _is_link(p: Path) -> bool:
    """符号链接，或 Windows 的目录联接（junction）/其他重解析点。Python 3.11 没有 Path.is_junction。"""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _inside(p: Path, root: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _size(p: Path) -> tuple[int, int]:
    """(字节数, 文件数)。不跟随链接。"""
    if _is_link(p) or not p.exists():
        return 0, 0
    if p.is_file():
        try:
            return p.stat().st_size, 1
        except OSError:
            return 0, 0
    total = files = 0
    stack = [p]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            ep = Path(e.path)
            if _is_link(ep):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(ep)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                continue
    return total, files


# ------------------------------------------------------------------ 占用空间
def storage(base: Path, data_dir: Path | None = None) -> dict:
    items = []
    for key, paths in _dirs(base, data_dir).items():
        b = f = 0
        for p in paths:
            pb, pf = _size(p)
            b, f = b + pb, f + pf
        items.append({"id": key, "label": LABELS[key], "bytes": b, "files": f, "cleanable": key in CLEANABLE})
    data = Path(data_dir) if data_dir else Path(base) / "data"
    return {"items": items, "total": sum(i["bytes"] for i in items), "base": str(Path(base)), "data_dir": str(data)}


# ------------------------------------------------------------------ 清理
def _delete_tree_contents(root: Path, *, older_than: float | None = None, name_re: re.Pattern | None = None) -> int:
    """删掉 root 里面的东西（root 本身保留），返回释放的字节数。跳过链接；older_than 给出时只删修改时间更早的文件；
    name_re 给出时只删文件名匹配的文件。"""
    if _is_link(root) or not root.is_dir():
        return 0
    return _purge(root, root.resolve(), older_than, name_re)


def _purge(d: Path, real_root: Path, older_than: float | None, name_re: re.Pattern | None = None) -> int:
    # 不用 os.walk：它在 Windows 上会把目录联接（junction）当成普通目录走进去
    freed = 0
    try:
        entries = list(os.scandir(d))
    except OSError:
        return 0
    for e in entries:
        p = Path(e.path)
        if _is_link(p) or not _inside(p, real_root):
            continue                           # 链接 / 联接：既不进去，也不删链接本身
        try:
            if e.is_dir(follow_symlinks=False):
                freed += _purge(p, real_root, older_than, name_re)
                try:
                    p.rmdir()                  # 只删已经空了的目录
                except OSError:
                    pass
            else:
                st = os.lstat(p)
                if older_than is not None and st.st_mtime >= older_than:
                    continue
                if name_re is not None and not name_re.match(p.name):
                    continue
                p.unlink()
                freed += st.st_size
        except OSError:
            continue
    return freed


def clean(base: Path, what: str, *, data_dir: Path | None = None, log_keep_days: int = 30,
          now: float | None = None) -> int:
    """清理一类可以随时删除的文件，返回释放的字节数。what 只能是 cache / tmp / exports / logs。"""
    if what not in CLEANABLE:
        raise MaintenanceError(f"不能清理“{str(what)[:20]}”：只能清理 接口缓存 / 临时文件 / 导出的日志包 / 旧日志")
    if isinstance(log_keep_days, bool) or not isinstance(log_keep_days, int) or log_keep_days < 0:
        raise MaintenanceError("日志保留天数不对")
    now = time.time() if now is None else now
    older = None
    if what == "logs":
        older = now - (log_keep_days or LOG_DEFAULT_DAYS) * 86400
    freed = 0
    base = Path(base)
    for root in _dirs(base, data_dir)[what]:
        parent = root.parent
        if _is_link(root) or not root.is_dir() or not _inside(root, parent):
            continue                           # 目录本身是链接 / 联接：一个字节都不碰
        freed += _delete_tree_contents(root, older_than=older, name_re=LOG_FILE_RE if what == "logs" else None)
    return freed


# ------------------------------------------------------------------ 备份
def _connect_ro(path: Path) -> sqlite3.Connection:
    # immutable=1：只读、不加锁、不需要 -shm 文件。只用于检查备份文件（备份文件不会被别的进程改）
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def _backup_name(dest_dir: Path, tag: str, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    p = dest_dir / f"dfq-{stamp}-{tag}.db"
    n = 2
    while p.exists():
        p = dest_dir / f"dfq-{stamp}-{n}-{tag}.db"
        n += 1
    return p


def _online_copy(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(str(src_path), timeout=30)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup_db(base: Path, *, data_dir: Path | None = None, tag: str = "manual", now: float | None = None) -> dict:
    """把 dfq.db 在线备份到 data/backups/db/dfq-YYYYmmdd-HHMMSS-<tag>.db，返回 {"name","bytes"}。"""
    if tag not in ("manual", "auto", "before-restore"):
        raise MaintenanceError("备份类型不对")
    db = _dirs(base, data_dir)["db"][0]
    if not db.is_file():
        raise MaintenanceError("还没有数据库（运行过一次之后才会有），没有需要备份的内容")
    dest_dir = backups_dir(base)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if _is_link(dest_dir):
        raise MaintenanceError("备份文件夹是一个链接，为安全起见没有备份")
    dst = _backup_name(dest_dir, tag, now)
    tmp = dst.with_suffix(".db.partial")
    try:
        _online_copy(db, tmp)
        os.replace(tmp, dst)
    except sqlite3.Error as e:
        tmp.unlink(missing_ok=True)
        raise MaintenanceError(f"备份失败（{type(e).__name__}）：数据库可能正被占用，请稍后再试") from None
    return {"name": dst.name, "bytes": dst.stat().st_size}


def list_backups(base: Path) -> list[dict]:
    d = backups_dir(base)
    out = []
    if d.is_dir() and not _is_link(d):
        for p in d.iterdir():
            if not BACKUP_RE.match(p.name) or _is_link(p) or not p.is_file():
                continue
            st = p.stat()
            out.append({"name": p.name, "bytes": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                        "auto": p.name.endswith("-auto.db"), "_t": st.st_mtime})
    out.sort(key=lambda x: (x["_t"], x["name"]), reverse=True)
    for x in out:
        del x["_t"]
    return out


def check_backup(path: Path) -> None:
    """备份文件能打开、quick_check 通过、而且确实是 DFQ 的数据库（有 trades 表）。否则抛 MaintenanceError。"""
    try:
        conn = _connect_ro(path)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        raise MaintenanceError("这个备份文件打不开或已经损坏，没有恢复") from None
    if not row or str(row[0]).lower() != "ok":
        raise MaintenanceError("这个备份文件完整性检查没有通过，没有恢复")
    if not has:
        raise MaintenanceError("这个文件不是 DFQ 的数据库备份，没有恢复")


def resolve_backup(base: Path, name: Any) -> Path:
    """name 必须是 list_backups() 列出的文件名之一（防路径穿越）。"""
    if not isinstance(name, str) or not BACKUP_RE.match(name):
        raise MaintenanceError("备份文件名不对")
    if name not in {b["name"] for b in list_backups(base)}:
        raise MaintenanceError("没有这个备份（可能已被删除），请刷新列表")
    p = backups_dir(base) / name
    if _is_link(p) or not _inside(p, backups_dir(base)):
        raise MaintenanceError("备份文件名不对")
    return p


def restore(base: Path, name: str, *, data_dir: Path | None = None) -> dict:
    """用备份覆盖当前数据库。先检查备份，再把当前库备份一份（-before-restore），再用在线备份 API 反向写回。"""
    src = resolve_backup(base, name)
    check_backup(src)
    db = _dirs(base, data_dir)["db"][0]
    safety = None
    if db.is_file():
        safety = backup_db(base, data_dir=data_dir, tag="before-restore")["name"]
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        _online_copy(src, db)
    except sqlite3.Error as e:
        raise MaintenanceError(f"恢复失败（{type(e).__name__}）：数据库可能正被另一个 DFQ 进程占用，"
                               f"请关掉命令行窗口、等计划任务跑完后再试。当前数据库没有被改动"
                               + (f"，恢复前的备份是 {safety}" if safety else "")) from None
    return {"safety": safety}


def prune_auto(base: Path, keep: int) -> int:
    """自动备份只留最新的 keep 份（手动备份、恢复前备份不动）。返回删除的份数。"""
    autos = [b for b in list_backups(base) if b["auto"]]
    n = 0
    for b in autos[max(int(keep), 1):]:
        p = backups_dir(base) / b["name"]
        try:
            if not _is_link(p):
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


def startup(base: Path, prefs: dict, *, data_dir: Path | None = None, now: float | None = None) -> list[str]:
    """启动时调用（app.py）：按界面偏好做自动备份、清理过期日志。返回给日志区看的说明；任何失败都不影响启动。"""
    now = time.time() if now is None else now
    lines: list[str] = []
    if prefs.get("backup_on_start", True):
        try:
            last = max((b for b in list_backups(base) if b["auto"]),
                       key=lambda b: (backups_dir(base) / b["name"]).stat().st_mtime, default=None)
            age = now - (backups_dir(base) / last["name"]).stat().st_mtime if last else None
            db = _dirs(base, data_dir)["db"][0]
            if db.is_file() and (age is None or age > AUTO_BACKUP_GAP_HOURS * 3600):
                r = backup_db(base, data_dir=data_dir, tag="auto", now=now)
                removed = prune_auto(base, int(prefs.get("backup_keep", 7)))
                lines.append(f"已自动备份数据库：{r['name']}" + (f"（清理了 {removed} 份较早的自动备份）" if removed else ""))
        except (MaintenanceError, OSError) as e:
            lines.append(f"自动备份没有完成：{e}")
    keep = prefs.get("log_keep_days", 30)
    if isinstance(keep, int) and not isinstance(keep, bool) and keep > 0:
        try:
            freed = clean(base, "logs", data_dir=data_dir, log_keep_days=keep, now=now)
            if freed:
                lines.append(f"已清理 {keep} 天前的日志，释放 {freed / 1024 / 1024:.1f} MB")
        except (MaintenanceError, OSError):
            pass
    return lines
