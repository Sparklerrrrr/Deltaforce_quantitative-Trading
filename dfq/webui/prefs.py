"""界面偏好（只和这台电脑上的界面有关，不影响交易逻辑）。

保存在 <程序文件夹>/data/ui-settings.json：和其他数据一样只在安装文件夹里，卸载时一并处理。
文件坏了或不存在时一律用默认值，绝不因为偏好文件出错而打不开程序。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CLOSE_ACTIONS = ("ask", "tray", "quit")      # 关闭窗口时：每次询问 / 最小化到托盘 / 退出程序
THEMES = ("", "light", "dark")               # 空 = 还没选过（跟随系统）；用于窗口底色，缩放时不闪
DEFAULTS = {"close_action": "ask", "theme": ""}


def path_for(base: Path) -> Path:
    return Path(base) / "data" / "ui-settings.json"


def load(base: Path) -> dict:
    out = dict(DEFAULTS)
    try:
        # utf-8-sig：用记事本 / PowerShell 改过的文件开头可能带 BOM，严格 utf-8 会读失败、设置悄悄失效
        data = json.loads(path_for(base).read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and data.get("close_action") in CLOSE_ACTIONS:
            out["close_action"] = data["close_action"]
        if isinstance(data, dict) and data.get("theme") in THEMES:
            out["theme"] = data["theme"]
    except (OSError, ValueError):
        pass
    return out


def save(base: Path, **changes) -> dict:
    cur = load(base)
    if "close_action" in changes:
        if changes["close_action"] not in CLOSE_ACTIONS:
            raise ValueError(f"不认识的关闭方式：{changes['close_action']}")
        cur["close_action"] = changes["close_action"]
    if "theme" in changes:
        if changes["theme"] not in THEMES:
            raise ValueError(f"不认识的主题：{changes['theme']}")
        cur["theme"] = changes["theme"]
    p = path_for(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="ui-settings.", suffix=".tmp", dir=str(p.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return cur
