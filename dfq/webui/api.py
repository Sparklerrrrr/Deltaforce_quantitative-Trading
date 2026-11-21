"""界面后端：网页界面能调用的全部操作（pywebview 的 js_api）。接口契约见 BRIDGE.md。

设计要点
  - 每个按钮都只是调用一条现有的 dfq 命令（gui_core.run_cli），界面里没有任何交易逻辑。
  - 会花钱（真实接口 / 大模型）、改动账本、改文件、删东西的动作，必须带 confirmed=True 才执行。
    第一次调用只返回确认文字，由界面弹窗，用户点“确定”后再调一次。确认在这里强制，
    就算界面代码写错、漏了弹窗，也不会悄悄花钱或改账。confirmed 只认真正的 true（字符串 "false" 不算确认）。
  - 密钥只能写入、不能读出：这里没有任何返回密钥值的方法，只返回“已设置/未设置”；
    写进日志区的文字一律先脱敏（本次会话里刚填的密钥也会被认出来）。
  - 命令在后台线程执行；输出按顺序存进缓冲区，界面每隔一小段时间用 poll(after) 取新内容。
  - 页面拿到的不是 Api 本身，而是 make_bridge() 生成的白名单“门面”：任何异常都在门面里转成 {"error": 中文}，
    不会把堆栈抛给页面。
  - 纯 Python，不依赖窗口：tests/test_webui.py、tests/test_bridge_v2.py 直接调用这些方法测试。
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .. import __version__, gui_core, maintenance, yamlpatch
from ..log import redact
from . import prefs, settings_spec, tasks

MAX_IMPORT_BYTES = 1_000_000                         # 导入的配置文件大小上限
MAX_CHUNKS = 5000                                    # 日志缓冲上限，防止长时间运行占满内存
MAX_LOG_EXPORT_BYTES = 5 * 1024 * 1024               # 导出日志时每个日志文件最多取最后 5 MB
MAX_NOTE_CHARS = 200_000                             # 手动录入资讯的正文上限
MAX_ITEM_CHARS = 100                                 # 品种名长度上限
REPO_URL = "https://github.com/Sparklerrrrr/Deltaforce_quantitative-Trading"
RELEASES_API = "https://api.github.com/repos/Sparklerrrrr/Deltaforce_quantitative-Trading/releases/latest"
URLS = {"repo": REPO_URL, "releases": REPO_URL + "/releases", "issues": REPO_URL + "/issues"}
TASK_NAMES = ("dfq-nightly", "dfq-mail-poll", "dfq-watch")
DISCLAIMER = ("本程序只根据行情数据给出交易建议并记账，不操作游戏客户端、不保证收益。"
              "价格数据来自第三方平台（小涛查），手续费等参数以游戏内实际为准；所有买卖由你自己决定和执行。")
BUSY_TEXT = "上一个操作还没结束，请稍候"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_VERSION = re.compile(r"^v?(\d{1,6}(?:\.\d{1,6}){0,3})$")


@dataclass
class Action:
    label: str                                       # 显示在日志里的名称
    argv: Callable[[dict, "Api"], list[str]]         # 生成 dfq 命令参数
    target: str | None = None                        # 结果还要显示在哪个页面（plan / review / None）
    confirm: Callable[[dict, "Api"], str | None] | None = None   # 返回确认文字；None = 不需要确认
    validate: Callable[[dict, "Api"], str | None] | None = None  # 返回错误文字；None = 参数没问题
    after: str | None = None                         # 成功后接着执行的动作（如运行后刷新计划）
    needs_continue: bool = False                     # 命令中途要用户点“我已完成，继续”
    before: Callable[["Api"], Any] | None = None     # 执行前的界面状态调整（如演示自动切到模拟数据）；返回值不用
    extra: dict = field(default_factory=dict)


def _cost_text(api: "Api", llm: bool) -> str | None:
    if api.mock:
        return None
    return ("会请求小涛查接口（每晚约 13 次，首次约 21 次，按次计费，每日上限见配置）"
            + ("，并调用大模型（按 token 计费）" if llm else "")
            + "；如果开了邮件，还会发邮件。\n\n确定继续吗？")


def _run_args(llm: bool):
    def build(p: dict, api: "Api") -> list[str]:
        return ["run", "--show"] + ([] if llm else ["--offline"]) + (["--force"] if p.get("force") else [])
    return build


def _need_digits(key: str, what: str):
    def check(p: dict, api: "Api") -> str | None:
        v = str(p.get(key, "")).strip()
        return None if v.isascii() and v.isdigit() and len(v) <= 12 else f"请先填写{what}（正整数）"
    return check


def _item_problem(item: Any) -> str | None:
    """品种名：命令行的位置参数。以 - 开头会被当成命令行选项（如 -h），换行会破坏日志，一律拒绝。"""
    s = str(item if item is not None else "").strip()
    if not s:
        return "请选择或输入品种"
    if len(s) > MAX_ITEM_CHARS or any(ord(c) < 0x20 for c in s) or s.startswith("-"):
        return "品种名不对（太长、有换行或以 - 开头）"
    return None


def _num(v: Any) -> float:
    """界面传来的数字（可以是数或带千分位逗号的文字）→ 有限的 float；不合法抛 ValueError。"""
    if isinstance(v, bool):
        raise ValueError
    x = float(str(v).replace(",", "").strip())
    if not math.isfinite(x):
        raise ValueError
    return x


def _trade_check(p: dict, api: "Api") -> str | None:
    if p.get("side") not in ("buy", "sell"):
        return "请选择买入或卖出"
    bad = _item_problem(p.get("item"))
    if bad:
        return bad
    try:
        q = int(str(p.get("qty", "")).replace(",", "").strip())
        price = _num(p.get("price", ""))
        if q <= 0 or price <= 0:
            raise ValueError
    except ValueError:
        return "数量要填正整数，单价要填正数"
    if str(p.get("net") if p.get("net") is not None else "").strip():
        if p["side"] != "sell":
            return "实际到手金额只有卖出时才填"
        try:
            if _num(p["net"]) <= 0:
                raise ValueError
        except ValueError:
            return "实际到手金额要填正数（扣完手续费后真正收到的总金额）"
    return None


def _trade_argv(p: dict, api: "Api") -> list[str]:
    q = int(str(p["qty"]).replace(",", "").strip())
    price = _num(p["price"])
    net = p.get("net")
    has_net = str(net if net is not None else "").strip() != ""
    return (["trade", p["side"], str(p["item"]).strip(), str(q), str(price), "--source", "gui"]
            + (["--paper"] if p.get("paper", True) else [])
            + (["--net", str(_num(net))] if has_net else []))


def _trade_confirm(p: dict, api: "Api") -> str:
    book = "模拟账本" if p.get("paper", True) else "真实账本"
    side = "买入" if p["side"] == "buy" else "卖出"
    net = p.get("net")
    net_txt = f"\n实际到手：{_num(net):,.0f}" if str(net if net is not None else "").strip() else ""
    return f"{side} {str(p['item']).strip()} × {p['qty']} @ {p['price']}{net_txt}\n记入：{book}\n\n确定吗？"


def _balance_check(p: dict, api: "Api") -> str | None:
    try:
        v = _num(p.get("amount", ""))
        if v < 0:
            raise ValueError
    except ValueError:
        return "余额要填不小于 0 的数字"
    return None


def _probe_check(p: dict, api: "Api") -> str | None:
    if not api.mock and not api.store.is_set("DFQ_XTC_TOKEN"):
        return "还没有小涛查 token：请先到“设置”页填写"
    return None


def _inventory_check(p: dict, api: "Api") -> str | None:
    bad = _item_problem(p.get("item"))
    if bad:
        return bad
    try:
        q = int(str(p.get("qty", "")).replace(",", "").strip())
        cost = _num(p.get("cost", ""))
        if q <= 0 or cost <= 0:
            raise ValueError
    except ValueError:
        return "数量要填正整数，成本单价要填正数（不知道就填现在的市价）"
    return None


def _inventory_argv(p: dict, api: "Api") -> list[str]:
    q = int(str(p["qty"]).replace(",", "").strip())
    return (["inventory", "add", str(p["item"]).strip(), str(q), str(_num(p["cost"]))]
            + (["--paper"] if p.get("paper") else []))


def _inventory_confirm(p: dict, api: "Api") -> str:
    book = "模拟账本" if p.get("paper") else "真实账本"
    return (f"录入期初库存：{str(p['item']).strip()} × {p['qty']}，成本 {p['cost']}/发\n记入：{book}\n\n"
            "期初库存只提供成本依据，不动余额；录错了可以在成交记录里撤销。确定吗？")


def _book_flag(api: "Api") -> list[str]:
    """看“当前账本”：配置是模拟模式时加 --paper（命令行默认看真实账本）。模拟数据模式下命令行自己会看模拟账本。"""
    return ["--paper"] if api._current_paper() else []


def _moligod_login_check(p: dict, api: "Api") -> str | None:
    from .. import moligod
    return None if moligod.AUTO_SYNC_AVAILABLE else "自动设置功能暂不可用：" + moligod.UNAVAILABLE_TEXT


def _fee_check_confirm(p: dict, api: "Api") -> str | None:
    if api.mock:
        return None
    return ("会请求小涛查 1 次（按次计费；6 小时内重复运行走缓存，不再花次数），用平台数据核对卖出费率。\n\n"
            "结果只作为依据写进计划，不会自动修改配置。确定继续吗？")


ACTIONS: dict[str, Action] = {
    "demo": Action("演示（模拟数据、只用规则）", lambda p, a: ["run", "--offline", "--force", "--show"],
                   before=lambda a: a.set_mock(True), after="plan"),
    "run_rules": Action("运行（只用规则）", _run_args(False), confirm=lambda p, a: _cost_text(a, False), after="plan"),
    "run_llm": Action("运行（规则 + 大模型）", _run_args(True), confirm=lambda p, a: _cost_text(a, True), after="plan"),
    "status": Action("查看状态", lambda p, a: ["status"]),
    "probe": Action("接口实测", lambda p, a: ["probe"], validate=_probe_check,
                    confirm=lambda p, a: None if a.mock else
                    "接口实测会真实请求小涛查，约 11～13 次，程序自带 15 次上限，按次计费。\n\n"
                    "结果会写到 data/reports/接口探测报告-实测.md。确定继续吗？"),
    "mail_check": Action("检查邮件回复（只看不记账）", lambda p, a: ["mail-poll", "--dry-run"]),
    "mail_poll": Action("读取邮件回复并记账", lambda p, a: ["mail-poll"],
                        confirm=lambda p, a: "将读取邮箱里对计划邮件的回复，并把成交写入账本（模拟计划的回复只进模拟账本）。"
                                             "\n\n建议先点“检查回复”（只看不记账）。确定继续吗？"),
    "mail_test": Action("发送测试邮件", lambda p, a: ["mail-test"]),
    "plan": Action("查看最新计划", lambda p, a: ["plan"], target="plan"),
    "perf": Action("复盘绩效", lambda p, a: ["perf"], target="review"),
    "perf7": Action("近 7 天绩效", lambda p, a: ["perf", "--days", "7"], target="review"),
    "review": Action("给到期建议打分", lambda p, a: ["review"], target="review"),
    "reflect_list": Action("反思记录", lambda p, a: ["reflect", "list"], target="review"),
    "reflect_run": Action("反思（不用模型）", lambda p, a: ["reflect", "run", "--offline"], target="review"),
    "reflect_show": Action("查看反思", lambda p, a: ["reflect", "show", str(p["id"]).strip()], target="review",
                           validate=_need_digits("id", "反思编号")),
    "reflect_accept": Action("采纳反思", lambda p, a: ["reflect", "accept", str(p["id"]).strip()], target="review",
                             validate=_need_digits("id", "反思编号"),
                             confirm=lambda p, a: f"采纳反思 #{str(p['id']).strip()} 里的参数建议？下次运行起生效，"
                                                  "之后可在 data/overrides.yaml 里删除对应行撤销。"),
    "reflect_reject": Action("不采纳反思", lambda p, a: ["reflect", "reject", str(p["id"]).strip()], target="review",
                             validate=_need_digits("id", "反思编号")),
    "trade": Action("记账", _trade_argv, validate=_trade_check, confirm=_trade_confirm),
    "trade_undo": Action("撤销成交", lambda p, a: ["trade-undo", str(p["id"]).strip()],
                         validate=_need_digits("id", "成交编号"),
                         confirm=lambda p, a: f"撤销成交 #{str(p['id']).strip()}？\n\n持仓和余额会恢复到没记这一笔之前。"),
    "balance_set": Action("设置余额", lambda p, a: ["balance", "set", str(_num(p["amount"]))],
                          validate=_balance_check,
                          confirm=lambda p, a: f"把余额基准设为 {_num(p['amount']):,.0f}？"),
    "balance": Action("查看余额", lambda p, a: ["balance"]),
    "note_list": Action("已录入资讯", lambda p, a: ["note", "list"]),
    "init": Action("生成配置文件", lambda p, a: ["init"]),
    # 自动设置 moligod 提醒目前停用：动作保留（以后恢复时界面不用改），但 validate 直接拒绝，不会启动浏览器
    "moligod_login": Action("登录 moligod", lambda p, a: ["moligod-login"], needs_continue=True,
                            validate=_moligod_login_check),
    "fee_check": Action("核对费率", lambda p, a: ["fee-check"], validate=_probe_check, confirm=_fee_check_confirm),
    "exec_stats": Action("成交统计", lambda p, a: ["exec-stats", *_book_flag(a)], target="review"),
    "exec_stats7": Action("近 7 天成交统计", lambda p, a: ["exec-stats", "--days", "7", *_book_flag(a)], target="review"),
    "risk_status": Action("风控状态", lambda p, a: ["risk", "status", *_book_flag(a)], target="review"),
    "risk_resume": Action("确认风控、恢复买入建议", lambda p, a: ["risk", "resume", *_book_flag(a)], target="review",
                          confirm=lambda p, a: "确认你已经看过暂停原因，恢复新的买入建议？\n\n"
                                               "之后只有出现新的亏损才会再次暂停。卖出和止损建议不受影响。"),
    "inventory_add": Action("录入期初库存", _inventory_argv, validate=_inventory_check, confirm=_inventory_confirm),
    "inventory_list": Action("期初库存", lambda p, a: ["inventory", "list", *_book_flag(a)]),
}


def _size_text(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} 字节"


def _confirmed(v: Any) -> bool:
    """只有真正的 true 才算用户确认过（防止页面传来字符串 "false" 也被当成确认）。"""
    return v is True


def _atomic_write(path: Path, data: bytes) -> None:
    """先写同一文件夹里的临时文件，再 os.replace：中途断电 / 出错时原文件完好，不会只写了一半。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _run_powershell(script: str, timeout: float = 10) -> tuple[int, str]:
    """运行一段 PowerShell，返回 (退出码, 标准输出)。不弹黑窗口。测试里会被替换掉。"""
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return p.returncode, (p.stdout or b"").decode("utf-8", errors="replace")


# 读取三个计划任务的状态（输出 JSON）。先把输出编码设成 UTF-8：安装路径里有中文时不乱码。
_TASKS_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$out = @()
foreach ($n in @('dfq-nightly', 'dfq-mail-poll', 'dfq-watch')) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if (-not $t) { continue }
  $i = $t | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
  $a = @($t.Actions)[0]
  $next = $null; $last = $null; $res = $null
  if ($i) {
    if ($i.NextRunTime) { $next = $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') }
    if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $last = $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') }
    $res = [int64]$i.LastTaskResult
  }
  $out += [pscustomobject]@{ name = $n; state = [string]$t.State; next = $next; last = $last; result = $res;
                             exe = [string]$a.Execute; args = [string]$a.Arguments; wd = [string]$a.WorkingDirectory }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 3
"""


def _webview2_version() -> str | None:
    """WebView2 运行时版本（注册表里 EdgeUpdate 记录的版本号）。读不到就返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}")]
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
            if isinstance(v, str) and re.fullmatch(r"[\d.]{1,40}", v) and v != "0.0.0.0":
                return v
        except OSError:
            continue
    return None


def _version_tuple(v: Any) -> tuple[int, ...] | None:
    m = _VERSION.match(str(v or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


class Api:
    """pywebview 会把 make_bridge(Api) 的方法暴露给页面：window.pywebview.api.<方法名>(...)

    app.py 注入的回调都以下划线开头（_theme_handler / _scale_handler / _close_handler / _done_handler / _window），
    不在白名单里、页面调不到；它们都在后台线程里被调用。"""

    def __init__(self, config_path: str | Path, store: gui_core.SecretStore | None = None,
                 runner: Callable[..., int] = gui_core.run_cli):
        self._config_path = Path(config_path).resolve()
        self._base = self._config_path.parent
        self.store = store or gui_core.SecretStore(gui_core.default_backend(self._base))
        self._runner = runner
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()           # 改 config.yaml / 备份恢复 / 清理：同一时间只做一件
        self._chunks: list[tuple[int, str]] = []     # (序号, 文本)
        self._seq = 0
        self._results: dict[str, str] = {}           # 页面名 → 最近一次结果
        self._results_seq = 0
        self._busy_label: str | None = None
        self._continue = threading.Event()
        self._waiting_continue = False
        self._http_transport = None                  # 测试用：替换 check_update 的网络请求
        self.mock: bool = False
        loaded =self.store.load_into_process()
        if loaded:
            self._emit(f"已读取密钥：{'、'.join(loaded)}（只读取是否存在，不显示内容）\n")
        if not self._config_path.exists():
            self._emit(f"还没有配置文件 {self._config_path.name}，当前使用内置默认配置。可以在“设置”页生成。\n")

    # ------------------------------------------------------------------ 内部
    def _emit(self, text: str) -> None:
        text = redact(text)                          # 日志区和导出的日志里都不能出现密钥（含本次会话刚填的）
        with self._lock:
            self._seq += 1
            self._chunks.append((self._seq, text))
            if len(self._chunks) > MAX_CHUNKS:
                del self._chunks[: len(self._chunks) - MAX_CHUNKS]

    def _cfg(self):
        cfg, err = gui_core.load_config(self._config_path)
        if cfg is not None and self.mock:
            cfg.set("general.data_dir", str(cfg.data_dir / "mock"))
        return cfg, err

    def _data_dir(self) -> Path:
        """真实数据目录（不受“模拟数据”开关影响）；配置读不了时退回 <程序文件夹>/data。"""
        cfg, _ = gui_core.load_config(self._config_path)
        return cfg.data_dir if cfg is not None else self._base / "data"

    def _current_paper(self) -> bool:
        cfg, _ = gui_core.load_config(self._config_path)
        return bool(cfg.paper) if cfg is not None else True

    @contextmanager
    def _open_db(self, cfg) -> Iterator[Any]:
        """打开数据库（不存在时给 None，不为了“读一下”去新建数据库文件）。"""
        from ..db import DB
        path = cfg.data_dir / "dfq.db"
        if not path.exists():
            yield None
            return
        db = DB(path)
        try:
            yield db
        finally:
            db.close()

    @property
    def busy(self) -> bool:
        return self._busy_label is not None

    # ------------------------------------------------------------------ 状态
    def state(self) -> dict:
        """界面需要显示的全部状态（不含任何密钥值）。"""
        from .. import fees, moligod
        cfg, err = self._cfg()
        usage = None
        fee = None
        if cfg is not None:
            fee = {"rate": cfg.fee, "evidence": None, "calibrated": None}
            try:
                with self._open_db(cfg) as db:
                    if db is not None:
                        usage = {"today": db.usage_on("xtc"), "limit": cfg.get("xtc.daily_limit")}
                        fee["evidence"] = fees.evidence(db)
                        fee["calibrated"] = fees.calibration(db, paper=cfg.paper, fee=cfg.fee_for)
                    else:
                        usage = {"today": 0, "limit": cfg.get("xtc.daily_limit")}
            except Exception:
                usage = None
        return {
            "version": __version__,
            "mock": self.mock,
            "busy": self.busy,
            "busy_label": self._busy_label,
            "waiting_continue": self._waiting_continue,
            "config_path": str(self._config_path),
            "config_exists": self._config_path.exists(),
            "config_error": err,
            "paper": bool(cfg.paper) if cfg is not None else None,
            "book": None if cfg is None else ("paper" if cfg.paper else "real"),
            "features": None if cfg is None else {
                "llm": bool(cfg.get("llm.enabled")), "mail": bool(cfg.get("mail.enabled")),
                "news": bool(cfg.get("intel.news.enabled")), "moligod": moligod.auto_enabled(cfg),
            },
            "moligod": None if cfg is None else {
                "mode": "auto" if moligod.auto_enabled(cfg) else "manual",
                "auto_available": bool(moligod.AUTO_SYNC_AVAILABLE),
                "configured_auto": bool(cfg.get("moligod.enabled")),
                "url": redact(str(cfg.get("moligod.url") or "")),
            },
            "fee": fee,
            "usage": usage,
            "secrets": [{"name": n, "desc": d, "set": self.store.is_set(n)} for n, d in gui_core.SECRETS],
            "secret_where": self.store.backend.where,
            "secret_writable": self.store.backend.available,
            "items": gui_core.item_choices(cfg),
        }

    def tasks(self) -> dict:
        return tasks.as_json()

    def trades(self) -> list[dict]:
        cfg, _ = self._cfg()
        if cfg is None:
            return []
        try:
            rows = gui_core.recent_trades(cfg)
        except Exception as e:                       # 数据库被占用等：不影响界面
            self._emit(f"读取成交记录失败：{type(e).__name__}\n")
            return []
        return [{"id": r["id"], "ts": (r["ts"] or "")[:16], "side": r["side"], "name": r["name"],
                 "qty": r["qty"], "price": round(float(r["price"])), "paper": bool(r["paper"]),
                 "source": r["source"] or "", "note": r["note"] or ""} for r in rows]

    def poll(self, after: int = 0, results_after: int = 0) -> dict:
        """取 after 之后的新输出；结果页有更新时一并返回。"""
        try:
            after, results_after = int(after or 0), int(results_after or 0)
        except (TypeError, ValueError):
            after = results_after = 0
        with self._lock:
            new = [t for s, t in self._chunks if s > after]
            seq = self._seq
            res = dict(self._results) if self._results_seq > results_after else None
            rseq = self._results_seq
        return {"text": "".join(new), "seq": seq, "results": res, "results_seq": rseq,
                "busy": self.busy, "busy_label": self._busy_label, "waiting_continue": self._waiting_continue}

    def clear_log(self) -> bool:
        with self._lock:
            self._chunks.clear()
        return True

    def set_mock(self, value: bool) -> dict:
        if self.busy:
            return {"error": "正在运行，结束后再切换"}
        self.mock = bool(value)
        return {"ok": True}

    # ------------------------------------------------------------------ 动作
    def run(self, action_id: str, params: dict | None = None, confirmed: bool = False) -> dict:
        """执行一个动作。返回 {"ok"} / {"error"} / {"confirm": {"title", "text"}}（需要用户确认后带 confirmed=True 再调一次）。"""
        if not isinstance(action_id, str):
            return {"error": "未知操作"}
        if params is not None and not isinstance(params, dict):
            return {"error": "参数格式不对"}
        params = dict(params or {})
        act = ACTIONS.get(action_id)
        if act is None:
            return {"error": f"未知操作：{action_id[:40]}"}
        with self._exclusive() as ok:
            if not ok:
                return {"error": BUSY_TEXT}
            if act.before:
                act.before(self)
            if act.validate:
                msg = act.validate(params, self)
                if msg:
                    return {"error": msg}
            text = act.confirm(params, self) if act.confirm else None
            if text and not _confirmed(confirmed):
                return {"confirm": {"title": act.label, "text": text}}
            self._start(action_id, act, params)
        return {"ok": True}

    @contextmanager
    def _exclusive(self) -> Iterator[bool]:
        """启动后台命令前占住文件锁再看 busy：和“改配置 / 清理 / 恢复备份”互斥。

        pywebview 每次调用都在新线程里执行，只看 busy 有空档：恢复备份刚检查完 busy、还没开始写库，
        这边就可能启动一次运行去读写同一个数据库。文件操作正在进行（锁被占着）时直接当作忙，不排队等待。"""
        if not self._file_lock.acquire(blocking=False):
            yield False
            return
        try:
            yield not self.busy
        finally:
            self._file_lock.release()

    def _start(self, action_id: str, act: Action, params: dict, runner: Callable[..., int] | None = None,
               notify_label: str | None = None) -> None:
        runner = runner or self._runner                # 在这里取定，后台线程不再读 self._runner
        argv = act.argv(params, self)
        full = [argv[0], "--config", str(self._config_path), *(["--mock"] if self.mock else []), *argv[1:]]
        self._busy_label = act.label
        self._waiting_continue = act.needs_continue
        self._continue.clear()
        if act.target:
            with self._lock:
                self._results[act.target] = ""
                self._results_seq += 1
        self._emit(f"\n▶ {act.label}\n")
        label = notify_label or act.label              # 接着执行的动作（运行后刷新计划）通知时用最初那个动作的名字

        def emit(text: str) -> None:
            self._emit(text)
            if act.target:
                with self._lock:
                    self._results[act.target] = self._results.get(act.target, "") + redact(text)
                    self._results_seq += 1

        def work() -> None:
            code = 1
            try:
                code = runner(full, emit, continue_event=self._continue if act.needs_continue else None)
            finally:
                self._emit(f"■ 结束（{'成功' if code == 0 else f'返回码 {code}'}）\n")
                self._busy_label = None
                self._waiting_continue = False
            if code == 0 and act.after and act.after in ACTIONS:
                self._start(act.after, ACTIONS[act.after], {}, notify_label=label)
                return
            handler = getattr(self, "_done_handler", None)   # app.py 注入：窗口在托盘里时弹系统通知
            if handler is not None:
                try:
                    handler(label, code == 0)
                except Exception:                    # 通知失败不能影响任何东西
                    pass

        threading.Thread(target=work, daemon=True).start()

    def continue_(self) -> bool:
        self._continue.set()
        return True

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """测试用：等当前动作（包括接着执行的动作）全部结束。"""
        end = time.time() + timeout
        quiet_since = None
        while time.time() < end:
            if self.busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.time()
            elif time.time() - quiet_since > 0.15:
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------ 资讯
    def note_add(self, source: str, name: str, title: str, text: str, confirmed: bool = False) -> dict:
        if not all(isinstance(x, str) or x is None for x in (source, name, title, text)):
            return {"error": "内容格式不对"}
        text, name, title = (text or "").strip(), (name or "").strip(), (title or "").strip()
        if not text or not name:
            return {"error": "请填写信源名并粘贴正文"}
        if source not in ("bili", "qq", "news", "other"):
            return {"error": "来源不对"}
        if len(text) > MAX_NOTE_CHARS or len(name) > 100 or len(title) > 300:
            return {"error": f"内容太长：正文最多 {MAX_NOTE_CHARS:,} 字，信源名最多 100 字，标题最多 300 字"}
        if name.startswith("-") or title.startswith("-") or any(ord(c) < 0x20 for c in name + title):
            return {"error": "信源名和标题不能以 - 开头，也不能有换行"}
        with self._exclusive() as ok:
            if not ok:
                return {"error": BUSY_TEXT}
            self._start_note(source, name, title, text)
        return {"ok": True}

    def _start_note(self, source: str, name: str, title: str, text: str) -> None:
        tmp_dir = self._base / "data" / "tmp"            # 临时文件也放在程序自己的文件夹里，不写系统临时目录
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="dfq_note_", dir=str(tmp_dir))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        argv = ["note", "add", "--source", source, "--name", name, "--file", path]
        if title:
            argv += ["--title", title]

        def cleanup_runner(full, emit, continue_event=None):
            try:
                return self._runner(full, emit, continue_event=continue_event)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._start("note_add", Action("录入资讯", lambda p, a: argv), {}, runner=cleanup_runner)

    # ------------------------------------------------------------------ 密钥
    def set_secret(self, name: str, value: str) -> dict:
        if not isinstance(name, str) or not isinstance(value, str):
            return {"error": "内容格式不对，没有保存"}
        try:
            self.store.set(name, value)
        except Exception as e:
            return {"error": redact(str(e))}
        finally:
            del value                                # 不再持有密钥原文
        self._emit(f"已保存 {name}（内容不显示）。\n")
        return {"ok": True}

    def clear_secret(self, name: str, confirmed: bool = False) -> dict:
        if not isinstance(name, str) or name not in gui_core.SECRET_NAMES:
            return {"error": "不认识的密钥名"}
        if not _confirmed(confirmed):
            return {"confirm": {"title": "确认清除", "text": f"从 .env 里删除 {name}？"}}
        try:
            self.store.clear(name)
        except Exception as e:
            return {"error": redact(str(e))}
        self._emit(f"已清除 {name}。\n")
        return {"ok": True}

    # ------------------------------------------------------------------ 设置
    def check_config(self) -> dict:
        cfg, err = gui_core.load_config(self._config_path)
        if err:
            self._emit(err + "\n")
            return {"error": err}
        return {"ok": True, "notes": list(cfg.notes)}

    def reset_config(self, confirmed: bool = False) -> dict:
        """配置坏了时的安全恢复：先把现在的 config.yaml 备份到 data/backups，再用模板重新生成。不碰 .env 和数据。"""
        from .. import layout
        src = layout.resource("config.example.yaml")
        if src is None:
            return {"error": "找不到配置模板 config.example.yaml，安装可能不完整，请重新安装"}
        if not _confirmed(confirmed):
            return {"confirm": {"title": "恢复默认配置",
                                "text": "将用默认模板替换当前的 config.yaml。\n\n当前的 config.yaml 会先备份到 data\\backups，"
                                        "密钥（.env）和交易记录都不受影响。确定吗？"}}
        if self.busy:
            return {"error": BUSY_TEXT}
        with self._file_lock:
            if self.busy:                                # 拿到锁后再看一次：检查和动手之间不会插进一次运行
                return {"error": BUSY_TEXT}
            backup = None
            if self._config_path.exists():
                backup = self._base / "data" / "backups" / time.strftime("%Y%m%d-%H%M%S")
                backup.mkdir(parents=True, exist_ok=True)
                backup.joinpath("config.yaml").write_bytes(self._config_path.read_bytes())
            _atomic_write(self._config_path, src.read_bytes())
        self._emit("已恢复默认配置" + (f"（旧文件备份在 {backup}）" if backup else "") + "\n")
        return {"ok": True, "backup": str(backup) if backup else None}

    def open_path(self, what: str) -> dict:
        if not isinstance(what, str):
            return {"error": "未知位置"}
        target = {"data": self._base / "data", "config": self._config_path, "folder": self._base,
                  "docs": self._base / "docs"}.get(what)
        if target is None:
            return {"error": "未知位置"}
        if not target.exists():
            return {"error": f"{target} 还不存在（运行过一次之后才会有）"}
        if sys.platform != "win32":
            return {"error": str(target)}
        if what == "config":
            subprocess.Popen(["notepad.exe", str(target)])
        else:
            os.startfile(str(target))                # noqa: S606  打开文件夹
        return {"ok": True}

    def export_logs(self) -> dict:
        """学 MaaEnd：一键把日志打包成 zip，反馈问题时直接发这个文件。

        打包前每一份内容都再脱敏一遍（已登记的密钥原文、URL 里的 token=…、Bearer …）：
        日志文件本身写盘时已脱敏，这里是第二道保险。只打包日志文件夹里的普通文件，
        跳过符号链接 / 目录联接（防止有人把 .env 链接成 xxx.log 混进来），.env 和 config.yaml 不会被打包。"""
        cfg, _ = self._cfg()
        self._register_all_secrets()
        out_dir = self._base / "data" / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        zpath = out_dir / f"DFQ-logs-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        with self._lock:
            console = "".join(t for _, t in self._chunks)
        # 程序日志在数据目录的 logs 里；计划任务的输出（task-日期.log）固定写在程序文件夹的 data\logs 里。两处可能是同一个
        log_dirs = list(dict.fromkeys([(cfg.data_dir if cfg is not None else self._base / "data") / "logs",
                                       self._base / "data" / "logs"]))
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("界面运行日志.txt", redact(console))
            z.writestr("版本.txt", f"dfq {__version__}\nPython {sys.version}\n")
            files = []
            for log_dir in log_dirs:
                if log_dir.is_dir() and not maintenance._is_link(log_dir):
                    files += [p for p in log_dir.iterdir()
                              if p.suffix in (".log", ".txt", ".jsonl") and not maintenance._is_link(p) and p.is_file()]

            def mtime(p: Path) -> float:
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0
            files = sorted({p.name: p for p in files}.values(), key=mtime)  # 同名只取一份；按修改时间取最新的 10 份
            for p in files[-10:]:
                try:
                    with open(p, "rb") as fh:
                        size = os.fstat(fh.fileno()).st_size
                        if size > MAX_LOG_EXPORT_BYTES:
                            fh.seek(size - MAX_LOG_EXPORT_BYTES)
                        raw = fh.read(MAX_LOG_EXPORT_BYTES)
                except OSError:
                    continue
                z.writestr(f"logs/{p.name}", redact(raw.decode("utf-8", errors="replace")))
        self._emit(f"日志已导出：{zpath}\n")
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", "/select,", str(zpath)])
        return {"ok": True, "path": str(zpath)}

    def _register_all_secrets(self) -> None:
        """导出日志前，把所有登记过名字的密钥原文都登记给脱敏器（只登记，不显示、不返回）。

        界面启动时只登记了界面管理的那几个密钥；.env 里的其他密钥（如 B 站 cookie）只有命令行读过，
        计划任务写的 task-日期.log 里如果带出了它们，单靠界面进程的登记认不出来。"""
        from .. import envfile
        from ..log import SECRET_ENV_NAMES, register_env_secrets, register_secret
        register_env_secrets()
        env_path = self._base / envfile.FILENAME
        try:
            if env_path.is_file() and not maintenance._is_link(env_path):
                for k, v in envfile.read(env_path).items():
                    if k in SECRET_ENV_NAMES:
                        register_secret(v)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ 计划任务
    def install_tasks(self, confirmed: bool = False, nightly_at: str = "", poll_minutes: int = 0) -> dict:
        script = self._base / "scripts" / "install_tasks.ps1"
        if not isinstance(nightly_at, str) or (nightly_at and not _HHMM.match(nightly_at)):
            return {"error": "每晚运行时间要写成 HH:MM（如 21:30）"}
        if isinstance(poll_minutes, bool) or not isinstance(poll_minutes, int) or \
                not (poll_minutes == 0 or 5 <= poll_minutes <= 720):
            return {"error": "读取邮件的间隔要在 5～720 分钟之间"}
        if sys.platform != "win32":
            return {"error": "计划任务只能在 Windows 上创建"}
        if not script.exists():
            return {"error": f"没有找到 {script}。安装目录可能不完整，请重新安装。"}
        frozen = gui_core.is_frozen()                   # 安装版：计划任务直接调用 DFQ.exe，不需要 .venv
        if not _confirmed(confirmed):
            extra = "" if frozen or (self._base / ".venv").exists() else \
                "\n\n注意：没有找到项目里的 .venv，计划任务需要它（见 README 安装步骤）。"
            return {"confirm": {"title": "创建计划任务",
                                "text": f"将创建两个计划任务：每晚 {nightly_at or '21:30'} 运行一次、"
                                        f"每 {poll_minutes or 30} 分钟读取一次邮件回复。" + extra + "\n\n确定吗？"}}
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        if frozen:
            cmd += ["-Exe", sys.executable]
        if nightly_at:
            cmd += ["-NightlyAt", nightly_at]
        if poll_minutes:
            cmd += ["-PollMinutes", str(poll_minutes)]
        with self._exclusive() as ok:
            if not ok:
                return {"error": BUSY_TEXT}
            self._start("install_tasks", Action("创建计划任务", lambda p, a: ["install-tasks"]), {},
                        runner=self._ps_file_runner(cmd))
        return {"ok": True}

    def _ps_file_runner(self, cmd: list[str]):
        def runner(full, emit, continue_event=None):
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               cwd=str(self._base), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            emit((p.stdout or "") + (p.stderr or ""))
            return p.returncode
        return runner

    def remove_tasks(self, confirmed: bool = False) -> dict:
        script = self._base / "scripts" / "install_tasks.ps1"
        if sys.platform != "win32":
            return {"error": "计划任务只能在 Windows 上管理"}
        if not script.exists():
            return {"error": f"没有找到 {script}。安装目录可能不完整，请重新安装。"}
        if not _confirmed(confirmed):
            return {"confirm": {"title": "删除计划任务",
                                "text": "将删除 dfq-nightly、dfq-mail-poll、dfq-watch 三个计划任务（存在的才删）。\n\n"
                                        "之后程序不会再每晚自动运行、也不会自动读取邮件回复；数据和配置不受影响。确定吗？"}}
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Remove"]
        with self._exclusive() as ok:
            if not ok:
                return {"error": BUSY_TEXT}
            self._start("remove_tasks", Action("删除计划任务", lambda p, a: ["remove-tasks"]), {},
                        runner=self._ps_file_runner(cmd))
        return {"ok": True}

    def tasks_status(self) -> dict:
        """三个计划任务现在的状态（只读）。读不到时当作不存在。"""
        empty = [{"name": n, "exists": False, "state": None, "next_run": None, "last_run": None,
                  "last_result": None, "points_here": None} for n in TASK_NAMES]
        if sys.platform != "win32":
            return {"supported": False, "tasks": empty}
        try:
            code, out = _run_powershell(_TASKS_PS, timeout=10)
            data = json.loads(out.strip() or "[]") if code == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            return {"supported": True, "tasks": empty}
        if isinstance(data, dict):
            data = [data]
        found = {d.get("name"): d for d in data if isinstance(d, dict)} if isinstance(data, list) else {}
        here = str(self._base).lower().rstrip("\\/")
        rows = []
        for n in TASK_NAMES:
            d = found.get(n)
            if d is None:
                rows.append({"name": n, "exists": False, "state": None, "next_run": None, "last_run": None,
                             "last_result": None, "points_here": None})
                continue
            where = " ".join(str(d.get(k) or "") for k in ("exe", "args", "wd")).lower()
            res = d.get("result")
            rows.append({"name": n, "exists": True, "state": str(d.get("state") or "")[:40] or None,
                         "next_run": str(d["next"])[:16] if d.get("next") else None,
                         "last_run": str(d["last"])[:16] if d.get("last") else None,
                         "last_result": int(res) if isinstance(res, (int, float)) and not isinstance(res, bool) else None,
                         "points_here": bool(here) and here in where})
        return {"supported": True, "tasks": rows}

    # ------------------------------------------------------------------ 配置导入 / 导出
    def export_config(self, include_secrets: bool = False, confirmed: bool = False, folder: str | None = None) -> dict:
        """导出配置为一个 zip（config.yaml + 说明；勾选时再加 .env）。不传 folder 时弹出“选择文件夹”。"""
        cfg_file = self._config_path
        if not cfg_file.exists():
            return {"error": "还没有 config.yaml，没有可导出的配置（可以先在“设置”页生成）"}
        if folder is not None and (not isinstance(folder, str) or len(folder) > 1000):
            return {"error": "文件夹路径不对"}
        include_secrets = include_secrets is True
        env_file = self._base / ".env"
        if include_secrets and not _confirmed(confirmed):
            return {"confirm": {"title": "同时导出密钥？",
                                "text": "导出的文件里会包含 .env 中的 API Key 和 token（明文）。\n\n"
                                        "拿到这个文件的人就能用你的账号消耗付费次数。请只保存在自己的电脑或私人网盘里，"
                                        "不要发给别人、不要传到群里。\n\n确定同时导出密钥吗？"}}
        if folder is None:
            folder = self._pick_folder()
            if not folder:
                return {"cancelled": True}
        out_dir = Path(folder)
        if not out_dir.is_dir():
            return {"error": f"文件夹不存在：{out_dir}"}
        name = f"DFQ配置备份-{time.strftime('%Y%m%d-%H%M%S')}{'-含密钥' if include_secrets else ''}.zip"
        zpath = out_dir / name
        manifest = {"app": "dfq", "version": __version__, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "files": ["config.yaml"] + ([".env"] if include_secrets and env_file.exists() else [])}
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("config.yaml", cfg_file.read_bytes())
            if include_secrets and env_file.is_file() and not maintenance._is_link(env_file):
                z.writestr(".env", env_file.read_bytes())
            z.writestr("dfq-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        self._emit(f"配置已导出：{zpath}{'（含密钥）' if include_secrets else '（不含密钥）'}\n")
        return {"ok": True, "path": str(zpath)}

    def _pick_folder(self) -> str | None:
        win = getattr(self, "_window", None)
        if win is None:
            return None
        import webview
        dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        res = win.create_file_dialog(dialog)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def import_config(self, filename: str, content_b64: str, confirmed: bool = False) -> dict:
        """导入配置：拖进来的 .zip（本程序导出的）/ .yaml / .env。先完整校验，再备份旧文件、替换。

        安全：只读压缩包里名字恰好是 config.yaml / .env 的两项（不解压任何东西到磁盘，路径穿越无从发生）；
        不相信压缩包头里写的大小，边解压边数（防压缩炸弹）；YAML 用和启动时同样的 safe_load + 严格校验；
        .env 里只认登记过的密钥名。"""
        import base64
        import binascii
        from .. import envfile
        if not isinstance(filename, str) or len(filename) > 260 or not isinstance(content_b64, str):
            return {"error": "文件读取失败"}
        if len(content_b64) > (MAX_IMPORT_BYTES * 4) // 3 + 8:       # 先看编码后的长度，不为解码大文件占内存
            return {"error": "文件太大，不像是配置文件"}
        try:
            raw = base64.b64decode(content_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            return {"error": "文件读取失败"}
        if not raw:
            return {"error": "文件是空的"}
        if len(raw) > MAX_IMPORT_BYTES:
            return {"error": "文件太大，不像是配置文件"}
        if self.busy:
            return {"error": BUSY_TEXT}
        ext = Path(filename or "").suffix.lower()
        new_cfg: bytes | None = None
        new_env: bytes | None = None
        if ext == ".zip":
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    names = set(z.namelist())
                    if "config.yaml" not in names and ".env" not in names:
                        return {"error": "压缩包里没有 config.yaml 或 .env，不是本程序导出的配置"}
                    for member, slot in (("config.yaml", "cfg"), (".env", "env")):
                        if member in names:
                            # 不相信压缩包头里写的大小（可以伪造成“压缩炸弹”）：边解压边数，超过上限就停
                            with z.open(member) as fh:
                                data = fh.read(MAX_IMPORT_BYTES + 1)
                            if len(data) > MAX_IMPORT_BYTES:
                                return {"error": f"{member} 太大"}
                            if slot == "cfg":
                                new_cfg = data
                            else:
                                new_env = data
            except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, OSError):
                return {"error": "压缩包损坏、加了密码或用了不支持的压缩方式，无法读取"}
        elif ext in (".yaml", ".yml"):
            new_cfg = raw
        elif ext == ".env" or (filename or "").lower().endswith(".env"):
            new_env = raw
        else:
            return {"error": "只支持本程序导出的 .zip、config.yaml（.yaml/.yml）或 .env 文件"}

        problems, summary = [], []
        if new_cfg is not None:
            try:
                text = new_cfg.decode("utf-8-sig")
            except UnicodeDecodeError:
                return {"error": "config.yaml 不是 UTF-8 文本"}
            err = self._check_config_text(text)          # 和启动时完全一样的严格校验
            if err:
                problems.append("config.yaml 校验没通过：\n" + err)
            summary.append("替换当前的 config.yaml")
        env_keys: list[str] = []
        values: dict[str, str] = {}
        if new_env is not None:
            allv = self._read_env_bytes(new_env)
            # 只导入界面能管理的密钥（SecretStore 认识的名字）；其余登记过的（如 B 站 cookie）不在界面里管，跳过并说明
            values = {k: v for k, v in allv.items() if k in gui_core.SECRET_NAMES}
            skipped = sorted(set(allv) - set(values))
            env_keys = sorted(values)
            if not env_keys:
                problems.append(".env 里没有可识别的密钥")
            else:
                summary.append(f"写入密钥：{'、'.join(env_keys)}（同名的会被覆盖）"
                               + (f"；跳过界面不管理的：{'、'.join(skipped)}" if skipped else ""))
        if problems:
            return {"error": "\n\n".join(problems)}
        if not _confirmed(confirmed):
            return {"confirm": {"title": "导入配置",
                                "text": f"文件：{filename}\n\n将会：\n· " + "\n· ".join(summary)
                                        + "\n\n旧文件会先备份到 data\\backups。确定导入吗？"}}
        with self._file_lock:
            if self.busy:
                return {"error": BUSY_TEXT}
            backup = self._base / "data" / "backups" / time.strftime("%Y%m%d-%H%M%S")
            backup.mkdir(parents=True, exist_ok=True)
            if new_cfg is not None:
                if self._config_path.exists():
                    backup.joinpath("config.yaml").write_bytes(self._config_path.read_bytes())
                text = new_cfg.decode("utf-8-sig").replace("\r\n", "\n")
                _atomic_write(self._config_path, text.encode("utf-8"))
            if new_env is not None:
                env_path = self._base / envfile.FILENAME
                if env_path.exists():
                    backup.joinpath(".env").write_bytes(env_path.read_bytes())
                failed = []
                for k, v in values.items():
                    try:
                        self.store.set(k, v)                   # 走同一套校验，并立即生效
                    except Exception:
                        failed.append(k)
                if failed:
                    self._emit(f"这些密钥格式不对，没有导入：{'、'.join(failed)}\n")
        self._emit(f"已导入配置：{filename}（旧文件备份在 {backup}）\n")
        return {"ok": True, "backup": str(backup)}

    def _read_env_bytes(self, data: bytes) -> dict[str, str]:
        """用 envfile 的同一套解析规则读 .env 内容（只认登记过的密钥名）。临时文件放在程序自己的 data/tmp 里，读完即删。"""
        from .. import envfile
        tmp_dir = self._base / "data" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="import-check.", suffix=".env", dir=str(tmp_dir))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return dict(envfile.read(Path(tmp)))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _check_config_text(self, text: str) -> str | None:
        """把一份 config.yaml 文本写到 data/tmp 的临时文件，按启动时同样的规则严格校验。返回错误说明，通过返回 None。"""
        tmp_dir = self._base / "data" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="config-check.", suffix=".yaml", dir=str(tmp_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text.lstrip("\ufeff"))
            _, err = gui_core.load_config(Path(tmp))
            return err
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def quit_check(self) -> dict:
        """关窗前问一下：正在运行时提醒。"""
        return {"busy": self.busy, "label": self._busy_label}

    # ------------------------------------------------------------------ 界面偏好
    def ui_prefs(self) -> dict:
        return prefs.load(self._base)

    def set_pref(self, key: str, value: Any) -> dict:
        """改一项界面偏好（BRIDGE.md 第 1 节）。window 由 app.py 维护，页面不能改。"""
        if not isinstance(key, str) or key not in prefs.PAGE_KEYS:
            return {"error": f"不能修改的界面设置：{str(key)[:40]}"}
        old = prefs.load(self._base).get(key)
        try:
            cur = prefs.save(self._base, **{key: value})
        except ValueError as e:
            return {"error": str(e)}
        handler = {"theme": "_theme_handler", "scale": "_scale_handler"}.get(key)
        fn = getattr(self, handler, None) if handler else None
        if fn is not None:
            threading.Thread(target=fn, args=(cur[key],), daemon=True).start()
        return {"ok": True, "prefs": cur, "restart": key in prefs.RESTART_KEYS and cur[key] != old}

    def set_close_action(self, value: str) -> dict:
        """设置页：关闭窗口时 每次询问 / 最小化到托盘 / 退出程序。"""
        try:
            return {"ok": True, "prefs": prefs.save(self._base, close_action=value)}
        except ValueError as e:
            return {"error": str(e)}

    def set_theme(self, theme: str) -> dict:
        """页面切换深色/浅色时告诉后端：窗口底色跟着变，缩放窗口时露出的边缘不会闪一下浅色。"""
        try:
            prefs.save(self._base, theme=theme)
        except ValueError as e:
            return {"error": str(e)}
        handler = getattr(self, "_theme_handler", None)
        if handler is not None:
            threading.Thread(target=handler, args=(theme,), daemon=True).start()
        return {"ok": True}

    def close_choice(self, action: str, remember: bool = False) -> dict:
        """关闭提示框里的选择。action：tray（最小化到托盘）/ quit（退出程序）。"""
        if action not in ("tray", "quit"):
            return {"error": "不认识的选择"}
        if remember is True:
            prefs.save(self._base, close_action=action)
        handler = getattr(self, "_close_handler", None)
        if handler is not None:
            threading.Thread(target=handler, args=(action,), daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ 配置项（config.yaml）在界面里直接改
    def settings(self) -> dict:
        """设置页的表单：分组、清单、当前生效值（含默认值）。不含任何密钥（密钥不在 config.yaml 里）。"""
        cfg, err = gui_core.load_config(self._config_path)
        base = {"groups": settings_spec.GROUPS, "spec": settings_spec.SPEC}
        if cfg is None:
            return {"ok": False, "error": err, **base, "values": {}, "from_overrides": []}
        values = {}
        for s in settings_spec.SPEC:
            v = cfg.get(s["key"])
            values[s["key"]] = redact(v) if isinstance(v, str) else v
        return {"ok": True, "error": None, **base, "values": values, "from_overrides": self._overridden_keys(cfg)}

    def _overridden_keys(self, cfg) -> list[str]:
        """被 data/overrides.yaml（采纳过的反思建议）覆盖的设置项。"""
        import yaml
        p = cfg.data_dir / "overrides.yaml"
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
        except (OSError, ValueError, yaml.YAMLError):
            return []
        flat: set[str] = set()

        def walk(d: Any, prefix: str) -> None:
            if isinstance(d, dict):
                for k, v in d.items():
                    walk(v, f"{prefix}{k}.")
            elif prefix:
                flat.add(prefix[:-1])
        walk(data, "")
        return [s["key"] for s in settings_spec.SPEC if s["key"] in flat]

    @staticmethod
    def _coerce(spec: dict, value: Any) -> Any:
        """按 SPEC 的类型转换 / 校验界面传来的值。不合法抛 ValueError（中文说明）。范围的权威校验在 config_check。"""
        t = spec["type"]
        if t == "bool":
            if not isinstance(value, bool):
                raise ValueError("只能是开或关")
            return value
        if t == "int":
            if isinstance(value, bool):
                raise ValueError("要填整数")
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            elif isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,15}\s*", value):
                value = int(value)
            if not isinstance(value, int) or abs(value) > 10 ** 15:
                raise ValueError("要填整数")
            return value
        if t in ("float", "percent"):
            if isinstance(value, bool):
                raise ValueError("要填数字")
            try:
                x = float(value.strip()) if isinstance(value, str) and len(value) <= 40 else float(value)
            except (TypeError, ValueError):
                raise ValueError("要填数字") from None
            if not math.isfinite(x) or abs(x) > 1e15:
                raise ValueError("要填有限的数字")
            return x
        if t == "text":
            if not isinstance(value, str):
                raise ValueError("要填文字")
            s = value.strip()
            if len(s) > settings_spec.TEXT_MAX:
                raise ValueError(f"太长了（最多 {settings_spec.TEXT_MAX} 个字）")
            if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
                raise ValueError("不能有换行或控制字符")
            return s
        if t == "choice":
            allowed = [c[0] for c in spec.get("choices") or []]
            if value not in allowed or isinstance(value, bool) != any(isinstance(a, bool) for a in allowed):
                raise ValueError("不是可选的值")
            return value
        raise ValueError("不支持的设置类型")

    @staticmethod
    def _display(spec: dict, value: Any) -> str:
        if spec["type"] == "bool":
            return "开" if value else "关"
        if spec["type"] == "percent":
            return f"{value:.4g}（{value * 100:.4g}%）"
        return redact(str(value)) if value != "" else "（空）"

    def set_config(self, key: str, value: Any, confirmed: bool = False) -> dict:
        """改 config.yaml 里的一个设置项（BRIDGE.md 第 2 节）。

        白名单 → 转换 → 只改那一行 → 回读校验 → 严格校验 → 备份 → 原子替换。"""
        spec = settings_spec.SPEC_BY_KEY.get(key) if isinstance(key, str) else None
        if spec is None:
            return {"error": f"这一项不能在界面里修改：{str(key)[:60]}"}
        try:
            value = self._coerce(spec, value)
        except ValueError as e:
            return {"error": f"{spec['label']}：{e}"}
        if self.busy:
            return {"error": "正在运行任务，结束后再改设置"}
        if "confirm_on" in spec and value == spec["confirm_on"] and type(value) is type(spec["confirm_on"]) \
                and not _confirmed(confirmed):
            cfg, _ = gui_core.load_config(self._config_path)
            if cfg is None or cfg.get(key) != value:
                return {"confirm": {"title": spec["label"], "text": spec.get("confirm_text") or "确定修改吗？"}}
        try:
            backup = self._patch_config(key, value)
        except (yamlpatch.PatchError, ValueError) as e:
            return {"error": redact(str(e))}
        self._emit(f"已修改配置：{spec['label']} → {self._display(spec, value)}\n")
        return {"ok": True, "values": self.settings().get("values", {}), "backup": backup}

    def _patch_config(self, key: str, value: Any) -> str | None:
        """把 config.yaml 里的 key 改成 value 并原子落盘；返回这次做的备份文件夹（当天已备份过则 None）。失败抛 ValueError。"""
        from .. import layout
        with self._file_lock:
            if self.busy:                                  # 拿到锁后再看一次（运行中的命令会读配置）
                raise ValueError("正在运行任务，结束后再改设置")
            if self._config_path.exists():
                raw = self._config_path.read_bytes()
            else:
                src = layout.resource("config.example.yaml")     # 与 reset_config 同源
                if src is None:
                    raise ValueError("还没有 config.yaml，也找不到配置模板 config.example.yaml，安装可能不完整")
                raw = src.read_bytes()
            try:
                old = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("config.yaml 不是 UTF-8 文本，请用记事本另存为 UTF-8 后再改") from None
            new = yamlpatch.set_scalar(old, key, value)
            yamlpatch.verify(old, new, key, value)
            err = self._check_config_text(new)
            if err:
                raise ValueError("改完以后配置校验没通过，没有保存：\n" + err)
            backup = None
            if self._config_path.exists():
                bdir = self._base / "data" / "backups"
                today = time.strftime("%Y%m%d")
                done = bdir.is_dir() and any(p.name.startswith(today) and p.name.endswith("-settings")
                                             for p in bdir.iterdir())
                if not done:                               # 当天第一次修改才备份，同一天之后的修改不再重复备份
                    b = bdir / f"{time.strftime('%Y%m%d-%H%M%S')}-settings"
                    b.mkdir(parents=True, exist_ok=True)
                    b.joinpath("config.yaml").write_bytes(raw)
                    backup = str(b)
            _atomic_write(self._config_path, new.encode("utf-8"))   # 换行风格、BOM 都已在 new 里原样保留
            return backup

    # ------------------------------------------------------------------ moligod 价格提醒：手动 / 自动
    def set_moligod_mode(self, mode: str) -> dict:
        from .. import moligod
        if mode not in ("manual", "auto"):
            return {"error": "模式只能是 manual（手动）或 auto（自动）"}
        if mode == "auto" and not moligod.AUTO_SYNC_AVAILABLE:
            return {"unavailable": True, "title": "自动设置暂不可用", "message": moligod.UNAVAILABLE_TEXT}
        if self.busy:
            return {"error": "正在运行任务，结束后再切换"}
        cfg, err = gui_core.load_config(self._config_path)
        want = mode == "auto"
        if cfg is not None and bool(cfg.get("moligod.enabled")) == want:
            return {"ok": True, "mode": mode}
        if cfg is None and self._config_path.exists():
            return {"error": err or "配置文件不合法"}
        if not want and not self._config_path.exists():
            return {"ok": True, "mode": "manual"}             # 没有配置文件 = 内置默认（关），什么都不用写
        try:
            self._patch_config("moligod.enabled", want)
        except (yamlpatch.PatchError, ValueError) as e:
            return {"error": redact(str(e))}
        self._emit(f"moligod 价格提醒已切换为{'自动' if want else '手动'}模式\n")
        return {"ok": True, "mode": mode}

    def open_url(self, which: str) -> dict:
        """用系统默认浏览器打开白名单里的网址（只允许 https）。"""
        if not isinstance(which, str):
            return {"error": "未知链接"}
        if which == "moligod":
            cfg, _ = gui_core.load_config(self._config_path)
            url = str((cfg.get("moligod.url") if cfg is not None else None) or "https://moligod.com/ammo-realtime")
        else:
            url = URLS.get(which, "")
            if not url:
                return {"error": "未知链接"}
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            return {"error": "只能打开 https:// 开头的网址"}
        webbrowser.open(url)
        return {"ok": True}

    # ------------------------------------------------------------------ 计划（结构化显示）
    def plan_view(self) -> dict:
        """最新计划：原样的 JSON + 提醒价位 + 邮件同款文字。只读，不联网、不写文件。"""
        from .. import guardrails, moligod, plan as planmod
        from ..mail import compose
        cfg, err = self._cfg()
        if cfg is None:
            return {"exists": False, "error": err}
        path = cfg.data_dir / "plans" / "latest.json"
        if not path.exists():
            return {"exists": False}
        p = planmod.load_latest(cfg)
        if not isinstance(p, dict):
            return {"exists": False, "error": "计划文件损坏，读不出来。重新运行一次就会生成新的计划"}
        try:
            alerts = moligod.desired_alerts(cfg, p)
            return {"exists": True, "expired": bool(guardrails.is_expired(p)), "plan": p, "alerts": alerts,
                    "text": compose.build_body(cfg, p, manual_alerts=alerts), "title": compose.build_subject(cfg, p),
                    "mock": self.mock}
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            return {"exists": False, "error": f"计划文件格式不对（{type(e).__name__}），重新运行一次就会生成新的计划"}

    # ------------------------------------------------------------------ 首页仪表盘
    def dashboard(self) -> dict:
        """首页各块的数据。任何一块出错就给 None，不影响其他块。只读。"""
        from .. import fees, guardrails, ledger, plan as planmod, risk
        out: dict[str, Any] = {"usage": None, "last_run": None, "plan": None, "positions": None,
                               "balance": {"known": False, "amount": None}, "data": {"minute_days": 0, "day_rows": 0},
                               "health": [], "risk": None, "book": None}
        health: list[dict] = out["health"]
        cfg, err = self._cfg()
        if cfg is None:
            health.append({"level": "error", "text": "配置文件有问题，程序不会运行：" + (err or "")[:300]})
            return out
        paper = bool(cfg.paper)
        out["book"] = "paper" if paper else "real"
        fee = getattr(cfg, "fee_for", None) or cfg.fee
        try:
            p = planmod.load_latest(cfg)
            if isinstance(p, dict):
                items = p.get("items") or []
                expired = bool(guardrails.is_expired(p))
                out["plan"] = {"plan_id": p.get("plan_id"), "valid_until": p.get("valid_until"), "expired": expired,
                               "buy": sum(1 for i in items if i.get("action") == "buy"),
                               "sell": len(p.get("sells") or []),
                               "watch": sum(1 for i in items if i.get("action") != "buy"),
                               "paper": bool(p.get("paper"))}
                if expired:
                    health.append({"level": "warn", "text": f"最新计划已于 {p.get('valid_until')} 过期，以它为准前请重新运行"})
        except Exception:
            out["plan"] = None
        if not self.mock and not self.store.is_set("DFQ_XTC_TOKEN"):
            health.append({"level": "warn", "text": "还没有填写小涛查 token，无法获取行情（可以先用“演示”试用）"})
        try:
            with self._open_db(cfg) as db:
                if db is None:
                    out["usage"] = {"today": 0, "limit": cfg.get("xtc.daily_limit")}
                    out["positions"] = {"count": 0, "cost": 0.0, "value": None, "unrealized": None, "book": out["book"]}
                    health.append({"level": "ok", "text": "还没有运行过。可以先点“演示”看看完整流程"})
                    return out
                self._dash_blocks(out, cfg, db, paper, fee, fees, ledger, risk)
        except Exception:                          # 打开数据库失败（被占用 / 损坏）：其他块照常显示
            health.append({"level": "warn", "text": "数据库暂时打不开（可能正被计划任务使用），稍后刷新"})
            return out
        if not any(h["level"] != "ok" for h in health):
            health.append({"level": "ok", "text": "一切正常"})
        return out

    def _dash_blocks(self, out: dict, cfg, db, paper: bool, fee, fees, ledger, risk) -> None:
        health: list[dict] = out["health"]
        try:
            used, limit = db.usage_on("xtc"), cfg.get("xtc.daily_limit")
            out["usage"] = {"today": used, "limit": limit}
            if isinstance(limit, int) and limit and used >= limit:
                health.append({"level": "warn", "text": f"今天的接口次数已用完（{used}/{limit}），明天自动恢复"})
        except Exception:
            out["usage"] = None
        try:
            r = db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
            if r is not None:
                out["last_run"] = {"at": r["started_at"], "status": r["status"], "mode": r["mode"], "cost": r["api_cost"]}
                if r["status"] not in (None, "ok", "success", "done", "running"):
                    health.append({"level": "warn", "text": f"最近一次运行的结果是“{r['status']}”，详情看日志"})
        except Exception:
            out["last_run"] = None
        try:
            rows = self._position_rows(cfg, db, paper)
            known = [r for r in rows if r["value"] is not None]
            all_known = len(known) == len(rows)
            out["positions"] = {"count": len(rows), "cost": round(sum(r["qty"] * r["avg_cost"] for r in rows), 2),
                                "value": round(sum(r["value"] for r in rows), 2) if all_known else None,
                                "unrealized": round(sum(r["unrealized"] for r in rows), 2) if all_known else None,
                                "book": out["book"]}
        except Exception:
            out["positions"] = None
        try:
            bal = ledger.balance(db, fee, paper=paper)
            out["balance"] = {"known": bool(bal["known"]), "amount": bal["amount"]}
            if not bal["known"]:
                health.append({"level": "warn", "text": "还没有填写余额，仓位按配置里的假定资金估算"})
        except Exception:
            out["balance"] = {"known": False, "amount": None}
        try:
            m = db.one("SELECT COUNT(DISTINCT substr(ts, 1, 10)) AS d FROM minute_prices")
            d = db.one("SELECT COUNT(*) AS c FROM day_prices")
            out["data"] = {"minute_days": int(m["d"] or 0), "day_rows": int(d["c"] or 0)}
        except Exception:
            pass
        try:
            st = risk.status(cfg, db, paper=paper, persist=False)       # 只读：不在这里锁定新的暂停
            out["risk"] = {"halted": bool(st["halted"]), "reason": st.get("reason")}
            if st["halted"]:
                health.append({"level": "error", "text": f"风控已暂停新的买入建议：{st.get('reason')}"})
        except Exception:
            out["risk"] = None
        try:
            b = fees.basis(cfg, db, paper=paper)
            if b.get("status") == "unverified":
                health.append({"level": "warn", "text": "卖出费率还没有核对过（13% 是默认值），可以运行“核对费率”"})
        except Exception:
            pass
        try:
            lines = ledger.integrity_report(db, paper=paper)
            if lines:
                health.append({"level": "warn", "text": f"账本里有 {len(lines)} 笔卖出缺少成本依据（详见“持仓”页）"})
        except Exception:
            pass

    # ------------------------------------------------------------------ 持仓
    def _position_rows(self, cfg, db, paper: bool) -> list[dict]:
        from .. import fees, ledger, risk, timeutil
        fee_of = getattr(cfg, "fee_for", None) or (lambda _n: cfg.fee)
        now = timeutil.now()
        rows: list[dict[str, Any]] = []
        for name, p in ledger.open_positions(db, fee_of, paper=paper).items():
            last_price, last_ts = None, None
            oid = p.get("oid")
            if oid is not None:
                r = db.one("SELECT price, ts FROM minute_prices WHERE oid=? ORDER BY ts DESC LIMIT 1", (oid,))
                if r is None:
                    r = db.one("SELECT price, ts FROM snapshots WHERE oid=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1",
                               (oid,))
                if r is not None and r["price"]:
                    last_price, last_ts = float(r["price"]), r["ts"]
            rate = float(fee_of(name))
            avg = float(p["avg_cost"])
            qty = int(p["qty"])
            unreal = None if last_price is None else (last_price * (1 - rate) - avg) * qty
            held = risk.holding_days(p.get("held_since"), now) if hasattr(risk, "holding_days") else None
            rows.append({"name": name, "oid": oid, "qty": qty, "avg_cost": round(avg, 2),
                         "breakeven": fees.breakeven_price(avg, rate) if avg > 0 else None,
                         "last_price": last_price, "last_ts": last_ts,
                         "value": None if last_price is None else round(last_price * qty, 2),
                         "unrealized": None if unreal is None else round(unreal, 2),
                         "unrealized_pct": None if unreal is None or avg <= 0 else round(unreal / (avg * qty), 4),
                         "held_days": None if held is None else round(held, 1),
                         "uncovered_qty": int(p.get("uncovered_qty") or 0)})
        rows.sort(key=lambda r: -(r["qty"] * r["avg_cost"]))
        return rows

    def positions(self, paper: bool | None = None) -> dict:
        """某本账的持仓。paper 为 None 时用配置的 paper_mode。浮动盈亏按“现价 ×(1−费率) − 均价”（卖出要扣费）。"""
        from .. import ledger
        if paper is not None and not isinstance(paper, bool):
            return {"error": "账本参数不对"}
        cfg, err = self._cfg()
        if cfg is None:
            return {"error": err or "配置文件不合法"}
        if paper is None:
            paper = bool(cfg.paper)
        out: dict[str, Any] = {"book": "paper" if paper else "real", "rows": [], "realized": 0.0, "trades": 0, "notes": []}
        with self._open_db(cfg) as db:
            if db is None:
                out["notes"].append("还没有数据库：运行过一次或记过一笔账之后才会有持仓")
                return out
            fee = getattr(cfg, "fee_for", None) or cfg.fee
            out["rows"] = self._position_rows(cfg, db, paper)
            s = ledger.realized_summary(db, fee, paper=paper)
            out["realized"] = round(float(s["realized"]), 2)
            out["trades"] = int(s["trades"])
            try:
                out["notes"] += ledger.integrity_report(db, paper=paper)
            except Exception:
                pass
        if any(r["last_price"] is None for r in out["rows"]):
            out["notes"].append("有的品种还没有价格数据，市值和浮动盈亏暂时算不出来（运行一次后就有）")
        return out

    # ------------------------------------------------------------------ 数据与备份
    def storage(self) -> dict:
        return maintenance.storage(self._base, self._data_dir())

    def clean(self, what: str, confirmed: bool = False) -> dict:
        if not isinstance(what, str) or what not in maintenance.CLEANABLE:
            return {"error": "只能清理 接口缓存 / 临时文件 / 导出的日志包 / 旧日志"}
        if self.busy:
            return {"error": "正在运行任务，结束后再清理"}
        keep = prefs.load(self._base).get("log_keep_days", 30)
        if not _confirmed(confirmed):
            label = maintenance.LABELS[what]
            extra = (f"（只删 {keep or maintenance.LOG_DEFAULT_DAYS} 天以前的）" if what == "logs" else "")
            return {"confirm": {"title": f"清理{label}",
                                "text": f"删除{label}{extra}？\n\n交易数据库、计划、备份、配置和密钥都不会被删。确定吗？"}}
        with self._file_lock:
            if self.busy:
                return {"error": "正在运行任务，结束后再清理"}
            try:
                freed = maintenance.clean(self._base, what, data_dir=self._data_dir(), log_keep_days=keep)
            except maintenance.MaintenanceError as e:
                return {"error": str(e)}
        self._emit(f"已清理{maintenance.LABELS[what]}，释放 {_size_text(freed)}\n")
        return {"ok": True, "freed": freed}

    def backup_now(self) -> dict:
        with self._file_lock:
            try:
                r = maintenance.backup_db(self._base, data_dir=self._data_dir(), tag="manual")
            except maintenance.MaintenanceError as e:
                return {"error": str(e)}
        self._emit(f"已备份数据库：{r['name']}（{_size_text(r['bytes'])}）\n")
        return {"ok": True, "name": r["name"], "bytes": r["bytes"]}

    def backups(self) -> dict:
        return {"items": maintenance.list_backups(self._base)}

    def restore_backup(self, name: str, confirmed: bool = False) -> dict:
        try:
            maintenance.resolve_backup(self._base, name)          # 先校验文件名（防路径穿越），确认前就拒绝坏名字
        except maintenance.MaintenanceError as e:
            return {"error": str(e)}
        if self.busy:
            return {"error": "正在运行任务，结束后再恢复"}
        if not _confirmed(confirmed):
            return {"confirm": {"title": "从备份恢复数据库",
                                "text": f"用备份 {name} 覆盖当前的交易数据库？\n\n备份之后记的成交、拉到的行情都会消失。"
                                        "恢复前会先把当前数据库再备份一份（文件名带 before-restore），"
                                        "后悔了可以再恢复回来。确定吗？"}}
        with self._file_lock:
            if self.busy:
                return {"error": "正在运行任务，结束后再恢复"}
            try:
                r = maintenance.restore(self._base, name, data_dir=self._data_dir())
            except maintenance.MaintenanceError as e:
                return {"error": str(e)}
        self._emit(f"已从备份 {name} 恢复数据库" + (f"（恢复前的数据库备份为 {r['safety']}）" if r["safety"] else "") + "\n")
        return {"ok": True, "safety": r["safety"]}

    # ------------------------------------------------------------------ 关于 / 更新
    def about(self) -> dict:
        return {"version": __version__, "frozen": gui_core.is_frozen(), "python": sys.version.split()[0],
                "webview2": _webview2_version(), "base": str(self._base), "data_dir": str(self._data_dir()),
                "repo": REPO_URL, "license": "MIT", "disclaimer": DISCLAIMER}

    def check_update(self, confirmed: bool = False) -> dict:
        """查询 GitHub 上的最新发布版本。只查询、只显示：绝不自动下载、绝不自动安装。"""
        from .. import http
        if not _confirmed(confirmed):
            return {"confirm": {"title": "检查更新",
                                "text": "将访问 GitHub 查询最新版本，不发送任何个人数据。\n\n确定吗？"}}
        try:
            data = http.request_json("GET", RELEASES_API, timeout=8, retries=0, transport=self._http_transport,
                                     headers={"Accept": "application/vnd.github+json"})
        except http.HttpError as e:
            if e.status == 404:
                return {"ok": True, "current": __version__, "latest": None, "newer": False, "notes": ""}
            return {"error": f"检查更新失败：{e}"}
        if not isinstance(data, dict):
            return {"error": "检查更新失败：GitHub 返回的内容格式不对"}
        tag = str(data.get("tag_name") or "").strip()
        latest_t, cur_t = _version_tuple(tag), _version_tuple(__version__)
        if latest_t is None:
            return {"error": "检查更新失败：最新发布的版本号格式不对"}
        latest = ".".join(map(str, latest_t))
        newer = cur_t is not None and latest_t > cur_t
        notes = redact(str(data.get("body") or ""))[:600]
        return {"ok": True, "current": __version__, "latest": latest, "newer": bool(newer), "notes": notes}


def public_methods() -> list[str]:
    return sorted(n for n in dir(Api) if not n.startswith("_") and callable(getattr(Api, n)))


# 页面能调用的方法（白名单）。新增给页面用的方法时要加在这里。
BRIDGE_METHODS = (
    "state", "tasks", "trades", "poll", "clear_log", "set_mock", "run", "continue_", "note_add",
    "set_secret", "clear_secret", "check_config", "reset_config", "open_path", "export_logs", "install_tasks",
    "quit_check", "ui_prefs", "set_close_action", "set_theme", "close_choice", "export_config", "import_config",
    # BRIDGE v2
    "set_pref", "settings", "set_config", "set_moligod_mode", "open_url", "plan_view", "dashboard", "positions",
    "storage", "clean", "backup_now", "backups", "restore_backup", "tasks_status", "remove_tasks", "about",
    "check_update",
)


def _bridge_error(e: BaseException) -> dict:
    return {"error": f"出错了（{type(e).__name__}）：{redact(str(e))[:300]}。可以导出日志反馈问题"}


def make_bridge(api: Api):
    """给页面用的“门面”对象：只有白名单里的方法，没有任何属性。

    为什么不直接把 Api 交给 pywebview：pywebview 会递归暴露对象的公开属性（Api.store 是密钥存储，
    页面就能调用 store.backend.read 读出密钥原文），而且它按点号路径取属性时不拦截下划线开头的名字
    （页面可以调用 "poll.__globals__.get" 这类路径）。
    这里每个方法都是按原方法签名生成的转发函数（pywebview 按位置传参、按参数名生成 JS 函数）：
      - 真正的 Api 方法只放在闭包里：闭包单元格是元组里的元素，属性路径够不到；
      - 生成函数的 globals 是一个只装了空 __builtins__ 的新字典，里面没有任何指向 Api 的东西；
      - 不用 functools.wraps：它留下的 __wrapped__ 会指回真正的 Api 方法；
      - 方法里任何没预料到的异常都转成 {"error": 中文}（已脱敏），不把堆栈抛给页面。"""
    import inspect
    members = {}
    for name in BRIDGE_METHODS:
        target = getattr(api, name)
        params = list(inspect.signature(target).parameters.values())
        decl = ", ".join(["self"] + [p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}"
                                     for p in params])
        call = ", ".join(p.name for p in params)
        scope: dict[str, Any] = {"__builtins__": {}}
        src = (f"def _factory(_target, _guard, _exc):\n"
               f"    def {name}({decl}):\n"
               f"        try:\n"
               f"            return _target({call})\n"
               f"        except _exc as e:\n"
               f"            return _guard(e)\n"
               f"    return {name}\n")
        exec(src, scope)                                     # noqa: S102  源码是上面固定拼出来的
        fn = scope.pop("_factory")(target, _bridge_error, Exception)
        members[name] = fn
    return type("DFQBridge", (), members)()


def describe_actions() -> dict[str, Any]:
    return {k: {"label": v.label, "costly_confirm": v.confirm is not None} for k, v in ACTIONS.items()}
