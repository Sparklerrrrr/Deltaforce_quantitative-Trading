"""审查问题三：API Key / token 只能走加密连接；不进日志、异常和错误正文。"""
import json
import logging
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dfq import llm as L
from dfq import log as LOG
from dfq.http import HttpError, InsecureUrl, check_secure_url, is_loopback_host, request_json
from dfq.sources import xtc as X

KEY = "sk-live-9f8e7d6c5b4a39281706"


def make(cfg, base_url, allow_local=False, transport=None):
    cfg.set("llm.enabled", True)
    cfg.set("llm.base_url", base_url)
    cfg.set("llm.model", "m")
    cfg.set("llm.retries", 0)
    cfg.set("llm.allow_http_local", allow_local)
    return L.LLMClient(cfg, KEY, transport=transport)


def recording(sent, status=200, body=None):
    def transport(method, url, params, payload, headers, timeout):
        sent.append({"url": url, "headers": dict(headers)})
        return status, (body if body is not None else json.dumps(
            {"choices": [{"message": {"content": "{\"items\": []}"}}]}).encode())
    return transport


# ------------------------------------------------------------------ 地址规则
@pytest.mark.parametrize("url", ["https://api.example.com/v1", "https://api.example.com:8443/v1/",
                                 "HTTPS://API.EXAMPLE.COM/v1", "https://127.0.0.1:8000/v1"])
def test_https_is_always_allowed(url):
    assert check_secure_url(url).lower().startswith("https://")


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1",            # 远程 http
    "http://192.168.1.20:8000/v1",          # 局域网也不算本机：同网段的人能看到明文
    "http://10.0.0.5/v1", "http://0.0.0.0:8000/v1",
    "http://localhost.evil.com/v1",         # 形似本机
    "http://127.0.0.1.evil.com/v1",
    "http://example.com@127.0.0.1/v1",      # 带用户名的障眼法
])
def test_remote_http_is_refused_even_with_local_exception_enabled(url):
    with pytest.raises(InsecureUrl):
        check_secure_url(url, allow_http_local=True)


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1",
                                 "http://127.8.9.10/v1", "http://[::1]:8000/v1", "http://LOCALHOST/v1"])
def test_local_http_needs_explicit_opt_in(url):
    with pytest.raises(InsecureUrl, match="allow_http_local"):
        check_secure_url(url)                                   # 默认：不允许
    assert check_secure_url(url, allow_http_local=True)         # 明确打开开关后才行


@pytest.mark.parametrize("url", ["", "api.example.com/v1", "ftp://example.com", "file:///etc/passwd",
                                 "javascript:alert(1)", "https://", "https://user:pw@api.example.com/v1",
                                 "//example.com/v1", "http://[bad"])
def test_malformed_or_credentialed_urls_are_refused(url):
    with pytest.raises(InsecureUrl):
        check_secure_url(url, allow_http_local=True)


def test_loopback_detection():
    assert all(is_loopback_host(h) for h in ("localhost", "LOCALHOST.", "127.0.0.1", "127.255.0.1", "::1", "[::1]"))
    assert not any(is_loopback_host(h) for h in ("", None, "192.168.0.1", "10.1.1.1", "example.com",
                                                 "localhost.example.com", "0.0.0.0", "169.254.1.1"))


# ------------------------------------------------------------------ 大模型客户端
def test_llm_https_works_and_key_goes_only_in_header(cfg):
    sent = []
    c = make(cfg, "https://api.example.com/v1", transport=recording(sent))
    assert c.ready
    c.chat_json("s", "u")
    assert sent[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert sent[0]["headers"]["Authorization"] == f"Bearer {KEY}" and KEY not in sent[0]["url"]


def test_llm_remote_http_never_sends_the_key(cfg):
    sent = []
    c = make(cfg, "http://api.example.com/v1", allow_local=True, transport=recording(sent))
    assert not c.ready and "http://" in c.why_not_ready() and "https://" in c.why_not_ready()
    with pytest.raises(L.LLMError):
        c.chat_json("s", "u")
    assert sent == []                                            # 一个字节都没发


def test_llm_local_http_blocked_by_default_allowed_when_opted_in(cfg):
    sent = []
    c = make(cfg, "http://127.0.0.1:11434/v1", transport=recording(sent))
    assert not c.ready and "allow_http_local" in c.why_not_ready()
    with pytest.raises(L.LLMError):
        c.chat_json("s", "u")
    assert sent == []
    c = make(cfg, "http://127.0.0.1:11434/v1", allow_local=True, transport=recording(sent))
    assert c.ready
    c.chat_json("s", "u")
    assert len(sent) == 1 and sent[0]["url"].startswith("http://127.0.0.1:11434/")


def test_request_layer_refuses_plain_http_regardless_of_caller():
    """最后一道闸：就算上层忘了检查，底层也不会把请求发到 http 地址。"""
    sent = []
    with pytest.raises(HttpError, match="已拒绝发送"):
        request_json("POST", "http://api.example.com/v1/x", headers={"Authorization": f"Bearer {KEY}"},
                     json_body={}, transport=recording(sent), retries=0)
    assert sent == []


def test_xtc_token_is_not_sent_over_http(cfg, db):
    cfg.set("xtc.base_url", "http://orzice.com/workApi")
    sent = []
    client = X.XtcClient(cfg, db, "tok_ABCDEF_12345678", transport=recording(sent))
    with pytest.raises(X.XtcError) as ei:
        client.minute(626)
    assert sent == [] and "tok_ABCDEF_12345678" not in str(ei.value)
    assert db.usage_on("xtc") == 0                               # 没发出去就不计次


# ------------------------------------------------------------------ Key 不外泄
@pytest.mark.parametrize("status,body", [
    (401, f'{{"error": "invalid api key: {KEY}"}}'),             # 服务端把 Key 原样写进报错正文
    (500, f"Internal error while handling Authorization: Bearer {KEY}"),
    (200, f"not json, but echoes {KEY}"),
    (200, json.dumps({"error": {"message": f"bad key {KEY}"}})),  # 200 但结构不对
])
def test_key_never_appears_in_exceptions_or_logs(cfg, tmp_path, status, body):
    LOG.setup_logging(tmp_path, verbose=True)
    c = make(cfg, "https://api.example.com/v1", transport=recording([], status=status, body=body.encode()))
    with pytest.raises(L.LLMError) as ei:
        c.chat_json("s", "u")
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__      # 异常链里也没有原始报错
    for h in logging.getLogger("dfq").handlers:
        h.flush()
    logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    assert KEY not in logs


def test_key_not_in_pipeline_warnings_when_llm_fails(cfg, db):
    from dfq import nodes  # noqa: F401
    from dfq import timeutil
    from dfq.pipeline import Context, run_pipeline
    c = make(cfg, "https://api.example.com/v1",
             transport=recording([], status=403, body=f"forbidden for key {KEY}".encode()))
    timeutil.set_now("2026-11-01 21:30:00")
    ctx = Context(cfg=cfg, db=db, now=timeutil.now(), mock=True)
    ctx.services["llm"] = c
    res = run_pipeline(ctx, cfg.get("pipeline.nodes"))
    blob = json.dumps([r.__dict__ for r in res], ensure_ascii=False, default=str) + "".join(ctx.warnings) \
        + ctx["notified"]["body"] + (cfg.data_dir / "outbox" / "latest.txt").read_text(encoding="utf-8")
    assert KEY not in blob and ctx["plan"]["mode"] == "offline"


def test_redact_url_strips_userinfo():
    assert LOG.redact_url("https://user:hunter2@api.example.com/v1?token=abc") == "https://api.example.com/v1"


# ------------------------------------------------------------------ 重定向（真的起一个本机服务来验证）
class _Handler(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        type(self).hits.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/v1/chat"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices": [{"message": {"content": "{}"}}]}')

    def log_message(self, *a):
        pass


def test_redirects_are_not_followed_so_the_key_cannot_be_led_elsewhere(cfg, monkeypatch):
    """接口回 302 想把请求（连同 Authorization 头）引到别处：不跟，Key 只到过原地址一次。"""
    from tests.conftest import REAL_OPENER_OPEN
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", REAL_OPENER_OPEN)   # 只连 127.0.0.1
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Handler.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = make(cfg, f"http://127.0.0.1:{srv.server_port}/v1", allow_local=True)
        with pytest.raises(L.LLMError, match="302"):
            c.chat_json("s", "u")
    finally:
        srv.shutdown()
        srv.server_close()
    assert _Handler.hits == [("/v1/chat/completions", f"Bearer {KEY}")]          # /stolen 从未被访问
