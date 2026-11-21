"""moligod 自动同步停用开关（用户要求：功能保留、但停用，只能用手动模式）。

默认 AUTO_SYNC_AVAILABLE = False：不管 config.yaml 里 moligod.enabled 写的是什么，
都不导入 / 启动 Playwright、不写 moligod_alerts 表、只返回手动清单；命令行按约定表现。
保留下来的自动同步代码由 tests/test_moligod_executor_intel.py 在打开开关的情况下演练。
"""
import builtins
import json

import pytest
import yaml

from dfq import moligod as M
from dfq import nodes  # noqa: F401  注册节点
from dfq.cli import main
from dfq.config import load_config
from dfq.mail import compose

PLAN = {
    "plan_id": "20261113-2130", "valid_from": "2026-11-13 21:30:00", "valid_until": "2026-11-14 21:00:00", "paper": True, "mode": "offline",
    "items": [
        {"oid": 626, "name": "12.7x55mm PS12B", "action": "buy", "buy_price": 3600, "sell_price": 4400,
         "stop_price": 3240, "qty": 200, "reason": "r"},
        {"oid": 640, "name": "5.7x28mm SS190", "action": "buy", "buy_price": 4000, "sell_price": 4900,
         "stop_price": 3600, "qty": 100, "reason": "r"},
    ],
    "sells": [{"oid": 640, "name": "5.7x28mm SS190", "qty": 150, "sell_price": 5400, "stop_price": 4140,
               "avg_cost": 4600, "breakeven": 5290}],
}


@pytest.fixture
def no_playwright(monkeypatch):
    """记录任何导入 playwright 的尝试（停用时一次都不应该有）。"""
    tried: list[str] = []
    real = builtins.__import__

    def guarded(name, *a, **k):
        if name.startswith("playwright"):
            tried.append(name)
            raise AssertionError("自动同步停用时不应该导入 playwright")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", guarded)
    return tried


def test_switch_is_off_by_default():
    assert M.AUTO_SYNC_AVAILABLE is False
    assert "手动模式" in M.UNAVAILABLE_TEXT


@pytest.mark.parametrize("enabled", [True, False])
def test_auto_enabled_is_false_whatever_the_config_says(cfg, enabled):
    cfg.set("moligod.enabled", enabled)
    assert M.auto_enabled(cfg) is False
    assert M.mode_label(cfg) == ("手动（自动同步暂时停用）" if enabled else "手动")


def test_auto_enabled_needs_both_switches(cfg, monkeypatch):
    monkeypatch.setattr(M, "AUTO_SYNC_AVAILABLE", True)
    assert M.auto_enabled(cfg) is False and M.mode_label(cfg) == "手动"
    cfg.set("moligod.enabled", True)
    assert M.auto_enabled(cfg) is True and M.mode_label(cfg) == "自动"


def test_sync_with_enabled_true_returns_manual_list_and_touches_nothing(cfg, db, no_playwright):
    cfg.set("moligod.enabled", True)
    desired = M.desired_alerts(cfg, PLAN)
    res = M.sync(cfg, db, desired)
    assert res["manual"] == desired and res["synced"] == [] and res["unmatched"] == []
    assert "自动同步功能暂时停用，已按手动模式列出提醒价位" in res["status"]
    assert no_playwright == []
    assert db.one("SELECT COUNT(*) AS c FROM moligod_alerts")["c"] == 0
    assert db.kv_get("moligod_name_map", None) is None


def test_sync_with_enabled_false_does_not_mention_the_suspension(cfg, db, no_playwright):
    res = M.sync(cfg, db, M.desired_alerts(cfg, PLAN))
    assert len(res["manual"]) == 2 and "停用" not in res["status"] and no_playwright == []


def test_sync_ignores_a_stale_alert_table_in_manual_mode(cfg, db):
    """手动模式下不能因为表里有旧的“已同步”记录就把清单省掉：用户要照着清单在网页上设置。"""
    cfg.set("moligod.enabled", True)
    for a in M.desired_alerts(cfg, PLAN):
        db.execute("INSERT INTO moligod_alerts(name,low,high,synced_at,verified) VALUES(?,?,?,?,1)",
                   (a["name"], a["low"], a["high"], "2026-11-12 21:30:00"))
    assert len(M.sync(cfg, db, M.desired_alerts(cfg, PLAN))["manual"]) == 2


def test_login_does_not_open_a_browser(cfg, no_playwright):
    assert M.login(cfg) == M.UNAVAILABLE_TEXT and no_playwright == []


def test_pipeline_node_lists_manual_alerts_without_a_warning(cfg, db, no_playwright):
    from dfq.pipeline import Context, run_pipeline
    from dfq import timeutil
    cfg.set("moligod.enabled", True)
    ctx = Context(cfg=cfg, db=db, now=timeutil.now(), offline=True, mock=False, dry_run=False)
    ctx["plan"] = PLAN
    res = run_pipeline(ctx, [{"id": "sync_alerts", "enabled": True}])
    assert res[0].status == "ok" and len(ctx["alert_result"]["manual"]) == 2
    assert not any("moligod" in w for w in ctx.warnings)                 # 手动模式列清单是正常结果，不报警
    assert no_playwright == [] and db.one("SELECT COUNT(*) AS c FROM moligod_alerts")["c"] == 0


def test_plan_mail_tells_how_to_set_each_alert_by_hand(cfg):
    body = compose.build_body(cfg, PLAN, manual_alerts=M.desired_alerts(cfg, PLAN),
                              alert_status="自动同步功能暂时停用，已按手动模式列出提醒价位。")
    assert "12.7x55mm PS12B：低于(≤) 3600（买入线）" in body and "高于(≥) 4400（卖出线）" in body
    assert "5.7x28mm SS190：低于(≤) 4140（止损线）" in body
    assert "买入线 = " in body and "止损线 = " in body and "卖出线 = " in body


# ---------------------------------------------------------------- 命令行
@pytest.fixture
def cli_env(tmp_path):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump({"general": {"data_dir": "data"}, "moligod": {"enabled": True}}), encoding="utf-8")
    cfg = load_config(cfgp)
    plans = cfg.data_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "latest.json").write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    return cfgp, cfg


def test_config_with_enabled_true_still_loads_with_a_note(cli_env):
    _, cfg = cli_env
    assert any("自动同步功能暂时停用，按手动模式运行" in n for n in cfg.notes)


def test_cli_login_prints_unavailable_and_fails(cli_env, capsys, no_playwright):
    cfgp, _ = cli_env
    assert main(["moligod-login", "--config", str(cfgp)]) != 0
    assert M.UNAVAILABLE_TEXT in capsys.readouterr().out and no_playwright == []


def test_cli_sync_prints_the_manual_list_without_touching_the_web(cli_env, capsys, no_playwright):
    cfgp, cfg = cli_env
    # 返回 1 = 提醒没有被自动设置，需要手动处理（与 test_e2e.py::test_cli_smoke 的既有约定一致）
    assert main(["moligod-sync", "--config", str(cfgp), "--now", "2026-11-13 21:30"]) == 1
    out = capsys.readouterr().out
    assert "手动模式（不操作网页）" in out and "自动同步功能暂时停用" in out
    assert "需手动：12.7x55mm PS12B　低于(≤) 3600（买入线）　高于(≥) 4400（卖出线）" in out
    assert "需手动：5.7x28mm SS190　低于(≤) 4140（止损线）" in out
    assert no_playwright == []


def test_cli_status_shows_manual_mode(cli_env, capsys):
    cfgp, _ = cli_env
    assert main(["status", "--config", str(cfgp)]) == 0
    assert "moligod 提醒：手动（自动同步暂时停用）" in capsys.readouterr().out


def test_mail_poll_never_starts_a_sync_while_suspended(cli_env, monkeypatch, capsys):
    cfgp, _ = cli_env
    from dfq.mail import poll, transport
    fake = {"checked": 1, "ignored": 0, "real_recorded": 1, "paper_recorded": 1, "errors": [], "rejected": [],
            "recorded": ["买 PS12B 100 @ 3600"]}
    monkeypatch.setattr(poll, "poll", lambda *a, **k: fake)
    monkeypatch.setattr(transport, "make_transport", lambda *a, **k: object())

    def boom(*a, **k):
        raise AssertionError("自动同步停用时，mail-poll 不应该触发 moligod 同步")
    monkeypatch.setattr(M, "sync", boom)
    assert main(["mail-poll", "--config", str(cfgp), "--now", "2026-11-13 21:30"]) == 0
    assert "moligod：" not in capsys.readouterr().out
