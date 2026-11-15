"""图形界面：核心逻辑（不需要显示器）+ 真实窗口冒烟测试（有 tkinter 和显示器时才跑）。"""
import os
import sys
import threading
from pathlib import Path

import pytest

from dfq import gui_core as G
from dfq import ledger
from dfq.cli import main

TOKEN = "tk_gui_SECRET_778899"


class FakeBackend(G.SecretBackend):
    available = True
    where = "测试用"

    def __init__(self, data=None):
        self.data = dict(data or {})
        self.fail = False

    def read(self, name):
        return self.data.get(name)

    def write(self, name, value):
        if self.fail:
            raise OSError("拒绝访问")
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)


# ------------------------------------------------------------------ 执行命令
def test_run_cli_captures_output_and_restores_streams(tmp_path):
    out = []
    before = sys.stdout, sys.stderr, sys.stdin
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    code = G.run_cli(["status", "--config", str(cfgp), "--mock"], out.append)
    assert code == 0 and "DFQ_XTC_TOKEN" in "".join(out) and "未设置" in "".join(out)
    assert (sys.stdout, sys.stderr, sys.stdin) == before


def test_run_cli_reports_bad_arguments_and_bad_config_without_crashing(tmp_path):
    out = []
    assert G.run_cli(["trade", "buy"], out.append) != 0                      # argparse 报错 → SystemExit
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("risk:\n  max_position_pct: 25\n", encoding="utf-8")
    out.clear()
    assert G.run_cli(["status", "--config", str(cfgp)], out.append) == 2
    assert "25% 应写 0.25" in "".join(out)


def test_run_cli_unexpected_error_is_shown_redacted(monkeypatch):
    from dfq import cli
    from dfq.log import register_secret
    register_secret(TOKEN)

    def boom(argv):
        raise RuntimeError(f"炸了 token={TOKEN}")
    monkeypatch.setattr(cli, "main", boom)
    out = []
    assert G.run_cli(["status"], out.append) == 1
    text = "".join(out)
    assert "RuntimeError" in text and TOKEN not in text


def test_only_one_command_at_a_time(monkeypatch):
    from dfq import cli
    gate, entered = threading.Event(), threading.Event()

    def slow(argv):
        entered.set()
        gate.wait(5)
        return 0
    monkeypatch.setattr(cli, "main", slow)
    t = threading.Thread(target=G.run_cli, args=(["status"], lambda s: None))
    t.start()
    entered.wait(5)
    out = []
    assert G.run_cli(["status"], out.append) == 1 and "还在运行" in "".join(out)
    gate.set()
    t.join(5)


def test_waiting_stdin_unblocks_input(monkeypatch):
    from dfq import cli
    ev = threading.Event()
    got = []

    def wants_enter(argv):
        print("请按回车")
        got.append(input())
        print("继续了")
        return 0
    monkeypatch.setattr(cli, "main", wants_enter)
    out = []
    t = threading.Thread(target=lambda: got.append(G.run_cli(["x"], out.append, continue_event=ev)))
    t.start()
    t.join(0.3)
    assert t.is_alive()                                                       # 正在等按钮
    ev.set()
    t.join(5)
    assert got == ["", 0] and "继续了" in "".join(out)


# ------------------------------------------------------------------ 密钥
def test_secret_set_persists_and_applies_to_this_process():
    env, be = {}, FakeBackend()
    s = G.SecretStore(be, env)
    assert not s.is_set("DFQ_XTC_TOKEN")
    s.set("DFQ_XTC_TOKEN", f"  {TOKEN}  ")
    assert be.data["DFQ_XTC_TOKEN"] == TOKEN and env["DFQ_XTC_TOKEN"] == TOKEN and s.is_set("DFQ_XTC_TOKEN")
    from dfq.log import redact
    assert TOKEN not in redact(f"url?x={TOKEN}")                             # 登记进了脱敏表
    s.clear("DFQ_XTC_TOKEN")
    assert "DFQ_XTC_TOKEN" not in be.data and "DFQ_XTC_TOKEN" not in env


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "x" * 5000])
def test_secret_rejects_garbage(bad):
    env, be = {}, FakeBackend()
    with pytest.raises(ValueError):
        G.SecretStore(be, env).set("DFQ_XTC_TOKEN", bad)
    assert be.data == {} and env == {}


def test_secret_unknown_name_rejected():
    s = G.SecretStore(FakeBackend(), {})
    with pytest.raises(ValueError):
        s.set("PATH", "x")                                                    # 不能借界面改别的环境变量
    with pytest.raises(ValueError):
        s.clear("PATH")


def test_secret_failed_write_leaves_process_unchanged():
    env, be = {}, FakeBackend()
    be.fail = True
    with pytest.raises(OSError):
        G.SecretStore(be, env).set("DFQ_XTC_TOKEN", TOKEN)
    assert env == {}


def test_startup_picks_up_token_set_after_parent_process_started():
    """你遇到的情况：环境变量已经写进系统，但程序是从旧环境启动的。启动时会补读进来。"""
    env = {"DFQ_SMTP_PASSWORD": "already-here-123"}
    be = FakeBackend({"DFQ_XTC_TOKEN": TOKEN, "DFQ_SMTP_PASSWORD": "stale-value-456"})
    loaded = G.SecretStore(be, env).load_into_process()
    assert loaded == ["DFQ_XTC_TOKEN"] and env["DFQ_XTC_TOKEN"] == TOKEN
    assert env["DFQ_SMTP_PASSWORD"] == "already-here-123"                    # 进程里已有的不覆盖


def test_unavailable_backend_is_safe():
    s = G.SecretStore(G.SecretBackend(), {})
    assert s.load_into_process() == [] and not s.is_set("DFQ_XTC_TOKEN")
    with pytest.raises(RuntimeError):
        s.set("DFQ_XTC_TOKEN", TOKEN)


def test_default_backend_is_dotenv_next_to_config(tmp_path):
    be = G.default_backend(tmp_path)
    assert isinstance(be, G.DotEnvFile) and be.available and be.path == tmp_path / ".env"


# ------------------------------------------------------------------ .env 文件
def test_dotenv_store_roundtrip_keeps_other_lines(tmp_path):
    from dfq import envfile
    p = tmp_path / ".env"
    p.write_text("# 我的注释\nOTHER=keep-me\nDFQ_XTC_TOKEN=old-value-1\n", encoding="utf-8")
    env = {}
    s = G.SecretStore(G.DotEnvFile(p), env)
    s.set("DFQ_XTC_TOKEN", TOKEN)
    s.set("DFQ_SMTP_PASSWORD", "smtp-code-789")
    text = p.read_text(encoding="utf-8")
    assert "# 我的注释" in text and "OTHER=keep-me" in text and "old-value-1" not in text
    assert text.count("DFQ_XTC_TOKEN=") == 1
    assert envfile.read(p) == {"DFQ_XTC_TOKEN": TOKEN, "DFQ_SMTP_PASSWORD": "smtp-code-789"}   # OTHER 不读入
    s.clear("DFQ_XTC_TOKEN")
    assert "DFQ_XTC_TOKEN" not in envfile.read(p) and "DFQ_XTC_TOKEN" not in env
    assert list(tmp_path.glob("*.tmp")) == []                                # 没留下临时文件


def test_dotenv_creates_file_with_warning_header(tmp_path):
    p = tmp_path / ".env"
    G.SecretStore(G.DotEnvFile(p), {}).set("DFQ_XTC_TOKEN", TOKEN)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# DFQ 本地密钥文件") and f"DFQ_XTC_TOKEN={TOKEN}\n" in text


def test_dotenv_parsing_tolerates_notepad_quotes_and_bom(tmp_path):
    from dfq import envfile
    p = tmp_path / ".env"
    p.write_bytes("﻿DFQ_XTC_TOKEN = \"quoted-tok-1\"\nexport DFQ_LLM_API_KEY='k-2'\nDFQ_SMTP_PASSWORD=\nPATH=x\n"
                  .encode("utf-8"))
    assert envfile.read(p) == {"DFQ_XTC_TOKEN": "quoted-tok-1", "DFQ_LLM_API_KEY": "k-2"}


def test_dotenv_autoload_respects_existing_env_and_redacts(tmp_path):
    from dfq import envfile
    from dfq.log import redact
    cfgp = tmp_path / "config.yaml"
    (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={TOKEN}\nDFQ_SMTP_PASSWORD=from-file-1\n", encoding="utf-8")
    env = {"DFQ_SMTP_PASSWORD": "from-system-2"}
    assert envfile.autoload(cfgp, env) == ["DFQ_XTC_TOKEN"]
    assert env["DFQ_XTC_TOKEN"] == TOKEN and env["DFQ_SMTP_PASSWORD"] == "from-system-2"   # 系统环境变量优先
    assert "from-file-1" not in redact("x from-file-1 y")                     # 文件里的值也会被打码
    assert envfile.autoload(cfgp, {"DFQ_DOTENV": "0"}) == []                  # 可关闭（测试就靠它）


def test_dotenv_is_gitignored_but_template_is_not():
    import subprocess
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        pytest.skip("不是 git 仓库")
    r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=root)
    assert r.returncode == 0                                                  # .env 被忽略
    r = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=root)
    assert r.returncode == 1                                                  # 模板可以提交
    template = (root / ".env.example").read_text(encoding="utf-8")
    from dfq import envfile
    assert envfile.read(root / ".env.example") == {}                          # 模板里没有任何值
    assert "DFQ_XTC_TOKEN=" in template


# ------------------------------------------------------------------ 位置、查询
def test_base_dir_frozen_vs_source(monkeypatch, tmp_path):
    assert G.default_base_dir() == Path.cwd()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "DFQ.exe"))
    assert G.is_frozen() and G.default_base_dir() == tmp_path.resolve()


def test_cmd_init_finds_example_config_when_frozen(tmp_path, monkeypatch):
    """打包后 config.example.yaml 被放在 exe 解压目录的根部；init 要能找到它。"""
    import dfq.cli as cli
    expected = Path(cli.__file__).resolve().parent.parent / "config.example.yaml"
    assert expected.exists()                                                  # 源码运行时
    # 打包脚本把它 --add-data 到 "."，即 _MEIPASS 根目录 = dfq 包的上一级，和源码布局一致
    bat = (Path(__file__).parent.parent / "scripts" / "build_exe.bat").read_text(encoding="ascii")
    # 源路径写成 %CD%\ 绝对路径（--specpath build 会让相对路径按 build\ 解析），目标仍是 "."
    assert '--add-data "%CD%\\config.example.yaml;."' in bat and "--collect-submodules dfq" in bat


def test_queries_for_dropdown_and_trade_table(tmp_path):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    cfg, err = G.load_config(cfgp)
    assert err is None and "12.7x55mm PS12B" in G.item_choices(cfg)
    assert G.recent_trades(cfg) == []                                         # 还没有数据库也不出错
    assert main(["trade", "buy", "PS12B", "100", "3600", "--paper", "--source", "gui", "--config", str(cfgp)]) == 0
    rows = G.recent_trades(cfg)
    assert rows[0]["source"] == "gui" and rows[0]["paper"] == 1 and rows[0]["qty"] == 100
    cfgp.write_text("risk:\n  lot_size: -1\n", encoding="utf-8")
    cfg2, err2 = G.load_config(cfgp)
    assert cfg2 is None and "risk.lot_size" in err2
    assert G.item_choices(None) == []


def test_trade_undo(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    base = ["--config", str(cfgp)]
    main(["balance", "set", "1000000", *base])
    main(["trade", "buy", "PS12B", "100", "3600", *base, "--at", "2099-01-01 00:00"])
    from dfq.config import load_config
    from dfq.db import DB
    cfg = load_config(cfgp)
    db = DB(cfg.data_dir / "dfq.db")
    tid = db.one("SELECT id FROM trades")["id"]
    assert ledger.positions(db)["12.7x55mm PS12B"]["qty"] == 100
    assert main(["trade-undo", str(tid), *base]) == 0
    assert "已撤销" in capsys.readouterr().out
    assert ledger.positions(db) == {} and ledger.balance(db)["amount"] == 1_000_000
    assert main(["trade-undo", str(tid), *base]) == 2                         # 不能撤销两次
    with pytest.raises(ledger.LedgerError):
        ledger.delete_trade(db, 0)
    db.close()
