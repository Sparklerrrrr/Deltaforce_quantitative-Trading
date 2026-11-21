"""程序文件夹的初始化（dfq/layout.py）、卸载保留规则、以及页面桥接的安全性。"""
import base64
import io
import re
import zipfile
from pathlib import Path

import pytest

from dfq import gui_core as G
from dfq import layout

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 首次启动初始化
def test_bootstrap_creates_layout_and_templates(tmp_path):
    created = layout.bootstrap(tmp_path)
    for d in layout.DIRS:
        assert (tmp_path / d).is_dir(), d
    assert (tmp_path / "config.yaml").read_bytes() == (ROOT / "config.example.yaml").read_bytes()
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    from dfq import envfile
    assert envfile.read(tmp_path / ".env") == {}                              # 只有空占位，不生成假密钥
    assert "DFQ_XTC_TOKEN=" in env
    assert "config.yaml" in created and ".env" in created


def test_bootstrap_never_overwrites_existing_files(tmp_path):
    (tmp_path / "config.yaml").write_text("general:\n  data_dir: data\n# 我的设置\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DFQ_XTC_TOKEN=dummy-test-token-0000\n", encoding="utf-8")
    db = tmp_path / "data" / "dfq.db"
    db.parent.mkdir()
    db.write_bytes(b"trade records")
    for _ in range(3):                                                        # 重复启动
        layout.bootstrap(tmp_path)
    assert "# 我的设置" in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "dummy-test-token-0000" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert db.read_bytes() == b"trade records"
    assert layout.bootstrap(tmp_path) == []                                   # 第二次起什么都不新建


def test_bootstrap_cli_mode_only_creates_dirs(tmp_path):
    layout.bootstrap(tmp_path, create_config=False)
    assert not (tmp_path / "config.yaml").exists() and not (tmp_path / ".env").exists()
    assert (tmp_path / "data" / "logs").is_dir()


def test_temp_cleanup_only_touches_old_files_inside_tmp(tmp_path):
    import os
    import time
    layout.bootstrap(tmp_path, create_config=False)
    tmp = tmp_path / "data" / "tmp"
    old, new = tmp / "old.txt", tmp / "new.txt"
    old.write_text("x")
    new.write_text("y")
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))
    keep = tmp_path / "data" / "dfq.db"
    keep.write_text("db")
    os.utime(keep, (past, past))
    assert layout.clean_temp(tmp_path) == 1
    assert not old.exists() and new.exists() and keep.exists()


# ------------------------------------------------------------------ 卸载保留规则与 dfq/layout.py 一致
def _iss_list(func: str) -> set[str]:
    src = (ROOT / "installer" / "DFQ.iss").read_text(encoding="utf-8-sig")
    m = re.search(rf"function {func}\(\): String;\s*begin\s*Result := '([^']*)'", src)
    assert m, func
    return {x for x in m.group(1).split("|") if x}


def test_uninstaller_keep_lists_match_layout():
    top = _iss_list("KeepTopList")
    data = _iss_list("KeepDataList")
    secret = _iss_list("KeepSecretList")
    want_top = {p for p in layout.KEEP_ON_UNINSTALL_DEFAULT if "/" not in p}
    want_data = {p.split("/", 1)[1] for p in layout.KEEP_ON_UNINSTALL_DEFAULT if p.startswith("data/")}
    want_secret = {Path(p).name for p in layout.SECRET_FILES + layout.LOGIN_STATE_DIRS}
    assert top == want_top and data == want_data and secret == want_secret
    assert "dfq.db" in data                                                   # 普通卸载不删交易记录


def test_installer_does_not_ship_personal_files():
    src = (ROOT / "installer" / "DFQ.iss").read_text(encoding="utf-8-sig")
    files = [line for line in src.splitlines() if line.startswith("Source:")]
    joined = "\n".join(files)
    assert "config.yaml" not in joined.replace("config.example.yaml", "")
    assert ".env\"" not in joined and ".env;" not in joined                  # 只带 .env.example
    bat = (ROOT / "scripts" / "build_exe.bat").read_text(encoding="ascii")
    assert "config.yaml;" not in bat and '\\.env;' not in bat


# ------------------------------------------------------------------ 页面桥接：只暴露白名单方法
class FakeBackend(G.SecretBackend):
    available, where = True, "测试用"

    def __init__(self):
        self.data = {"DFQ_XTC_TOKEN": "tok-REAL-looking-0123456789"}

    def read(self, n):
        return self.data.get(n)

    def write(self, n, v):
        self.data[n] = v

    def delete(self, n):
        self.data.pop(n, None)


def _bridge(tmp_path):
    from dfq.webui import api as W
    cfg = tmp_path / "config.yaml"
    cfg.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    a = W.Api(cfg, store=G.SecretStore(FakeBackend(), {}))
    return W, a, W.make_bridge(a)


def _nested(obj, path):                                                       # 与 pywebview 的取属性方式相同
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def test_bridge_exposes_only_whitelisted_methods(tmp_path):
    W, a, b = _bridge(tmp_path)
    public = sorted(n for n in dir(b) if not n.startswith("_"))
    assert public == sorted(W.BRIDGE_METHODS)
    assert not any(not callable(getattr(b, n)) for n in public)              # 没有任何数据属性


@pytest.mark.parametrize("path", ["store", "_store", "store.backend.read", "poll.__self__.store",
                                  "poll.__wrapped__", "poll.__func__.__wrapped__", "_window",
                                  "poll.__self__._close_handler", "state.__self__.__dict__"])
def test_bridge_cannot_reach_secret_store(tmp_path, path):
    _, _, b = _bridge(tmp_path)
    got = _nested(b, path)
    assert got is None or (isinstance(got, dict) and not got), path


def test_bridge_methods_forward_with_same_signature(tmp_path):
    import inspect
    W, a, b = _bridge(tmp_path)
    for name in W.BRIDGE_METHODS:
        real = list(inspect.signature(getattr(a, name)).parameters)
        exposed = inspect.getfullargspec(getattr(b, name)).args[1:]          # pywebview 用它生成 JS 参数
        assert exposed == real, name
    assert b.set_mock(True) == {"ok": True} and a.mock is True
    assert b.run("rm -rf")["error"].startswith("未知操作")


def test_page_has_strict_csp_without_network():
    from dfq.webui.app import static_dir
    html = (static_dir() / "index.html").read_text(encoding="utf-8")
    m = re.search(r'Content-Security-Policy" content="([^"]+)"', html)
    assert m and "connect-src 'none'" in m.group(1) and "default-src 'none'" in m.group(1)
    assert "<script>" not in html                                            # 没有内联脚本


# ------------------------------------------------------------------ 配置导入：压缩炸弹
def test_import_rejects_zip_bomb_without_trusting_header(tmp_path):
    _, a, _ = _bridge(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("config.yaml", b"#" * (5 * 1024 * 1024))                  # 压缩后很小，解压 5 MB
    r = a.import_config("bomb.zip", base64.b64encode(buf.getvalue()).decode(), True)
    assert "太大" in r["error"]


def test_reset_config_backs_up_then_restores_template(tmp_path):
    _, a, _ = _bridge(tmp_path)
    (tmp_path / "config.yaml").write_text("broken: [", encoding="utf-8")
    assert "confirm" in a.reset_config()
    r = a.reset_config(True)
    assert (tmp_path / "config.yaml").read_bytes() == (ROOT / "config.example.yaml").read_bytes()
    assert (Path(r["backup"]) / "config.yaml").read_text(encoding="utf-8") == "broken: ["


def test_powershell_scripts_have_no_curly_quotes():
    """Windows PowerShell 5.1 把中文弯引号 “ ” ‘ ’ 当作字符串的引号：写在双引号字符串里会把字符串截断，
    语法检查照样通过、运行时才报错（2026-11-15 实测：计划任务因此没有创建）。中文引号请用「」。"""
    for p in (ROOT / "scripts").glob("*.ps1"):
        text = p.read_text(encoding="utf-8-sig")
        bad = [i for i, line in enumerate(text.splitlines(), 1) if any(c in line for c in "\u201c\u201d\u2018\u2019")]
        assert not bad, f"{p.name} 第 {bad} 行有弯引号"
