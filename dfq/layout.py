"""DFQ 程序文件夹（安装目录 / 源码运行时的项目根目录）里每个文件和目录的归属。

这是“哪些是用户的、哪些可以清理”的唯一定义：
  - bootstrap()       首次启动时创建缺少的目录和文件（只创建，绝不覆盖已有的）；
  - 卸载程序           installer/DFQ.iss 的保留规则必须与这里一致（tests/test_layout.py 会检查）；
  - .gitignore         这些运行时文件都不进 Git。

    <程序文件夹>/
    ├── DFQ.exe, _internal/        程序本体（安装/升级时覆盖）
    ├── unins000.exe/.dat          卸载程序
    ├── scripts/install_tasks.ps1  计划任务脚本（随程序安装）
    ├── .env.example               密钥模板（随程序安装，没有任何真实值）
    ├── config.yaml                【用户配置】首次启动从模板生成，之后只由用户修改
    ├── .env                       【密钥】首次启动从模板生成（全部为空），由用户在“设置”页填写
    └── data/
        ├── ui-settings.json       【用户配置】界面偏好
        ├── dfq.db (+ -wal/-shm)   【用户数据】交易记录、行情历史、建议与复盘
        ├── overrides.yaml         【用户数据】你采纳的反思调参
        ├── plans/ outbox/ inbox/  【用户数据】历次计划、计划邮件正文、邮件回复
        ├── reflections/ reports/  【用户数据】反思记录、接口实测报告
        ├── backups/               【用户数据】改配置 / 导入配置前的备份；backups/db/ 是数据库备份（卸载时随 backups/ 保留）
        ├── moligod_profile/       【登录状态】moligod 浏览器登录信息（等同凭据）
        ├── cache/ probe/          【缓存】接口缓存、实测原始返回，可以随时删除
        ├── webview/               【缓存】界面浏览器组件的数据
        ├── logs/ exports/         【日志】运行日志、导出的日志包（只是日志的副本，可以随时删除，卸载时删除）
        ├── tmp/                   【临时】启动时清理
        └── mock/                  【演示】模拟数据，可以随时删除

数据隔离（tests/test_isolation.py 会检查）：
  - 安装包里只有程序和两个模板（config.example.yaml、.env.example），没有任何人的配置、密钥或数据；
  - 程序只读写“程序文件夹”里的这些文件：打包后以 DFQ.exe 所在文件夹为准，不看当前目录、不往上级目录找；
  - 不同安装文件夹之间互不相干（各有各的 config.yaml / .env / data）。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

DATA = "data"

# 用户配置：升级不覆盖；普通卸载保留
CONFIG_FILES = ("config.yaml",)
UI_SETTINGS = f"{DATA}/ui-settings.json"
# 密钥 / 登录状态：卸载时由用户选择是否保留
SECRET_FILES = (".env",)
LOGIN_STATE_DIRS = (f"{DATA}/moligod_profile",)
# 用户数据：普通卸载保留，只有“完全删除”才删
USER_DATA = (f"{DATA}/dfq.db", f"{DATA}/dfq.db-wal", f"{DATA}/dfq.db-shm", f"{DATA}/overrides.yaml",
             f"{DATA}/plans", f"{DATA}/outbox", f"{DATA}/inbox", f"{DATA}/reflections", f"{DATA}/reports",
             f"{DATA}/backups")
# 可以随时安全删除的
CACHE_DIRS = (f"{DATA}/cache", f"{DATA}/probe", f"{DATA}/webview")
LOG_DIRS = (f"{DATA}/logs", f"{DATA}/exports")
TEMP_DIR = f"{DATA}/tmp"
DEMO_DIR = f"{DATA}/mock"

# 首次启动要有的目录
DIRS = (DATA, f"{DATA}/logs", f"{DATA}/cache", TEMP_DIR, f"{DATA}/backups", f"{DATA}/reports")

# 卸载时（“保留”类选项）留下的东西；与 installer/DFQ.iss 的 KeepName 一致
KEEP_ON_UNINSTALL_DEFAULT = CONFIG_FILES + (UI_SETTINGS,) + USER_DATA
KEEP_ON_UNINSTALL_WITH_SECRETS = KEEP_ON_UNINSTALL_DEFAULT + SECRET_FILES + LOGIN_STATE_DIRS

TMP_MAX_AGE = 24 * 3600


def resource(name: str) -> Path | None:
    """随程序发布的模板文件（config.example.yaml / .env.example）：打包后在 _internal 根部，源码运行时在项目根目录。"""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent.parent)
    for r in roots:
        p = r / name
        if p.is_file():
            return p
    return None


def bootstrap(base: str | Path, *, create_config: bool = True) -> list[str]:
    """首次启动初始化。可以重复调用：已有的文件和目录一概不动。返回这次新建了什么（给日志看）。"""
    base = Path(base)
    created: list[str] = []
    for d in DIRS:
        p = base / d
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(d + "/")
    if create_config:
        for name, template in (("config.yaml", "config.example.yaml"), (".env", ".env.example")):
            dst = base / name
            if dst.exists():
                continue                                   # 已有配置：绝不覆盖
            src = resource(template)
            if src is None:
                continue
            try:
                with open(dst, "xb") as fh:                  # x = 只在文件不存在时创建：两个进程同时启动也不会覆盖
                    fh.write(src.read_bytes())
            except FileExistsError:
                continue
            created.append(name)
    clean_temp(base)
    return created


def clean_temp(base: str | Path, max_age: float = TMP_MAX_AGE) -> int:
    """清理 data/tmp 里超过一天的残留临时文件（程序异常退出时可能留下）。只删 data/tmp 里面的东西。"""
    tmp = (Path(base) / TEMP_DIR).resolve()
    if not tmp.is_dir():
        return 0
    now, n = time.time(), 0
    for p in tmp.iterdir():
        try:
            if p.is_symlink() or not p.resolve().is_relative_to(tmp):
                continue                                   # 不跟随链接到别处
            if now - p.stat().st_mtime < max_age:
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            n += 1
        except OSError:
            continue
    return n
