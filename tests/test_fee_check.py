"""费率依据（评审第 3 条）：13% 是默认值不是事实。平台核对（fee-check / exchange_pay）、按实际到手金额校准、
按品种设置费率，以及计划里如实写明“费率依据”。"""
import copy
import json
from datetime import datetime, timedelta

import pytest

from dfq import config_check as C
from dfq import features as F
from dfq import fees, ledger, timeutil
from dfq.cli import main
from dfq.config import DEFAULTS, load_config
from dfq.db import DB
from dfq.sources import xtc as X
from dfq.sources.mock import MockXtcTransport

PS = {"oid": 626, "name": "12.7x55mm PS12B"}

# 和 2026-11-16 实测的响应同一形状（字段名一致；数值是构造的，比例落在实测的 12.998%～13.000%）
REAL_SHAPE = {"code": 0, "msg": "成功", "data": [
    {"oid": 37000 + i, "name": f"子弹{i}", "grade": 2 + i % 3, "count": 60 + i, "priceMax": pm, "sxf": sxf,
     "is_pay": 1, "isGet": "1", "pic": ""}
    for i, (pm, sxf) in enumerate([(308880, 40154), (7980, 1037), (55000, 7150), (175560, 22822), (12000, 1560),
                                   (98000, 12740), (40020, 5202), (61200, 7956), (23100, 3003)])]}


# ------------------------------------------------------------------ 解析与汇总
def test_parse_exchange_pay_real_shape_and_evidence():
    rows = X.parse_exchange_pay(REAL_SHAPE)
    assert len(rows) == 9 and all(0.1299 <= r["rate"] <= 0.1301 for r in rows)
    ev = fees.build_evidence(rows, 0.13, checked_at="2026-11-16 21:00:00")
    assert ev["n"] == 9 and ev["consistent"] and ev["source"] == "xtc.exchange_pay"
    assert ev["median"] == pytest.approx(0.13, abs=1e-4) and ev["min"] <= ev["median"] <= ev["max"]
    assert {"name", "grade", "rate"} == set(ev["items"][0])
    assert not fees.build_evidence(rows, 0.10)["consistent"]           # 配置 10%：相差 3 个百分点 → 不一致


def test_parse_exchange_pay_tolerates_nesting_and_garbage():
    resp = {"data": {"list": [
        {"name": "好", "priceMax": "10000", "sxf": "1300", "grade": "3"},
        {"name": "零价", "priceMax": 0, "sxf": 0},
        {"name": "费比价高", "priceMax": 100, "sxf": 100},
        {"name": "负费", "priceMax": 100, "sxf": -1},
        {"name": "文字", "priceMax": "abc", "sxf": 1},
        {"name": "布尔", "priceMax": True, "sxf": 0},
        {"name": "无穷", "priceMax": float("inf"), "sxf": 1},
        {"name": "缺字段", "priceMax": 100},
    ]}}
    rows = X.parse_exchange_pay(resp)
    assert [r["name"] for r in rows] == ["好"] and rows[0]["rate"] == pytest.approx(0.13) and rows[0]["grade"] == 3
    assert X.parse_exchange_pay({"data": None}) == [] and X.parse_exchange_pay("垃圾") == []
    with pytest.raises(ValueError):
        fees.build_evidence([], 0.13)


def test_mock_platform_fee_is_fixed_not_echoing_config():
    t = MockXtcTransport(fee=0.05)
    _status, body = t("GET", "https://example.invalid/v1/sjz_api/exchange_pay", {}, None, {}, 5)
    rows = X.parse_exchange_pay(json.loads(body))
    assert len(rows) == 9 and all(abs(r["rate"] - 0.13) < 0.001 for r in rows)


# ------------------------------------------------------------------ 命令行 fee-check
@pytest.fixture
def cfgp(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    return p


def mock_db(cfgp):
    return DB(load_config(cfgp).data_dir / "mock" / "dfq.db")


def test_fee_check_mock_saves_evidence_and_uses_cache(cfgp, capsys):
    assert main(["fee-check", "--config", str(cfgp), "--mock"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("【模拟数据】") and "中位数 13.0" in out and "与配置一致" in out and "不是游戏官方公告" in out
    db = mock_db(cfgp)
    ev = fees.evidence(db)
    assert ev["n"] == 9 and ev["consistent"] and ev["mock"] and not ev["from_cache"]
    used = db.usage_on("xtc")
    db.close()
    assert used == 1
    assert main(["fee-check", "--config", str(cfgp), "--mock"]) == 0                # 6 小时内：走缓存，不花次数
    assert "没有花次数" in capsys.readouterr().out
    db = mock_db(cfgp)
    assert db.usage_on("xtc") == 1 and fees.evidence(db)["from_cache"]
    db.close()


def test_fee_check_reports_inconsistent_config(cfgp, capsys):
    cfgp.write_text("general:\n  data_dir: data\nfees:\n  sell_fee_rate: 0.10\n", encoding="utf-8")
    assert main(["fee-check", "--config", str(cfgp), "--mock"]) == 0
    out = capsys.readouterr().out
    assert "与配置不一致" in out and "0.130" in out
    db = mock_db(cfgp)
    assert fees.evidence(db)["consistent"] is False
    db.close()


def test_fee_check_without_token_fails_cleanly_and_keeps_old_basis(cfgp, capsys):
    assert main(["fee-check", "--config", str(cfgp)]) == 1                          # 无 token：不联网、不写依据
    assert "没有核对成功" in capsys.readouterr().out
    db = DB(load_config(cfgp).data_dir / "dfq.db")
    assert fees.evidence(db) is None and db.usage_on("xtc") == 0
    db.close()


# ------------------------------------------------------------------ 费率依据的三种状态
def sell_with_net(db, net_rate, *, at, paper=False, name=PS):
    t = timeutil.parse_dt(at)
    ledger.record_trade(db, "buy", name, 100, 3600, ts=t, paper=paper)
    ledger.record_trade(db, "sell", name, 100, 4000, ts=t + timedelta(hours=1), paper=paper,
                        net=4000 * 100 * (1 - net_rate))


def test_basis_unverified_never_states_13_percent_as_fact(cfg, db):
    b = fees.basis(cfg, db)
    assert b["status"] == "unverified"
    assert "未核对" in b["line"] and "默认值" in b["line"] and "fee-check" in b["line"]
    assert "已用平台数据核对" not in b["line"] and b["warnings"] and "费率未核对" in b["warnings"][0]


def test_basis_platform(cfg, db):
    fees.save_evidence(db, fees.build_evidence(X.parse_exchange_pay(REAL_SHAPE), 0.13, checked_at="2026-11-16 21:00:00"))
    b = fees.basis(cfg, db)
    assert b["status"] == "platform" and "已用平台数据核对（2026-11-16，9 种子弹" in b["line"] and b["warnings"] == []
    cfg.set("fees.sell_fee_rate", 0.10)
    b = fees.basis(cfg, db)
    assert b["status"] == "platform" and any("不一致" in w for w in b["warnings"])


def test_basis_calibrated_from_actual_net(cfg, db):
    sell_with_net(db, 0.13, at="2026-11-01 10:00:00")
    sell_with_net(db, 0.13, at="2026-11-02 10:00:00")
    b = fees.basis(cfg, db)
    assert b["status"] == "unverified" and "另有 2 笔实际到手记录" in b["line"]     # 不足 3 笔：只提一句
    sell_with_net(db, 0.13, at="2026-11-03 10:00:00")
    b = fees.basis(cfg, db)
    assert b["status"] == "calibrated" and "按你的实际成交校准（真实账本 3 笔" in b["line"] and b["warnings"] == []


def test_calibration_warns_when_actual_fee_differs(cfg, db):
    for d in (1, 2, 3):
        sell_with_net(db, 0.20, at=f"2026-11-0{d} 10:00:00")
    cal = fees.calibration(db, fee=cfg.fee_for)
    assert cal["n"] == 3 and cal["median"] == pytest.approx(0.20) and cal["diff_median"] == pytest.approx(0.07)
    b = fees.basis(cfg, db)
    assert b["status"] == "calibrated" and any("fees.sell_fee_rate" in w and "20.0%" in w for w in b["warnings"])


def test_calibration_respects_per_item_override(cfg, db):
    cfg.set("fees.overrides", {"12.7x55mm PS12B": 0.20})
    for d in (1, 2, 3):
        sell_with_net(db, 0.20, at=f"2026-11-0{d} 10:00:00")
    assert fees.calibration(db, fee=cfg.fee_for)["diff_median"] == pytest.approx(0.0, abs=1e-9)
    assert fees.basis(cfg, db)["warnings"] == []                  # 按品种费率比较：一致，不提示


def test_calibration_only_uses_the_requested_book_and_real_net(cfg, db):
    for d in (1, 2, 3):
        sell_with_net(db, 0.20, at=f"2026-11-0{d} 10:00:00", paper=True)
    ledger.record_trade(db, "buy", PS, 10, 3600, ts=datetime(2026, 11, 5, 10))
    ledger.record_trade(db, "sell", PS, 10, 4000, ts=datetime(2026, 11, 5, 11))      # 没填 net：不参与校准
    assert fees.calibration(db) is None
    assert fees.calibration(db, paper=True)["n"] == 3
    assert fees.basis(cfg, db)["status"] == "unverified"          # 计划的费率依据只认真实成交


def test_old_quick_sell_inference_is_not_a_fee():
    assert fees.implied_fee is fees.quick_sell_discount
    assert "不是" in fees.quick_sell_discount.__doc__
    # 真实数据里关注品种的秒出价折让 1.2%～3.7%：不提示（旧版会误报“与 13% 相差较大”）
    assert F.fee_check(None, [{"quick_sell_discount": x} for x in (0.012, 0.02, 0.027, 0.037)]) is None


# ------------------------------------------------------------------ 按品种设置费率
def test_fee_for_overrides(cfg):
    cfg.set("fees.overrides", {" 12.7X55mm ps12b": 0.08})
    assert cfg.fee_for("12.7x55mm PS12B") == 0.08 and cfg.fee_for("别的") == 0.13 and cfg.fee_for(None) == 0.13


def test_ledger_positions_use_per_item_fee(cfg, db):
    cfg.set("fees.overrides", {"12.7x55mm PS12B": 0.0})
    ledger.record_trade(db, "buy", PS, 100, 3600, ts=datetime(2026, 11, 1, 10))
    ledger.record_trade(db, "sell", PS, 100, 4000, ts=datetime(2026, 11, 1, 11))
    assert ledger.positions(db, cfg.fee_for)["12.7x55mm PS12B"]["realized"] == pytest.approx(40_000)
    assert ledger.positions(db, cfg.fee)["12.7x55mm PS12B"]["realized"] == pytest.approx(4000 * 0.87 * 100 - 360_000)


def test_config_check_validates_overrides():
    def d(ov):
        x = copy.deepcopy(DEFAULTS)
        x["fees"]["overrides"] = ov
        return x
    assert C.check(d({"12.7x55mm PS12B": 0.12})) == []
    notes = C.check(d({"不在清单的弹": 0.12}))
    assert any("不在关注清单里" in n for n in notes)
    for bad, want in (({"12.7x55mm PS12B": 13}, "0.13"), ({"12.7x55mm PS12B": 1.0}, "不含 1"),
                      ({"12.7x55mm PS12B": "13%"}, "fees.overrides"), ([0.13], "对照表")):
        with pytest.raises(C.ConfigError) as ei:
            C.check(d(bad))
        assert want in "\n".join(ei.value.problems)


# ------------------------------------------------------------------ 计划里写明费率依据
def run_mock(cfg, db, when="2026-11-01 21:30:00"):
    from dfq import nodes  # noqa: F401
    from dfq.pipeline import Context, run_pipeline
    timeutil.set_now(when)
    ctx = Context(cfg=cfg, db=db, now=timeutil.now(), offline=True, mock=True)
    run_pipeline(ctx, cfg.get("pipeline.nodes"))
    return ctx, (cfg.data_dir / "outbox" / "latest.txt").read_text(encoding="utf-8")


def test_plan_states_fee_basis_and_drops_false_alarm(cfg, db):
    ctx, mail = run_mock(cfg, db)
    assert "费率依据：未核对" in ctx["plan"]["fee_basis"]
    assert "费率依据：未核对" in mail and "风控（模拟账本）" in mail
    assert "按秒出价反推" not in mail and not any("反推" in w and "秒出价" in w for w in ctx.warnings)
    assert "【模拟数据】人为生成的行情" in mail


def test_plan_after_platform_check(cfg, db):
    fees.save_evidence(db, fees.build_evidence(X.parse_exchange_pay(REAL_SHAPE), 0.13, checked_at="2026-11-01 20:00:00"))
    _ctx, mail = run_mock(cfg, db)
    assert "已用平台数据核对" in mail and "费率未核对" not in mail
