import pytest

from dfq import pipeline as P


@pytest.fixture
def reg(monkeypatch):
    """临时节点注册表，不污染真正的节点。"""
    table = {}
    monkeypatch.setattr(P, "REGISTRY", table)
    return table


def node(reg, nid, fn=None, requires=(), provides=(), degrade=None):
    calls = []

    class N(P.Node):
        pass

    N.id, N.requires, N.provides = nid, tuple(requires), tuple(provides)

    def run(self, ctx):
        calls.append(nid)
        if fn:
            return fn(ctx, len(calls))
        for k in provides:
            ctx[k] = nid
        return {}

    N.run = run
    if degrade:
        N.degrade = lambda self, ctx, err: degrade(ctx, err)
    reg[nid] = N
    return calls


def ctx_():
    return P.Context(cfg=None, db=None, now=None)


def boom(ctx, n):
    raise RuntimeError("坏了 token=SECRET123456")


def statuses(results):
    return [(r.id, r.status) for r in results]


def test_runs_nodes_in_order_and_passes_data(reg):
    node(reg, "a", provides=["x"])
    node(reg, "b", requires=["x"], provides=["y"])
    ctx = ctx_()
    res = P.run_pipeline(ctx, [{"id": "a"}, {"id": "b"}])
    assert statuses(res) == [("a", "ok"), ("b", "ok")] and ctx["y"] == "b"
    assert P.overall_status(res) == "ok"


def test_retry_then_success(reg):
    def flaky(ctx, n):
        if n < 3:
            raise RuntimeError("临时故障")
        ctx["x"] = 1
    calls = node(reg, "a", fn=flaky, provides=["x"])
    res = P.run_pipeline(ctx_(), [{"id": "a", "retries": 2}])
    assert len(calls) == 3 and res[0].status == "ok" and res[0].attempts == 3


def test_on_fail_degrade_uses_fallback_and_continues(reg):
    def fallback(ctx, err):
        ctx["x"] = "旧数据"
        return {"fallback": True}
    node(reg, "a", fn=boom, provides=["x"], degrade=fallback)
    node(reg, "b", requires=["x"])
    ctx = ctx_()
    res = P.run_pipeline(ctx, [{"id": "a", "on_fail": "degrade"}, {"id": "b"}])
    assert statuses(res) == [("a", "degraded"), ("b", "ok")] and ctx["x"] == "旧数据"
    assert P.overall_status(res) == "degraded" and ctx.warnings


def test_degrade_without_fallback_is_a_failure_but_does_not_crash(reg):
    node(reg, "a", fn=boom)
    b = node(reg, "b")
    res = P.run_pipeline(ctx_(), [{"id": "a", "on_fail": "degrade"}, {"id": "b"}])
    assert statuses(res) == [("a", "failed"), ("b", "ok")] and b == ["b"]


def test_on_fail_skip_continues(reg):
    node(reg, "a", fn=boom)
    node(reg, "b")
    res = P.run_pipeline(ctx_(), [{"id": "a", "on_fail": "skip"}, {"id": "b"}])
    assert statuses(res) == [("a", "skipped"), ("b", "ok")]


def test_on_fail_abort_stops_but_always_nodes_still_run(reg):
    node(reg, "a", fn=boom)
    b = node(reg, "b")
    notify = node(reg, "notify", requires=["plan"])
    res = P.run_pipeline(ctx_(), [{"id": "a", "on_fail": "abort"}, {"id": "b"},
                                  {"id": "notify", "always": True}])
    assert statuses(res) == [("a", "failed"), ("b", "not_run"), ("notify", "ok")]
    assert b == [] and notify == ["notify"]                # 中止后仍会发通知
    assert P.overall_status(res) == "failed"


def test_missing_input_skips_node(reg):
    node(reg, "a", fn=boom)
    b = node(reg, "b", requires=["x"])
    c = node(reg, "c")
    res = P.run_pipeline(ctx_(), [{"id": "a", "on_fail": "skip"}, {"id": "b", "on_fail": "skip"}, {"id": "c"}])
    assert statuses(res) == [("a", "skipped"), ("b", "skipped"), ("c", "ok")] and b == [] and c == ["c"]


def test_missing_input_with_abort_policy_stops_pipeline(reg):
    node(reg, "b", requires=["x"])
    c = node(reg, "c")
    res = P.run_pipeline(ctx_(), [{"id": "b", "on_fail": "abort"}, {"id": "c"}])
    assert statuses(res) == [("b", "skipped"), ("c", "not_run")] and c == []


def test_goto_jumps_to_named_node(reg):
    node(reg, "a", fn=boom)
    b = node(reg, "b")
    node(reg, "c")
    res = P.run_pipeline(ctx_(), [{"id": "a", "on_fail": "goto:c"}, {"id": "b"}, {"id": "c"}])
    assert statuses(res) == [("a", "failed"), ("c", "ok")] and b == []


def test_goto_loop_is_broken(reg):
    node(reg, "a", fn=boom)
    res = P.run_pipeline(ctx_(), [{"id": "a", "on_fail": "goto:a"}])
    assert res[-1].status == "failed" and len(res) <= 4    # 不会死循环


def test_disabled_and_unknown_nodes(reg):
    a = node(reg, "a")
    res = P.run_pipeline(ctx_(), [{"id": "a", "enabled": False}, {"id": "不存在"}])
    assert statuses(res) == [("a", "skipped"), ("不存在", "failed")] and a == []


def test_node_skip_is_not_a_failure(reg):
    def skip(ctx, n):
        raise P.NodeSkip("今天不需要")
    node(reg, "a", fn=skip)
    ctx = ctx_()
    res = P.run_pipeline(ctx, [{"id": "a", "on_fail": "abort"}])
    assert res[0].status == "skipped" and res[0].detail == "今天不需要" and not ctx.warnings
    assert P.overall_status(res) == "ok"


def test_error_details_are_redacted(reg):
    node(reg, "a", fn=boom)
    ctx = ctx_()
    res = P.run_pipeline(ctx, [{"id": "a", "on_fail": "skip"}])
    assert "SECRET123456" not in res[0].detail and "SECRET123456" not in "".join(ctx.warnings)


def test_undelivered_output_is_noted(reg):
    node(reg, "a", fn=lambda ctx, n: {}, provides=["x"])
    res = P.run_pipeline(ctx_(), [{"id": "a"}])
    assert "未产出" in res[0].detail


def test_default_pipeline_config_references_only_real_nodes(cfg):
    from dfq import nodes  # noqa: F401
    ids = [s["id"] for s in cfg.get("pipeline.nodes")]
    assert set(ids) <= set(P.REGISTRY)
    assert ids.index("collect_market") < ids.index("features") < ids.index("rule_baseline") \
        < ids.index("llm_analyze") < ids.index("guardrails") < ids.index("write_plan") \
        < ids.index("sync_alerts") < ids.index("notify")
