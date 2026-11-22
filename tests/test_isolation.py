"""用户配置和个人数据的隔离（评审第 9 条）。

五个检查点：
  1. 首次启动只生成“空白”的密钥文件；
  2. 不会读到开发者电脑上的密钥（模板里没有值；程序不看当前目录 / 上级目录的 .env、config.yaml；打包时不带 .env）；
  3. 不会把开发者的数据带给新用户（安装包 / 打包目录里没有个人文件）；
  4. 升级不覆盖用户配置；
  5. 卸载不误删（保留清单与 dfq/layout.py 一致，data/ 下每个目录都有明确的归属）。
另外：系统里已有同名 DFQ_* 环境变量时，dfq status 要说清楚“这个密钥来自系统环境变量”（只说来源，不显示值）。
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from dfq import envfile, gui_core, layout
from dfq.log import SECRET_ENV_NAMES

ROOT = Path(__file__).resolve().parent.parent
DUMMY = "dummy-test-token-0000"
DUMMY2 = "dummy-test-token-9999"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _iss() -> str:
    return (ROOT / "installer" / "DFQ.iss").read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------ 1. 首次启动只生成空白密钥文件
def test_env_template_has_blank_lines_and_no_values():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert envfile.read(ROOT / ".env.example") == {}
    for line in text.splitlines():                                            # 模板里任何 DFQ_* 行都不能带值
        if re.match(r"\s*(export\s+)?DFQ_", line):
            assert re.fullmatch(r"\s*(export\s+)?DFQ_[A-Z_]+=\s*", line), line
    for name in ("DFQ_XTC_TOKEN", "DFQ_SMTP_PASSWORD", "DFQ_LLM_API_KEY"):       # 常用的几个给出空白占位
        assert name in SECRET_ENV_NAMES and re.search(rf"(?m)^{name}=\s*$", text), name


def test_first_start_env_is_blank_even_if_cwd_and_parent_have_secrets(tmp_path, monkeypatch):
    """开发者在别的目录（或上级目录）放着自己的 .env / config.yaml：新安装文件夹里生成的必须是模板，不是这些文件。"""
    app = tmp_path / "parent" / "app"
    app.mkdir(parents=True)
    (tmp_path / "parent" / ".env").write_text(f"DFQ_XTC_TOKEN={DUMMY}\n", encoding="utf-8")
    other = tmp_path / "devcwd"
    other.mkdir()
    (other / ".env").write_text(f"DFQ_XTC_TOKEN={DUMMY}\n", encoding="utf-8")
    (other / "config.yaml").write_text("mail:\n  to: someone-dev-marker\n", encoding="utf-8")
    monkeypatch.chdir(other)
    layout.bootstrap(app)
    assert envfile.read(app / ".env") == {}
    assert (app / "config.yaml").read_bytes() == (ROOT / "config.example.yaml").read_bytes()


def test_templates_contain_no_personal_information():
    scan = _load_script("scan_secrets")
    for name in (".env.example", "config.example.yaml"):
        found = scan.scan_bytes((ROOT / name).read_bytes(), name, "tree")
        assert not [f for f in found if f.category == scan.REAL], name


def test_resource_prefers_bundled_templates_over_current_directory(tmp_path, monkeypatch):
    """打包后模板从程序自己的 _internal 取；当前目录里的同名文件不会被当成模板。"""
    bundle = tmp_path / "app" / "_internal"
    bundle.mkdir(parents=True)
    (bundle / "config.example.yaml").write_text("# bundled\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "config.example.yaml").write_text("# from cwd\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "DFQ.exe"))
    assert layout.resource("config.example.yaml") == bundle / "config.example.yaml"


# ------------------------------------------------------------------ 2. 不读开发者电脑上的密钥
def test_dotenv_is_only_read_next_to_the_config_file(tmp_path, monkeypatch):
    app = tmp_path / "parent" / "app"
    app.mkdir(parents=True)
    (tmp_path / "parent" / ".env").write_text(f"DFQ_XTC_TOKEN={DUMMY}\n", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(f"DFQ_LLM_API_KEY={DUMMY}\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    environ: dict[str, str] = {}
    assert envfile.autoload(app / "config.yaml", environ) == []               # 上级目录、当前目录的 .env 都不读
    assert environ == {}
    (app / ".env").write_text(f"DFQ_XTC_TOKEN={DUMMY2}\n", encoding="utf-8")
    assert envfile.autoload(app / "config.yaml", environ) == ["DFQ_XTC_TOKEN"]
    assert environ == {"DFQ_XTC_TOKEN": DUMMY2}


def test_frozen_program_uses_its_own_folder_not_the_current_directory(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "DFQ.exe"))
    assert gui_core.default_base_dir() == app


def test_build_script_only_bundles_templates_and_program_files():
    bat = (ROOT / "scripts" / "build_exe.bat").read_text(encoding="ascii")
    sources = re.findall(r'--add-data "%CD%\\([^;"]+);', bat)
    assert sources and set(sources) <= {"config.example.yaml", r"dfq\webui\static", "dfq.ico", ".env.example"}, sources


# ------------------------------------------------------------------ 命令行模式（计划任务）：配置以 DFQ.exe 所在文件夹为准
@pytest.fixture
def entry():
    return _load_script("dfq_gui_entry")


@pytest.mark.parametrize("argv,want", [
    (["status"], ["status", "--config", "C"]),
    (["run", "--mock", "--offline"], ["run", "--config", "C", "--mock", "--offline"]),
    (["--now", "2026-11-13 21:30", "run"], ["--now", "2026-11-13 21:30", "run", "--config", "C"]),
    (["-v", "mail-poll"], ["-v", "mail-poll", "--config", "C"]),
    (["--help"], ["--help", "--config", "C"]),
    (["run", "--config", "X"], ["run", "--config", "X"]),
    (["run", "--config=X"], ["run", "--config=X"]),
])
def test_entry_puts_config_after_the_subcommand(entry, argv, want):
    """--config 放在子命令名前面会被子命令解析器的默认值（当前目录的 config.yaml）盖掉。"""
    assert entry.with_config(argv, "C") == want


def test_entry_cli_uses_install_folder_even_from_another_cwd(entry, tmp_path, monkeypatch):
    app, elsewhere = tmp_path / "app", tmp_path / "System32"
    app.mkdir()
    elsewhere.mkdir()
    (elsewhere / "config.yaml").write_text("# someone else's config\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "DFQ.exe"))
    seen = {}

    def fake_main(argv):
        seen["argv"], seen["cwd"] = argv, Path.cwd()
        return 0

    from dfq import cli
    monkeypatch.setattr(cli, "main", fake_main)
    assert entry._cli(["--now", "2026-11-13 21:30", "status"]) == 0
    i = seen["argv"].index("--config")
    assert Path(seen["argv"][i + 1]) == app / "config.yaml" and seen["argv"][i - 1] == "status"
    assert seen["cwd"] == app                                                  # 相对路径落在安装文件夹
    assert (app / "data" / "logs").is_dir() and not (elsewhere / "data").exists()
    assert not (app / "config.yaml").exists() and not (app / ".env").exists()  # 命令行模式不生成配置


# ------------------------------------------------------------------ 3/4. 安装包不带个人文件、升级不覆盖
def test_installer_excludes_personal_files_from_the_program_folder():
    src = _iss()
    m = re.search(r'#define PersonalExcludes "([^"]*)"', src)
    assert m
    excl = set(m.group(1).split(","))
    assert {r"\config.yaml", r"\.env", r"\data", "*.db"} <= excl
    dist_line = next(x for x in src.splitlines() if x.startswith('Source: "{#SourceDir}'))
    assert 'Excludes: "{#PersonalExcludes}"' in dist_line


def test_upgrade_only_replaces_program_files():
    src = _iss()
    section = src.split("[InstallDelete]", 1)[1].split("\n[", 1)[0]
    targets = re.findall(r'Name: "([^"]+)"', section)
    assert targets == [r"{app}\_internal"]                                    # 升级只先清掉旧的程序内部文件
    files = [x for x in src.splitlines() if x.startswith("Source:")]
    assert not any("onlyifdoesntexist" in x for x in files)                   # 没有“装一份默认配置”这种做法
    assert not any(re.search(r'DestName: "(config\.yaml|\.env)"', x) for x in files)


# ------------------------------------------------------------------ 5. 卸载不误删：data/ 下每样东西都有归属
def test_every_data_entry_has_exactly_one_uninstall_category():
    groups = {
        "user": set(layout.USER_DATA) | {layout.UI_SETTINGS},
        "login": set(layout.LOGIN_STATE_DIRS),
        "cache": set(layout.CACHE_DIRS),
        "logs": set(layout.LOG_DIRS),
        "tmp": {layout.TEMP_DIR},
        "demo": {layout.DEMO_DIR},
    }
    seen: dict[str, str] = {}
    for g, items in groups.items():
        for p in items:
            assert p not in seen, f"{p} 同时属于 {seen[p]} 和 {g}"
            seen[p] = g
    for d in layout.DIRS:                                                     # 首次启动建的目录都要有归属
        assert d == layout.DATA or d in seen, d
    assert f"{layout.DATA}/backups" in layout.KEEP_ON_UNINSTALL_DEFAULT       # backups/db（数据库备份）随 backups 保留
    assert f"{layout.DATA}/exports" not in layout.KEEP_ON_UNINSTALL_DEFAULT   # 导出的日志包只是日志副本
    assert not set(layout.SECRET_FILES) & set(layout.KEEP_ON_UNINSTALL_DEFAULT)


def test_uninstall_removes_only_own_scheduled_tasks():
    """计划任务名是全用户共用的：卸载 A 文件夹时不能删掉 B 文件夹创建的同名任务。"""
    body = _iss().split("procedure RemoveScheduledTasks();", 1)[1].split("\nend;", 1)[0]
    assert "schtasks" not in body.lower() or "/delete" not in body.lower()
    assert "StartsWith($pre" in body and "WorkingDirectory" in body and "Unregister-ScheduledTask" in body


# ------------------------------------------------------------------ 系统环境变量盖过 .env 时说清楚来源
@pytest.mark.parametrize("env_val,file_val,want", [
    (None, None, envfile.SOURCE_NONE),
    (None, DUMMY, envfile.SOURCE_DOTENV),
    (DUMMY, DUMMY, envfile.SOURCE_DOTENV),
    (DUMMY, None, envfile.SOURCE_SYSTEM),
    (DUMMY2, DUMMY, envfile.SOURCE_SYSTEM_OVERRIDES),
])
def test_secret_source(tmp_path, env_val, file_val, want):
    path = tmp_path / ".env"
    if file_val:
        path.write_text(f"DFQ_XTC_TOKEN={file_val}\n", encoding="utf-8")
    environ = {"DFQ_XTC_TOKEN": env_val} if env_val else {}
    assert envfile.source("DFQ_XTC_TOKEN", path, environ) == want
    assert envfile.sources(path, environ)["DFQ_XTC_TOKEN"] == want


def test_status_says_where_a_secret_comes_from_without_showing_it(tmp_path, monkeypatch, capsys):
    from dfq.cli import main
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"general:\n  data_dir: {(tmp_path / 'data').as_posix()}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={DUMMY}\nDFQ_LLM_API_KEY={DUMMY}\n", encoding="utf-8")
    monkeypatch.setenv("DFQ_XTC_TOKEN", DUMMY2)                               # 系统环境变量，值与 .env 不同
    assert main(["status", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    xtc = next(x for x in out.splitlines() if "DFQ_XTC_TOKEN" in x)
    llm = next(x for x in out.splitlines() if "DFQ_LLM_API_KEY" in x)
    assert envfile.SOURCE_TEXT[envfile.SOURCE_SYSTEM_OVERRIDES] in xtc
    assert envfile.SOURCE_TEXT[envfile.SOURCE_DOTENV] in llm
    assert DUMMY not in out and DUMMY2 not in out
