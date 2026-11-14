"""结构化日志 + 脱敏。

硬性要求：小涛查的 token 放在 URL 参数里，所以任何日志都不允许出现完整请求 URL。
这里做两层防护：
1. `redact()`：把文本里的敏感参数（token=xxx、api_key=xxx、Bearer xxx …）和
   已登记的密钥原文全部替换成 ***；
2. `RedactFilter`：挂在所有 handler 上，日志落盘/打印前统一过一遍，
   即使调用方忘了脱敏也漏不出去。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 存放密钥的环境变量名。值会被登记进脱敏表。
SECRET_ENV_NAMES = (
    "DFQ_XTC_TOKEN",
    "DFQ_LLM_API_KEY",
    "DFQ_SMTP_PASSWORD",
    "DFQ_IMAP_PASSWORD",
    "DFQ_TAVILY_API_KEY",
    "DFQ_BILI_COOKIE",
    "DFQ_DF_COOKIE",
)

_MASK = "***"
_secrets: set[str] = set()

# URL / 表单 / JSON 里常见的敏感键
_SENSITIVE_KEYS = (
    r"token|access_token|accesstoken|api_key|apikey|key|secret|password|passwd|pwd|"
    r"authorization|cookie|sessdata|openid|sign"
)
_RE_QUERY = re.compile(rf"(?i)\b({_SENSITIVE_KEYS})=([^&\s\"'<>]+)")
_RE_JSON = re.compile(rf"(?i)([\"']({_SENSITIVE_KEYS})[\"']\s*:\s*[\"'])([^\"']+)([\"'])")
_RE_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")


def register_secret(value: str | None) -> None:
    """登记一个密钥原文，之后任何日志里出现它都会被打码。"""
    if value and len(value) >= 4:
        _secrets.add(value)


def register_env_secrets() -> None:
    for name in SECRET_ENV_NAMES:
        register_secret(os.environ.get(name))


def redact(text: Any) -> str:
    """把任意对象转成字符串并脱敏。"""
    s = text if isinstance(text, str) else str(text)
    for sec in sorted(_secrets, key=len, reverse=True):
        if sec in s:
            s = s.replace(sec, _MASK)
    s = _RE_QUERY.sub(lambda m: f"{m.group(1)}={_MASK}", s)
    s = _RE_JSON.sub(lambda m: f"{m.group(1)}{_MASK}{m.group(4)}", s)
    s = _RE_BEARER.sub(lambda m: f"{m.group(1)} {_MASK}", s)
    return s


def redact_url(url: str) -> str:
    """只保留 协议+主机+路径，查询串整体丢弃（最保守的做法）。"""
    base = url.split("?", 1)[0].split("#", 1)[0]
    base = re.sub(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@]*@", r"\1", base)   # 去掉 用户名:密码@
    return redact(base)


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if re.fullmatch(rf"(?i)({_SENSITIVE_KEYS})", str(k)):
                out[k] = _MASK
            else:
                out[k] = _redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # 格式化失败也不能让日志崩
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = _redact_obj(fields)
        if record.exc_info:
            # 异常文本也可能带 URL：提前格式化并脱敏
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        row = {
            "ts": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            row.update(fields)
        if record.exc_text:
            row["exc"] = record.exc_text
        return json.dumps(row, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        fields = getattr(record, "fields", None) or {}
        node = fields.get("node")
        prefix = f"[{node}] " if node else ""
        extra = " ".join(f"{k}={v}" for k, v in fields.items() if k not in ("node", "event"))
        line = f"{ts} {record.levelname:<7} {prefix}{record.getMessage()}"
        if extra:
            line += f"  ({extra})"
        if record.exc_text:
            line += "\n" + record.exc_text
        return line


def setup_logging(log_dir: str | Path | None = None, verbose: bool = False) -> None:
    register_env_secrets()
    root = logging.getLogger("dfq")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False
    filt = RedactFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(ConsoleFormatter())
    console.addFilter(filt)
    root.addHandler(console)

    if log_dir:
        p = Path(log_dir)
        p.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p / f"dfq-{datetime.now():%Y%m%d}.jsonl", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JsonLineFormatter())
        fh.addFilter(filt)
        root.addHandler(fh)


def get_logger(name: str = "dfq") -> logging.Logger:
    return logging.getLogger(name if name.startswith("dfq") else f"dfq.{name}")


def event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """写一条结构化日志：msg 给人看，fields 给机器查。"""
    logger.log(level, msg, extra={"fields": fields})
