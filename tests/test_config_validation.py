"""审查问题四：配置必须严格校验。不合法就明确报错并拒绝运行，绝不悄悄换成默认值。"""
import copy
import math

import pytest
import yaml

from dfq import config_check as C
from dfq import ledger, nodes, pipeline  # noqa: F401
from dfq.cli import main
from dfq.config import DEFAULTS, OVERRIDE_WHITELIST, load_config


def data(**changes):
    d = copy.deepcopy(DEFAULTS)
    for dotted, value in changes.items():
        cur = d
        parts = dotted.split("__")
        for part in parts[:-1]:
            cur = cur[part]
        cur[parts[-1]] = value
    return d


def problems(**changes) -> str:
    with pytest.raises(C.ConfigError) as ei:
        C.check(data(**changes))
    return "\n".join(ei.value.problems)


def test_defaults_and_example_file_are_valid():
    assert C.check(copy.deepcopy(DEFAULTS)) == []
    import pathlib
    ex = yaml.safe_load((pathlib.Path(__file__).parent.parent / "config.example.yaml").read_text(encoding="utf-8"))
    from dfq.config import deep_merge
    assert C.check(deep_merge(DEFAULTS, ex)) == []


def test_every_default_setting_has_a_rule():
    """新增配置项却忘了写校验规则 → 这个测试会失败。"""
    def leaves(d, prefix=""):
        for k, v in d.items():
            path = f"{prefix}{k}"
            if path in C.SPEC:
                yield path
            elif isinstance(v, dict):
                yield from leaves(v, path + ".")
            else:
                yield path
    missing = [p for p in leaves(DEFAULTS) if p not in C.SPEC]
    assert missing == []
    assert set(C.KNOWN_NODES) == set(pipeline.REGISTRY)
    assert all(k in C.SPEC for k in OVERRIDE_WHITELIST)


# ------------------------------------------------------------------ 比例：5% 写成 5
RATIOS = ["fees__sell_fee_rate", "risk__max_daily_spend_pct", "risk__max_position_pct",
          "risk__max_total_exposure_pct", "risk__llm_max_adjust_pct", "rules__min_margin",
          "rules__stop_pct", "rules__stop_buffer", "rules__buy_quantile", "rules__sell_quantile",
          "rules__cold_shrink", "fees__warn_diff"]


@pytest.mark.parametrize("key", RATIOS)
@pytest.mark.parametrize("bad", [5, 25, 100, -0.1, 1.5])
def test_ratio_out_of_range_is_rejected(key, bad):
    assert key.replace("__", ".") in problems(**{key: bad})


def test_percent_mistake_gets_a_helpful_hint():
    msg = problems(risk__max_position_pct=25)
    assert "risk.max_position_pct" in msg and "25% 应写 0.25" in msg
    msg = problems(rules__stop_pct=10)
    assert "10% 应写 0.1" in msg
    assert "13% 应写 0.13" in problems(fees__sell_fee_rate=13)


@pytest.mark.parametrize("key,value", [
    ("risk__max_daily_spend_pct", 0), ("risk__max_position_pct", 0), ("risk__max_total_exposure_pct", 0),
    ("rules__buy_quantile", 0), ("rules__sell_quantile", 1), ("fees__sell_fee_rate", 0.5), ("rules__stop_pct", 0),
])
def test_boundary_values_excluded(key, value):
    assert key.replace("__", ".") in problems(**{key: value})


@pytest.mark.parametrize("key,value", [
    ("risk__max_daily_spend_pct", 1), ("risk__max_position_pct", 1), ("risk__max_total_exposure_pct", 1),
    ("rules__buy_quantile", 0.5), ("rules__sell_quantile", 0.5001), ("fees__sell_fee_rate", 0.4999),
    ("rules__min_margin", 0), ("rules__stop_pct", 0.01), ("rules__stop_pct", 0.5),
    ("risk__llm_max_adjust_pct", 0), ("xtc__list_limit", 10), ("xtc__list_limit", 20),
])
def test_boundary_values_included(key, value):
    C.check(data(**{key: value}))


# ------------------------------------------------------------------ 类型
@pytest.mark.parametrize("bad", ["0.13", "13%", None, True, False, [0.13], {"v": 0.13}, float("nan"), float("inf")])
def test_wrong_type_for_number(bad):
    assert "fees.sell_fee_rate 应为数字" in problems(fees__sell_fee_rate=bad)


@pytest.mark.parametrize("bad", [1.5, "20", None, True, 2.0])
def test_wrong_type_for_integer(bad):
    assert "risk.lot_size 应为整数" in problems(risk__lot_size=bad)


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, None])
def test_wrong_type_for_boolean(bad):
    assert "general.paper_mode 应为 true 或 false" in problems(general__paper_mode=bad)


def test_yaml_booleans_cannot_pose_as_numbers(tmp_path):
    """YAML 里 yes/on 是布尔值；写在数字的位置上必须报错，而不是被当成 1。"""
    p = tmp_path / "config.yaml"
    p.write_text("risk:\n  max_position_pct: yes\n  lot_size: on\n", encoding="utf-8")
    with pytest.raises(C.ConfigError) as ei:
        load_config(p)
    assert len(ei.value.problems) == 2


# ------------------------------------------------------------------ 数量、金额、价格类
@pytest.mark.parametrize("key,bad", [
    ("risk__assumed_capital", 0), ("risk__assumed_capital", -100), ("risk__assumed_capital", 1e15),
    ("risk__lot_size", 0), ("risk__lot_size", -10), ("risk__min_qty", 0), ("risk__min_qty", 10**9),
    ("xtc__daily_limit", 0), ("xtc__daily_limit", -1), ("xtc__daily_limit", 10**6),
    ("xtc__list_limit", 9), ("xtc__list_limit", 21), ("xtc__retries", 99), ("xtc__timeout", 0),
    ("features__window_days", 1), ("features__window_days", 365), ("general__run_every_days", 0),
    ("general__plan_valid_hours", 0), ("risk__max_valid_hours", 100000), ("review__sell_horizon_hours", 0),
    ("mail__smtp_port", 0), ("mail__smtp_port", 70000), ("llm__max_tokens", 1), ("llm__temperature", 9),
    ("moligod__max_ops_per_run", 0), ("rules__trend_block_pct", 0), ("watch__move_pct", 0),
])
def test_quantities_amounts_and_limits(key, bad):
    assert key.replace("__", ".") in problems(**{key: bad})


def test_cross_field_rules():
    assert "必须小于 rules.sell_quantile" in problems(rules__buy_quantile=0.5, rules__sell_quantile=0.5)
    assert "不能超过 risk.max_valid_hours" in problems(general__plan_valid_hours=100, risk__max_valid_hours=50)
    assert "不能小于 risk.lot_size" in problems(risk__min_qty=5, risk__lot_size=10)
    assert "不能大于 max_gap_sec" in problems(moligod__min_gap_sec=30, moligod__max_gap_sec=5)
    notes = C.check(data(risk__max_position_pct=0.9, risk__max_total_exposure_pct=0.5))
    assert any("实际起作用的是总仓位上限" in n for n in notes)       # 不致命的只提示


# ------------------------------------------------------------------ 拼写错误、多余的键
def test_misspelled_key_is_an_error_with_suggestion():
    d = data()
    d["risk"]["max_postion_pct"] = 0.1                # 少了个 i：以为设了 10% 上限，其实没生效
    with pytest.raises(C.ConfigError) as ei:
        C.check(d)
    assert "不认识的配置项 risk.max_postion_pct，是不是想写 risk.max_position_pct" in ei.value.problems[0]


def test_unknown_section_and_scalar_where_section_expected():
    d = data()
    d["riks"] = {"max_position_pct": 0.1}
    with pytest.raises(C.ConfigError, match="不认识的配置项 riks，是不是想写 risk"):
        C.check(d)
    d = data()
    d["risk"] = 0.25
    with pytest.raises(C.ConfigError, match="risk 应该是一组设置"):
        C.check(d)
    d = data()
    d["rules"] = None                                 # “rules:” 后面什么都没写
    with pytest.raises(C.ConfigError):
        C.check(d)


def test_all_problems_reported_at_once(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("fees:\n  sell_fee_rate: 13\nrisk:\n  max_position_pct: 25\n  lot_size: -1\n"
                 "executor:\n  mode: robot\nxtc:\n  base_url: http://orzice.com/workApi\n", encoding="utf-8")
    with pytest.raises(C.ConfigError) as ei:
        load_config(p)
    assert len(ei.value.problems) == 5 and "已拒绝运行" in str(ei.value)


def test_cli_refuses_to_run_with_bad_config(tmp_path, capsys):
    p = tmp_path / "config.yaml"
    p.write_text("general:\n  data_dir: data\nrisk:\n  max_daily_spend_pct: 50\n", encoding="utf-8")
    assert main(["run", "--config", str(p), "--mock", "--offline"]) == 2
    out = capsys.readouterr().out
    assert "已拒绝运行" in out and "50% 应写 0.5" in out
    assert not (tmp_path / "data").exists()           # 什么都没做：没建库、没出计划


def test_malformed_yaml_is_reported_plainly(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("risk:\n  max_position_pct: [0.25\n", encoding="utf-8")
    with pytest.raises(C.ConfigError, match="不是合法的 YAML"):
        load_config(p)
    p.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(C.ConfigError, match="键值对"):
        load_config(p)


# ------------------------------------------------------------------ 关注清单、流水线、网址、邮件
@pytest.mark.parametrize("wl,expect", [
    ([], "watchlist 不能为空"),
    (None, "watchlist 不能为空"),
    ([{"oid": 626}], "缺少 name"),
    ([{"name": "  "}], "缺少 name"),
    ([{"name": "A"}, {"name": "a"}], "重复出现"),
    ([{"name": "A", "oid": -5}], "oid 应为正整数"),
    ([{"name": "A", "oid": "626"}], "oid 应为正整数"),
    ([{"name": "A", "alias": "PS12B"}], "alias 应为一组文字"),
    ([{"name": "A", "alias": ["X"]}, {"name": "B", "alias": ["x"]}], "简称 x 同时属于"),
    ([{"name": "A", "qty": 100}], "不认识的键 qty"),
    ([42], "格式不对"),
])
def test_watchlist_rules(wl, expect):
    assert expect in problems(watchlist=wl)


def test_pipeline_cannot_drop_or_reorder_the_guardrails():
    base = copy.deepcopy(DEFAULTS["pipeline"]["nodes"])
    without = [n for n in base if n["id"] != "guardrails"]
    assert "不能去掉或关闭 guardrails" in problems(pipeline__nodes=without)
    disabled = [dict(n, enabled=False) if n["id"] == "guardrails" else n for n in base]
    assert "不能去掉或关闭 guardrails" in problems(pipeline__nodes=disabled)
    ids = [n["id"] for n in base]
    i, j = ids.index("llm_analyze"), ids.index("guardrails")
    swapped = list(base)
    swapped[i], swapped[j] = swapped[j], swapped[i]
    assert "模型的建议必须先过护栏" in problems(pipeline__nodes=swapped)


@pytest.mark.parametrize("node,expect", [
    ({"id": "colect_market"}, "是不是 collect_market"),
    ({"id": "review", "on_fail": "ignore"}, "on_fail 只能是"),
    ({"id": "review", "on_fail": "goto:nowhere"}, "on_fail 只能是"),
    ({"id": "review", "retries": 50}, "retries 应为 0～5"),
    ({"id": "review", "always": "yes"}, "always 应为 true 或 false"),
    ({"id": "review", "timeout": 5}, "不认识的键 timeout"),
])
def test_pipeline_node_rules(node, expect):
    nodes_ = [n for n in copy.deepcopy(DEFAULTS["pipeline"]["nodes"]) if n["id"] != node["id"]] + [node]
    assert expect in problems(pipeline__nodes=nodes_)


def test_urls_must_be_secure():
    assert "http://" in problems(xtc__base_url="http://orzice.com/workApi")
    assert "llm.base_url" in problems(llm__base_url="http://api.example.com/v1")
    assert "allow_http_local" in problems(llm__base_url="http://127.0.0.1:11434/v1")
    C.check(data(llm__base_url="http://127.0.0.1:11434/v1", llm__allow_http_local=True))
    assert "llm.base_url" in problems(llm__base_url="http://192.168.1.5:11434/v1", llm__allow_http_local=True)
    assert "moligod.url" in problems(moligod__url="http://moligod.com/ammo-realtime")


def test_enabled_features_need_their_settings():
    assert "必须填写 llm.base_url" in problems(llm__enabled=True)
    assert "必须填写 llm.model" in problems(llm__enabled=True, llm__base_url="https://x.example/v1")
    msg = problems(mail__enabled=True)
    assert "mail.owner_address 必须是你的邮箱地址" in msg and "mail.from_address" in msg
    assert "mail.owner_address" in problems(mail__enabled=True, mail__owner_address="不是邮箱",
                                            mail__from_address="a@example.com")
    notes = C.check(data(mail__enabled=True, mail__owner_address="a@example.com", mail__from_address="a@example.com"))
    assert any("一律不会记账" in n for n in notes)       # 没配置验证标识：能运行，但明确告诉你后果
    assert "不像一个服务器标识" in problems(mail__auth__trusted_authserv=["mx.example.com; dkim=pass"])
    assert "xtc.grades" in problems(xtc__grades=[7])
    assert "xtc.cost.minute" in problems(xtc__cost={"minute": -1})


# ------------------------------------------------------------------ 运行时输入（不是配置，但同样要查）
ITEM = {"name": "12.7x55mm PS12B", "oid": 626}


@pytest.mark.parametrize("qty,price", [
    (0, 100), (-5, 100), (100, 0), (100, -1), (1.5, 100), (float("nan"), 100), (100, float("nan")),
    (100, float("inf")), (True, 100), ("abc", 100), (None, 100), (10**9, 100), (100, 10**12),
])
def test_ledger_rejects_bad_quantity_and_price(db, qty, price):
    with pytest.raises(ledger.LedgerError):
        ledger.record_trade(db, "buy", ITEM, qty, price)
    assert db.one("SELECT COUNT(*) AS c FROM trades")["c"] == 0


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), True, "100", None])
def test_balance_rejects_bad_amount(db, amount):
    with pytest.raises(ledger.LedgerError):
        ledger.set_balance(db, amount)
    assert ledger.balance(db)["known"] is False


def test_cli_rejects_nan_balance_and_absurd_trade(tmp_path, capsys):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("general:\n  data_dir: data\n", encoding="utf-8")
    base = ["--config", str(cfgp), "--mock"]
    main(["run", *base, "--offline", "--now", "2026-11-01 21:30"])
    assert main(["balance", "set", "nan", *base]) == 2
    assert main(["balance", "set", "-5", *base]) == 2
    assert "没有设置" in capsys.readouterr().out
    assert main(["trade", "buy", "PS12B", "999999999", "3600", *base]) == 2
    assert "数字大得不合理" in capsys.readouterr().out
