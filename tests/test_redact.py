import json
import logging

from dfq import log as L
from dfq.http import HttpError, request_json

TOKEN = "dummy-test-token-9f8e7d6c5b4a3210"


def test_redact_token_in_url():
    url = f"https://orzice.com/workApi/v1/sjz_api/minute?id=626&token={TOKEN}"
    out = L.redact(url)
    assert TOKEN not in out and "token=***" in out
    assert "id=626" in out                      # 非敏感参数保留，便于排查


def test_redact_url_drops_query_entirely():
    assert L.redact_url(f"https://orzice.com/workApi/v1/sjz_api/minute?token={TOKEN}&id=1") == \
        "https://orzice.com/workApi/v1/sjz_api/minute"


def test_redact_registered_secret_anywhere():
    L.register_secret(TOKEN)
    assert TOKEN not in L.redact(f"连接失败，上下文里夹着 {TOKEN} 这样的原文")
    assert TOKEN not in L.redact({"nested": [TOKEN]})


def test_redact_bearer_and_json_fields():
    s = L.redact('Authorization: Bearer sk-abcdef1234567890 {"api_key": "zzz-secret-zzz", "password":"p@ss"}')
    assert "sk-abcdef1234567890" not in s and "zzz-secret-zzz" not in s and "p@ss" not in s


def test_log_files_never_contain_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DFQ_XTC_TOKEN", TOKEN)
    L.setup_logging(tmp_path, verbose=True)
    lg = L.get_logger("t")
    lg.info("请求 https://orzice.com/workApi/v1/sjz_api/minute?id=1&token=%s", TOKEN)
    L.event(lg, logging.WARNING, "结构化字段", url=f"https://x/y?token={TOKEN}", token=TOKEN,
            params={"token": TOKEN, "id": 1})
    try:
        raise RuntimeError(f"boom https://x/y?token={TOKEN}")
    except RuntimeError:
        lg.exception("带堆栈的异常")
    for h in logging.getLogger("dfq").handlers:
        h.flush()
    text = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    for h in list(logging.getLogger("dfq").handlers):
        h.close()
        logging.getLogger("dfq").removeHandler(h)
    assert text and TOKEN not in text
    for line in text.splitlines():
        json.loads(line)                         # 每行都是合法 JSON


def test_http_error_message_has_no_query_string():
    def transport(method, url, params, body, headers, timeout):
        return 500, f"upstream echo token={TOKEN}".encode()

    try:
        request_json("GET", "https://orzice.com/workApi/v1/sjz_api/minute",
                     params={"id": 1, "token": TOKEN}, retries=0, transport=transport)
    except HttpError as e:
        assert TOKEN not in str(e)
        assert "HTTP 500" in str(e)
    else:
        raise AssertionError("应当抛 HttpError")


def test_http_retries_only_retryable(monkeypatch):
    calls = []

    def flaky(method, url, params, body, headers, timeout):
        calls.append(1)
        return (503, b"busy") if len(calls) < 3 else (200, b'{"ok": true}')

    assert request_json("GET", "https://x/y", retries=2, transport=flaky, sleep=lambda s: None) == {"ok": True}
    assert len(calls) == 3

    calls.clear()

    def bad_request(method, url, params, body, headers, timeout):
        calls.append(1)
        return 400, b"nope"

    try:
        request_json("GET", "https://x/y", retries=2, transport=bad_request, sleep=lambda s: None)
    except HttpError:
        pass
    assert len(calls) == 1                       # 4xx 不重试
