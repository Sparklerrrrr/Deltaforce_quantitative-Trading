"""敏感信息扫描（只用标准库）。发布前、CI 里和打包后都跑一遍。

  python scripts/scan_secrets.py --tree              Git 跟踪的文件（= 推送到 GitHub 的内容）
  python scripts/scan_secrets.py --history           Git 历史里所有引用（分支、标签）能到达的全部文件版本
  python scripts/scan_secrets.py --dir dist/DFQ      打包目录 / 安装目录：内容 + 按文件名找个人文件
  三种可以一起用。--json 结果.json 另存一份机器可读的结果（同样已打码）。

为什么这样设计：
  - 输出一律打码（前 4 个字符 + ***，再附一个 sha256 指纹用于登记允许清单），扫描报告本身可以放心贴出来；
  - 每个命中都分成“疑似真实 / 占位符 / 测试假值 / 误报”四类，只有“疑似真实”会让退出码变成 1，
    这样 CI 不会被文档里的 your-token 之类的写法刷红，而真正的密钥一定会让它失败；
  - 确认无害但规则认不出来的，登记进 scripts/scan_allowlist.txt（按规则 + 路径 + 指纹），不在代码里开后门。
  - 只读：不改文件、不改 Git 历史。发现历史泄露时只报告位置和类型——处理办法是作废那个密钥，而不是改写历史。

局限（如实说明）：
  - 按“形状”找密钥，认不出没有固定前缀、又没写在“xxx_token = ...”这种位置的密钥；
  - 二进制文件（exe/dll/pyd）只查前缀很明确的几类密钥；PyInstaller 把 .pyc 压缩进归档，压缩包内部不查——
    打包内容来自 --tree 已扫描过的源码，所以发布流程要求从干净的检出构建。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = ROOT / "scripts" / "scan_allowlist.txt"

REAL, PLACEHOLDER, TEST, FALSE_POSITIVE = "疑似真实", "占位符", "测试假值", "误报"
CATEGORIES = (REAL, PLACEHOLDER, TEST, FALSE_POSITIVE)
MAX_TEXT = 5 * 1024 * 1024          # 超过 5 MB 的文本不扫（仓库里没有这么大的源码）
MAX_BINARY = 200 * 1024 * 1024


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    binary: bool = False             # 二进制文件里也查（只给前缀明确、几乎不会误报的规则）


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p)


# 值放在命名组 v 里；没有 v 组的规则以整个命中为值
RULES: tuple[Rule, ...] = (
    Rule("private_key", _r(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"
                           r"\s*(?P<v>[A-Za-z0-9+/=\s]{0,64})"), binary=True),
    Rule("openai_style_key", _r(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"), binary=True),
    Rule("tavily_key", _r(r"\btvly-[A-Za-z0-9_\-]{16,}"), binary=True),
    Rule("github_token", _r(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), binary=True),
    Rule("aws_access_key", _r(r"\bAKIA[0-9A-Z]{16}\b"), binary=True),
    Rule("google_api_key", _r(r"\bAIza[0-9A-Za-z_\-]{35}\b"), binary=True),
    Rule("slack_token", _r(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), binary=True),
    Rule("jwt", _r(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), binary=True),
    Rule("db_url", _r(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|mssql|amqp)://"
                      r"[^\s:@/'\"]+:(?P<v>[^\s@/'\"]+)@")),
    Rule("cookie", _r(r"(?i)\b(?:SESSDATA|bili_jct|DedeUserID|PHPSESSID|JSESSIONID|sessionid|session_id|csrftoken)"
                      r"\s*[=:]\s*[\"']?(?P<v>[^\s\"';,]{6,})")),
    Rule("auth_header", _r(r"(?i)\bauthorization\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+(?P<v>[A-Za-z0-9._~+/=\-]{12,})")),
    Rule("secret_assignment", _r(r"(?i)\b[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|AUTH_?CODE|COOKIE)"
                                 r"[A-Z0-9_]*[\"']?\s*[:=]\s*[\"']?(?P<v>[A-Za-z0-9_\-+/=.~!@^*]{8,})")),
    Rule("mail_auth_code", _r(r"授权码[^\n]{0,20}?[:：=]\s*(?P<v>[a-z]{16})\b")),
    Rule("email", _r(r"(?<![\w.+\-])(?P<v>[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})\b")),
    Rule("user_path", _r(r"(?i)(?:\b[A-Z]:[\\/]+Users[\\/]+|(?<![\w.])/home/|(?<![\w.])/Users/)(?P<v>[^\\/\s\"'<>|:*?`)]+)")),
)

# 一眼能看出是假的：测试、示例、打码后的值
_FAKE = re.compile(r"(?i)dummy|test|fake|example|sample|mock|placeholder|redact|changeme|xxxx|\*\*\*|0000|1234|0123|abcd|"
                   r"not[-_]?real|假|示例|测试")
# 占位写法：<你的token>、${VAR}、%VAR%、{{x}}、your-...、你的...
_PLACEHOLDER = re.compile(r"(?i)^(?:<.*>|\$\{.*\}|\$env:.*|%[^%]+%|\{\{.*\}\}|your[-_ ]?.*|my[-_ ].*|你的.*"
                          r"|\.\.\.|…+|x+|\*+|none|null)$")
_PLACEHOLDER_USERS = {"you", "yourname", "username", "user", "public", "default", "runneradmin", "runner", "name",
                      "admin", "administrator", "me", "xxx", "用户名", "你的用户名", "someone", "foo", "bob", "alice"}
_SAFE_MAIL_DOMAINS = ("example.com", "example.org", "example.net", "localhost", "test", "invalid", "example", "local",
                      "users.noreply.github.com", "anthropic.com", "evil.com")
_PLACEHOLDER_MAIL_USERS = {"you", "your", "user", "me", "someone", "name", "xxx", "foo", "bar", "alice", "bob",
                           "noreply", "no-reply", "your_name", "yourname", "dfq", "attacker", "victim"}
# 公共邮箱服务：这些域名下的地址几乎一定属于某个真实的人
_PUBLIC_MAIL = ("qq.com", "foxmail.com", "163.com", "126.com", "yeah.net", "sina.com", "sina.cn", "sohu.com", "aliyun.com",
                "139.com", "gmail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
                "me.com", "proton.me", "protonmail.com")
_TEXT_EXT = {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".bat", ".cmd", ".ps1", ".iss",
             ".isl", ".html", ".htm", ".js", ".css", ".xml", ".csv", ".log", ".eml", ".env", ".example", ".gitignore",
             ".gitattributes", ".sql", ".sh"}

# 打包 / 安装目录里不应该出现的个人文件（按名字找；data/ 下的东西都是某个人的运行数据）
PERSONAL_NAMES = (".env", "config.yaml", "config.local.yaml", "ui-settings.json", "overrides.yaml", "Cookies",
                  "Login Data", "Web Data", "History")
PERSONAL_GLOBS = ("*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.eml", "*.log", ".env.*", "*.env",
                  "task-*.log", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*")
PERSONAL_DIRS = ("data", "moligod_profile", "browser_profile", "EBWebView", "manual_notes")
PERSONAL_OK = (".env.example", "config.example.yaml", "manual_notes/README.md")


@dataclass
class Finding:
    scope: str           # tree / history / dir
    path: str
    line: int
    rule: str
    masked: str
    fingerprint: str
    category: str
    note: str = ""


def mask(value: str) -> str:
    return (value[:4] + "***") if value else "***"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _looks_like_code(value: str) -> bool:
    """secret_assignment 命中的是不是代码 / 配置里的引用，而不是一个值。"""
    if any(c in value for c in "()[]{}$%<>"):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", value):                     # 环境变量名，例如 DFQ_SMTP_PASSWORD
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value) and not re.search(r"\d", value):
        return True                                                  # 变量名 / 属性：token、self.token、cfg.mail.password
    return False


def _looks_random(value: str) -> bool:
    """像不像随机生成的密钥：有一段 >= 8 个字符、字母数字混排的，或者 >= 6 位的数字串 / >= 12 位的十六进制串。
    “already-here-123”“from-file-1”这种由单词拼起来的可读值不算。"""
    for chunk in re.split(r"[-_.:/+=~!@^*]+", value):
        if len(chunk) >= 8 and re.search(r"[A-Za-z]", chunk) and re.search(r"\d", chunk):
            return True
        if re.fullmatch(r"\d{6,}", chunk) or re.fullmatch(r"[0-9a-fA-F]{12,}", chunk):
            return True
        if len(chunk) >= 20:                                         # 很长的一段（base64 之类）
            return True
    return False


def _in_tests(path: str) -> bool:
    return path.replace("\\", "/").startswith("tests/") or "/tests/" in path.replace("\\", "/")


def classify(rule: str, value: str, path: str) -> tuple[str, str]:
    """把一个命中分类。返回 (分类, 说明)。"""
    v = value.strip()
    low = v.lower()
    if not v or _PLACEHOLDER.match(v):
        return PLACEHOLDER, "占位写法"
    if rule == "user_path":
        if low in _PLACEHOLDER_USERS or any(c in v for c in "<>%${}…") or low.startswith("<"):
            return PLACEHOLDER, "示例用户名"
        return REAL, "本机用户目录（暴露用户名）"
    if rule == "email":
        local, _, domain = low.partition("@")
        if domain in _SAFE_MAIL_DOMAINS or any(domain.endswith("." + d) for d in _SAFE_MAIL_DOMAINS):
            return FALSE_POSITIVE, "保留域名 / 示例域名 / 公开的无回复地址"
        if local in _PLACEHOLDER_MAIL_USERS or local.startswith("your"):
            return PLACEHOLDER, "示例邮箱"
        if _FAKE.search(v):
            return TEST, "测试邮箱"
        if _in_tests(path) and domain not in _PUBLIC_MAIL:
            return TEST, "测试里的地址，不是公共邮箱域名"
        return REAL, "邮箱地址"
    if rule == "secret_assignment" and _looks_like_code(v):
        return FALSE_POSITIVE, "是变量名 / 表达式，不是值"
    if _FAKE.search(v):
        return TEST, "带 dummy/test/example 等字样"
    if rule == "secret_assignment" and (len(v) < 12 or not _looks_random(v)):
        return (TEST if _in_tests(path) else FALSE_POSITIVE), "可读的单词组合，不像随机生成的密钥"
    return REAL, ""


# ------------------------------------------------------------------ 允许清单
@dataclass(frozen=True)
class Allow:
    rule: str
    path: str
    fp: str
    category: str
    reason: str

    def matches(self, f: Finding) -> bool:
        path = self.path
        scope, sep, rest = path.partition(":")
        if sep and scope in ("tree", "history", "dir"):              # history:tests/x.py = 只对历史生效
            if scope != f.scope:
                return False
            path = rest
        return ((self.rule == "*" or self.rule == f.rule) and fnmatch.fnmatchcase(f.path, path)
                and (self.fp == "*" or self.fp == f.fingerprint))


def load_allowlist(path: Path | None) -> list[Allow]:
    """格式：规则 | 路径（可用 * 通配）| 指纹（或 *）| 分类（占位符/测试假值/误报）| 理由。# 开头是注释。"""
    out: list[Allow] = []
    if path is None or not path.is_file():
        return out
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) != 5 or parts[3] not in (PLACEHOLDER, TEST, FALSE_POSITIVE) or not parts[4]:
            raise ValueError(f"{path.name} 第 {n} 行格式不对：应为 规则 | 路径 | 指纹 | 分类 | 理由"
                             "（分类不能是“疑似真实”，理由必填）")
        out.append(Allow(*parts))
    return out


def apply_allowlist(findings: list[Finding], allow: list[Allow]) -> None:
    for f in findings:
        for a in allow:
            if a.matches(f):
                f.category, f.note = a.category, f"允许清单：{a.reason}"
                break


# ------------------------------------------------------------------ 扫描内容
def is_binary(data: bytes, path: str = "") -> bool:
    if Path(path).suffix.lower() in _TEXT_EXT or Path(path).name.startswith(".env"):
        return b"\0" in data[:8000] and not data.startswith((b"\xff\xfe", b"\xfe\xff"))
    return b"\0" in data[:8000]


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8", "replace")


def scan_bytes(data: bytes, path: str, scope: str) -> list[Finding]:
    binary = is_binary(data, path)
    if len(data) > (MAX_BINARY if binary else MAX_TEXT):
        return []
    text = data.decode("latin-1") if binary else _decode(data)
    out: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()
    for rule in RULES:
        if binary and not rule.binary:
            continue
        for m in rule.pattern.finditer(text):
            value = m.group("v") if "v" in rule.pattern.groupindex else m.group(0)
            value = (value or "").strip()
            line = text.count("\n", 0, m.start()) + 1
            key = (rule.name, line, value)
            if key in seen:
                continue
            seen.add(key)
            cat, note = classify(rule.name, value, path)
            out.append(Finding(scope, path, line, rule.name, mask(value), fingerprint(value), cat, note))
    return out


def personal_file(rel: str) -> str | None:
    """按路径判断是不是个人文件；是的话返回原因。rel 用 / 分隔。"""
    parts = rel.split("/")
    name = parts[-1]
    if name in PERSONAL_OK or rel in PERSONAL_OK:
        return None
    for d in parts[:-1]:
        if d in PERSONAL_DIRS:
            return f"在 {d}/ 目录里（运行数据 / 登录状态）"
    if name in PERSONAL_NAMES:
        return "个人配置 / 密钥 / 浏览器数据文件"
    for g in PERSONAL_GLOBS:
        if fnmatch.fnmatchcase(name, g):
            return f"文件类型 {g}（数据库、邮件、日志或密钥）"
    return None


def _personal_finding(scope: str, rel: str, why: str) -> Finding:
    return Finding(scope, rel, 0, "personal_file", mask(Path(rel).name), fingerprint(rel), REAL, why)


# ------------------------------------------------------------------ 三种来源
def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args], input=data,
                       capture_output=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return r.stdout


def scan_tree(repo: Path) -> list[Finding]:
    out: list[Finding] = []
    for rel in _git(repo, "ls-files", "-z").decode("utf-8", "replace").split("\0"):
        if not rel:
            continue
        p = repo / rel
        if p.is_symlink() or not p.is_file():
            continue
        why = personal_file(rel)
        if why:
            out.append(_personal_finding("tree", rel, why))
        try:
            out += scan_bytes(p.read_bytes(), rel, "tree")
        except OSError:
            continue
    return out


def _cat_blobs(repo: Path, shas: list[str]):
    """逐个读出 blob 内容（一次 git cat-file --batch 调用）。"""
    if not shas:
        return
    raw = _git(repo, "cat-file", "--batch", data=("\n".join(shas) + "\n").encode())
    pos = 0
    for _ in shas:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode().split()
        size = int(header[2])
        yield header[0], raw[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1


def scan_history(repo: Path) -> tuple[list[Finding], dict[str, object]]:
    """所有引用可达的全部 blob。同一个 blob 只扫一次，报告时附上它出现过的路径。"""
    paths: dict[str, list[str]] = {}
    for line in _git(repo, "rev-list", "--all", "--objects").decode("utf-8", "replace").splitlines():
        sha, _, rel = line.partition(" ")
        if rel:
            paths.setdefault(sha, [])
            if rel not in paths[sha]:
                paths[sha].append(rel)
    shas = list(paths)
    check = _git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", data=("\n".join(shas) + "\n").encode())
    blobs = [s for s, t in (ln.split() for ln in check.decode().splitlines() if ln) if t == "blob"]
    commits = _git(repo, "rev-list", "--all", "--count").decode().strip()
    out: list[Finding] = []
    flagged_paths: set[str] = set()
    for sha, data in _cat_blobs(repo, blobs):
        rels = paths.get(sha) or ["?"]
        for rel in rels:
            why = personal_file(rel)
            if why and rel not in flagged_paths:
                flagged_paths.add(rel)
                out.append(_personal_finding("history", rel, why + "（曾被提交）"))
        for f in scan_bytes(data, rels[0], "history"):
            more = f"，也出现在 {len(rels) - 1} 个其他路径" if len(rels) > 1 else ""
            f.note = (f.note + "；" if f.note else "") + f"blob {sha[:10]}" + more
            out.append(f)
    return out, {"commits": int(commits or 0), "blobs": len(blobs), "author_mails": author_mails(repo)}


def author_mails(repo: Path) -> dict[str, int]:
    """提交作者 / 提交者邮箱（提交元数据，推送后公开可见）。只统计、打码，不影响退出码：
    改它只能改写历史，这里只提醒以后改用 GitHub 的 noreply 地址。"""
    raw = _git(repo, "log", "--all", "--format=%ae%n%ce").decode("utf-8", "replace")
    out: dict[str, int] = {}
    for mail in raw.split():
        cat, _ = classify("email", mail, "")
        key = f"{mask(mail)}（{'个人邮箱' if cat == REAL else '无回复 / 示例地址'}）"
        out[key] = out.get(key, 0) + 1
    return out


def scan_dir(base: Path) -> list[Finding]:
    out: list[Finding] = []
    base = base.resolve()
    for root, dirs, files in os.walk(base, followlinks=False):
        rel_root = Path(root).relative_to(base).as_posix()
        for d in list(dirs):
            rel = d if rel_root == "." else f"{rel_root}/{d}"
            if d in PERSONAL_DIRS:
                out.append(_personal_finding("dir", rel + "/", "运行数据 / 登录状态目录"))
                dirs.remove(d)                                   # 里面都是个人数据，不读内容
        for fn in files:
            rel = fn if rel_root == "." else f"{rel_root}/{fn}"
            why = personal_file(rel)
            if why:
                out.append(_personal_finding("dir", rel, why))
                continue                                         # 个人文件只报告名字，不读内容
            p = Path(root) / fn
            try:
                if p.is_symlink() or p.stat().st_size > MAX_BINARY:
                    continue
                out += scan_bytes(p.read_bytes(), rel, "dir")
            except OSError:
                continue
    return out


# ------------------------------------------------------------------ 命令行
def summarize(findings: list[Finding]) -> dict[str, int]:
    return {c: sum(1 for f in findings if f.category == c) for c in CATEGORIES}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描密钥、个人信息和个人文件（输出已打码）")
    ap.add_argument("--tree", action="store_true", help="扫描 Git 跟踪的文件")
    ap.add_argument("--history", action="store_true", help="扫描 Git 历史里所有引用可达的文件版本")
    ap.add_argument("--dir", action="append", default=[], help="扫描一个打包目录 / 安装目录（可重复）")
    ap.add_argument("--repo", default=str(ROOT), help="仓库位置（默认本脚本所在的仓库）")
    ap.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST), help="允许清单文件")
    ap.add_argument("--json", help="另存一份 JSON 结果（已打码）")
    ap.add_argument("--all", action="store_true", help="也列出“误报”类")
    args = ap.parse_args(argv)
    if not (args.tree or args.history or args.dir):
        ap.error("至少指定 --tree、--history、--dir 之一")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    repo = Path(args.repo).resolve()
    try:
        allow = load_allowlist(Path(args.allowlist) if args.allowlist else None)
    except ValueError as e:
        print(f"允许清单有错：{e}", file=sys.stderr)
        return 2
    findings: list[Finding] = []
    stats: dict[str, object] = {}
    try:
        if args.tree:
            findings += scan_tree(repo)
        if args.history:
            hist, stats["history"] = scan_history(repo)
            findings += hist
    except (RuntimeError, OSError) as e:
        print(f"读取 Git 失败：{e}", file=sys.stderr)
        return 2
    for d in args.dir:
        if not Path(d).is_dir():
            print(f"目录不存在：{d}", file=sys.stderr)
            return 2
        findings += scan_dir(Path(d))
    apply_allowlist(findings, allow)
    counts = summarize(findings)
    shown = [f for f in findings if args.all or f.category != FALSE_POSITIVE]
    order = {c: i for i, c in enumerate(CATEGORIES)}
    for f in sorted(shown, key=lambda f: (order[f.category], f.scope, f.path, f.line)):
        where = f"{f.path}:{f.line}" if f.line else f.path
        note = f"  （{f.note}）" if f.note else ""
        print(f"[{f.category}] {f.scope:<7} {where}  {f.rule}  {f.masked}  指纹 {f.fingerprint}{note}")
    scopes = [s for s, on in (("tree", args.tree), ("history", args.history)) if on] + [f"dir:{d}" for d in args.dir]
    print(f"\n扫描范围：{', '.join(scopes)}")
    hist_stats = stats.get("history")
    if isinstance(hist_stats, dict):
        print(f"Git 历史：{hist_stats['commits']} 个提交（所有分支和标签），{hist_stats['blobs']} 个不同的文件版本")
        mails = "，".join(f"{k} × {n}" for k, n in hist_stats["author_mails"].items())
        print(f"提交作者 / 提交者邮箱（提交元数据，推送后公开可见；只提示，不计入结果）：{mails}")
    print("结果：" + "，".join(f"{c} {n}" for c, n in counts.items()))
    if args.json:
        Path(args.json).write_text(json.dumps({"scopes": scopes, "stats": stats, "counts": counts,
                                               "findings": [asdict(f) for f in findings]}, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    if counts[REAL]:
        print("发现“疑似真实”的密钥或个人信息：请确认。真的泄露了就立刻到对应平台作废 / 重新生成，再从文件里删掉；"
              "确认是假值的，改成 dummy/test 写法或登记进允许清单。不要改写 Git 历史。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
