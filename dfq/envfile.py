"""本地密钥文件 .env。

放在项目文件夹里（与 config.yaml、DFQ.exe 同级），已写进 .gitignore，不会被提交或推送。
做法与常见的 AI 插件 / dotenv 工具一致：

  - 格式：一行一个 NAME=value；# 开头是注释；值两边的引号会被去掉。
  - 只读 dfq 登记过的密钥名（log.SECRET_ENV_NAMES），其他行原样保留、不读入，
    所以不能借这个文件改 PATH 之类的系统变量。
  - 优先级：系统 / 终端里已经设置的环境变量 > .env。已存在的环境变量不会被覆盖。
  - 读到的每个值都登记进脱敏表，任何日志里出现都会被打码。

注意：.env 是明文文件，防的是“不小心提交、上传、贴到日志里”，防不了本机上的恶意程序。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .log import SECRET_ENV_NAMES, register_secret

FILENAME = ".env"
DISABLE_VAR = "DFQ_DOTENV"          # 设为 0 时不自动读取 .env（测试用）

HEADER = (
    "# DFQ 本地密钥文件 —— 只保存在这台电脑上，已加入 .gitignore，不会被提交或上传。\n"
    "# 不要把这个文件发给别人、贴到群里或截图。\n"
    "# 格式：一行一个 名字=值，等号两边不要加空格。可以在 DFQ.exe 的“设置”页填写，也可以用记事本直接改。\n"
    "# 系统里如果也设置了同名环境变量，以系统环境变量为准。\n"
)


def path_for_config(config_path: str | Path | None) -> Path:
    """.env 的位置：配置文件所在的文件夹。"""
    return Path(config_path or "config.yaml").resolve().parent / FILENAME


def _parse(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    key, val = s.split("=", 1)
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    return key, val


def _lines(path: Path) -> list[str]:
    try:
        # utf-8-sig：记事本保存时可能带 BOM
        return Path(path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []


def read(path: str | Path) -> dict[str, str]:
    """读出文件里登记过的密钥（空值不算）。"""
    out: dict[str, str] = {}
    for line in _lines(Path(path)):
        kv = _parse(line)
        if kv and kv[0] in SECRET_ENV_NAMES and kv[1]:
            out[kv[0]] = kv[1]
    return out


def write(path: str | Path, name: str, value: str | None) -> None:
    """设置（value 为 None 时删除）一个密钥，其余内容原样保留。先写临时文件再替换，中途失败不会留下半个文件。"""
    if name not in SECRET_ENV_NAMES:
        raise ValueError(f"未登记的密钥名: {name}")
    path = Path(path)
    lines = _lines(path)
    if not lines:
        lines = HEADER.rstrip("\n").split("\n") + [""]
    out, done = [], False
    for line in lines:
        kv = _parse(line)
        if kv and kv[0] == name:
            if value is not None and not done:
                out.append(f"{name}={value}")
                done = True
            continue                                 # 删除，或去掉重复的同名行
        out.append(line)
    if value is not None and not done:
        out.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out).rstrip("\n") + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_into(environ, path: str | Path) -> list[str]:
    """把 .env 里的密钥补进环境（已有的不覆盖）。返回补进去的名字。"""
    loaded = []
    for name, val in read(path).items():
        register_secret(val)
        if not environ.get(name):
            environ[name] = val
            loaded.append(name)
    return loaded


SOURCE_NONE = "none"          # 哪里都没有
SOURCE_DOTENV = "dotenv"      # 来自程序文件夹里的 .env
SOURCE_SYSTEM = "system"      # 来自系统 / 终端的环境变量（.env 里没有，或者值一样）
SOURCE_SYSTEM_OVERRIDES = "system_overrides"   # 系统环境变量和 .env 都有、值不同：.env 里的值被忽略

SOURCE_TEXT = {
    SOURCE_NONE: "未设置",
    SOURCE_DOTENV: "已设置（来自 .env）",
    SOURCE_SYSTEM: "已设置（来自系统环境变量）",
    SOURCE_SYSTEM_OVERRIDES: "已设置（来自系统环境变量；.env 里另有一个值，但没有生效）",
}


def source(name: str, path: str | Path, environ=None) -> str:
    """这个密钥现在从哪里来（只看来源，绝不返回值）。

    为什么需要：系统里如果早就有同名的 DFQ_* 环境变量（例如以前按旧说明设置过，或别的程序设置的），
    它会静默盖过 .env——用户在“设置”页改了 .env 却不生效，很难查。这里按“进程里的值是否等于 .env 里的值”判断：
      - 进程里没有、.env 里也没有              → none
      - 进程里的值与 .env 相同                  → dotenv（启动时从 .env 读进来的，或刚在界面里保存的）
      - 进程里有、.env 里没有                    → system
      - 进程里有、.env 里也有但不同              → system_overrides（.env 的值被忽略）
    进程里没有、.env 里有的情况只会出现在禁用了自动读取（DFQ_DOTENV=0）时，按 dotenv 报告。
    """
    environ = os.environ if environ is None else environ
    env_val = environ.get(name) or ""
    file_val = read(path).get(name, "")
    if not env_val:
        return SOURCE_DOTENV if file_val else SOURCE_NONE
    if file_val and env_val == file_val:
        return SOURCE_DOTENV
    return SOURCE_SYSTEM_OVERRIDES if file_val else SOURCE_SYSTEM


def sources(path: str | Path, environ=None) -> dict[str, str]:
    """所有登记过的密钥各自的来源（给 dfq status 和界面用）。"""
    return {name: source(name, path, environ) for name in SECRET_ENV_NAMES}


def autoload(config_path: str | Path | None, environ=None) -> list[str]:
    """命令行启动时调用：读取配置文件旁边的 .env。"""
    environ = os.environ if environ is None else environ
    if environ.get(DISABLE_VAR) == "0":
        return []
    return load_into(environ, path_for_config(config_path))
