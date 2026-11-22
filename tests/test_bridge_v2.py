"""界面后端 BRIDGE v2（BRIDGE.md 第 1～10 节）+ 评审第 8 条（页面拿不到密钥、白名单、属性路径、导入导出、日志导出）。

不打开窗口、不联网：直接调用 Api 的方法；需要真实数据的用例先在模拟数据里跑一遍演示（和 test_webui 的端到端用例相同）。
"""
import base64
import io
import json
import shutil
import sys
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

from dfq import gui_core as G
from dfq import http as H
from dfq import moligod
from dfq.log import register_secret
from dfq.webui import api as W
from dfq.webui import settings_spec as SS

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "dummy-test-token-0000-bridge"
LLM_KEY = "dummy-test-key-0000-bridge"


class FakeBackend(G.SecretBackend):
    available = True
    where = "测试用"

    def __init__(self, data=None):
        self.data = dict(data or {})

    def read(self, name):
        return self.data.get(name)

    def write(self, name, value):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


class Recorder:
    def __init__(self, code=0, output="ok\n"):
        self.calls, self.code, self.output = [], code, output

    def __call__(self, argv, emit, continue_event=None):
        self.calls.append(list(argv))
        emit(self.output)
        return self.code


def make(tmp_path, *, example=True, secrets=None, runner=None, text=None):
    cfgp = tmp_path / "config.yaml"
    if text is not None:
        cfgp.write_text(text, encoding="utf-8")
    elif example:
        shutil.copyfile(ROOT / "config.example.yaml", cfgp)
    store = G.SecretStore(FakeBackend(secrets if secrets is not None else {"DFQ_XTC_TOKEN": TOKEN}), {})
    return W.Api(cfgp, store=store, runner=runner or Recorder())


def no_secret(obj):
    s = json.dumps(obj, ensure_ascii=False)
    assert TOKEN not in s and LLM_KEY not in s
    return s


# ------------------------------------------------------------------ 白名单 / 门面
V1 = {"state", "tasks", "trades", "poll", "clear_log", "set_mock", "run", "continue_", "note_add", "set_secret",
      "clear_secret", "check_config", "reset_config", "open_path", "export_logs", "install_tasks", "quit_check",
      "ui_prefs", "set_close_action", "set_theme", "close_choice", "export_config", "import_config"}
V2 = {"set_pref", "settings", "set_config", "set_moligod_mode", "open_url", "plan_view", "dashboard", "positions",
      "storage", "clean", "backup_now", "backups", "restore_backup", "tasks_status", "remove_tasks", "about",
      "check_update"}


def test_whitelist_is_exactly_the_contract():
    assert set(W.BRIDGE_METHODS) == V1 | V2 and len(W.BRIDGE_METHODS) == len(V1 | V2)
    assert set(W.public_methods()) - set(W.BRIDGE_METHODS) == {"wait_idle"}     # 只有测试用的等待不暴露


def _nested(obj, path):                                                         # 与 pywebview 的取属性方式相同
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def test_facade_has_no_route_back_to_api(tmp_path):
    a = make(tmp_path)
    b = W.make_bridge(a)
    for name in W.BRIDGE_METHODS:
        fn = getattr(b, name)
        g = _nested(b, f"{name}.__globals__")
        assert g is not None and not any(callable(v) or v is a for v in g.values() if v is not g.get("__builtins__"))
        assert _nested(b, f"{name}.__wrapped__") is None and _nested(b, f"{name}.__self__.store") is None
        assert _nested(b, f"{name}.__globals__.get") is not None
        assert _nested(b, f"{name}.__globals__.get")("_target") is None              # 以前的写法能从这里取回 Api 方法
        assert _nested(b, f"{name}.__closure__.0") is None                           # 闭包单元格用属性路径够不到
        assert fn.__defaults__ is None or all(isinstance(d, (bool, str, int, type(None))) for d in fn.__defaults__)


def test_no_attribute_path_from_facade_reaches_api_or_secrets(tmp_path):
    """评审第 8 条“能否用属性访问绕过白名单”：按 pywebview 的方式（点号路径逐级 getattr，下划线名字也放行）
    从门面出发把三层以内能走到的对象全部走一遍，没有一个是 Api、密钥存储、模块，或任何 dfq 里的函数 / 方法。"""
    import types
    a = make(tmp_path)
    b = W.make_bridge(a)
    forbidden = {id(a), id(a.store), id(a.store.backend), id(a.store.environ), id(W), id(G)}
    frontier, seen, walked = [(b, "")], {id(b)}, 0
    for _depth in range(3):
        nxt = []
        for obj, path in frontier:
            for name in dir(obj):
                try:
                    got = getattr(obj, name, None)
                except Exception:                                                # 有的描述符取值会报错：等于走不通
                    continue
                if got is None or got is type(b) or id(got) in seen:            # 门面类本身只有转发函数
                    continue
                    continue
                seen.add(id(got))
                walked += 1
                where = f"{path}.{name}".lstrip(".")
                assert id(got) not in forbidden, where
                assert not isinstance(got, (W.Api, G.SecretStore, G.SecretBackend, types.ModuleType)), where
                owner = getattr(got, "__self__", None)
                assert not isinstance(owner, (W.Api, G.SecretStore, G.SecretBackend)), where
                assert not str(getattr(got, "__module__", "") or "").startswith("dfq"), where
                nxt.append((got, where))
        frontier = nxt
    assert walked > 1000                                                         # 真的走了一大片


def test_facade_turns_unexpected_errors_into_error_dict(tmp_path, monkeypatch):
    a = make(tmp_path)

    def boom():
        raise RuntimeError(f"内部错误 token={TOKEN}")
    monkeypatch.setattr(a, "about", boom)
    r = W.make_bridge(a).about()
    assert "error" in r and "RuntimeError" in r["error"] and TOKEN not in r["error"]


def test_every_read_method_returns_json_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "linux")
    register_secret(TOKEN)
    a = make(tmp_path, secrets={"DFQ_XTC_TOKEN": TOKEN, "DFQ_LLM_API_KEY": LLM_KEY})
    b = W.make_bridge(a)
    for name in ("state", "tasks", "trades", "poll", "ui_prefs", "settings", "plan_view", "dashboard", "positions",
                 "storage", "backups", "tasks_status", "about", "quit_check", "check_config"):
        no_secret(getattr(b, name)())


# ------------------------------------------------------------------ 配置项：settings / set_config
def test_settings_lists_groups_spec_and_current_values(tmp_path):
    a = make(tmp_path)
    s = a.settings()
    assert s["ok"] and s["error"] is None and [g["id"] for g in s["groups"]] == [g["id"] for g in SS.GROUPS]
    assert set(s["values"]) == set(SS.SPEC_BY_KEY) and s["values"]["fees.sell_fee_rate"] == 0.13
    assert s["from_overrides"] == []
    assert all(sp["group"] in {g["id"] for g in SS.GROUPS} and sp["type"] in SS.TYPES for sp in SS.SPEC)


def test_settings_reports_bad_config_without_values(tmp_path):
    a = make(tmp_path, text="xtc:\n  daily_limitt: 4\n")
    s = a.settings()
    assert s["ok"] is False and "daily_limitt" in s["error"] and s["values"] == {} and s["spec"]


def test_settings_marks_keys_overridden_by_reflection(tmp_path, monkeypatch):
    a = make(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "overrides.yaml").write_text("rules:\n  buy_quantile: 0.2\n", encoding="utf-8")
    monkeypatch.setattr(SS, "SPEC", SS.SPEC + [{"key": "rules.buy_quantile", "group": "trade", "label": "x",
                                                  "type": "float"}])
    assert a.settings()["from_overrides"] == ["rules.buy_quantile"]


@pytest.mark.parametrize("spec", SS.SPEC, ids=[s["key"] for s in SS.SPEC])
def test_every_spec_key_accepts_its_default_value(tmp_path, spec):
    """SPEC 里的每个键都必须是 config_check 认识的键：用默认值 set_config 一次都能通过。"""
    a = make(tmp_path)
    value = a.settings()["values"][spec["key"]]
    r = a.set_config(spec["key"], value)
    assert r.get("ok"), r
    assert G.load_config(tmp_path / "config.yaml")[1] is None


def test_every_spec_key_is_a_config_check_key():
    """界面能改的键必须由 config_check 逐键校验（不是 FREE 那种另行检查的结构），新增的风控 / 邮件键也要在界面里。"""
    from dfq import config_check as C
    for s in SS.SPEC:
        assert s["key"] in C.SPEC and C.SPEC[s["key"]] != C.FREE, s["key"]
    for k in ("risk.max_loss_per_trade_pct", "risk.max_holding_days", "risk.max_cumulative_loss_pct",
              "risk.loss_window_days", "risk.halt_after_losses", "risk.abnormal_move_pct", "mail.reply_max_age_days"):
        assert k in SS.SPEC_BY_KEY, k


def test_set_config_changes_one_line_backs_up_once_and_logs(tmp_path):
    a = make(tmp_path)
    old = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    r = a.set_config("fees.sell_fee_rate", 0.12)
    assert r["ok"] and r["values"]["fees.sell_fee_rate"] == 0.12 and r["backup"]
    assert (Path(r["backup"]) / "config.yaml").read_text(encoding="utf-8") == old
    new = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    diff = [i for i, (x, y) in enumerate(zip(old.splitlines(), new.splitlines())) if x != y]
    assert len(diff) == 1 and len(old.splitlines()) == len(new.splitlines())
    assert a.set_config("xtc.daily_limit", 80)["backup"] is None                # 同一天第二次改不再备份
    assert "已修改配置：卖出费率" in a.poll()["text"]
    assert not list((tmp_path / "data" / "tmp").glob("*"))                      # 校验用的临时文件已删
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_set_config_keeps_crlf_and_bom(tmp_path):
    text = "\ufeffgeneral:\r\n  data_dir: data\r\n  paper_mode: true\r\n"
    cfgp = tmp_path / "config.yaml"
    cfgp.write_bytes(text.encode("utf-8"))
    a = W.Api(cfgp, store=G.SecretStore(FakeBackend(), {}), runner=Recorder())
    assert a.set_config("general.run_every_days", 2)["ok"]
    raw = cfgp.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n  run_every_days: 2\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_set_config_without_file_starts_from_template(tmp_path):
    a = make(tmp_path, example=False)
    assert a.set_config("xtc.daily_limit", 70)["ok"]
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["xtc"]["daily_limit"] == 70
    assert "# " in (tmp_path / "config.yaml").read_text(encoding="utf-8")       # 模板的注释都还在


@pytest.mark.parametrize("key,value,msg", [
    ("watchlist", [], "不能在界面里修改"), ("general.data_dir", "C:/x", "不能在界面里修改"), (["a"], 1, "不能"),
    ("xtc.daily_limit", "abc", "整数"), ("xtc.daily_limit", True, "整数"), ("xtc.daily_limit", 1.5, "整数"),
    ("general.paper_mode", "false", "开或关"), ("general.paper_mode", 0, "开或关"),
    ("fees.sell_fee_rate", float("nan"), "数字"), ("fees.sell_fee_rate", "1e999", "数字"),
    ("llm.model", "a\nb", "换行"), ("llm.model", "x" * 301, "太长"), ("llm.model", 5, "文字"),
])
def test_set_config_rejects_bad_input_before_touching_file(tmp_path, key, value, msg):
    a = make(tmp_path)
    before = (tmp_path / "config.yaml").read_bytes()
    r = a.set_config(key, value)
    assert msg in r["error"]
    assert (tmp_path / "config.yaml").read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("xtc.daily_limit", 0), ("fees.sell_fee_rate", 13), ("llm.base_url", "http://example.com/v1"),
    ("llm.base_url", f"https://api.example.com/v1?key={LLM_KEY}"), ("llm.base_url", f"https://u:{LLM_KEY}@x.com/v1"),
    ("mail.smtp_port", 70000),
])
def test_set_config_runs_strict_validation_and_never_echoes_secrets(tmp_path, key, value):
    a = make(tmp_path)
    before = (tmp_path / "config.yaml").read_bytes()
    r = a.set_config(key, value)
    assert "error" in r and LLM_KEY not in r["error"]
    assert (tmp_path / "config.yaml").read_bytes() == before
    assert LLM_KEY not in a.poll()["text"]


def test_switching_to_real_book_needs_confirmation(tmp_path):
    a = make(tmp_path)
    r = a.set_config("general.paper_mode", False)
    assert "confirm" in r and "实盘" in r["confirm"]["text"]
    assert a.set_config("general.paper_mode", False, "true").get("confirm")      # 字符串不算确认
    assert a.set_config("general.paper_mode", False, True)["ok"]
    assert a.set_config("general.paper_mode", False)["ok"]                      # 已经是 false：不再确认
    assert a.set_config("general.paper_mode", True)["ok"]                       # 切回模拟不用确认


def test_set_config_refused_while_busy(tmp_path):
    gate = threading.Event()

    def slow(argv, emit, continue_event=None):
        gate.wait(5)
        return 0
    a = make(tmp_path, runner=slow)
    a.run("status")
    assert "正在运行" in a.set_config("xtc.daily_limit", 70)["error"]
    assert "正在运行" in a.clean("tmp", True)["error"]
    gate.set()
    assert a.wait_idle()


def test_commands_cannot_start_while_a_file_operation_holds_the_lock(tmp_path, monkeypatch):
    """恢复备份 / 改配置 / 清理进行中（文件锁被占着）时，不能插进来启动一次运行；结束后照常可以。"""
    rec = Recorder()
    a = make(tmp_path, runner=rec)
    inside, release = threading.Event(), threading.Event()

    def slow_clean(*args, **kw):
        inside.set()
        release.wait(5)
        return 0
    monkeypatch.setattr(W.maintenance, "clean", slow_clean)
    t = threading.Thread(target=a.clean, args=("tmp", True))
    t.start()
    assert inside.wait(5)
    for call in (lambda: a.run("status"), lambda: a.note_add("bili", "up", "", "正文")):
        r = call()
        assert r == {"error": W.BUSY_TEXT}, r                                    # 立即拒绝，不排队等待
    release.set()
    t.join(5)
    assert rec.calls == [] and a.run("status") == {"ok": True} and a.wait_idle()
    assert len(rec.calls) == 1


def test_set_config_refuses_unsafe_yaml_shape(tmp_path):
    a = make(tmp_path, text="general: {data_dir: data}\nxtc:\n  daily_limit: 60\n")
    r = a.set_config("general.run_every_days", 2)
    assert "error" in r and "{a: 1}" in r["error"]


# ------------------------------------------------------------------ moligod 模式
def test_moligod_auto_is_unavailable_and_writes_nothing(tmp_path):
    a = make(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    cfg_before = (tmp_path / "config.yaml").read_bytes()
    r = a.set_moligod_mode("auto")
    assert r["unavailable"] is True and r["title"] == "自动设置暂不可用" and "手动" in r["message"]
    assert (tmp_path / "config.yaml").read_bytes() == cfg_before and sorted(p.name for p in tmp_path.rglob("*")) == before
    assert "error" in a.set_moligod_mode("magic") and "error" in a.set_moligod_mode(None)


def test_moligod_manual_turns_enabled_flag_off(tmp_path):
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    a = make(tmp_path, text=text.replace("moligod:\n  enabled: false", "moligod:\n  enabled: true"))
    st = a.state()
    assert st["moligod"] == {"mode": "manual", "auto_available": moligod.AUTO_SYNC_AVAILABLE, "configured_auto": True,
                             "url": "https://moligod.com/ammo-realtime"}
    assert st["features"]["moligod"] is False                                   # 写了 true 但功能停用：实际不会同步
    assert a.set_moligod_mode("manual") == {"ok": True, "mode": "manual"}
    assert a.state()["moligod"]["configured_auto"] is False
    assert a.set_moligod_mode("manual") == {"ok": True, "mode": "manual"}


def test_moligod_login_action_is_refused_while_unavailable(tmp_path):
    rec = Recorder()
    a = make(tmp_path, runner=rec)
    r = a.run("moligod_login", {}, True)
    assert "暂不可用" in r["error"] and rec.calls == []


# ------------------------------------------------------------------ open_url
def test_open_url_only_whitelisted_https(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(W.webbrowser, "open", lambda u: opened.append(u))
    a = make(tmp_path)
    assert a.open_url("repo") == {"ok": True} and a.open_url("issues")["ok"] and a.open_url("releases")["ok"]
    assert a.open_url("moligod")["ok"]
    assert opened == [W.REPO_URL, W.REPO_URL + "/issues", W.REPO_URL + "/releases", "https://moligod.com/ammo-realtime"]
    for bad in ("file:///C:/Windows", "https://evil.example", "javascript:alert(1)", "", None, 1):
        assert "error" in a.open_url(bad)
    monkeypatch.setitem(W.URLS, "repo", "http://insecure.example")
    assert "https" in a.open_url("repo")["error"] and len(opened) == 4


# ------------------------------------------------------------------ 计划任务
def test_tasks_status_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "linux")
    r = make(tmp_path).tasks_status()
    assert r["supported"] is False and [t["name"] for t in r["tasks"]] == list(W.TASK_NAMES)
    assert not any(t["exists"] for t in r["tasks"])


def test_tasks_status_parses_powershell_json(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "win32")
    a = make(tmp_path)
    here = str(tmp_path)
    data = [{"name": "dfq-nightly", "state": "Ready", "next": "2026-11-18 21:30", "last": None, "result": 267011,
             "exe": "cmd.exe", "args": f'/c "{here}\\scripts\\run_dfq.bat" run', "wd": here},
            {"name": "dfq-mail-poll", "state": "Disabled", "next": None, "last": "2026-11-17 10:00", "result": 0,
             "exe": "D:\\Other\\DFQ.exe", "args": "mail-poll", "wd": "D:\\Other"}]
    seen = {}

    def fake(script, timeout=10):
        seen["timeout"] = timeout
        return 0, json.dumps(data)
    monkeypatch.setattr(W, "_run_powershell", fake)
    rows = {t["name"]: t for t in a.tasks_status()["tasks"]}
    assert seen["timeout"] == 10
    assert rows["dfq-nightly"] == {"name": "dfq-nightly", "exists": True, "state": "Ready", "next_run": "2026-11-18 21:30",
                                   "last_run": None, "last_result": 267011, "points_here": True}
    assert rows["dfq-mail-poll"]["points_here"] is False and rows["dfq-mail-poll"]["last_result"] == 0
    assert rows["dfq-watch"]["exists"] is False
    monkeypatch.setattr(W, "_run_powershell", lambda s, timeout=10: (0, json.dumps(data[0])))   # 只有一个任务时是对象
    assert a.tasks_status()["tasks"][0]["exists"] is True


@pytest.mark.parametrize("result", [(1, ""), (0, "not json"), (0, "")])
def test_tasks_status_failures_mean_not_found(tmp_path, monkeypatch, result):
    monkeypatch.setattr(W.sys, "platform", "win32")
    monkeypatch.setattr(W, "_run_powershell", lambda s, timeout=10: result)
    assert not any(t["exists"] for t in make(tmp_path).tasks_status()["tasks"])


def test_tasks_status_timeout(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(W.sys, "platform", "win32")

    def slow(script, timeout=10):
        raise subprocess.TimeoutExpired("powershell", timeout)
    monkeypatch.setattr(W, "_run_powershell", slow)
    r = make(tmp_path).tasks_status()
    assert r["supported"] is True and not any(t["exists"] for t in r["tasks"])


@pytest.mark.parametrize("nightly,poll", [("25:00", 0), ("9:30", 0), ("21:30; calc", 0), (5, 0), ("", 3), ("", 721),
                                          ("", True), ("", "30")])
def test_install_tasks_validates_parameters(tmp_path, nightly, poll):
    assert "error" in make(tmp_path).install_tasks(True, nightly, poll)


def test_install_and_remove_tasks_need_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "win32")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "install_tasks.ps1").write_text("# dummy", encoding="utf-8")
    called = []
    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: called.append(a))
    a = make(tmp_path)
    r = a.install_tasks(False, "22:15", 45)
    assert "22:15" in r["confirm"]["text"] and "45 分钟" in r["confirm"]["text"]
    assert "confirm" in a.remove_tasks() and "confirm" in a.remove_tasks("yes")
    assert called == []


def test_install_and_remove_tasks_pass_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "win32")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "install_tasks.ps1").write_text("# dummy", encoding="utf-8")
    called = []

    class P:
        returncode, stdout, stderr = 0, "done\n", ""
    monkeypatch.setattr(W.subprocess, "run", lambda cmd, **k: (called.append(cmd), P())[1])
    a = make(tmp_path)
    assert a.install_tasks(True, "22:15", 45) == {"ok": True}
    assert a.wait_idle()
    assert called[0][-4:] == ["-NightlyAt", "22:15", "-PollMinutes", "45"]
    assert a.remove_tasks(True) == {"ok": True}
    assert a.wait_idle()
    assert called[1][-1] == "-Remove"


# ------------------------------------------------------------------ 关于 / 更新
def test_about(tmp_path):
    r = make(tmp_path).about()
    assert set(r) == {"version", "frozen", "python", "webview2", "base", "data_dir", "repo", "license", "disclaimer"}
    assert r["license"] == "MIT" and r["repo"].startswith("https://github.com/") and r["frozen"] is False
    assert r["webview2"] is None or isinstance(r["webview2"], str)


def _transport(status, payload, seen=None):
    def t(method, url, params, body, headers, timeout):
        if seen is not None:
            seen.append((method, url, params, body, timeout))
        return status, json.dumps(payload).encode()
    return t


def test_check_update_asks_first_then_only_reads(tmp_path):
    a = make(tmp_path)
    seen = []
    a._http_transport = _transport(200, {"tag_name": "v1.1.0", "body": "x" * 1000}, seen)
    r = a.check_update()
    assert "GitHub" in r["confirm"]["text"] and seen == []
    r = a.check_update(True)
    assert r == {"ok": True, "current": "1.0.0", "latest": "1.1.0", "newer": True, "notes": "x" * 600}
    assert seen == [("GET", W.RELEASES_API, None, None, 8)]                   # 不带任何参数和正文


@pytest.mark.parametrize("status,payload,expect", [
    (404, {"message": "Not Found"}, {"ok": True, "latest": None, "newer": False}),
    (200, {"tag_name": "1.0.0"}, {"ok": True, "latest": "1.0.0", "newer": False}),
    (200, {"tag_name": "v0.9"}, {"ok": True, "latest": "0.9", "newer": False}),
    (200, {"tag_name": "<script>"}, "error"), (200, [1, 2], "error"), (500, {}, "error"),
])
def test_check_update_results(tmp_path, status, payload, expect):
    a = make(tmp_path)
    a._http_transport = _transport(status, payload)
    r = a.check_update(True)
    if expect == "error":
        assert "error" in r
    else:
        assert {k: r[k] for k in expect} == expect


def test_check_update_network_error(tmp_path):
    a = make(tmp_path)

    def down(*args):
        raise H.HttpError("网络错误", retryable=True)
    a._http_transport = down
    assert "检查更新失败" in a.check_update(True)["error"]


# ------------------------------------------------------------------ 数据与备份（界面）
def test_storage_clean_backup_restore_via_api(tmp_path):
    from dfq.db import DB
    a = make(tmp_path)
    db = DB(tmp_path / "data" / "dfq.db")
    db.execute("INSERT INTO trades (ts, side, name, qty, price) VALUES ('2026-11-01 10:00:00', 'buy', 'A', 1, 100)")
    db.close()
    (tmp_path / "data" / "cache").mkdir()
    (tmp_path / "data" / "cache" / "x.json").write_bytes(b"x" * 10)
    assert {i["id"] for i in a.storage()["items"]} >= {"db", "cache"}
    assert "confirm" in a.clean("cache") and (tmp_path / "data" / "cache" / "x.json").exists()
    assert a.clean("cache", True) == {"ok": True, "freed": 10}
    for bad in ("db", "../x", None, ["cache"]):
        assert "error" in a.clean(bad, True)
    r = a.backup_now()
    assert r["ok"] and r["name"].endswith("-manual.db")
    assert [b["name"] for b in a.backups()["items"]] == [r["name"]]
    assert "error" in a.restore_backup("../config.yaml", True)                  # 坏名字：确认之前就拒绝
    assert "error" in a.restore_backup(None)
    assert "confirm" in a.restore_backup(r["name"])
    out = a.restore_backup(r["name"], True)
    assert out["ok"] and out["safety"].endswith("-before-restore.db")


def test_backup_now_without_db(tmp_path):
    assert "还没有数据库" in make(tmp_path).backup_now()["error"]


# ------------------------------------------------------------------ 新动作
def test_new_actions_build_cli_commands(tmp_path):
    rec = Recorder()
    a = make(tmp_path, runner=rec)                                              # 示例配置：paper_mode true
    for act in ("exec_stats", "exec_stats7", "risk_status", "inventory_list"):
        assert a.run(act) == {"ok": True}, act
        assert a.wait_idle()
    assert [c[0] for c in rec.calls] == ["exec-stats", "exec-stats", "risk", "inventory"]
    assert all("--paper" in c for c in rec.calls)                               # 看的是当前账本（模拟）
    assert rec.calls[1][-3:] == ["--days", "7", "--paper"]
    assert "confirm" in a.run("risk_resume")
    assert a.run("risk_resume", {}, True)["ok"] and a.wait_idle()
    assert rec.calls[-1][:1] == ["risk"] and "resume" in rec.calls[-1]


def test_fee_check_confirms_in_real_mode_and_needs_token(tmp_path):
    rec = Recorder()
    a = make(tmp_path, runner=rec, secrets={})
    assert "token" in a.run("fee_check")["error"]
    b = make(tmp_path, runner=rec)
    assert "1 次" in b.run("fee_check")["confirm"]["text"]
    b.set_mock(True)
    assert b.run("fee_check") == {"ok": True} and b.wait_idle()
    assert rec.calls[0][0] == "fee-check" and "--mock" in rec.calls[0]


def test_inventory_add_validation_and_argv(tmp_path):
    rec = Recorder()
    a = make(tmp_path, runner=rec)
    for bad in ({"item": "", "qty": 1, "cost": 1}, {"item": "-h", "qty": 1, "cost": 1}, {"item": "A\nB", "qty": 1, "cost": 1},
                {"item": "PS12B", "qty": 0, "cost": 1}, {"item": "PS12B", "qty": 1, "cost": "nan"},
                {"item": "x" * 101, "qty": 1, "cost": 1}):
        assert "error" in a.run("inventory_add", bad, True), bad
    p = {"item": " PS12B ", "qty": "1,000", "cost": "3500", "paper": True}
    assert "期初库存" in a.run("inventory_add", p)["confirm"]["text"]
    assert a.run("inventory_add", p, True)["ok"] and a.wait_idle()
    c = rec.calls[0]
    assert c[0] == "inventory" and c[3:] == ["add", "PS12B", "1000", "3500.0", "--paper"]


def test_trade_with_net_amount(tmp_path):
    rec = Recorder()
    a = make(tmp_path, runner=rec)
    base = {"side": "sell", "item": "PS12B", "qty": "100", "price": "4000", "paper": False}
    assert "到手" in a.run("trade", {**base, "side": "buy", "net": "1"})["error"]
    assert "到手" in a.run("trade", {**base, "net": "-5"})["error"]
    assert "实际到手：348,000" in a.run("trade", {**base, "net": "348,000"})["confirm"]["text"]
    a.run("trade", {**base, "net": 348000}, True)
    a.run("trade", {**base, "net": ""}, True)
    assert a.wait_idle()
    assert rec.calls[0][-2:] == ["--net", "348000.0"] and "--net" not in rec.calls[1] and "--paper" not in rec.calls[0]
    assert "以 - 开头" in a.run("trade", {**base, "item": "--help"})["error"]


def test_confirmation_requires_real_true(tmp_path):
    rec = Recorder()
    a = make(tmp_path, runner=rec)
    for fake in ("false", "true", 1, [True], {"x": 1}):
        assert "confirm" in a.run("trade_undo", {"id": "3"}, fake)
        assert "confirm" in a.clean("tmp", fake)
        assert "confirm" in a.reset_config(fake)
    assert rec.calls == []
    assert "error" in a.run(["status"]) and "error" in a.run("status", "notadict")


# ------------------------------------------------------------------ 任务结束通知
def test_done_handler_called_once_per_chain_with_original_label(tmp_path):
    a = make(tmp_path)
    got, ev = [], threading.Event()
    a._done_handler = lambda label, ok: (got.append((label, ok, threading.current_thread() is threading.main_thread())),
                                         ev.set())
    a.set_mock(True)
    a.run("run_rules")                                                           # 成功后自动接着“查看最新计划”
    assert a.wait_idle() and ev.wait(2)
    assert got == [("运行（只用规则）", True, False)]
    ev.clear()
    b = make(tmp_path, runner=Recorder(code=2))
    b._done_handler = lambda label, ok: (got.append((label, ok)), ev.set())
    b.run("status")
    assert b.wait_idle() and ev.wait(2) and got[-1] == ("查看状态", False)


def test_done_handler_errors_are_swallowed(tmp_path):
    a = make(tmp_path)

    def bad(label, ok):
        raise RuntimeError("通知失败")
    a._done_handler = bad
    a.run("status")
    assert a.wait_idle() and not a.busy


# ------------------------------------------------------------------ 评审第 8 条：日志 / 导入导出
def test_console_and_log_export_never_contain_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "linux")
    secret = "dummy-test-key-0000-fresh-in-session"
    a = make(tmp_path, runner=Recorder(output=f"调试输出 key={secret} 以及 {secret}\n"))
    assert a.set_secret("DFQ_LLM_API_KEY", secret) == {"ok": True}             # 本次会话刚填的密钥
    a.run("plan")
    assert a.wait_idle()
    p = a.poll()
    assert secret not in p["text"] and secret not in json.dumps(p["results"], ensure_ascii=False)
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "run.log").write_text(f"GET https://x/api?token={TOKEN}&a=1\n{secret}\n", encoding="utf-8")
    (logs / "big.jsonl").write_bytes(b"a" * (W.MAX_LOG_EXPORT_BYTES + 100) + secret.encode())
    try:                                                                         # 链接成 .log 的 .env：不能被打包
        (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={TOKEN}\n", encoding="utf-8")
        (logs / "sneaky.log").symlink_to(tmp_path / ".env")
    except OSError:
        pass                                                                     # 没有创建符号链接的权限：这一项不测
    r = a.export_logs()
    with zipfile.ZipFile(r["path"]) as z:
        body = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
        assert "logs/sneaky.log" not in z.namelist()
        assert len(z.read("logs/big.jsonl")) <= W.MAX_LOG_EXPORT_BYTES
    assert secret not in body and TOKEN not in body and "token=***" in body


def test_log_export_masks_env_only_secrets_and_includes_task_logs(tmp_path, monkeypatch):
    """.env 里界面不管理的密钥（B 站 cookie）界面进程原本不认识；计划任务日志写在程序文件夹的 data\\logs。"""
    monkeypatch.setattr(W.sys, "platform", "linux")
    cookie = "dummy-test-cookie-0000-only-in-dotenv"
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8").replace("data_dir: data", "data_dir: moved", 1)
    a = make(tmp_path, text=text)
    (tmp_path / ".env").write_text(f"DFQ_BILI_COOKIE={cookie}\n", encoding="utf-8")
    task_logs = tmp_path / "data" / "logs"
    task_logs.mkdir(parents=True)
    (task_logs / "task-20261120.log").write_text(f"bili cookie {cookie}\n", encoding="utf-8")
    (tmp_path / "moved" / "logs").mkdir(parents=True)
    (tmp_path / "moved" / "logs" / "dfq-20261120.jsonl").write_text("{}\n", encoding="utf-8")
    with zipfile.ZipFile(a.export_logs()["path"]) as z:
        names = z.namelist()
        body = "".join(z.read(n).decode("utf-8", "replace") for n in names)
    assert {"logs/task-20261120.log", "logs/dfq-20261120.jsonl"} <= set(names)
    assert cookie not in body and "bili cookie" in body


def test_import_rejects_oversized_payload_early_and_bad_types(tmp_path):
    a = make(tmp_path)
    assert "太大" in a.import_config("config.yaml", "A" * (W.MAX_IMPORT_BYTES * 2))["error"]
    assert "error" in a.import_config(None, "")
    assert "error" in a.import_config("config.yaml", 12345)
    assert "error" in a.import_config("x" * 300 + ".yaml", base64.b64encode(b"a: 1").decode())


def test_import_encrypted_or_odd_zip_is_an_error_not_a_crash(tmp_path):
    a = make(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", "general:\n  data_dir: data\n")
    raw = bytearray(buf.getvalue())
    i = raw.find(b"PK\x01\x02")                                                  # 中央目录里把“已加密”标志打开
    raw[i + 8] |= 0x01
    r = a.import_config("x.zip", base64.b64encode(bytes(raw)).decode(), True)
    assert "error" in r


def test_import_env_skips_names_the_ui_does_not_manage(tmp_path):
    a = make(tmp_path, secrets={})
    data = f"DFQ_XTC_TOKEN={TOKEN}\nDFQ_BILI_COOKIE=dummy-test-cookie-0000\n".encode()
    r = a.import_config(".env", base64.b64encode(data).decode())
    assert "DFQ_XTC_TOKEN" in r["confirm"]["text"] and "跳过" in r["confirm"]["text"] and TOKEN not in r["confirm"]["text"]
    assert a.import_config(".env", base64.b64encode(data).decode(), True)["ok"]   # 以前这里会抛异常
    assert a.store.is_set("DFQ_XTC_TOKEN")


def test_import_yaml_uses_safe_load(tmp_path):
    a = make(tmp_path)
    evil = "general: !!python/object/apply:os.system ['echo hacked']\n"
    r = a.import_config("config.yaml", base64.b64encode(evil.encode()).decode(), True)
    assert "校验没通过" in r["error"]


def test_export_config_rejects_bad_folder_argument(tmp_path):
    a = make(tmp_path)
    assert "error" in a.export_config(False, False, 5)
    assert "error" in a.export_config(False, False, str(tmp_path / "missing"))
    out = tmp_path / "out"
    out.mkdir()
    assert "confirm" in a.export_config(True, "yes", str(out)) and list(out.iterdir()) == []


def test_note_add_validates_types_and_lengths(tmp_path):
    a = make(tmp_path)
    assert "格式" in a.note_add("bili", 5, "", "正文")["error"]
    assert "太长" in a.note_add("bili", "up", "", "x" * (W.MAX_NOTE_CHARS + 1))["error"]
    assert "开头" in a.note_add("bili", "--help", "", "正文")["error"]


def test_set_secret_rejects_non_strings(tmp_path):
    a = make(tmp_path)
    assert "error" in a.set_secret("DFQ_XTC_TOKEN", 123) and "error" in a.set_secret(None, "x")
    assert "error" in a.clear_secret("PATH", True)


# ------------------------------------------------------------------ 需要真实数据：演示一遍后的计划 / 仪表盘 / 持仓
@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    a = make(d, runner=G.run_cli)
    assert a.run("demo") == {"ok": True}
    assert a.wait_idle(180)
    a.run("trade", {"side": "buy", "item": "12.7x55mm PS12B", "qty": "100", "price": "3600", "paper": True}, True)
    assert a.wait_idle(60)
    assert len(a.trades()) == 1
    return a


def test_plan_view_after_demo(demo):
    r = demo.plan_view()
    assert r["exists"] is True and r["mock"] is True and isinstance(r["expired"], bool)
    assert r["title"].startswith("【模拟计划】") and r["text"] and r["plan"]["plan_id"]
    assert isinstance(r["alerts"], list) and all({"name", "low", "high"} <= set(x) for x in r["alerts"])
    no_secret(r)
    json.dumps(r)


def test_plan_view_missing_and_corrupt(tmp_path):
    a = make(tmp_path)
    assert a.plan_view() == {"exists": False}
    p = tmp_path / "data" / "plans"
    p.mkdir(parents=True)
    (p / "latest.json").write_text("{坏的", encoding="utf-8")
    r = a.plan_view()
    assert r["exists"] is False and "损坏" in r["error"]
    (p / "latest.json").write_text('{"items": 5}', encoding="utf-8")
    r = a.plan_view()
    assert r["exists"] is False and "error" in r
    assert not list((tmp_path / "data").glob("*.db"))                           # 只读：没有顺手建数据库


def test_dashboard_after_demo(demo):
    d = demo.dashboard()
    json.dumps(d)
    assert d["plan"]["paper"] is True and d["plan"]["buy"] + d["plan"]["watch"] >= 1
    assert d["data"]["minute_days"] >= 1 and d["data"]["day_rows"] >= 1
    assert d["positions"]["count"] == 1 and d["positions"]["book"] == "paper" and d["positions"]["cost"] == 360000
    assert d["last_run"]["status"] and d["usage"]["limit"] == 60
    assert d["risk"] is None or set(d["risk"]) == {"halted", "reason"}
    assert d["health"] and all(h["level"] in ("ok", "warn", "error") for h in d["health"])


def test_dashboard_blocks_fail_independently(demo, monkeypatch):
    from dfq import ledger, risk
    monkeypatch.setattr(ledger, "balance", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(risk, "status", lambda *a, **k: 1 / 0)
    d = demo.dashboard()
    assert d["balance"] == {"known": False, "amount": None} and d["risk"] is None
    assert d["plan"] is not None and d["positions"] is not None


def test_dashboard_without_data_and_with_bad_config(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    d = make(tmp_path / "a", secrets={}).dashboard()
    assert d["plan"] is None and d["positions"]["count"] == 0 and d["usage"]["today"] == 0
    assert any("小涛查 token" in h["text"] for h in d["health"])
    assert not (tmp_path / "a" / "data" / "dfq.db").exists()                    # 只读：没有顺手建数据库
    bad = make(tmp_path / "b", text="xtc:\n  nope: 1\n").dashboard()
    assert bad["health"][0]["level"] == "error" and bad["plan"] is None


def test_positions_after_demo(demo):
    from dfq import fees
    r = demo.positions()
    assert r["book"] == "paper" and r["trades"] == 1 and len(r["rows"]) == 1
    row = r["rows"][0]
    assert row["qty"] == 100 and row["avg_cost"] == 3600 and row["uncovered_qty"] == 0
    assert row["breakeven"] == fees.breakeven_price(3600, 0.13)
    if row["last_price"] is not None:
        assert row["unrealized"] == round((row["last_price"] * 0.87 - 3600) * 100, 2)
        assert row["value"] == round(row["last_price"] * 100, 2)
    assert demo.positions(False)["book"] == "real" and demo.positions(False)["rows"] == []
    assert "error" in demo.positions("yes")
    json.dumps(r)


def test_state_v2_fields(demo):
    s = demo.state()
    assert s["book"] == "paper" and s["fee"]["rate"] == 0.13 and "evidence" in s["fee"] and "calibrated" in s["fee"]
    assert s["moligod"]["mode"] == "manual" and s["moligod"]["auto_available"] is False
    no_secret(s)


def test_pywebview_style_call_by_path(tmp_path):
    """按 pywebview 的方式（点号路径 + 位置参数）调用：白名单方法能用，绕路的路径都拿不到东西。"""
    a = make(tmp_path)
    b = W.make_bridge(a)
    assert _nested(b, "settings")()["ok"] is True
    assert _nested(b, "set_pref")("density", "compact")["ok"] is True
    for path in ("store", "_config_path", "settings.__self__", "settings.__func__.__closure__.0",
                 "settings.__globals__._target", "settings.__globals__.__builtins__.open", "_Api__x"):
        got = _nested(b, path)
        assert got is None or not callable(got), path
    assert sys.modules["dfq.webui.api"] is W
