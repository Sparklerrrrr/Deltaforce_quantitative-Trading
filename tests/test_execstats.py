"""真实成交统计（dfq/execstats.py）：只用成交账本 + 建议记录，和“理论触线统计”分开说。"""
from datetime import datetime

import pytest

from dfq import execstats as E
from dfq import ledger, review, timeutil
from dfq.cli import main

PS = {"oid": 626, "name": "12.7x55mm PS12B"}
OTHER = {"oid": 999, "name": "测试弹"}


def dec(db, *, created, until, name="12.7x55mm PS12B", oid=626, action="buy", buy=3600, sell=4400, stop=3234,
        qty=100, hit=None, paper=0):
    db.execute("INSERT INTO decisions(plan_id,variant,created_at,oid,name,action,buy_price,sell_price,stop_price,"
               "qty,valid_from,valid_until,paper,buy_hit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"p-{created}", "final", created, oid, name, action, buy, sell, stop, qty, created, until, paper, hit))


def dt(s):
    return timeutil.parse_dt(s)


@pytest.fixture
def world(cfg, db):
    """真实账本：两条有数量的买入建议（理论上都触线了），只真的买到了第一条。"""
    dec(db, created="2026-11-05 21:30:00", until="2026-11-06 21:30:00", qty=100, hit=1)
    dec(db, created="2026-11-06 21:30:00", until="2026-11-07 21:30:00", action="watch", qty=0, buy=3600, sell=4400)
    dec(db, created="2026-11-08 21:30:00", until="2026-11-09 21:30:00", buy=3500, sell=4300, stop=3150, qty=200, hit=1)
    ledger.record_trade(db, "buy", PS, 100, 3636, ts=dt("2026-11-06 02:00:00"))            # 比买入线贵 1%
    ledger.record_trade(db, "sell", PS, 100, 4356, ts=dt("2026-11-07 10:00:00"))           # 比卖出线便宜 1%
    ledger.record_trade(db, "buy", PS, 50, 3000, ts=dt("2026-11-12 10:00:00"))             # 不在任何建议有效期内
    ledger.record_opening(db, OTHER, 30, 1000, ts=dt("2026-11-01 00:00:00"))               # 期初库存
    ledger.record_trade(db, "sell", OTHER, 30, 1500, ts=dt("2026-11-09 10:00:00"), net=39_000)
    return cfg, db


def test_fill_rate_slippage_holding_and_realized(world):
    cfg, db = world
    s = E.compute(cfg, db, days=30, paper=False)
    f = s["fill"]
    assert (f["suggestions"], f["filled"], f["suggested_qty"], f["filled_qty"]) == (2, 1, 300, 100)
    assert (f["theory_scored"], f["theory_hit"], f["unmatched_buys"]) == (2, 2, 1)
    sl = s["slippage"]
    assert sl["buy"]["n"] == 1 and sl["buy"]["mean"] == pytest.approx(0.01)
    assert sl["sell"]["n"] == 1 and sl["sell"]["mean"] == pytest.approx(-0.01)
    h = s["holding"]
    assert h["lots"] == 1 and h["days"]["median"] == pytest.approx(32 / 24)
    assert h["capital_days"] == pytest.approx(363_600 * 32 / 24)
    assert h["pnl"] == pytest.approx((4356 * 0.87 - 3636) * 100)
    assert h["pnl_per_million_day"] == pytest.approx(h["pnl"] / h["capital_days"] * 1e6)
    assert h["opening_lots_excluded"] == 1 and h["open_capital"] == 150_000
    r = s["realized"]
    assert r["sells"] == 2 and r["wins"] == 2 and r["with_net"] == 1
    assert r["total"] == pytest.approx((4356 * 0.87 - 3636) * 100 + 9_000)     # 有 net 用 net：39000 − 30000
    assert s["trades_total"] == 4                                              # 期初库存不算成交


def test_format_names_the_data_source_and_separates_theory(world):
    cfg, db = world
    txt = E.format_stats(E.compute(cfg, db, days=30))
    assert "数据来源：真实账本的成交记录 + 计划里的建议记录；不使用任何行情数据" in txt
    assert "1/2（50%）" in txt and "理论触线率 2/2" in txt and "不代表真的成交" in txt
    assert "自主操作" in txt and "期初库存" in txt and "样本太少" in txt


def test_empty_ledger_says_so_plainly(cfg, db):
    dec(db, created="2026-11-10 21:30:00", until="2026-11-11 21:30:00", hit=1)
    txt = E.format_stats(E.compute(cfg, db, days=30))
    assert "真实账本还没有成交记录，无法统计真实成交率和滑点；下面只有理论触线统计，不能代表真实收益" in txt
    assert "1/1" in txt


def test_books_never_mix(world):
    cfg, db = world
    s = E.compute(cfg, db, days=30, paper=True)
    assert s["trades_total"] == 0 and s["fill"]["suggestions"] == 0
    assert "模拟账本还没有成交记录" in E.format_stats(s)


def test_stop_sells_are_counted_separately(cfg, db):
    dec(db, created="2026-11-05 21:30:00", until="2026-11-06 21:30:00", hit=1)
    ledger.record_trade(db, "buy", PS, 100, 3600, ts=dt("2026-11-05 23:00:00"))
    ledger.record_trade(db, "sell", PS, 100, 3200, ts=dt("2026-11-06 10:00:00"))           # 止损线 3234 以下
    s = E.compute(cfg, db, days=30)
    assert s["slippage"]["stop_sells"] == 1 and s["slippage"]["sell"] is None
    assert s["realized"]["losses"] == 1 and s["realized"]["win_rate"] == 0


def test_window_excludes_old_trades(world):
    cfg, db = world
    timeutil.set_now("2026-12-30 21:30:00")
    s = E.compute(cfg, db, days=7)
    assert s["trades_in_window"] == 0 and s["realized"]["sells"] == 0 and s["fill"]["suggestions"] == 0


def test_mock_banner_and_theory_labels(cfg, db):
    assert E.format_stats(E.compute(cfg, db), mock=True).startswith(E.MOCK_BANNER)
    assert "人为生成的行情" in E.MOCK_BANNER and "不能说明策略是否赚钱" in E.MOCK_BANNER
    perf_txt = review.format_perf(review.perf(cfg, db))
    assert review.THEORY_LABEL in perf_txt and "理论总盈亏" in perf_txt and "模拟总盈亏" not in perf_txt
    assert "exec-stats" in perf_txt


def test_sell_results_skip_uncovered_legacy_oversell(cfg, db):
    # 旧版本允许的超卖（当时账上没有持仓）：直接写库模拟历史数据；没有成本依据的部分不计盈亏
    db.execute("INSERT INTO trades(ts,side,oid,name,qty,price,source,paper,kind) VALUES(?,?,?,?,?,?,?,?,?)",
               ("2026-11-01 10:00:00", "sell", 626, PS["name"], 100, 4000.0, "cli", 0, "trade"))
    assert E.sell_results(db, 0.13) == []
    assert E.compute(cfg, db)["realized"]["total"] == 0


def test_cli_exec_stats(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    assert main(["exec-stats", "--config", str(cfgp)]) == 0
    out = capsys.readouterr().out
    assert "真实账本还没有成交记录" in out and "现在是模拟模式" in out
    assert main(["exec-stats", "--config", str(cfgp), "--mock"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("【模拟数据】") and "模拟账本" in out
    assert main(["exec-stats", "--config", str(cfgp), "--days", "0"]) == 2


def test_cli_perf_mock_is_labelled(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    assert main(["perf", "--config", str(cfgp), "--mock"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("【模拟数据】") and "【理论触线统计】" in out


def test_notify_perf_line_is_labelled_theory(cfg, db):
    from dfq import nodes
    from dfq.pipeline import Context
    timeutil.set_now(datetime(2026, 11, 13, 21, 30))
    ctx = Context(cfg=cfg, db=db, now=timeutil.now(), offline=True, mock=True)
    dec(db, created="2026-11-10 21:30:00", until="2026-11-11 21:30:00", hit=1)
    db.execute("UPDATE decisions SET status='final', outcome='win', net_return=0.05, net_profit=1000, "
               "scored_at='2026-11-12 00:00:00'")
    ctx["recent"] = review.recent_summary(db, 7)
    assert ctx["recent"]["final"]["signals"] >= 1
    text = nodes.Notify()._review_text(ctx)
    assert text.startswith("理论触线统计") and "不代表你真的成交" in text and "exec-stats" in text
    assert "模拟盈亏" not in text
