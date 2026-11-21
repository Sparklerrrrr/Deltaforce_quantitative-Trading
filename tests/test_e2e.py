"""端到端：用模拟行情把整条流水线跑通（不联网，不花次数）。"""
import json

import pytest
import yaml

from dfq import llm as L
from dfq import nodes  # noqa: F401  注册节点
from dfq import reflect, review, timeutil
from dfq.cli import main
from dfq.config import load_config
from dfq.pipeline import Context, run_pipeline

TOKEN = "tk_e2e_SECRET_0001"


def run_once(cfg, db, *, when, mock=True, offline=True, dry_run=False, services=None):
    timeutil.set_now(when)
    ctx = Context(cfg=cfg, db=db, now=timeutil.now(), offline=offline, dry_run=dry_run, mock=mock)
    ctx.services.update(services or {})
    return ctx, run_pipeline(ctx, cfg.get("pipeline.nodes"))


def status(results):
    return {r.id: r.status for r in results}


def test_full_pipeline_on_mock_data(cfg, db):
    ctx, res = run_once(cfg, db, when="2026-11-01 21:30:00")
    st = status(res)
    assert all(st[n] == "ok" for n in ("collect_market", "features", "rule_baseline", "llm_analyze",
                                       "guardrails", "write_plan", "sync_alerts", "notify"))
    plan = ctx["plan"]
    assert plan["paper"] is True and plan["mode"] == "offline" and len(plan["items"]) == 8
    for it in plan["items"]:
        if it["action"] == "buy":
            assert it["stop_price"] < it["buy_price"] < it["breakeven"] <= it["sell_price"]
            assert it["qty"] >= cfg.get("risk.min_qty") and it["budget"] == it["qty"] * it["buy_price"]
    assert sum(i.get("budget") or 0 for i in plan["items"]) <= ctx["state"]["equity"] * cfg.get("risk.max_daily_spend_pct")
    # 计划文件、决策记录、邮件都落地了
    saved = json.loads((cfg.data_dir / "plans" / "latest.json").read_text(encoding="utf-8"))
    assert saved["plan_id"] == plan["plan_id"]
    assert db.one("SELECT COUNT(*) AS c FROM decisions WHERE variant='final'")["c"] == 8
    assert db.one("SELECT COUNT(*) AS c FROM decisions WHERE variant='rule'")["c"] == 8
    mail = (cfg.data_dir / "outbox" / "latest.txt").read_text(encoding="utf-8")
    assert mail.startswith("Subject: 【模拟计划】") and "模拟数据" in mail and "moligod" in mail
    # 首晚 21 次（列表 3 + 分钟 8 + 天曲线 8 + 预测 2）
    assert db.usage_on("xtc") == 21


def test_second_night_is_cheaper_and_history_accumulates(cfg, db):
    run_once(cfg, db, when="2026-11-01 21:30:00")
    run_once(cfg, db, when="2026-11-02 21:30:00")
    assert db.usage_on("xtc") == 13                                   # 第二晚不再拉天曲线
    span = db.one("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) c FROM minute_prices WHERE oid=626")
    assert span["a"].startswith("2026-10-31") and span["b"].startswith("2026-11-02") and span["c"] == 576


def test_multi_day_run_scores_decisions_and_reflects(cfg, db):
    for d in range(1, 10):
        ctx, res = run_once(cfg, db, when=f"2026-11-{d:02d} 21:30:00")
    assert db.one("SELECT COUNT(*) AS c FROM decisions WHERE status='final'")["c"] > 0
    p = review.perf(cfg, db)
    assert p["final"]["signals"] == p["rule"]["signals"]              # 离线模式：最终建议 = 规则基线
    assert p["final"]["profit"] == p["rule"]["profit"]
    assert db.one("SELECT COUNT(*) AS c FROM reflections")["c"] == 1  # 第 8 天触发了一次每周反思
    assert db.one("SELECT status FROM reflections")["status"] == "pending"
    assert "有 1 条反思建议待你确认" in ctx["notified"]["body"]
    assert db.one("SELECT COUNT(*) AS c FROM opinions WHERE source_type='platform' AND status='final'")["c"] > 0


def test_missing_token_degrades_and_still_notifies(cfg, db):
    ctx, res = run_once(cfg, db, when="2026-11-13 21:30:00", mock=False)
    st = status(res)
    assert st["collect_market"] == "degraded" and st["notify"] == "ok"
    assert db.usage_on("xtc") == 0
    assert "⚠行情数据不全" in ctx["notified"]["subject"]
    assert any("DFQ_XTC_TOKEN" in w for w in ctx.warnings)
    assert all(i["action"] == "watch" for i in ctx["plan"]["items"])   # 没数据就不编造买卖线


def test_daily_limit_hit_raises_alarm_but_pipeline_finishes(cfg, db):
    cfg.set("xtc.daily_limit", 6)
    ctx, res = run_once(cfg, db, when="2026-11-01 21:30:00")
    assert db.usage_on("xtc") == 6 and status(res)["notify"] == "ok"
    assert any("【报警】" in w for w in ctx.warnings)
    assert "⚠行情数据不全" in ctx["notified"]["subject"]


def test_dry_run_has_no_side_effects(cfg, db):
    run_once(cfg, db, when="2026-11-01 21:30:00")
    before = {t: db.one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in ("decisions", "api_usage", "trades")}
    ctx, res = run_once(cfg, db, when="2026-11-02 21:30:00", mock=False, dry_run=True)
    after = {t: db.one(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in before}
    assert before == after                                            # 不记决策、不花次数
    assert ctx["plan"]["plan_id"].endswith("-dryrun")
    assert json.loads((cfg.data_dir / "plans" / "latest.json").read_text(encoding="utf-8"))["plan_id"] == "20261101-2130"


def fake_llm_transport(reply_fn, calls):
    def transport(method, url, params, body, headers, timeout):
        payload = json.loads(body)
        calls.append(payload)
        content = reply_fn(json.loads(payload["messages"][1]["content"]))
        return 200, json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    return transport


def llm_service(cfg, reply_fn, calls):
    cfg.set("llm.enabled", True); cfg.set("llm.base_url", "https://llm.example/v1"); cfg.set("llm.model", "test-model")
    return L.LLMClient(cfg, "sk-e2e-key-000111", transport=fake_llm_transport(reply_fn, calls))


def test_llm_adjustments_pass_through_guardrails(cfg, db):
    calls = []

    def reply(inp):
        items = []
        for it in inp["items"]:
            r = it["rule"]
            # 模型“发疯”：全部喊买、买入线砍半、卖出线翻倍、数量巨大
            items.append({"oid": it["oid"], "action": "buy", "buy_price": (r["buy_price"] or 1000) // 2,
                          "sell_price": (r["sell_price"] or 2000) * 2, "qty": 999999,
                          "reason": "模型理由", "confidence": 0.9})
        return json.dumps({"market_view": "偏强", "items": items, "next_run": "2026-11-02 21:30",
                           "short_term": [{"date": "2026-11-02", "note": "凌晨挂单"}]})

    ctx, res = run_once(cfg, db, when="2026-11-01 21:30:00", offline=False,
                        services={"llm": llm_service(cfg, reply, calls)})
    plan = ctx["plan"]
    assert plan["mode"] == "llm" and plan["model"] == "test-model" and plan["prompt_ver"] == L.PROMPT_VER
    assert plan["market_view"] == "偏强" and plan["next_run"] == "2026-11-02 21:30"
    adj = cfg.get("risk.llm_max_adjust_pct")
    equity = ctx["state"]["equity"]
    for it in plan["items"]:
        r = it["rule"]
        assert it["buy_price"] >= r["buy_price"] * (1 - adj) - 1       # 调整幅度被夹住
        assert it["sell_price"] <= r["sell_price"] * (1 + adj) + 1
        assert (it.get("budget") or 0) <= equity * cfg.get("risk.max_position_pct")
        if it["action"] == "buy":
            assert it["sell_price"] >= it["breakeven"]
    assert sum(i.get("budget") or 0 for i in plan["items"]) <= equity * cfg.get("risk.max_daily_spend_pct")
    # 对照组（纯规则）也入库了，且没被模型污染
    rule_rows = db.query("SELECT * FROM decisions WHERE variant='rule'")
    assert len(rule_rows) == 8 and all(r["model"] is None for r in rule_rows)
    assert db.one("SELECT model, prompt_ver FROM decisions WHERE variant='final'")["model"] == "test-model"
    # 发给模型的内容里没有任何密钥
    assert "sk-e2e-key-000111" not in json.dumps(calls) and TOKEN not in json.dumps(calls)


def test_llm_failure_falls_back_to_rules(cfg, db):
    calls = []
    ctx, res = run_once(cfg, db, when="2026-11-01 21:30:00", offline=False,
                        services={"llm": llm_service(cfg, lambda inp: "抱歉我不能输出 JSON", calls)})
    st = status(res)
    assert st["llm_analyze"] == "degraded" and st["notify"] == "ok"
    assert ctx["plan"]["mode"] == "offline" and len(calls) == 2        # 重试 1 次后放弃
    assert any("只用规则基线" in w for w in ctx.warnings)


def test_reflection_accept_writes_whitelisted_overrides_only(cfg, db, tmp_path):
    for d in range(1, 4):
        run_once(cfg, db, when=f"2026-11-{d:02d} 21:30:00")
    calls = []

    def reply(_):
        return json.dumps({"summary": "买入线偏低", "mistakes": ["触发率低"], "param_suggestions": [
            {"key": "rules.buy_quantile", "current": 0.15, "suggested": 0.2, "reason": "触发率低"},
            {"key": "risk.max_position_pct", "suggested": 1.0, "reason": "想加仓"},          # 不在白名单
            {"key": "rules.stop_pct", "suggested": 0.9, "reason": "越界"},                    # 超出范围
            {"key": "general.paper_mode", "suggested": 0, "reason": "直接实盘"},              # 绝不允许
        ], "prompt_suggestions": ["强调费率"]})

    client = llm_service(cfg, reply, calls)
    res = reflect.run(cfg, db, client, days=7)
    sugg = res["content"]["param_suggestions"]
    assert [s["key"] for s in sugg] == ["rules.buy_quantile"]
    assert not (cfg.data_dir / "overrides.yaml").exists()              # 存档 ≠ 生效
    msg = reflect.decide(cfg, db, res["id"], accept=True)
    assert "rules.buy_quantile = 0.2" in msg
    ov = yaml.safe_load((cfg.data_dir / "overrides.yaml").read_text(encoding="utf-8"))
    assert ov == {"rules": {"buy_quantile": 0.2}}
    assert "已经是" in reflect.decide(cfg, db, res["id"], accept=True)  # 不能重复确认


def test_overrides_file_applies_whitelisted_values(tmp_path):
    (tmp_path / "config.yaml").write_text("general:\n  data_dir: data\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "overrides.yaml").write_text("rules:\n  buy_quantile: 0.2\nfeatures:\n  window_days: 5\n",
                                                      encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.get("rules.buy_quantile") == 0.2 and cfg.get("features.window_days") == 5


@pytest.mark.parametrize("body,expect", [
    ("general:\n  paper_mode: false\n", "general.paper_mode 不允许通过这个文件修改"),   # 不能借它切到实盘
    ("risk:\n  max_position_pct: 1\n", "risk.max_position_pct 不允许"),               # 不能借它放开风控
    ("rules:\n  stop_pct: 5\n", "rules.stop_pct 应在"),                              # 白名单内但超范围
    ("rules:\n  buy_quantile: abc\n", "应为数字"),
    ("- 1\n- 2\n", "必须是键值对"),
])
def test_overrides_file_with_anything_else_refuses_to_run(tmp_path, body, expect):
    """被篡改或写坏的 overrides.yaml 不会被“悄悄忽略”，而是明确报错、拒绝运行。"""
    (tmp_path / "config.yaml").write_text("general:\n  data_dir: data\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "overrides.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=expect):
        load_config(tmp_path / "config.yaml")


def test_config_validation(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("fees:\n  sell_fee_rate: 1.5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
    p.write_text("executor:\n  mode: robot\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
    p.write_text("watchlist:\n  - name: 5.56x45mm M995\n    alias: [M995]\n  - 5.45x39mm BS\n", encoding="utf-8")
    cfg = load_config(p)
    assert [w["name"] for w in cfg.watchlist] == ["5.56x45mm M995", "5.45x39mm BS"]
    assert cfg.fee == 0.13 and cfg.paper is True                       # 没写的用默认值
    assert load_config(tmp_path / "不存在.yaml").get("xtc.daily_limit") == 60


def test_cli_smoke(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    base = ["--config", str(cfgp), "--mock"]
    assert main(["run", *base, "--offline", "--now", "2026-11-01 21:30"]) == 0
    assert main(["run", *base, "--offline", "--now", "2026-11-01 22:30"]) == 0      # 同一晚再跑：被频率限制挡住
    assert "还没到下一次" in capsys.readouterr().out
    assert main(["balance", "set", "2000000", *base]) == 0
    assert main(["trade", "buy", "ps12b", "100", "3600", *base]) == 0
    assert main(["trade", "buy", "不存在", "1", "1", *base]) == 2
    assert main(["status", *base]) == 0
    out = capsys.readouterr().out
    assert "持仓 12.7x55mm PS12B ×100" in out and "未设置" in out
    assert main(["plan", *base, "--now", "2026-11-01 23:00"]) == 0
    assert main(["perf", *base]) == 0
    assert main(["reflect", "list", *base]) == 0
    assert main(["probe", "--config", str(cfgp)]) == 2                              # 无 token：不执行、不花钱
    assert "没有消耗任何次数" in capsys.readouterr().out
    assert main(["moligod-sync", *base, "--dry-run", "--now", "2026-11-01 23:00"]) == 1   # 默认关闭 → 手动
    assert main([]) == 0
