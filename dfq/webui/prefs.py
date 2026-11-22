"""界面偏好（只和这台电脑上的界面有关，不影响交易逻辑）。

保存在 <程序文件夹>/data/ui-settings.json：和其他数据一样只在安装文件夹里，卸载时一并处理。
文件坏了或不存在时一律用默认值，绝不因为偏好文件出错而打不开程序：
某一个键的值不合法时只有**这个键**退回默认值，其他合法的键照常保留（以前整份作废，改一个键会把别的设置冲掉）。

每个键的取值规则写在 SCHEMA 里（界面的 set_pref 和测试都用它），见 BRIDGE.md 第 1 节。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

CLOSE_ACTIONS = ("ask", "tray", "quit")      # 关闭窗口时：每次询问 / 最小化到托盘 / 退出程序
THEMES = ("", "light", "dark")               # 空 = 还没选过（跟随系统）；用于窗口底色，缩放时不闪
SCALES = (90, 100, 110, 125)
DENSITIES = ("comfortable", "compact")
MOTIONS = ("auto", "full", "reduced", "off")
LOG_KEEP_DAYS = (0, 7, 30, 90, 365)
RESTART_KEYS = ("gpu",)                      # 改了要重启程序才生效的键
_ACCENT = re.compile(r"^#[0-9a-fA-F]{6}$")
_WINDOW_LIMIT = 100_000                      # 窗口坐标 / 尺寸的合理范围（多显示器时坐标可以是负数）


def _choice(*allowed: Any) -> Callable[[Any], Any]:
    def check(v: Any) -> Any:
        # bool 是 int 的子类：True 不能冒充 100% 缩放
        if isinstance(v, bool) or v not in allowed or type(v) is not type(allowed[0]):
            raise ValueError(f"只能是 {' / '.join(repr(a) for a in allowed)}")
        return v
    return check


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("只能是 true 或 false")
    return v


def _accent(v: Any) -> str:
    if not isinstance(v, str) or not _ACCENT.match(v):
        raise ValueError("主题色要写成 #rrggbb（6 位十六进制）")
    return v.lower()


def _backup_keep(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 60:
        raise ValueError("保留份数要在 1～60 之间")
    return v


def _window(v: Any) -> dict | None:
    """窗口位置由 app.py 维护（页面不能设置）。坏值一律当作“没有记住过”。"""
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) != {"w", "h", "x", "y", "max"}:
        raise ValueError("窗口位置格式不对")
    out: dict[str, Any] = {}
    for k in ("w", "h", "x", "y"):
        n = v[k]
        if isinstance(n, bool) or not isinstance(n, int) or abs(n) > _WINDOW_LIMIT:
            raise ValueError("窗口位置格式不对")
        out[k] = n
    if out["w"] <= 0 or out["h"] <= 0:
        raise ValueError("窗口大小必须是正数")
    out["max"] = _bool(v["max"])
    return out


# 键 → (默认值, 校验函数)。校验函数返回规范化后的值，不合法抛 ValueError。
SCHEMA: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "close_action": ("ask", _choice(*CLOSE_ACTIONS)),
    "theme": ("", _choice(*THEMES)),
    "accent": ("#78b49d", _accent),
    "scale": (100, _choice(*SCALES)),
    "density": ("comfortable", _choice(*DENSITIES)),
    "motion": ("auto", _choice(*MOTIONS)),
    "glow": (True, _bool),
    "gpu": (True, _bool),
    "tray_saver": (True, _bool),
    "notify_done": (True, _bool),
    "remember_window": (True, _bool),
    "side_collapsed": (False, _bool),
    "backup_on_start": (True, _bool),
    "backup_keep": (7, _backup_keep),
    "log_keep_days": (30, _choice(*LOG_KEEP_DAYS)),
    "window": (None, _window),
}
DEFAULTS: dict[str, Any] = {k: d for k, (d, _) in SCHEMA.items()}
PAGE_KEYS = tuple(k for k in SCHEMA if k != "window")     # 页面可以通过 set_pref 修改的键

_LABELS = {"close_action": "关闭方式", "theme": "主题", "accent": "主题色", "scale": "界面缩放",
           "density": "界面密度", "motion": "动效", "log_keep_days": "日志保留天数", "backup_keep": "备份保留份数",
           "window": "窗口位置"}


def validate(key: str, value: Any) -> Any:
    """校验一个键的值，返回规范化后的值；未知键或非法值抛 ValueError（中文说明）。"""
    if key not in SCHEMA:
        raise ValueError(f"不认识的界面设置：{str(key)[:40]}")
    try:
        return SCHEMA[key][1](value)
    except ValueError as e:
        raise ValueError(f"{_LABELS.get(key, key)}设置不对：{e}") from None


def path_for(base: Path) -> Path:
    return Path(base) / "data" / "ui-settings.json"


def load(base: Path) -> dict:
    out = dict(DEFAULTS)
    try:
        # utf-8-sig：用记事本 / PowerShell 改过的文件开头可能带 BOM，严格 utf-8 会读失败、设置悄悄失效
        data = json.loads(path_for(base).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for key in SCHEMA:
        if key in data:
            try:
                out[key] = SCHEMA[key][1](data[key])
            except ValueError:
                pass                                 # 只有这个键退回默认值
    return out


def save(base: Path, **changes) -> dict:
    """逐键校验后原子写入（临时文件 + os.replace）。任何一个键不合法都不写，抛 ValueError。"""
    cur = load(base)
    for key, value in changes.items():
        cur[key] = validate(key, value)
    p = path_for(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="ui-settings.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return cur
