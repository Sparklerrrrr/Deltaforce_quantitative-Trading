"""极简 HTTP 封装（只用标准库）：超时、有限重试、错误信息脱敏。

为什么不直接抛 urllib 的异常：它的异常对象里带着完整 URL（含 token）。
这里统一转成 HttpError，消息里只保留“主机+路径”，并用 `from None` 切断异常链。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .log import redact, redact_url

USER_AGENT = "dfq/1.0 (personal trading planner)"


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(redact(message))
        self.status = status
        self.retryable = retryable


Transport = Callable[[str, str, dict | None, bytes | None, dict, float], tuple[int, bytes]]


def urllib_transport(method: str, url: str, params: dict | None, body: bytes | None,
                     headers: dict, timeout: float) -> tuple[int, bytes]:
    full = url
    if params:
        full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        payload = b""
        try:
            payload = e.read() or b""
        except Exception:
            pass
        return e.code, payload
    except urllib.error.URLError as e:
        raise HttpError(f"网络错误 {redact_url(url)}: {e.reason}", retryable=True) from None
    except (TimeoutError, OSError) as e:
        raise HttpError(f"网络错误 {redact_url(url)}: {type(e).__name__}", retryable=True) from None


def request_json(method: str, url: str, *, params: dict | None = None, json_body: Any = None,
                 headers: dict | None = None, timeout: float = 15, retries: int = 2,
                 backoff: float = 1.5, transport: Transport | None = None,
                 on_attempt: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> Any:
    """发请求并解析 JSON。只对网络错误和 5xx/429 重试，最多 retries 次。

    on_attempt：每次真正发出请求前回调（用于按次计费的接口记账/查额度；它抛异常则中止）。
    """
    transport = transport or urllib_transport
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})

    last: HttpError | None = None
    for attempt in range(retries + 1):
        if on_attempt:
            on_attempt()
        try:
            status, raw = transport(method, url, params, body, hdrs, timeout)
        except HttpError as e:
            last = e
        else:
            if 200 <= status < 300:
                try:
                    return json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    raise HttpError(f"响应不是合法 JSON {redact_url(url)}", status=status) from None
            snippet = raw[:200].decode("utf-8", errors="replace")
            last = HttpError(f"HTTP {status} {redact_url(url)}: {snippet}", status=status,
                             retryable=status >= 500 or status == 429)
        if not last.retryable or attempt >= retries:
            break
        sleep(backoff * (2 ** attempt))
    assert last is not None
    raise last
