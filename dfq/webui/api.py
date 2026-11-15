"""界面后端：网页界面能调用的全部操作（pywebview 的 js_api）。

设计要点
  - 每个按钮都只是调用一条现有的 dfq 命令（gui_core.run_cli），界面里没有任何交易逻辑。
  - 会花钱（真实接口 / 大模型）或改动账本的动作，必须带 confirmed=True 才执行。
    第一次调用只返回确认文字，由界面弹窗，用户点“确定”后再调一次。确认在这里强制，
    就算界面代码写错、漏了弹窗，也不会悄悄花钱或改账。
  - 密钥只能写入、不能读出：这里没有任何返回密钥值的方法，只返回“已设置/未设置”。
  - 命令在后台线程执行；输出按顺序存进缓冲区，界面每隔一小段时间用 poll(after) 取新内容。
  - 纯 Python，不依赖窗口：tests/test_webui.py 直接调用这些方法测试。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__, gui_core
from . import prefs, tasks

MAX_IMPORT_BYTES = 1_000_000                         # 导入的配置文件大小上限
MAX_CHUNKS = 5000                                    # 日志缓冲上限，防止长时间运行占满内存


@dataclass
class Action:
    label: str                                       # 显示在日志里的名称
    argv: Callable[[dict, "Api"], list[str]]         # 生成 dfq 命令参数
    target: str | None = None                        # 结果还要显示在哪个页面（plan / review / None）
    confirm: Callable[[dict, "Api"], str | None] | None = None   # 返回确认文字；None = 不需要确认
    validate: Callable[[dict, "Api"], str | None] | None = None  # 返回错误文字；None = 参数没问题
    after: str | None = None                         # 成功后接着执行的动作（如运行后刷新计划）
    needs_continue: bool = False                     # 命令中途要用户点“我已完成，继续”
    before: Callable[["Api"], None] | None = None    # 执行前的界面状态调整（如演示自动切到模拟数据）
    extra: dict = field(default_factory=dict)


def _cost_text(api: "Api", llm: bool) -> str | None:
    if api.mock:
        return None
    return ("会请求小涛查接口（每晚约 13 次，首次约 21 次，按次计费，每日上限见配置）"
            + ("，并调用大模型（按 token 计费）" if llm else "")
            + "；如果开了邮件和 moligod 同步，还会发邮件、改提醒。\n\n确定继续吗？")


def _run_args(llm: bool):
    def build(p: dict, api: "Api") -> list[str]:
        return ["run", "--show"] + ([] if llm else ["--offline"]) + (["--force"] if p.get("force") else [])
    return build


def _need_digits(key: str, what: str):
    def check(p: dict, api: "Api") -> str | None:
        return None if str(p.get(key, "")).strip().isdigit() else f"请先填写{what}（正整数）"
    return check


def _trade_check(p: dict, api: "Api") -> str | None:
    if p.get("side") not in ("buy", "sell"):
        return "请选择买入或卖出"
    if not str(p.get("item", "")).strip():
        return "请选择或输入品种"
    try:
        q = int(str(p.get("qty", "")).replace(",", "").strip())
        price = float(str(p.get("price", "")).replace(",", "").strip())
        if q <= 0 or price <= 0:
            raise ValueError
    except ValueError:
        return "数量要填正整数，单价要填正数"
    return None


def _trade_argv(p: dict, api: "Api") -> list[str]:
    q = int(str(p["qty"]).replace(",", "").strip())
    price = float(str(p["price"]).replace(",", "").strip())
    return (["trade", p["side"], str(p["item"]).strip(), str(q), str(price), "--source", "gui"]
            + (["--paper"] if p.get("paper", True) else []))


def _trade_confirm(p: dict, api: "Api") -> str:
    book = "模拟账本" if p.get("paper", True) else "真实账本"
    side = "买入" if p["side"] == "buy" else "卖出"
    return f"{side} {str(p['item']).strip()} × {p['qty']} @ {p['price']}\n记入：{book}\n\n确定吗？"


def _balance_check(p: dict, api: "Api") -> str | None:
    try:
        v = float(str(p.get("amount", "")).replace(",", "").strip())
        if v < 0 or v != v:
            raise ValueError
    except ValueError:
        return "余额要填不小于 0 的数字"
    return None


def _probe_check(p: dict, api: "Api") -> str | None:
    if not api.mock and not api.store.is_set("DFQ_XTC_TOKEN"):
        return "还没有小涛查 token：请先到“设置”页填写"
    return None


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
    "balance_set": Action("设置余额", lambda p, a: ["balance", "set", str(float(str(p["amount"]).replace(",", "")))],
                          validate=_balance_check,
                          confirm=lambda p, a: f"把余额基准设为 {float(str(p['amount']).replace(',', '')):,.0f}？"),
    "balance": Action("查看余额", lambda p, a: ["balance"]),
    "note_list": Action("已录入资讯", lambda p, a: ["note", "list"]),
    "init": Action("生成配置文件", lambda p, a: ["init"]),
    "moligod_login": Action("登录 moligod", lambda p, a: ["moligod-login"], needs_continue=True),
}


class Api:
    """pywebview 会把这个对象的公开方法暴露给页面：window.pywebview.api.<方法名>(...)"""

    def __init__(self, config_path: str | Path, store: gui_core.SecretStore | None = None,
                 runner: Callable[..., int] = gui_core.run_cli):
        self._config_path = Path(config_path).resolve()
        self._base = self._config_path.parent
        self.store = store or gui_core.SecretStore(gui_core.default_backend(self._base))
        self._runner = runner
        self._lock = threading.Lock()
        self._chunks: list[tuple[int, str]] = []     # (序号, 文本)
        self._seq = 0
        self._results: dict[str, str] = {}           # 页面名 → 最近一次结果
        self._results_seq = 0
        self._busy_label: str | None = None
        self._continue = threading.Event()
        self._waiting_continue = False
        self.mock = False
        loaded = self.store.load_into_process()
        if loaded:
            self._emit(f"已读取密钥：{'、'.join(loaded)}（只读取是否存在，不显示内容）\n")
        if not self._config_path.exists():
            self._emit(f"还没有配置文件 {self._config_path.name}，当前使用内置默认配置。可以在“设置”页生成。\n")

    # ------------------------------------------------------------------ 内部
    def _emit(self, text: str) -> None:
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

    @property
    def busy(self) -> bool:
        return self._busy_label is not None

    # ------------------------------------------------------------------ 状态
    def state(self) -> dict:
        """界面需要显示的全部状态（不含任何密钥值）。"""
        cfg, err = self._cfg()
        usage = None
        if cfg is not None:
            try:
                from ..db import DB
                path = cfg.data_dir / "dfq.db"
                if path.exists():
                    db = DB(path)
                    try:
                        usage = {"today": db.usage_on("xtc"), "limit": cfg.get("xtc.daily_limit")}
                    finally:
                        db.close()
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
            "features": None if cfg is None else {
                "llm": bool(cfg.get("llm.enabled")), "mail": bool(cfg.get("mail.enabled")),
                "news": bool(cfg.get("intel.news.enabled")), "moligod": bool(cfg.get("moligod.enabled")),
            },
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
        params = dict(params or {})
        act = ACTIONS.get(action_id)
        if act is None:
            return {"error": f"未知操作：{action_id}"}
        if self.busy:
            return {"error": "上一个操作还没结束，请稍候"}
        if act.before:
            act.before(self)
        if act.validate:
            msg = act.validate(params, self)
            if msg:
                return {"error": msg}
        text = act.confirm(params, self) if act.confirm else None
        if text and not confirmed:
            return {"confirm": {"title": act.label, "text": text}}
        self._start(action_id, act, params)
        return {"ok": True}

    def _start(self, action_id: str, act: Action, params: dict, runner: Callable[..., int] | None = None) -> None:
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

        def emit(text: str) -> None:
            self._emit(text)
            if act.target:
                with self._lock:
                    self._results[act.target] = self._results.get(act.target, "") + text
                    self._results_seq += 1

        def work() -> None:
            code = 1
            try:
                code = runner(full, emit, continue_event=self._continue if act.needs_continue else None)
            finally:
                self._emit(f"■ 结束（{'成功' if code == 0 else f'返回码 {code}'}）\n")
                self._busy_label = None
                self._waiting_continue = False
            if code == 0 and act.after:
                self.run(act.after)

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
        text, name = (text or "").strip(), (name or "").strip()
        if not text or not name:
            return {"error": "请填写信源名并粘贴正文"}
        if source not in ("bili", "qq", "news", "other"):
            return {"error": "来源不对"}
        if self.busy:
            return {"error": "上一个操作还没结束，请稍候"}
        tmp_dir = self._base / "data" / "tmp"            # 临时文件也放在程序自己的文件夹里，不写系统临时目录
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="dfq_note_", dir=str(tmp_dir))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        argv = ["note", "add", "--source", source, "--name", name, "--file", path]
        if (title or "").strip():
            argv += ["--title", title.strip()]

        def cleanup_runner(full, emit, continue_event=None):
            try:
                return self._runner(full, emit, continue_event=continue_event)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._start("note_add", Action("录入资讯", lambda p, a: argv), {}, runner=cleanup_runner)
        return {"ok": True}

    # ------------------------------------------------------------------ 密钥
    def set_secret(self, name: str, value: str) -> dict:
        try:
            self.store.set(name, value)
        except Exception as e:
            return {"error": str(e)}
        finally:
            value = None                             # noqa: F841  不再持有
        self._emit(f"已保存 {name}（内容不显示）。\n")
        return {"ok": True}

    def clear_secret(self, name: str, confirmed: bool = False) -> dict:
        if not confirmed:
            return {"confirm": {"title": "确认清除", "text": f"从 .env 里删除 {name}？"}}
        try:
            self.store.clear(name)
        except Exception as e:
            return {"error": str(e)}
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
        if not confirmed:
            return {"confirm": {"title": "恢复默认配置",
                                "text": "将用默认模板替换当前的 config.yaml。\n\n当前的 config.yaml 会先备份到 data\\backups，"
                                        "密钥（.env）和交易记录都不受影响。确定吗？"}}
        backup = None
        if self._config_path.exists():
            backup = self._base / "data" / "backups" / time.strftime("%Y%m%d-%H%M%S")
            backup.mkdir(parents=True, exist_ok=True)
            backup.joinpath("config.yaml").write_bytes(self._config_path.read_bytes())
        self._config_path.write_bytes(src.read_bytes())
        self._emit("已恢复默认配置" + (f"（旧文件备份在 {backup}）" if backup else "") + "\n")
        return {"ok": True, "backup": str(backup) if backup else None}

    def open_path(self, what: str) -> dict:
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
        """学 MaaEnd：一键把日志打包成 zip，反馈问题时直接发这个文件。日志本身已脱敏，.env 不会被打包。"""
        cfg, _ = self._cfg()
        out_dir = self._base / "data" / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        zpath = out_dir / f"DFQ-logs-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        with self._lock:
            console = "".join(t for _, t in self._chunks)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("界面运行日志.txt", console)
            z.writestr("版本.txt", f"dfq {__version__}\nPython {sys.version}\n")
            log_dir = (cfg.data_dir if cfg is not None else self._base / "data") / "logs"
            if log_dir.exists():
                for p in sorted(log_dir.glob("*"))[-10:]:
                    if p.is_file() and p.suffix in (".log", ".txt", ".jsonl"):
                        z.write(p, f"logs/{p.name}")
        self._emit(f"日志已导出：{zpath}\n")
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", "/select,", str(zpath)])
        return {"ok": True, "path": str(zpath)}

    def install_tasks(self, confirmed: bool = False) -> dict:
        script = self._base / "scripts" / "install_tasks.ps1"
        if sys.platform != "win32":
            return {"error": "计划任务只能在 Windows 上创建"}
        if not script.exists():
            return {"error": f"没有找到 {script}。安装目录可能不完整，请重新安装。"}
        frozen = gui_core.is_frozen()                   # 安装版：计划任务直接调用 DFQ.exe，不需要 .venv
        if not confirmed:
            extra = "" if frozen or (self._base / ".venv").exists() else \
                "\n\n注意：没有找到项目里的 .venv，计划任务需要它（见 README 安装步骤）。"
            return {"confirm": {"title": "创建计划任务",
                                "text": "将创建两个计划任务：每晚 21:30 运行一次、每 30 分钟读取一次邮件回复。" + extra + "\n\n确定吗？"}}
        if self.busy:
            return {"error": "上一个操作还没结束，请稍候"}
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        if frozen:
            cmd += ["-Exe", sys.executable]

        def runner(full, emit, continue_event=None):
            p = subprocess.run(cmd,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               cwd=str(self._base), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            emit((p.stdout or "") + (p.stderr or ""))
            return p.returncode
        self._start("install_tasks", Action("创建计划任务", lambda p, a: ["install-tasks"]), {}, runner=runner)
        return {"ok": True}

    # ------------------------------------------------------------------ 配置导入 / 导出
    def export_config(self, include_secrets: bool = False, confirmed: bool = False, folder: str | None = None) -> dict:
        """导出配置为一个 zip（config.yaml + 说明；勾选时再加 .env）。不传 folder 时弹出“选择文件夹”。"""
        cfg_file = self._config_path
        if not cfg_file.exists():
            return {"error": "还没有 config.yaml，没有可导出的配置（可以先在“设置”页生成）"}
        env_file = self._base / ".env"
        if include_secrets and not confirmed:
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
            z.write(cfg_file, "config.yaml")
            if include_secrets and env_file.exists():
                z.write(env_file, ".env")
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
        """导入配置：拖进来的 .zip（本程序导出的）/ .yaml / .env。先完整校验，再备份旧文件、替换。"""
        import base64
        import binascii
        from .. import envfile
        try:
            raw = base64.b64decode(content_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            return {"error": "文件读取失败"}
        if not raw:
            return {"error": "文件是空的"}
        if len(raw) > MAX_IMPORT_BYTES:
            return {"error": "文件太大，不像是配置文件"}
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
            except zipfile.BadZipFile:
                return {"error": "压缩包损坏，无法读取"}
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
            tmp_dir = self._base / "data" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            probe = tmp_dir / "import-check.yaml"
            probe.write_text(text, encoding="utf-8")
            try:
                _, err = gui_core.load_config(probe)       # 和启动时完全一样的严格校验
            finally:
                probe.unlink(missing_ok=True)
            if err:
                problems.append("config.yaml 校验没通过：\n" + err)
            summary.append("替换当前的 config.yaml")
        env_keys: list[str] = []
        if new_env is not None:
            tmp_env = self._base / "data" / "tmp" / "import-check.env"
            tmp_env.parent.mkdir(parents=True, exist_ok=True)
            tmp_env.write_bytes(new_env)
            try:
                env_keys = sorted(envfile.read(tmp_env))
            finally:
                tmp_env.unlink(missing_ok=True)
            if not env_keys:
                problems.append(".env 里没有可识别的密钥")
            else:
                summary.append(f"写入密钥：{'、'.join(env_keys)}（同名的会被覆盖）")
        if problems:
            return {"error": "\n\n".join(problems)}
        if not confirmed:
            return {"confirm": {"title": "导入配置",
                                "text": f"文件：{filename}\n\n将会：\n· " + "\n· ".join(summary)
                                        + "\n\n旧文件会先备份到 data\\backups。确定导入吗？"}}
        backup = self._base / "data" / "backups" / time.strftime("%Y%m%d-%H%M%S")
        backup.mkdir(parents=True, exist_ok=True)
        if new_cfg is not None:
            if self._config_path.exists():
                backup.joinpath("config.yaml").write_bytes(self._config_path.read_bytes())
            self._config_path.write_text(new_cfg.decode("utf-8-sig"), encoding="utf-8", newline="\n")
        if new_env is not None:
            tmp_env.write_bytes(new_env)
            try:
                values = envfile.read(tmp_env)
            finally:
                tmp_env.unlink(missing_ok=True)
            env_path = self._base / ".env"
            if env_path.exists():
                backup.joinpath(".env").write_bytes(env_path.read_bytes())
            for k, v in values.items():
                self.store.set(k, v)                       # 走同一套校验，并立即生效
        self._emit(f"已导入配置：{filename}（旧文件备份在 {backup}）\n")
        return {"ok": True, "backup": str(backup)}

    def quit_check(self) -> dict:
        """关窗前问一下：正在运行时提醒。"""
        return {"busy": self.busy, "label": self._busy_label}

    # ------------------------------------------------------------------ 关闭窗口：托盘 / 退出
    def ui_prefs(self) -> dict:
        return prefs.load(self._base)

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
        if remember:
            prefs.save(self._base, close_action=action)
        handler = getattr(self, "_close_handler", None)
        if handler is not None:
            threading.Thread(target=handler, args=(action,), daemon=True).start()
        return {"ok": True}


def public_methods() -> list[str]:
    return sorted(n for n in dir(Api) if not n.startswith("_") and callable(getattr(Api, n)))


# 页面能调用的方法（白名单）。新增给页面用的方法时要加在这里。
BRIDGE_METHODS = (
    "state", "tasks", "trades", "poll", "clear_log", "set_mock", "run", "continue_", "note_add",
    "set_secret", "clear_secret", "check_config", "reset_config", "open_path", "export_logs", "install_tasks",
    "quit_check", "ui_prefs", "set_close_action", "set_theme", "close_choice", "export_config", "import_config",
)


def make_bridge(api: Api):
    """给页面用的“门面”对象：只有白名单里的方法，没有任何属性。

    为什么不直接把 Api 交给 pywebview：pywebview 会递归暴露对象的公开属性（Api.store 是密钥存储，
    页面就能调用 store.backend.read 读出密钥原文），而且它按点号路径取属性时不拦截下划线开头的名字。
    这里每个方法都是按原方法签名生成的转发函数（pywebview 按位置传参、按参数名生成 JS 函数），
    真正的 Api 方法只放在函数的 globals 字典里——字典的键不能用属性路径访问到。
    不用 functools.wraps：它留下的 __wrapped__ 会指回真正的 Api 方法。"""
    import inspect
    members = {}
    for name in BRIDGE_METHODS:
        target = getattr(api, name)
        params = list(inspect.signature(target).parameters.values())
        decl = ", ".join(["self"] + [p.name if p.default is inspect.Parameter.empty else f"{p.name}={p.default!r}"
                                     for p in params])
        call = ", ".join(p.name for p in params)
        scope = {"_target": target}
        exec(f"def {name}({decl}):\n    return _target({call})\n", scope)   # noqa: S102  源码是上面固定拼出来的
        members[name] = scope[name]
    return type("DFQBridge", (), members)()


def describe_actions() -> dict[str, Any]:
    return {k: {"label": v.label, "costly_confirm": v.confirm is not None} for k, v in ACTIONS.items()}
