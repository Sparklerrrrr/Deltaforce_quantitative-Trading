"""界面偏好 dfq/webui/prefs.py 与 Api.set_pref（BRIDGE.md 第 1 节）。"""
import json
import threading

import pytest

from dfq import gui_core as G
from dfq.webui import api as W
from dfq.webui import prefs


class _Backend(G.SecretBackend):
    available, where = True, "测试"

    def read(self, name):
        return None


def make(tmp_path):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    return W.Api(cfgp, store=G.SecretStore(_Backend(), {}), runner=lambda *a, **k: 0)


def test_defaults_cover_every_key_in_contract(tmp_path):
    d = prefs.load(tmp_path)
    assert d == {"close_action": "ask", "theme": "", "accent": "#78b49d", "scale": 100, "density": "comfortable",
                 "motion": "auto", "glow": True, "gpu": True, "tray_saver": True, "notify_done": True,
                 "remember_window": True, "side_collapsed": False, "backup_on_start": True, "backup_keep": 7,
                 "log_keep_days": 30, "window": None}
    assert prefs.RESTART_KEYS == ("gpu",) and set(prefs.SCHEMA) == set(d)


def test_one_bad_key_falls_back_alone(tmp_path):
    p = prefs.path_for(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"close_action": "tray", "scale": 77, "accent": "#ABCDEF", "glow": "yes",
                             "window": {"w": 800, "h": 600, "x": -1200, "y": 10, "max": False}}), encoding="utf-8")
    d = prefs.load(tmp_path)
    assert d["close_action"] == "tray" and d["accent"] == "#abcdef"          # 合法的键保留
    assert d["scale"] == 100 and d["glow"] is True                          # 只有坏的键退回默认值
    assert d["window"] == {"w": 800, "h": 600, "x": -1200, "y": 10, "max": False}


@pytest.mark.parametrize("data", ["[1, 2]", "null", "{bad", '"text"', ""])
def test_broken_or_odd_file_gives_defaults(tmp_path, data):
    p = prefs.path_for(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(data, encoding="utf-8")
    assert prefs.load(tmp_path) == prefs.DEFAULTS


@pytest.mark.parametrize("key,value", [
    ("scale", True), ("scale", 100.0), ("scale", "100"), ("scale", 95), ("accent", "#12345"), ("accent", "red"),
    ("accent", "#12345g"), ("density", "tiny"), ("motion", None), ("glow", 1), ("backup_keep", 0),
    ("backup_keep", 61), ("backup_keep", True), ("log_keep_days", 14), ("theme", "blue"), ("close_action", ""),
    ("window", {"w": 0, "h": 1, "x": 0, "y": 0, "max": False}), ("window", {"w": 1}), ("unknown", 1),
])
def test_save_rejects_invalid_values_and_writes_nothing(tmp_path, key, value):
    with pytest.raises(ValueError):
        prefs.save(tmp_path, **{key: value})
    assert not prefs.path_for(tmp_path).exists()


def test_save_is_atomic_and_normalizes_accent(tmp_path):
    out = prefs.save(tmp_path, accent="#AABBCC", scale=125, backup_keep=60, log_keep_days=0)
    assert out["accent"] == "#aabbcc" and out["scale"] == 125
    data = json.loads(prefs.path_for(tmp_path).read_text(encoding="utf-8"))
    assert data["accent"] == "#aabbcc" and data["log_keep_days"] == 0
    assert [p.name for p in prefs.path_for(tmp_path).parent.iterdir()] == ["ui-settings.json"]   # 没有残留临时文件


def test_set_pref_validates_and_reports_restart(tmp_path):
    a = make(tmp_path)
    r = a.set_pref("gpu", False)
    assert r["ok"] and r["restart"] is True and r["prefs"]["gpu"] is False
    assert a.set_pref("gpu", False)["restart"] is False                     # 没变就不用重启
    assert a.set_pref("density", "compact")["restart"] is False
    assert "error" in a.set_pref("window", {"w": 1, "h": 1, "x": 0, "y": 0, "max": False})   # 页面不能改窗口位置
    assert "error" in a.set_pref("nope", 1)
    assert "error" in a.set_pref(["scale"], 100)
    assert "error" in a.set_pref("scale", 99)
    assert a.ui_prefs()["scale"] == 100 and "window" in a.ui_prefs()


def test_set_pref_calls_theme_and_scale_handlers_in_background(tmp_path):
    a = make(tmp_path)
    got, ev = {}, threading.Event()

    def handler(kind):
        def h(v):
            got[kind] = (v, threading.current_thread() is not threading.main_thread())
            if len(got) == 2:
                ev.set()
        return h
    a._theme_handler, a._scale_handler = handler("theme"), handler("scale")
    a.set_pref("theme", "dark")
    a.set_pref("scale", 110)
    assert ev.wait(2)
    assert got == {"theme": ("dark", True), "scale": (110, True)}


def test_set_pref_without_handlers_is_fine(tmp_path):
    a = make(tmp_path)
    assert a.set_pref("scale", 90)["ok"] and a.set_pref("motion", "off")["prefs"]["motion"] == "off"
