"""网页界面后端（dfq/webui/api.py）。不打开窗口，直接调用后端方法。"""
import json
import zipfile

import pytest

from dfq import gui_core as G
from dfq.webui import api as W
from dfq.webui import tasks as T

TOKEN = "tok-SECRET-abcdef123456"


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
    """假的命令执行器：记下收到的命令，不真的运行。"""

    def __init__(self, code=0, output="ok\n"):
        self.calls, self.code, self.output = [], code, output

    def __call__(self, argv, emit, continue_event=None):
        self.calls.append(list(argv))
        emit(self.output)
        return self.code


def make(tmp_path, secrets=None, runner=None):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    env = {}
    store = G.SecretStore(FakeBackend(secrets), env)
    return W.Api(cfgp, store=store, runner=runner or Recorder()), env


def test_costly_actions_need_confirmation_before_anything_runs(tmp_path):
    rec = Recorder()
    a, _ = make(tmp_path, {"DFQ_XTC_TOKEN": TOKEN}, rec)
    for action in ("run_rules", "run_llm", "probe", "mail_poll"):
        r = a.run(action)
        assert "confirm" in r and r["confirm"]["text"], action
    assert rec.calls == []                                                    # 没确认就一条命令都没跑
    assert a.run("run_rules", confirmed=True) == {"ok": True}
    assert a.wait_idle()
    assert rec.calls[0][:1] == ["run"] and "--offline" in rec.calls[0] and "--mock" not in rec.calls[0]


def test_mock_mode_skips_cost_confirmation_and_adds_flag(tmp_path):
    rec = Recorder()
    a, _ = make(tmp_path, runner=rec)
    a.set_mock(True)
    assert a.run("run_llm") == {"ok": True}                                   # 模拟数据不花钱，不用确认
    assert a.wait_idle()
    assert "--mock" in rec.calls[0]
    assert rec.calls[1][0] == "plan"                                          # 运行后自动刷新计划页


def test_probe_without_token_is_refused(tmp_path):
    a, _ = make(tmp_path)
    assert "token" in a.run("probe", confirmed=True)["error"]


def test_trade_validation_and_confirmation(tmp_path):
    rec = Recorder()
    a, _ = make(tmp_path, runner=rec)
    assert "正整数" in a.run("trade", {"side": "buy", "item": "PS12B", "qty": "0", "price": "3600"})["error"]
    assert "买入或卖出" in a.run("trade", {"side": "x", "item": "PS12B", "qty": "1", "price": "1"})["error"]
    r = a.run("trade", {"side": "buy", "item": "PS12B", "qty": "1,000", "price": "3600", "paper": True})
    assert "模拟账本" in r["confirm"]["text"]
    a.run("trade", {"side": "buy", "item": "PS12B", "qty": "1,000", "price": "3600", "paper": True}, confirmed=True)
    assert a.wait_idle()
    assert rec.calls[0][:5] == ["trade", "--config", rec.calls[0][2], "buy", "PS12B"]
    assert "1000" in rec.calls[0] and "--paper" in rec.calls[0]


def test_one_action_at_a_time_and_unknown_action(tmp_path):
    import threading
    gate = threading.Event()

    def slow(argv, emit, continue_event=None):
        gate.wait(5)
        return 0
    a, _ = make(tmp_path, runner=slow)
    assert a.run("status") == {"ok": True}
    assert "还没结束" in a.run("status")["error"]
    assert "正在运行" in a.set_mock(True)["error"]
    gate.set()
    assert a.wait_idle()
    assert "未知操作" in a.run("rm -rf")["error"]


def test_secrets_are_write_only(tmp_path):
    a, env = make(tmp_path, {"DFQ_XTC_TOKEN": TOKEN})
    st = a.state()
    assert TOKEN not in json.dumps(st, ensure_ascii=False)                    # 状态里只有“已设置”
    assert next(s for s in st["secrets"] if s["name"] == "DFQ_XTC_TOKEN")["set"] is True
    assert TOKEN not in a.poll()["text"]
    assert a.set_secret("DFQ_LLM_API_KEY", "sk-new-key-998877") == {"ok": True}
    assert "sk-new-key-998877" not in json.dumps(a.state()) + a.poll()["text"]
    assert "error" in a.set_secret("PATH", "x")                               # 不能借界面改别的环境变量
    assert "confirm" in a.clear_secret("DFQ_LLM_API_KEY")                     # 清除也要确认
    assert a.clear_secret("DFQ_LLM_API_KEY", confirmed=True) == {"ok": True}
    assert not any(n for n in W.public_methods() if "get_secret" in n or "read_secret" in n)


def test_poll_streams_output_and_page_results(tmp_path):
    a, _ = make(tmp_path, runner=Recorder(output="计划正文\n"))
    p0 = a.poll()
    a.run("plan")
    assert a.wait_idle()
    p1 = a.poll(p0["seq"], 0)
    assert "▶ 查看最新计划" in p1["text"] and "计划正文" in p1["text"] and "■ 结束（成功）" in p1["text"]
    assert p1["results"]["plan"] == "计划正文\n"
    assert a.poll(p1["seq"], p1["results_seq"]) == {**a.poll(p1["seq"], p1["results_seq"]), "text": "", "results": None}


def test_reflect_and_undo_need_numeric_id(tmp_path):
    a, _ = make(tmp_path)
    assert "反思编号" in a.run("reflect_show", {"id": "abc"})["error"]
    assert "成交编号" in a.run("trade_undo", {"id": ""})["error"]
    assert "confirm" in a.run("trade_undo", {"id": "3"})


def test_note_add_writes_temp_file_and_cleans_up(tmp_path):
    seen = {}

    def runner(argv, emit, continue_event=None):
        path = argv[argv.index("--file") + 1]
        with open(path, encoding="utf-8") as fh:
            seen["text"] = fh.read()
        seen["path"] = path
        return 0
    a, _ = make(tmp_path, runner=runner)
    assert "信源名" in a.note_add("bili", "", "", "正文")["error"]
    assert a.note_add("bili", "moligod", "标题", "  视频文字稿  ") == {"ok": True}
    assert a.wait_idle()
    import os
    assert seen["text"] == "视频文字稿" and not os.path.exists(seen["path"])


def test_export_logs_contains_console_but_never_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(W.sys, "platform", "linux")                          # 不弹资源管理器
    a, _ = make(tmp_path)
    (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={TOKEN}\n", encoding="utf-8")
    a._emit("一行日志\n")
    r = a.export_logs()
    with zipfile.ZipFile(r["path"]) as z:
        names = z.namelist()
        body = "".join(z.read(n).decode("utf-8", "replace") for n in names)
    assert "界面运行日志.txt" in names and "一行日志" in body
    assert TOKEN not in body and not any(".env" in n for n in names)


def test_task_manifest_points_at_real_actions():
    """任务清单（学 MaaEnd 的 interface.json）里的每个按钮都必须对应一个真实动作。"""
    data = T.as_json()
    for group in data.values():
        for t in group:
            assert t["title"] and t["icon"] and t["actions"]
            for a in t["actions"]:
                assert a["action"] in W.ACTIONS, a
    costly = {a for t in T.HOME_TASKS if t["cost"] for _, a, _ in t["actions"]}
    assert costly <= {k for k, v in W.ACTIONS.items() if v.confirm}           # 标了“计费”的都有确认


def test_demo_end_to_end_with_real_commands(tmp_path):
    """用真实的命令执行器跑一遍模拟演示：流水线 → 自动刷新计划 → 记账 → 撤销。"""
    a, _ = make(tmp_path, runner=G.run_cli)
    assert a.run("demo") == {"ok": True}
    assert a.wait_idle(120)
    p = a.poll()
    assert "流水线" in p["text"] and "【模拟计划】" in p["results"]["plan"]
    a.run("trade", {"side": "buy", "item": "12.7x55mm PS12B", "qty": "100", "price": "3600", "paper": True},
          confirmed=True)
    assert a.wait_idle(60)
    rows = a.trades()
    assert len(rows) == 1 and rows[0]["paper"] is True and rows[0]["qty"] == 100
    a.run("trade_undo", {"id": rows[0]["id"]}, confirmed=True)
    assert a.wait_idle(60)
    assert a.trades() == []


# ------------------------------------------------------------------ 配置导入 / 导出
import base64  # noqa: E402
import io  # noqa: E402

GOOD_CFG = "general:\n  data_dir: data\nxtc:\n  daily_limit: 40\n"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_export_without_secrets_by_default(tmp_path):
    a, _ = make(tmp_path)
    (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={TOKEN}\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    r = a.export_config(False, folder=str(out))
    with zipfile.ZipFile(r["path"]) as z:
        names = set(z.namelist())
        body = b"".join(z.read(n) for n in names)
    assert names == {"config.yaml", "dfq-manifest.json"} and TOKEN.encode() not in body
    assert "含密钥" not in r["path"]


def test_export_with_secrets_needs_confirmation(tmp_path):
    a, _ = make(tmp_path)
    (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={TOKEN}\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert "confirm" in a.export_config(True, folder=str(out))
    assert list(out.iterdir()) == []                                          # 没确认就什么都不写
    r = a.export_config(True, True, folder=str(out))
    with zipfile.ZipFile(r["path"]) as z:
        assert ".env" in z.namelist() and TOKEN.encode() in z.read(".env")


def test_export_cancelled_folder_dialog_writes_nothing(tmp_path):
    a, _ = make(tmp_path)
    a._pick_folder = lambda: None
    assert a.export_config(False) == {"cancelled": True}


def test_import_yaml_validates_backs_up_and_replaces(tmp_path):
    a, _ = make(tmp_path)
    old = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    r = a.import_config("config.yaml", b64(GOOD_CFG.encode()))
    assert "confirm" in r and "config.yaml" in r["confirm"]["text"]
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == old      # 没确认不改
    r = a.import_config("config.yaml", b64(GOOD_CFG.encode()), True)
    assert r["ok"] and (tmp_path / "config.yaml").read_text(encoding="utf-8") == GOOD_CFG
    from pathlib import Path
    assert (Path(r["backup"]) / "config.yaml").read_text(encoding="utf-8") == old


def test_import_rejects_invalid_config_without_touching_files(tmp_path):
    a, _ = make(tmp_path)
    old = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    bad = "general:\n  data_dir: data\nxtc:\n  daily_limitt: 40\n"               # 键名拼错
    r = a.import_config("config.yaml", b64(bad.encode()), True)
    assert "校验没通过" in r["error"] and "daily_limitt" in r["error"]
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == old
    assert "只支持" in a.import_config("evil.exe", b64(b"MZ"), True)["error"]
    assert "空" in a.import_config("config.yaml", "", True)["error"]


def test_import_zip_round_trip_with_secrets(tmp_path):
    src_dir, dst_dir = tmp_path / "src", tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    a, _ = make(src_dir)
    (src_dir / "config.yaml").write_text(GOOD_CFG, encoding="utf-8")
    (src_dir / ".env").write_text(f"DFQ_XTC_TOKEN={TOKEN}\nPATH=evil\n", encoding="utf-8")
    zpath = a.export_config(True, True, folder=str(tmp_path))["path"]
    b, env = make(dst_dir)
    data = open(zpath, "rb").read()
    r = b.import_config("备份.zip", b64(data))
    assert "DFQ_XTC_TOKEN" in r["confirm"]["text"] and "PATH" not in r["confirm"]["text"]
    assert b.import_config("备份.zip", b64(data), True)["ok"]
    assert (dst_dir / "config.yaml").read_text(encoding="utf-8") == GOOD_CFG
    assert env["DFQ_XTC_TOKEN"] == TOKEN and "PATH" not in env               # 只认登记过的密钥名
    assert TOKEN not in b.poll()["text"]


def test_import_zip_ignores_other_members(tmp_path):
    a, _ = make(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", GOOD_CFG)
        z.writestr("../../evil.txt", "x")                                     # 路径穿越：不会被解压
    assert a.import_config("x.zip", b64(buf.getvalue()), True)["ok"]
    assert not (tmp_path.parent / "evil.txt").exists() and not (tmp_path / "evil.txt").exists()


def test_note_temp_file_stays_inside_program_folder(tmp_path):
    seen = {}

    def runner(argv, emit, continue_event=None):
        seen["path"] = argv[argv.index("--file") + 1]
        return 0
    a, _ = make(tmp_path, runner=runner)
    a.note_add("bili", "up", "", "正文")
    assert a.wait_idle()
    from pathlib import Path
    assert Path(seen["path"]).resolve().is_relative_to((tmp_path / "data" / "tmp").resolve())


# ------------------------------------------------------------------ 关闭窗口：托盘 / 退出
def test_close_pref_defaults_to_ask_and_survives_broken_file(tmp_path):
    from dfq.webui import prefs
    assert prefs.load(tmp_path)["close_action"] == "ask"
    prefs.path_for(tmp_path).parent.mkdir(parents=True)
    prefs.path_for(tmp_path).write_text("{坏掉的 json", encoding="utf-8")
    assert prefs.load(tmp_path)["close_action"] == "ask"                    # 文件坏了也能打开程序
    with pytest.raises(ValueError):
        prefs.save(tmp_path, close_action="explode")
    assert prefs.save(tmp_path, close_action="tray")["close_action"] == "tray"
    assert prefs.path_for(tmp_path).resolve().is_relative_to((tmp_path / "data").resolve())  # 只在程序文件夹里


def test_close_pref_file_with_bom_still_works(tmp_path):
    """用记事本 / PowerShell 保存的文件开头带 BOM：以前会读失败、悄悄退回“每次询问”。"""
    from dfq.webui import prefs
    prefs.path_for(tmp_path).parent.mkdir(parents=True)
    prefs.path_for(tmp_path).write_bytes(b'\xef\xbb\xbf{"close_action": "tray"}')
    assert prefs.load(tmp_path)["close_action"] == "tray"


def test_close_choice_remembers_only_when_asked(tmp_path):
    import threading
    a, _ = make(tmp_path)
    got, ev = [], threading.Event()
    a._close_handler = lambda action: (got.append(action), ev.set())
    assert a.close_choice("tray", False) == {"ok": True}
    assert ev.wait(2) and got == ["tray"]
    assert a.ui_prefs()["close_action"] == "ask"                              # 没勾“记住”就不保存
    ev.clear()
    a.close_choice("quit", True)
    assert ev.wait(2) and got == ["tray", "quit"]
    assert a.ui_prefs()["close_action"] == "quit"
    assert "error" in a.close_choice("format-c", True)
    assert a.set_close_action("ask")["prefs"]["close_action"] == "ask"        # 设置页可以改回“每次询问”
    assert "error" in a.set_close_action("nope")


def test_single_instance_address_is_per_install_folder(tmp_path):
    from dfq.webui import app
    a1, k1 = app._instance_address(tmp_path / "A")
    a2, _ = app._instance_address(tmp_path / "B")
    assert a1.startswith(r"\\.\pipe\dfq-") and a1 != a2 and k1
    assert app._instance_address(tmp_path / "A") == (a1, k1)                  # 同一个文件夹地址固定


def test_tray_icon_image_draws():
    pytest.importorskip("PIL")
    from dfq.webui import tray
    img = tray._icon_image()
    assert img.size == (64, 64) and img.getpixel((32, 4))[3] == 255


@pytest.mark.parametrize("name", ["index.html", "app.css", "app.js", "theme.js"])
def test_static_files_exist_and_use_no_network(name):
    from pathlib import Path
    p = Path(W.__file__).parent / "static" / name
    text = p.read_text(encoding="utf-8")
    assert "http://" not in text and "https://" not in text                   # 不加载任何网络资源


def test_theme_pref_is_validated_and_drives_window_background(tmp_path):
    import threading
    from dfq.webui import app as A, prefs
    a, _ = make(tmp_path)
    got, ev = [], threading.Event()
    a._theme_handler = lambda t: (got.append(t), ev.set())
    assert a.set_theme("dark") == {"ok": True} and ev.wait(2) and got == ["dark"]
    assert prefs.load(tmp_path)["theme"] == "dark"
    assert "error" in a.set_theme("rainbow")
    css = (A.static_dir() / "app.css").read_text(encoding="utf-8").lower()
    for theme, color in A.THEME_BG.items():                                  # 窗口底色必须和页面底色一致，否则缩放时会闪
        assert f"--bg: {color.lower()}" in css, theme


def test_icon_file_is_found_and_used_for_tray():
    from dfq.webui import app as A, tray
    ico = A.icon_path()
    assert ico is not None and ico.name == "dfq.ico"
    pytest.importorskip("PIL")
    img = tray._icon_image(ico)
    assert img.size[0] >= 16 and img.mode == "RGBA"


def test_browser_args_only_set_for_this_process():
    from dfq.webui import app as A
    assert "NetworkServiceInProcess2" in A.BROWSER_ARGS and "ElasticOverscroll" in A.BROWSER_ARGS
    src = (pathlib_root() / "dfq" / "webui" / "app.py").read_text(encoding="utf-8")
    assert "os.environ.setdefault(\"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS\"" in src   # 只在本进程里设，且不覆盖用户自己的
    assert "SetEnvironmentVariable" not in src                                         # 不改系统环境变量


def pathlib_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent
