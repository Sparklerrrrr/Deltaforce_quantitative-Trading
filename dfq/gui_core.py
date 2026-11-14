"""图形界面的核心逻辑（不依赖 tkinter，便于测试）。

界面上的每个按钮，本质上都是调用一次命令行入口 `dfq.cli.main([...])`，
把它的输出实时转到窗口里。这样界面和命令行走的是同一套代码、同一套校验和护栏，
界面本身不包含任何交易逻辑。
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

from .log import redact, register_secret

# 界面里可以设置的密钥：(环境变量名, 用途说明)
SECRETS: list[tuple[str, str]] = [
    ("DFQ_XTC_TOKEN", "小涛查接口 token（必需）"),
    ("DFQ_SMTP_PASSWORD", "邮箱发信授权码（开启邮件时需要）"),
    ("DFQ_IMAP_PASSWORD", "邮箱收信授权码（一般不用设，默认同发信）"),
    ("DFQ_LLM_API_KEY", "大模型 API Key（开启大模型时需要）"),
    ("DFQ_TAVILY_API_KEY", "新闻搜索 Key（开启新闻搜索时需要）"),
]
SECRET_NAMES = {n for n, _ in SECRETS}


# ------------------------------------------------------------------ 位置
def is_frozen() -> bool:
    """是否是打包后的 .exe。"""
    return bool(getattr(sys, "frozen", False))


def default_base_dir() -> Path:
    """数据和配置放在哪：.exe 所在的文件夹；用 Python 直接运行时是当前目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


# ------------------------------------------------------------------ 运行命令
class _Sink:
    """把 print / 日志的输出转给回调（回调负责线程安全地交给界面）。"""

    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self.encoding = "utf-8"

    def write(self, s) -> int:
        if s:
            self._emit(str(s))
        return len(s or "")

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class _WaitingStdin:
    """命令里需要“按回车继续”时（如登录 moligod），等界面上的按钮被点下。"""

    def __init__(self, event: threading.Event):
        self._event = event

    def readline(self, *a) -> str:
        self._event.wait()
        self._event.clear()
        return "\n"

    def read(self, *a) -> str:
        return ""


_run_lock = threading.Lock()


def run_cli(argv: list[str], emit: Callable[[str], None], *,
            continue_event: threading.Event | None = None) -> int:
    """在当前进程里执行一条 dfq 命令，输出交给 emit。返回退出码。

    同一时间只允许执行一条命令（标准输出是全局的）。
    """
    from . import cli

    if not _run_lock.acquire(blocking=False):
        emit("上一条命令还在运行，请稍候。\n")
        return 1
    sink = _Sink(emit)
    old = sys.stdout, sys.stderr, sys.stdin
    sys.stdout = sys.stderr = sink
    if continue_event is not None:
        sys.stdin = _WaitingStdin(continue_event)
    try:
        return int(cli.main(list(argv)) or 0)
    except SystemExit as e:                          # argparse 参数错误等
        return e.code if isinstance(e.code, int) else 1
    except Exception:                                # 任何意外都不能让窗口崩掉
        emit("\n出错了（以下信息已脱敏）：\n" + redact(traceback.format_exc()) + "\n")
        return 1
    finally:
        sys.stdout, sys.stderr, sys.stdin = old
        _run_lock.release()


# ------------------------------------------------------------------ 密钥
class SecretBackend:
    """密钥的持久化位置。Windows 上是“用户环境变量”（注册表 HKCU\\Environment）。"""

    available = False
    where = "不可用"

    def read(self, name: str) -> str | None:
        return None

    def write(self, name: str, value: str) -> None:
        raise RuntimeError("当前系统不支持在界面里保存密钥，请手动设置环境变量")

    def delete(self, name: str) -> None:
        raise RuntimeError("当前系统不支持在界面里删除密钥")


class WindowsUserEnv(SecretBackend):
    available = True
    where = "Windows 用户环境变量"

    def __init__(self):
        import winreg                                # noqa: F401 仅 Windows 有
        self._wr = winreg

    def _key(self, access):
        return self._wr.OpenKey(self._wr.HKEY_CURRENT_USER, "Environment", 0, access)

    def read(self, name: str) -> str | None:
        try:
            with self._key(self._wr.KEY_READ) as k:
                val, _ = self._wr.QueryValueEx(k, name)
                return str(val) if val else None
        except FileNotFoundError:
            return None

    def write(self, name: str, value: str) -> None:
        with self._key(self._wr.KEY_SET_VALUE) as k:
            self._wr.SetValueEx(k, name, 0, self._wr.REG_SZ, value)

    def delete(self, name: str) -> None:
        try:
            with self._key(self._wr.KEY_SET_VALUE) as k:
                self._wr.DeleteValue(k, name)
        except FileNotFoundError:
            pass


def default_backend() -> SecretBackend:
    if sys.platform == "win32":
        try:
            return WindowsUserEnv()
        except Exception:
            pass
    return SecretBackend()


class SecretStore:
    """保存 / 清除密钥。值只写进系统和本进程的环境变量，从不显示、不写文件、不写日志。"""

    def __init__(self, backend: SecretBackend | None = None, environ=None):
        self.backend = backend if backend is not None else default_backend()
        self.environ = environ if environ is not None else os.environ

    def load_into_process(self) -> list[str]:
        """启动时把系统里已保存、但本进程还看不到的密钥读进来。

        常见情况：设置环境变量之前就打开了的程序（或资源管理器）启动本程序，
        子进程继承的是旧环境，看不到新设置的 token。返回补读到的变量名。
        """
        loaded = []
        if not self.backend.available:
            return loaded
        for name, _ in SECRETS:
            if self.environ.get(name):
                register_secret(self.environ[name])
                continue
            val = self.backend.read(name)
            if val:
                self.environ[name] = val
                register_secret(val)
                loaded.append(name)
        return loaded

    def is_set(self, name: str) -> bool:
        if self.environ.get(name):
            return True
        return bool(self.backend.available and self.backend.read(name))

    def set(self, name: str, value: str) -> None:
        if name not in SECRET_NAMES:
            raise ValueError(f"不认识的密钥名：{name}")
        value = (value or "").strip()
        if not value:
            raise ValueError("内容为空，没有保存")
        if any(c in value for c in "\r\n\t") or len(value) > 4096:
            raise ValueError("内容里有换行或过长，像是粘贴错了，没有保存")
        register_secret(value)
        self.backend.write(name, value)              # 先持久化；失败则本进程也不改
        self.environ[name] = value

    def clear(self, name: str) -> None:
        if name not in SECRET_NAMES:
            raise ValueError(f"不认识的密钥名：{name}")
        self.backend.delete(name)
        self.environ.pop(name, None)


# ------------------------------------------------------------------ 只读查询（给界面填下拉框、表格）
def load_config(config_path: Path):
    """返回 (cfg, 错误信息)。配置不合法时 cfg 为 None。"""
    from .config import load_config as _load
    try:
        return _load(config_path), None
    except ValueError as e:
        return None, redact(str(e))


def item_choices(cfg) -> list[str]:
    """记账下拉框里的品种：关注清单的全名。"""
    return [w["name"] for w in cfg.watchlist] if cfg else []


def recent_trades(cfg, limit: int = 200) -> list[dict]:
    from .db import DB
    path = cfg.data_dir / "dfq.db"
    if not path.exists():
        return []
    db = DB(path)
    try:
        rows = db.query("SELECT id, ts, side, name, qty, price, paper, source, note FROM trades "
                        "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
    finally:
        db.close()
