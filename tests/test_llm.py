import json

import pytest

from dfq import llm as L

RULE = {
    626: {"oid": 626, "name": "12.7x55mm PS12B", "action": "buy", "buy_price": 3600, "sell_price": 4400,
          "stop_price": 3240, "confidence": 0.5, "reason": "规则", "risk": ""},
    640: {"oid": 640, "name": "5.7x28mm SS190", "action": "watch", "buy_price": 4000, "sell_price": 4500,
          "stop_price": 3600, "confidence": 0.3, "reason": "空间不足", "risk": ""},
}


def fake_llm(content, status=200, capture=None):
    def transport(method, url, params, body, headers, timeout):
        if capture is not None:
            capture.update(url=url, headers=headers, body=json.loads(body))
        if status != 200:
            return status, b'{"error": "x"}'
        return 200, json.dumps({"choices": [{"message": {"content": content}}],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
    return transport


def client(cfg, content, **kw):
    cfg.set("llm.enabled", True)
    cfg.set("llm.base_url", "https://llm.example/v1/")
    cfg.set("llm.model", "some-model")
    cfg.set("llm.retries", 0)
    return L.LLMClient(cfg, "sk-test-key-123456", transport=fake_llm(content, **kw))


def test_request_is_openai_compatible_and_key_only_in_header(cfg):
    cap = {}
    c = client(cfg, '{"items": []}', capture=cap)
    c.chat_json("系统", "用户")
    assert cap["url"] == "https://llm.example/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-test-key-123456"
    assert cap["body"]["model"] == "some-model" and cap["body"]["messages"][0]["role"] == "system"
    assert cap["body"]["response_format"] == {"type": "json_object"}
    assert "temperature" not in cap["body"]                # 没配置就不发，兼容不接受该参数的模型
    assert "sk-test-key-123456" not in json.dumps(cap["body"])


def test_not_ready_reasons(cfg):
    c = L.LLMClient(cfg, None)
    assert not c.ready and "enabled" in c.why_not_ready()
    cfg.set("llm.enabled", True)
    assert "base_url" in L.LLMClient(cfg, "k").why_not_ready()
    cfg.set("llm.base_url", "https://x/v1"); cfg.set("llm.model", "m")
    assert "DFQ_LLM_API_KEY" in L.LLMClient(cfg, None).why_not_ready()
    with pytest.raises(L.LLMError):
        L.LLMClient(cfg, None).chat_json("a", "b")


@pytest.mark.parametrize("text", [
    '{"a": 1}', '```json\n{"a": 1}\n```', '好的，结果如下：\n{"a": 1}\n以上。',
    '{"a": 1} 还有一些 {多余} 的话', '  \n{"a":\n1}  ',
])
def test_parse_json_loose_accepts(text):
    assert L.parse_json_loose(text) == {"a": 1}


def test_parse_json_loose_handles_braces_inside_strings():
    assert L.parse_json_loose('说明 {"r": "形如 {x} 的占位符", "n": 2} 结束') == {"r": "形如 {x} 的占位符", "n": 2}


@pytest.mark.parametrize("text", ["", "没有 JSON", '{"a": 1', "[1, 2]", '{"a": }'])
def test_parse_json_loose_rejects(text):
    with pytest.raises(L.LLMError):
        L.parse_json_loose(text)


def test_validate_normalizes_and_fills_missing():
    out = L.validate_analysis({
        "market_view": "震荡",
        "items": [
            {"oid": "626", "action": "BUY", "buy_price": "3550.4", "sell_price": 4450, "stop_price": None,
             "qty": "180", "reason": "低分位", "confidence": 7},
            {"oid": 99999, "action": "buy", "buy_price": 1, "sell_price": 2},       # 清单外：丢弃
            {"oid": 626, "action": "watch"},                                        # 重复：忽略
            "垃圾",
        ],
        "short_term": [{"date": "2026-11-14", "note": "凌晨挂单"}, {"note": ""}, "x"],
        "next_run": "2026-11-14 21:30",
    }, RULE)
    a, b = out["items"]
    assert (a["oid"], a["action"], a["buy_price"], a["sell_price"], a["qty"]) == (626, "buy", 3550, 4450, 180)
    assert a["confidence"] == 1.0 and a["from"] == "llm"                            # 置信度夹到 [0,1]
    assert b["oid"] == 640 and b["from"] == "rule" and b["action"] == "watch"       # 漏掉的用规则补
    assert out["missing"] == [640]
    assert out["short_term"] == [{"date": "2026-11-14", "note": "凌晨挂单"}]
    assert out["next_run"] == "2026-11-14 21:30"


def test_validate_rejects_structurally_wrong_output():
    with pytest.raises(L.LLMError):
        L.validate_analysis({"items": "不是数组"}, RULE)
    out = L.validate_analysis({"items": [], "next_run": "明天晚上"}, RULE)
    assert out["next_run"] is None and len(out["items"]) == 2                       # 时间不合法就丢掉


def test_unknown_action_defaults_to_watch():
    out = L.validate_analysis({"items": [{"oid": 626, "action": "梭哈", "buy_price": 1, "sell_price": 2}]}, RULE)
    assert out["items"][0]["action"] == "watch"


def test_http_failure_becomes_llm_error_without_key(cfg):
    c = client(cfg, "", status=401)
    with pytest.raises(L.LLMError) as ei:
        c.chat_json("a", "b")
    assert "sk-test-key-123456" not in str(ei.value)


def test_malformed_openai_envelope(cfg):
    cfg.set("llm.enabled", True); cfg.set("llm.base_url", "https://x/v1"); cfg.set("llm.model", "m")
    c = L.LLMClient(cfg, "k-123456", transport=lambda *a: (200, b'{"choices": []}'))
    with pytest.raises(L.LLMError, match="OpenAI"):
        c.chat_json("a", "b")


def test_analysis_input_contains_features_but_no_raw_documents(cfg):
    feats = {626: {"oid": 626, "name": "12.7x55mm PS12B", "grade": 5, "current": 3900, "pct_rank": 0.4,
                   "basis": "minute", "warnings": [], "q": {"q50": 3900}}}
    state = {"cash": 1e6, "cash_known": True, "positions": {}}
    s = L.build_analysis_input(cfg, feats, {626: RULE[626]}, state, {"days": 7}, [{"source_name": "甲", "score": 0.6}],
                               [{"source": "B站UP主/甲", "digest": "摘要"}], [])
    d = json.loads(s)
    assert d["fee_rate"] == 0.13 and d["items"][0]["rule"]["buy_price"] == 3600
    assert d["items"][0]["features"]["pct_rank"] == 0.4 and "warnings" not in d["items"][0]["features"]
    ext = d["untrusted_external"]
    assert "不可信" in ext["_note"]
    assert ext["opinion_digests"][0]["digest"] == "摘要" and ext["source_credibility"][0]["score"] == 0.6
    assert "opinion_digests" not in d                       # 外部内容不与可信字段混放
