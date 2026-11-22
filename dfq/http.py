"""极简 HTTP 封装（只用标准库）：超时、有限重试、错误信息脱敏。

为什么不直接抛 urllib 的异常：它的异常对象里带着完整 URL（含 token）。
这里统一转成 HttpError，消息里只保留“主机+路径”，并用 `from None` 切断异常链。
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .log import redact, redact_url

USER_AGENT = "dfq/1.0 (personal trading planner)"


class InsecureUrl(ValueError):
    """地址不满足安全传输要求（会让密钥明文上网）。"""


def is_loopback_host(host: str | None) -> bool:
    """是否是本机回环地址：localhost、127.x.x.x、::1。局域网地址不算。"""
    if not host:
        return False
    h = host.strip().lower().rstrip(".")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


def check_secure_url(url: str, *, allow_http_local: bool = False, what: str = "地址") -> str:
    """校验一个会携带密钥的请求地址，返回规范化后的地址；不合格抛 InsecureUrl。

    规则：
      - 默认只允许 https://；
      - http:// 只有同时满足“主机是本机回环地址”和“调用方明确允许”才放行
        （用于本机运行的模型服务，数据不出这台电脑）；
      - 地址里不许带 用户名:密码@（会被写进日志、历史记录）。
    """
    raw = str(url or "").strip()
    try:
        u = urllib.parse.urlsplit(raw)
        host = u.hostname
    except ValueError:
        raise InsecureUrl(f"{what} 不是合法的网址") from None
    if u.scheme not in ("http", "https") or not host:
        raise InsecureUrl(f"{what} 必须是以 https:// 开头的完整网址")
    if u.username or u.password:
        raise InsecureUrl(f"{what} 里不要写用户名或密码（密钥只放环境变量）")
    if u.query or u.fragment or "?" in raw or "#" in raw:
        # 程序会在这个地址后面拼路径、另外附加参数，带 ? / # 的地址本来就用不了；
        # 更要紧的是有人会把 ?key=… 这类密钥直接粘进地址里，而地址会出现在配置文件、界面和日志里。
        raise InsecureUrl(f"{what} 里不要带 ? 或 # 后面的参数（密钥只放在“设置”页或 .env 里）")
    if u.scheme == "http":
        if not is_loopback_host(host):
            raise InsecureUrl(f"{what} 用的是不加密的 http://，密钥会被明文发到 {host}。请改用 https://")
        if not allow_http_local:
            raise InsecureUrl(f"{what} 用的是 http://。本机服务如确需使用，请在配置里显式打开对应的 allow_http_local 开关")
    return raw.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向。

    标准库跟随重定向时会把请求头（包括 Authorization）原样带到新地址，
    一个 https 接口只要回一个 302 就能把密钥引到别的主机甚至明文 http 上。
    这里一律不跟：3xx 会当作普通的错误状态返回给上层。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
# 本机 http 服务（只有 allow_http_local 时才会用到）一律直连，不走系统代理：
# 走代理的话，明文的 Authorization 头会先交给代理程序，“数据不出这台电脑”就不成立了。
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

# 单个响应的大小上限。正常接口返回几十 KB；对方出错或被劫持时返回巨量数据，不能把内存吃光。
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(redact(message))
        self.status = status
        self.retryable = retryable

    @property
    def network(self) -> bool:
        """网络层面的失败（连不上、超时）：没有拿到任何 HTTP 状态码。"""
        return self.status is None and self.retryable


def _read_capped(resp) -> bytes:
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise HttpError(f"响应超过 {MAX_RESPONSE_BYTES // (1024 * 1024)} MB，已放弃（接口可能出错了）")
    return data


def _snippet(raw: bytes, limit: int = 200) -> str:
    """错误正文的摘录：先整体脱敏再截断。反过来做的话，密钥正好跨在截断处时，前半截会漏出去。"""
    return redact(raw[:64 * 1024].decode("utf-8", errors="replace"))[:limit]


Transport = Callable[[str, str, dict | None, bytes | None, dict, float], tuple[int, bytes]]


def urllib_transport(method: str, url: str, params: dict | None, body: bytes | None,
                     headers: dict, timeout: float) -> tuple[int, bytes]:
    full = url
    if params:
        full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, data=body, method=method, headers=headers)
    opener = _direct_opener if urllib.parse.urlsplit(url).scheme == "http" else _opener
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, _read_capped(resp)
    except urllib.error.HTTPError as e:
        payload = b""
        try:
            payload = e.read(64 * 1024) or b""
        except Exception:
            pass
        return e.code, payload
    except urllib.error.URLError as e:
        raise HttpError(f"网络错误 {redact_url(url)}: {e.reason}", retryable=True) from None
    except (TimeoutError, OSError, http.client.HTTPException) as e:
        # HTTPException：连接中途断开、响应残缺（IncompleteRead 等），不是 OSError，以前会原样漏到上层
        raise HttpError(f"网络错误 {redact_url(url)}: {type(e).__name__}", retryable=True) from None


def request_json(method: str, url: str, *, params: dict | None = None, json_body: Any = None,
                 headers: dict | None = None, timeout: float = 15, retries: int = 2,
                 backoff: float = 1.5, transport: Transport | None = None,
                 on_attempt: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 allow_http_local: bool = False) -> Any:
    """发请求并解析 JSON。只对网络错误和 5xx/429 重试，最多 retries 次。

    on_attempt：每次真正发出请求前回调（用于按次计费的接口记账/查额度；它抛异常则中止）。
    本项目所有对外请求都带密钥（token / API Key），所以这里是最后一道闸：
    地址不是 https（或未获明确许可的本机 http）就直接拒绝，一个字节都不发。
    """
    try:
        check_secure_url(url, allow_http_local=allow_http_local, what="请求地址")
    except InsecureUrl as e:
        raise HttpError(f"已拒绝发送：{e}") from None
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
            last = HttpError(f"HTTP {status} {redact_url(url)}: {_snippet(raw)}", status=status,
                             retryable=status >= 500 or status == 429)
        if not last.retryable or attempt >= retries:
            break
        sleep(backoff * (2 ** attempt))
    assert last is not None
    raise last
