"""dfq/yamlpatch.py：在 YAML 文本里只改一个标量值，其余每个字符原样保留。"""
from pathlib import Path

import pytest
import yaml

from dfq import yamlpatch as Y
from dfq.webui.settings_spec import SPEC

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = (ROOT / "config.example.yaml").read_text(encoding="utf-8")


def _get(data, dotted):
    for p in dotted.split("."):
        data = data[p]
    return data


def _new_value(spec, cur):
    """给每种类型挑一个和当前值不同的新值（字符串故意带 # 引号 冒号 反斜杠）。"""
    t = spec["type"]
    if t == "bool":
        return not cur
    if t == "int":
        return int(cur) + 7
    if t == "percent":
        return 0.137 if cur != 0.137 else 0.21
    if t == "float":
        return 9.5 if cur != 9.5 else 11.25
    return 'a "b" #c: d\\e'


def _changed_lines(old: str, new: str) -> list[int]:
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    assert len(a) == len(b), "改一个已有的键不应该增删行"
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


@pytest.mark.parametrize("spec", SPEC, ids=[s["key"] for s in SPEC])
def test_every_spec_key_patches_one_line_of_example_config(spec):
    old_data = yaml.safe_load(EXAMPLE)
    cur = _get(old_data, spec["key"])
    value = _new_value(spec, cur)
    new = Y.set_scalar(EXAMPLE, spec["key"], value)
    Y.verify(EXAMPLE, new, spec["key"], value)                               # 回读：只有目标键变了
    changed = _changed_lines(EXAMPLE, new)
    assert len(changed) == 1                                                 # 其余每一行一个字节都不变
    line_old = EXAMPLE.splitlines()[changed[0]]
    line_new = new.splitlines()[changed[0]]
    if "#" in line_old.split(":", 1)[1] and spec["type"] != "text":
        assert line_new[line_new.index(" #"):].strip() == line_old[line_old.index(" #"):].strip()  # 行尾注释保留
    assert _get(yaml.safe_load(new), spec["key"]) == value


@pytest.mark.parametrize("spec", SPEC[:6], ids=[s["key"] for s in SPEC[:6]])
def test_crlf_file_keeps_crlf_everywhere(spec):
    crlf = EXAMPLE.replace("\n", "\r\n")
    cur = _get(yaml.safe_load(crlf), spec["key"])
    value = _new_value(spec, cur)
    new = Y.set_scalar(crlf, spec["key"], value)
    Y.verify(crlf, new, spec["key"], value)
    assert new.count("\r\n") == crlf.count("\r\n") and "\n" not in new.replace("\r\n", "")
    assert len(_changed_lines(crlf, new)) == 1


def test_trailing_comment_stays_in_its_column():
    text = "a:\n  b: 1   # 说明：数字\n  c: x\n"
    new = Y.set_scalar(text, "a.b", 25)
    assert new == "a:\n  b: 25  # 说明：数字\n  c: x\n"
    new = Y.set_scalar(text, "a.b", 123456)                                  # 值变长：至少留一个空格
    assert new == "a:\n  b: 123456 # 说明：数字\n  c: x\n"


@pytest.mark.parametrize("value", ["http://x/#y", 'say "hi"', "k: v", "c:\\dir\\", "", "  spaced  ", "中文#注释",
                                   "'single'", "null", "true", "123", "- dash", "{a: 1}", "&anchor", "*alias"])
def test_strings_with_special_characters_round_trip(value):
    text = 'a:\n  s: "old # not a comment"  # real comment\n  t: 1\n'
    new = Y.set_scalar(text, "a.s", value)
    Y.verify(text, new, "a.s", value)
    assert yaml.safe_load(new)["a"]["s"] == value and new.endswith("  # real comment\n  t: 1\n")


def test_value_formats():
    assert Y.format_value(True) == "true" and Y.format_value(False) == "false"
    assert Y.format_value(None) == "null" and Y.format_value(42) == "42"
    assert Y.format_value(0.13) == "0.13" and Y.format_value(3.0) == "3.0"
    assert yaml.safe_load(Y.format_value(1e-7)) == 1e-7 and yaml.safe_load(Y.format_value(2.5e20)) == 2.5e20
    assert Y.format_value("") == '""' and Y.format_value('a"b\\c') == '"a\\"b\\\\c"'
    assert yaml.safe_load(Y.format_value("x\ny\t\u2028")) == "x\ny\t\u2028"
    for bad in (float("nan"), float("inf"), [1], {"a": 1}, b"x"):
        with pytest.raises(Y.PatchError):
            Y.format_value(bad)


def test_insert_missing_key_at_end_of_parent_section():
    text = "general:\n  a: 1\n  b: 2   # 注释\n\n# 下一节\nother:\n  c: 3\n"
    new = Y.set_scalar(text, "general.z", True)
    assert new == "general:\n  a: 1\n  b: 2   # 注释\n  z: true\n\n# 下一节\nother:\n  c: 3\n"
    Y.verify(text, new, "general.z", True)


def test_insert_uses_the_sections_own_indentation():
    text = "general:\n    a: 1\nother:\n  c: 3\n"
    new = Y.set_scalar(text, "general.b", 5)
    assert new == "general:\n    a: 1\n    b: 5\nother:\n  c: 3\n"
    new = Y.set_scalar(text, "other.d", "x")
    assert new == 'general:\n    a: 1\nother:\n  c: 3\n  d: "x"\n'


def test_insert_nested_missing_parents():
    text = "intel:\n  bilibili:\n    enabled: false\n"
    new = Y.set_scalar(text, "intel.news.days", 5)
    Y.verify(text, new, "intel.news.days", 5)
    assert new == "intel:\n  bilibili:\n    enabled: false\n  news:\n    days: 5\n"


def test_insert_whole_section_at_end_of_file_without_trailing_newline():
    text = "general:\n  a: 1"
    new = Y.set_scalar(text, "risk.max_holding_days", 9)
    Y.verify(text, new, "risk.max_holding_days", 9)
    assert new == "general:\n  a: 1\n\nrisk:\n  max_holding_days: 9\n"
    assert Y.set_scalar("", "a.b", 1) == "a:\n  b: 1\n"


def test_insert_into_crlf_file_uses_crlf():
    text = "general:\r\n  a: 1\r\n"
    new = Y.set_scalar(text, "general.b", 2)
    assert new == "general:\r\n  a: 1\r\n  b: 2\r\n"


def test_insert_into_empty_section():
    text = "general:\nother:\n  c: 3\n"
    new = Y.set_scalar(text, "general.a", 1)
    Y.verify(text, new, "general.a", 1)
    assert new == "general:\n  a: 1\nother:\n  c: 3\n"


def test_bom_is_kept():
    text = "\ufeffa:\n  b: 1\n"
    new = Y.set_scalar(text, "a.b", 2)
    assert new == "\ufeffa:\n  b: 2\n"
    Y.verify(text, new, "a.b", 2)


def test_quoted_keys_and_single_quoted_values():
    text = "\"a\":\n  'b': 'it''s'   # c\n"
    new = Y.set_scalar(text, "a.b", "x")
    Y.verify(text, new, "a.b", "x")
    assert new.endswith("   # c\n") or new.endswith("  # c\n")


def test_empty_value_becomes_value():
    text = "a:\n  b:\n  c: 1\n"
    new = Y.set_scalar(text, "a.b", 3)
    assert new == "a:\n  b: 3\n  c: 1\n"
    Y.verify(text, new, "a.b", 3)
    text = "a:\n  b:   # 留空\n"
    new = Y.set_scalar(text, "a.b", "x")
    assert new == 'a:\n  b: "x" # 留空\n'


def test_list_items_are_not_mistaken_for_target():
    text = "watchlist:\n  - name: enabled\n    enabled: true\nenabled: false\n"
    new = Y.set_scalar(text, "enabled", True)
    assert new == "watchlist:\n  - name: enabled\n    enabled: true\nenabled: true\n"


@pytest.mark.parametrize("text,key", [
    (EXAMPLE, "mail"),                                       # 目标是一组设置
    (EXAMPLE, "watchlist"),                                  # 目标是列表
    ("a: {b: 1}\n", "a.b"),                                  # 父节是流式写法
    ("a: [1, 2]\n", "a"),                                    # 目标是流式列表
    ("a:\n  b: |\n    多行\n", "a.b"),                        # 多行字符串
    ("a:\n  b: >\n    多行\n", "a.b"),
    ("a: &x\n  b: 1\nc: *x\n", "a.b"),                        # 锚点
    ("a:\n  b: &v 1\nc: *v\n", "a.b"),                        # 锚点在目标值上
    ("a:\n  b: *v\n", "a.b"),                                # 别名
    ("a:\n\tb: 1\n", "a.b"),                                 # tab 缩进
    ("a:\n  b: 1\n---\na:\n  b: 2\n", "a.b"),                 # 多文档
    ("a:\n  b: 1\n  b: 2\n", "a.b"),                         # 重复的键
    ("a:\n  b: \"没配对\n", "a.b"),                          # 引号没配对
    ("a:\n  - 1\n", "a.b"),                                  # 父节是列表：不能插入
    ("- 1\n- 2\n", "a.b"),                                   # 顶层是列表
    ("a:\n  b: 1 2 # x\n  c: !!str 3\n", "a.c"),             # 带标签
])
def test_unsafe_shapes_are_refused(text, key):
    with pytest.raises(Y.PatchError):
        new = Y.set_scalar(text, key, 5)
        Y.verify(text, new, key, 5)                         # 万一 set_scalar 漏过，回读校验也必须拦住


@pytest.mark.parametrize("key", ["", "a..b", "a.b c", "a.#", "a/b", None, 5])
def test_bad_key_names_are_refused(key):
    with pytest.raises(Y.PatchError):
        Y.set_scalar("a:\n  b: 1\n", key, 1)


def test_verify_rejects_extra_changes_and_type_changes():
    old = "a:\n  b: 1\n  c: 2\n"
    with pytest.raises(Y.PatchError):
        Y.verify(old, "a:\n  b: 5\n  c: 3\n", "a.b", 5)    # 别的键也变了
    with pytest.raises(Y.PatchError):
        Y.verify(old, "a:\n  b: 5\n  c: 2\n", "a.b", 6)    # 新值不对
    with pytest.raises(Y.PatchError):
        Y.verify(old, "a:\n  b: 1.0\n  c: 2\n", "a.b", 1)  # 1.0 和 1 是不同的值
    with pytest.raises(Y.PatchError):
        Y.verify(old, "a:\n  b: true\n  c: 2\n", "a.b", 1)
    with pytest.raises(Y.PatchError):
        Y.verify(old, "a: [\n", "a.b", 1)                   # 改坏了的 YAML
    Y.verify(old, "a:\n  b: 5\n  c: 2\n", "a.b", 5)


def test_user_hand_edited_indentation_variants():
    text = ("# 用户手改过的文件\n"
            "general:\n"
            "      paper_mode: true      # 6 格缩进\n"
            "      data_dir: data\n"
            "fees:\n"
            " sell_fee_rate: 0.13\n"
            "\n"
            "risk:   # 行尾注释的节\n"
            "   assumed_capital: 100\n"
            "   # 注释行\n"
            "   max_position_pct: 0.25\n")
    for key, value in (("general.paper_mode", False), ("fees.sell_fee_rate", 0.12),
                       ("risk.assumed_capital", 5), ("risk.max_position_pct", 0.3)):
        new = Y.set_scalar(text, key, value)
        Y.verify(text, new, key, value)
        assert len(_changed_lines(text, new)) == 1
    new = Y.set_scalar(text, "risk.max_holding_days", 9)
    Y.verify(text, new, "risk.max_holding_days", 9)
    assert "   max_holding_days: 9\n" in new
    new = Y.set_scalar(text, "fees.overrides_x", 1)
    assert " overrides_x: 1\n" in new


def test_pure_function_does_not_touch_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Y.set_scalar(EXAMPLE, "general.paper_mode", False)
    assert list(tmp_path.iterdir()) == []
