"""scripts/scan_secrets.py：敏感信息扫描（评审第 12 条）。

“长得像真密钥”的值都在运行时拼出来（"sk-" + ...），这样本文件自己不会被 --tree 扫成“疑似真实”。
"""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("scan_secrets", ROOT / "scripts" / "scan_secrets.py")
S = importlib.util.module_from_spec(_spec)
sys.modules["scan_secrets"] = S                         # dataclass 需要能在 sys.modules 里找到自己的模块
_spec.loader.exec_module(S)

REALISH_KEY = "sk-" + "Q7rVk2Lp9" * 3                  # 形状像真 key，又不带 dummy/test 字样
REALISH_TOKEN = "tk_" + "9q8w7e6r5t4y3u2i"


def _run(capsys, *argv):
    rc = S.main([*argv, "--allowlist", ""])
    return rc, capsys.readouterr().out


# ------------------------------------------------------------------ 分类
@pytest.mark.parametrize("rule,value,path,want", [
    ("openai_style_key", "sk-dummy-test-key-0000000000", "x.py", S.TEST),
    ("secret_assignment", "dummy-test-token-0000", "tests/t.py", S.TEST),
    ("secret_assignment", "<你的token>", "README.md", S.PLACEHOLDER),
    ("secret_assignment", "your-token-here", "README.md", S.PLACEHOLDER),
    ("secret_assignment", "DFQ_SMTP_PASSWORD", "dfq/x.py", S.FALSE_POSITIVE),
    ("secret_assignment", "cfg.mail.password", "dfq/x.py", S.FALSE_POSITIVE),
    ("email", "you@example.com", "README.md", S.FALSE_POSITIVE),
    ("email", "someone" + "@qq.com", "README.md", S.PLACEHOLDER),
    ("email", "zhang.san" + "@qq.com", "docs/x.md", S.REAL),
    ("user_path", "<用户名>", "docs/x.md", S.PLACEHOLDER),
    ("user_path", "zhangsan", "docs/x.md", S.REAL),
])
def test_classify(rule, value, path, want):
    assert S.classify(rule, value, path)[0] == want


def test_realish_key_and_token_are_real():
    found = S.scan_bytes(f'KEY = "{REALISH_KEY}"\nDFQ_XTC_TOKEN={REALISH_TOKEN}\n'.encode(), "dfq/x.py", "tree")
    assert {f.rule for f in found if f.category == S.REAL} >= {"openai_style_key", "secret_assignment"}


def test_output_is_masked_and_exit_code_signals_real(tmp_path, capsys):
    (tmp_path / "app.py").write_text(f'KEY = "{REALISH_KEY}"\n', encoding="utf-8")
    rc, out = _run(capsys, "--dir", str(tmp_path))
    assert rc == 1
    assert REALISH_KEY not in out and REALISH_KEY[:8] not in out        # 只露前 4 个字符
    assert "sk-Q***" in out


def test_only_fake_values_exit_zero(tmp_path, capsys):
    (tmp_path / "app.py").write_text('TOKEN = "dummy-test-token-0000"\nKEY = "<your-key>"\n', encoding="utf-8")
    rc, _ = _run(capsys, "--dir", str(tmp_path))
    assert rc == 0


# ------------------------------------------------------------------ 打包 / 安装目录里的个人文件
def test_dir_scan_flags_personal_files_without_reading_them(tmp_path, capsys):
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "config.example.yaml").write_text("general: {}\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("DFQ_XTC_TOKEN=\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"DFQ_XTC_TOKEN={REALISH_TOKEN}\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("general: {}\n", encoding="utf-8")
    (tmp_path / "data" / "logs").mkdir(parents=True)
    (tmp_path / "data" / "dfq.db").write_bytes(b"SQLite format 3\0")
    found = S.scan_dir(tmp_path)
    flagged = {f.path for f in found if f.rule == "personal_file"}
    assert flagged == {".env", "config.yaml", "data/"}
    assert not [f for f in found if f.rule != "personal_file"]           # .env 只报名字，不读内容
    rc, out = _run(capsys, "--dir", str(tmp_path))
    assert rc == 1 and REALISH_TOKEN[:6] not in out


def test_clean_program_folder_passes(tmp_path):
    (tmp_path / "_internal").mkdir()
    (tmp_path / "DFQ.exe").write_bytes(b"MZ\0\0 program bytes")
    (tmp_path / ".env.example").write_text("DFQ_XTC_TOKEN=\n", encoding="utf-8")
    (tmp_path / "manual_notes").mkdir()
    assert [f.path for f in S.scan_dir(tmp_path) if f.category == S.REAL] == ["manual_notes/"]
    shutil.rmtree(tmp_path / "manual_notes")
    assert not [f for f in S.scan_dir(tmp_path) if f.category == S.REAL]


def test_binary_files_only_checked_for_unambiguous_prefixes(tmp_path):
    blob = b"\0\x01MZ" + f"password={REALISH_TOKEN} {REALISH_KEY}".encode() + b"\0"
    found = S.scan_bytes(blob, "_internal/x.dll", "dir")
    assert {f.rule for f in found} == {"openai_style_key"}


# ------------------------------------------------------------------ 允许清单
def test_allowlist_rejects_real_category_and_missing_reason(tmp_path):
    bad = tmp_path / "a.txt"
    bad.write_text("email | x.md | * | 疑似真实 | 不允许\n", encoding="utf-8")
    with pytest.raises(ValueError):
        S.load_allowlist(bad)
    bad.write_text("email | x.md | * | 误报 |\n", encoding="utf-8")
    with pytest.raises(ValueError):
        S.load_allowlist(bad)


def test_allowlist_scope_prefix_only_applies_to_that_scan():
    allow = [S.Allow("secret_assignment", "history:tests/t.py", "abc", S.TEST, "旧假值")]
    hist = S.Finding("history", "tests/t.py", 1, "secret_assignment", "x***", "abc", S.REAL)
    tree = S.Finding("tree", "tests/t.py", 1, "secret_assignment", "x***", "abc", S.REAL)
    S.apply_allowlist([hist, tree], allow)
    assert hist.category == S.TEST and tree.category == S.REAL


def test_repo_allowlist_is_valid():
    entries = S.load_allowlist(S.DEFAULT_ALLOWLIST)
    assert entries and all(e.fp != "*" for e in entries)                  # 每条都钉在具体的值上


# ------------------------------------------------------------------ Git 历史
@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_history_finds_secret_deleted_from_tree(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "-c", "commit.gpgsign=false", *a], check=True, capture_output=True)

    git("init", "-q")
    (repo / "app.py").write_text(f'KEY = "{REALISH_KEY}"\n', encoding="utf-8")
    git("add", "app.py")
    git("commit", "-q", "-m", "add")
    (repo / "app.py").write_text('KEY = ""\n', encoding="utf-8")
    git("commit", "-q", "-am", "remove")
    rc_tree, _ = _run(capsys, "--tree", "--repo", str(repo))
    rc_hist, out = _run(capsys, "--history", "--repo", str(repo))
    assert rc_tree == 0 and rc_hist == 1
    assert "app.py" in out and "openai_style_key" in out and REALISH_KEY not in out
    assert "2 个提交" in out
