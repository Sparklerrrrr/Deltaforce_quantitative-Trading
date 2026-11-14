import pytest

from dfq import intel, moligod as M, timeutil
from dfq.executors import Executor, Instruction, ManualExecutor, Report, get_executor, plan_to_instructions, register_executor
import dfq.executors as EX

PLAN = {
    "valid_until": "2026-11-14 21:00:00",
    "items": [
        {"oid": 626, "name": "12.7x55mm PS12B", "action": "buy", "buy_price": 3600, "sell_price": 4400,
         "stop_price": 3240, "qty": 200, "reason": "r"},
        {"oid": 635, "name": "5.56x45mm M995", "action": "watch", "buy_price": 5000, "sell_price": 5500},
        {"oid": 640, "name": "5.7x28mm SS190", "action": "buy", "buy_price": 4000, "sell_price": 4900,
         "stop_price": 3600, "qty": 100, "reason": "r"},
    ],
    "sells": [{"oid": 640, "name": "5.7x28mm SS190", "qty": 150, "sell_price": 5400, "stop_price": 4140}],
}


# ---------------- moligod：阈值设计（不涉及浏览器） ----------------
def test_desired_alerts_flat_vs_holding(cfg):
    d = {a["name"]: a for a in M.desired_alerts(cfg, PLAN)}
    assert set(d) == {"12.7x55mm PS12B", "5.7x28mm SS190"}                # 观望且无持仓的不设
    assert (d["12.7x55mm PS12B"]["low"], d["12.7x55mm PS12B"]["low_role"]) == (3600.0, "买入线")
    assert (d["12.7x55mm PS12B"]["high"], d["12.7x55mm PS12B"]["high_role"]) == (4400.0, "卖出线")
    # 有持仓：低于线从买入线换成止损线，高于线用持仓的卖出线
    assert (d["5.7x28mm SS190"]["low"], d["5.7x28mm SS190"]["low_role"]) == (4140.0, "止损线")
    assert d["5.7x28mm SS190"]["high"] == 5400.0


def test_diff_only_returns_changed_thresholds(cfg, db):
    desired = M.desired_alerts(cfg, PLAN)
    assert len(M.diff_alerts(db, desired)) == 2                           # 首次：都要设
    db.execute("INSERT INTO moligod_alerts(name,low,high,synced_at,verified) VALUES(?,?,?,?,1)",
               ("12.7x55mm PS12B", 3600, 4400, "2026-11-12 21:30:00"))
    assert [a["name"] for a in M.diff_alerts(db, desired)] == ["5.7x28mm SS190"]
    db.execute("UPDATE moligod_alerts SET high=4500")                     # 阈值变了 → 需要重设
    assert len(M.diff_alerts(db, desired)) == 2
    db.execute("UPDATE moligod_alerts SET high=4400, verified=0")         # 上次没校验通过 → 重设
    assert len(M.diff_alerts(db, desired)) == 2


def test_sync_disabled_falls_back_to_manual_list(cfg, db):
    res = M.sync(cfg, db, M.desired_alerts(cfg, PLAN))
    assert not res["ok"] and len(res["manual"]) == 2 and "未开启" in res["status"]
    assert M.sync(cfg, db, [])["ok"]


def test_sync_dry_run_never_touches_browser(cfg, db):
    cfg.set("moligod.enabled", True)
    res = M.sync(cfg, db, M.desired_alerts(cfg, PLAN), dry_run=True)
    assert "dry-run" in res["status"] and len(res["manual"]) == 2 and res["synced"] == []


def test_sync_noop_when_nothing_changed(cfg, db):
    cfg.set("moligod.enabled", True)
    for a in M.desired_alerts(cfg, PLAN):
        db.execute("INSERT INTO moligod_alerts(name,low,high,synced_at,verified) VALUES(?,?,?,?,1)",
                   (a["name"], a["low"], a["high"], "x"))
    res = M.sync(cfg, db, M.desired_alerts(cfg, PLAN))
    assert res["ok"] and "无需改动" in res["status"] and res["manual"] == []


def test_sync_without_playwright_degrades(cfg, db, monkeypatch):
    cfg.set("moligod.enabled", True)
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    res = M.sync(cfg, db, M.desired_alerts(cfg, PLAN))
    assert not res["ok"] and "Playwright" in res["status"] and len(res["manual"]) == 2


def test_name_matching_between_sites():
    avail = ["12.7x55mm PS12B", "5.7×28mm SS190", "5.56x45mm M995", "12.7x55mm PS12"]
    assert M.match_name("12.7x55mm PS12B", None, avail) == ("12.7x55mm PS12B", [])
    assert M.match_name("5.7x28mm SS190", None, avail)[0] == "5.7×28mm SS190"      # × 与 x、大小写、空格
    assert M.match_name("5.56X45MM m995", None, avail)[0] == "5.56x45mm M995"
    name, cands = M.match_name("7.62x39mm AP", None, avail)
    assert name is None and cands == []                                            # 对不上就不猜
    name, cands = M.match_name("PS12B弹", None, avail)
    assert name is None                                                            # 模糊的只给候选
    assert M.match_name("X", "5.56x45mm M995", avail) == ("5.56x45mm M995", [])    # 配置里的映射优先
    assert M.match_name("X", "写错的名字", avail)[0] is None


def test_parse_value_and_same():
    assert M._parse_value("3,600") == 3600 and M._parse_value("") is None and M._parse_value("abc") is None
    assert M._same(3600, 3600.4) and not M._same(3600, 3601) and M._same(None, None) and not M._same(None, 1)


# ---------------- 执行器插件 ----------------
def test_manual_executor_collects_instructions(cfg):
    ex, note = get_executor(cfg)
    assert isinstance(ex, ManualExecutor) and note is None
    ins = plan_to_instructions(PLAN)
    assert [(i.side, i.name, i.qty) for i in ins] == [
        ("sell", "5.7x28mm SS190", 150), ("buy", "12.7x55mm PS12B", 200), ("buy", "5.7x28mm SS190", 100)]
    reports = [ex.execute(i) for i in ins]
    assert all(r.status == "notified" for r in reports) and len(ex.pending) == 3


def test_auto_mode_without_plugin_reports_and_falls_back(cfg):
    cfg.set("executor.mode", "auto")
    ex, note = get_executor(cfg)
    assert isinstance(ex, ManualExecutor)
    assert "没有已注册" in note and "manual" in note


def test_auto_plugin_can_be_registered(cfg, monkeypatch):
    monkeypatch.setattr(EX, "_REGISTRY", dict(EX._REGISTRY))

    class Dummy(Executor):
        name = "auto"

        def execute(self, instruction: Instruction) -> Report:
            return Report(instruction, status="done")

    register_executor("auto", Dummy)
    cfg.set("executor.mode", "auto")
    ex, note = get_executor(cfg)
    assert isinstance(ex, Dummy) and note is None


def test_repo_contains_no_game_automation_code():
    """硬性边界：不操作游戏客户端。扫一遍源码，确保没有引入相关的库。"""
    import pathlib
    import re
    banned = re.compile(r"\b(pyautogui|pydirectinput|pywinauto|win32api|win32gui|ctypes\.windll|"
                        r"ReadProcessMemory|SendInput|keyboard\.press_and_release|mss|PIL\.ImageGrab)\b")
    src = pathlib.Path(__file__).resolve().parent.parent / "dfq"
    hits = [p.name for p in src.rglob("*.py") if banned.search(p.read_text(encoding="utf-8"))]
    assert hits == []


# ---------------- 资讯 ----------------
@pytest.fixture
def aliases(cfg, db):
    db.upsert_item(626, "12.7x55mm PS12B", 5, 0)
    db.upsert_item(640, "5.7x28mm SS190", 5, 0)
    return intel._alias_table(cfg, db)


def test_heuristic_extracts_only_clear_opinions(aliases):
    text = ("今天聊聊子弹。PS12B 这周我看涨，可以买一点。SS190 下周要削弱，建议卖出！"
            "M995 嘛，不会涨也不会跌。另外 PS12B 别抄底……还有护甲的事。")
    ops = intel.heuristic_opinions(text, aliases)
    assert {(o["item"], o["direction"]) for o in ops} == {("12.7x55mm PS12B", "up"), ("5.7x28mm SS190", "down")}


def test_heuristic_skips_negation_and_mixed(aliases):
    assert intel.heuristic_opinions("PS12B 不会涨", aliases) == []
    assert intel.heuristic_opinions("PS12B 先涨后跌", aliases) == []          # 方向矛盾：不猜
    assert intel.heuristic_opinions("今天天气不错，会涨工资", aliases) == []   # 没提到品种


def test_documents_dedup_and_opinions_get_reference_price(cfg, db, aliases):
    db.execute("INSERT INTO snapshots(oid,ts,price) VALUES(626,'2026-11-13 21:00',3900)")
    assert intel.add_document(db, "bili", "moligod", "PS12B 我看涨，可以买。", title="本周子弹")
    assert intel.add_document(db, "bili", "moligod", "PS12B 我看涨，可以买。") is None    # 重复
    assert intel.add_document(db, "bili", "moligod", "   ") is None
    stats = intel.process_documents(cfg, db, None)
    assert stats == {"docs": 1, "opinions": 1, "by_llm": 0}
    op = db.one("SELECT * FROM opinions")
    assert (op["source_name"], op["direction"], op["ref_price"], op["status"]) == ("moligod", "up", 3900, "open")
    assert intel.process_documents(cfg, db, None)["docs"] == 0                          # 不重复提炼
    assert intel.recent_digests(db)[0]["source"] == "B站UP主/moligod"


def test_manual_note_file_parsing(tmp_path, cfg, db):
    d = tmp_path / "manual_notes"
    d.mkdir()
    (d / "a.txt").write_text("source: bili\nname: 十七\ntitle: 子弹行情\ndate: 2026-11-12\n\nPS12B 要涨\n", encoding="utf-8")
    (d / "qq_某群_闲聊.txt").write_text("群友说 SS190 要跌", encoding="utf-8")
    (d / "README.md").write_text("说明", encoding="utf-8")
    (d / "x.bin").write_bytes(b"\x00")
    n = intel.parse_note_file(d / "a.txt")
    assert (n["source_type"], n["source_name"], n["title"], n["published_at"], n["text"]) == \
        ("bili", "十七", "子弹行情", "2026-11-12", "PS12B 要涨")
    n = intel.parse_note_file(d / "qq_某群_闲聊.txt")
    assert (n["source_type"], n["source_name"], n["title"]) == ("qq", "某群", "闲聊")
    cfg.set("intel.manual_dir", str(d))
    assert intel.ingest_manual_dir(cfg, db) == 2 and intel.ingest_manual_dir(cfg, db) == 0


def test_platform_hints_become_scorable_opinions(cfg, db):
    db.execute("INSERT INTO xtc_hints(ts,kind,oid,name,hour,price) VALUES(?,?,?,?,?,?)",
               (timeutil.fmt(timeutil.now()), "ammo_day", 626, "12.7x55mm PS12B", 3, 3400))
    assert intel.hints_to_opinions(db) == 1 and intel.hints_to_opinions(db) == 0
    op = db.one("SELECT * FROM opinions")
    assert (op["source_type"], op["direction"], op["ref_price"], op["horizon_days"]) == ("platform", "low", 3400, 1)


def test_news_search_requires_key_and_is_off_by_default(cfg, db):
    assert intel.fetch_news(cfg, db, None) == 0
    cfg.set("intel.news.enabled", True)
    with pytest.raises(ValueError, match="DFQ_TAVILY_API_KEY"):
        intel.fetch_news(cfg, db, None)

    def fake(method, url, params, body, headers, timeout):
        assert url == "https://api.tavily.com/search" and headers["Authorization"] == "Bearer tvly-key-123"
        return 200, ('{"results": [{"title": "平衡调整", "url": "https://news.example/a", '
                     '"content": "PS12B 伤害削弱", "published_date": "2026-11-12"}]}').encode()
    assert intel.fetch_news(cfg, db, "tvly-key-123", transport=fake) == 2 - 1     # 两个查询返回同一篇 → 去重
    assert db.one("SELECT source_name FROM documents")["source_name"] == "news.example"


def test_windows_scripts_have_safe_encodings():
    """.bat 必须纯 ASCII（cmd 按系统代码页读）；.ps1 必须带 UTF-8 BOM（否则 PowerShell 5.1 中文乱码）。"""
    import pathlib
    scripts = pathlib.Path(__file__).resolve().parent.parent / "scripts"
    (scripts / "run_dfq.bat").read_bytes().decode("ascii")
    assert (scripts / "install_tasks.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"TOKEN=" not in (scripts / "run_dfq.bat").read_bytes()      # 脚本里不许出现密钥赋值
