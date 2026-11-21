"""moligod.com 价格提醒同步（可开关，默认关闭；自动同步目前整体停用，见 AUTO_SYNC_AVAILABLE）。

现状：只能用“手动”模式 —— 计划邮件里列出每个品种的提醒价位，由你自己到 moligod 网页上设置。
下面的自动同步代码完整保留（以后恢复时用），但在 AUTO_SYNC_AVAILABLE 为 False 时一行都不会执行：
sync() 只返回手动清单，login() 不打开浏览器。判断“现在是否自动同步”一律用 auto_enabled(cfg)，
不要直接读 moligod.enabled。

对 moligod 只做一件事：低频地在你自己的账号里设置价格提醒。
**不采集它的任何行情数据**（用户协议禁止脚本采集，robots.txt 禁止 /api/）。
本模块只读取“设置 XXX 的提醒”按钮的名称和你自己提醒弹窗里的两个阈值，用于定位和读回校验。

状态说明：本模块按你描述的页面结构编写，**没有在真实页面上实测过**
（需要你的登录状态）。页面元素一律用无障碍名称和文字定位，不用 CSS 类名。
任何一步失败都不重试：记录原因，剩下的阈值改为在邮件里列出，由你手动设置。

一个品种只有“低于/高于”两个阈值，止损线的处理方式：
  空仓时   低于线 = 买入线    高于线 = 卖出线
  持仓后   低于线 = 止损线    高于线 = 卖出线（不低于持仓回本价）
持仓状态来自你的记账。白天买入并回复邮件后，下一次 mail-poll 会把该品种的
低于线从“买入线”换成“止损线”（仍然只改有变化的那一个值）。
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

from . import timeutil
from .ledger import _norm
from .log import event, get_logger

log = get_logger("moligod")

BTN_PATTERN = re.compile(r"^设置\s*(.+?)\s*的提醒$")
LABEL_LOW = "低于(≤)提醒"
LABEL_HIGH = "高于(≥)提醒"
LABEL_SAVE = "保存提醒"
MODE_TRIGGER = "触发"

# 自动同步（登录 moligod 网页、自动设置提醒价位）的总开关。
# 目前是 False：功能和代码都保留，但暂时停用 —— 只能用“手动”模式（计划里列出价位，你自己到网页上设置）。
# 停用期间，即使 config.yaml 里写了 moligod.enabled: true 也不会启动浏览器；界面上切到“自动”会提示暂不可用。
# 以后要恢复：把这里改成 True（并在真实页面上实测一遍）。
AUTO_SYNC_AVAILABLE = False
UNAVAILABLE_TEXT = ("登录 moligod.com 网页自动设置交易区间的功能目前已停用，暂时只能使用手动模式："
                    "程序会在计划里列出每个品种的提醒价位，请到 moligod 网页上手动设置。")


def auto_enabled(cfg) -> bool:
    """现在是否真的会自动同步：功能可用（AUTO_SYNC_AVAILABLE）并且配置里开了（moligod.enabled）。"""
    return bool(AUTO_SYNC_AVAILABLE and cfg.get("moligod.enabled"))


def mode_label(cfg) -> str:
    """给人看的当前模式：“自动”“手动”或“手动（自动同步暂时停用）”。命令行 status 和界面共用。"""
    if auto_enabled(cfg):
        return "自动"
    if cfg.get("moligod.enabled") and not AUTO_SYNC_AVAILABLE:
        return "手动（自动同步暂时停用）"
    return "手动"


class SyncAbort(Exception):
    """同步中止（未登录、找不到元素、校验不一致……），不重试。"""


def desired_alerts(cfg, plan: dict) -> list[dict]:
    """根据计划和持仓算出每个品种应该设的两个阈值。观望且没有持仓的品种不动。"""
    names = {w["name"]: w.get("moligod_name") for w in cfg.watchlist}
    out: dict[str, dict] = {}
    for it in plan.get("items") or []:
        if it["action"] == "buy" and it.get("buy_price") and it.get("sell_price"):
            out[it["name"]] = {"name": it["name"], "low": float(it["buy_price"]), "low_role": "买入线",
                               "high": float(it["sell_price"]), "high_role": "卖出线"}
    for s in plan.get("sells") or []:           # 有持仓：低于线换成止损线
        out[s["name"]] = {"name": s["name"], "low": float(s["stop_price"]), "low_role": "止损线",
                          "high": float(s["sell_price"]), "high_role": "卖出线"}
    for a in out.values():
        a["moligod_name"] = names.get(a["name"])
    return list(out.values())


def diff_alerts(db, desired: list[dict]) -> list[dict]:
    """只保留和上次同步结果不同的阈值。"""
    changed = []
    for a in desired:
        row = db.one("SELECT low, high, verified FROM moligod_alerts WHERE name=?", (a["name"],))
        if row and row["verified"] and _same(row["low"], a["low"]) and _same(row["high"], a["high"]):
            continue
        changed.append(a)
    return changed


def _same(x, y) -> bool:
    if x is None or y is None:
        return x is None and y is None
    return abs(float(x) - float(y)) < 0.5


def _parse_value(text: str | None) -> float | None:
    if text is None:
        return None
    s = str(text).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def profile_dir(cfg) -> Path:
    p = cfg.get("moligod.profile_dir")
    return Path(p) if p else cfg.data_dir / "moligod_profile"


def match_name(target: str, configured: str | None, available: list[str]) -> tuple[str | None, list[str]]:
    """在页面上的品种名里找 target。返回 (匹配到的名字, 候选)。

    顺序：配置里的 moligod_name → 完全相同 → 归一化后相同（忽略大小写、空格、标点）。
    模糊到有歧义就不猜，把候选列出来让你确认。
    """
    if configured:
        return (configured, []) if configured in available else (None, _candidates(configured, available))
    if target in available:
        return target, []
    nt = _norm(target)
    hits = [a for a in available if _norm(a) == nt]
    if len(hits) == 1:
        return hits[0], []
    return None, _candidates(target, available)


def _candidates(target: str, available: list[str]) -> list[str]:
    nt = _norm(target)
    tail = _norm(target.split()[-1])
    scored = []
    for a in available:
        na = _norm(a)
        if nt in na or na in nt or (len(tail) >= 2 and tail in na):
            scored.append(a)
    return scored[:5]


def sync(cfg, db, desired: list[dict], *, dry_run: bool = False, sleep=time.sleep) -> dict:
    """执行同步。返回 {"status": 文字说明, "synced": [...], "manual": [...], "unmatched": [...]}。

    manual = 需要你手动设置的阈值（手动模式、失败、或没匹配上名字的）。

    自动同步暂时停用（AUTO_SYNC_AVAILABLE = False）时，不管配置怎么写都走手动模式：
    不导入、不启动 Playwright，不读写 moligod_alerts 表，只返回完整的手动清单。
    """
    res = {"status": "", "synced": [], "manual": [], "unmatched": [], "ok": False}
    if not desired:
        res.update(status="今天没有需要设置的提醒。", ok=True)
        return res
    if not auto_enabled(cfg):
        if cfg.get("moligod.enabled") and not AUTO_SYNC_AVAILABLE:
            status = "自动同步功能暂时停用，已按手动模式列出提醒价位，请到 moligod 网页上手动设置。"
        else:
            status = "自动同步未开启（moligod.enabled: false），请按下面的清单到 moligod 网页上手动设置。"
        res.update(status=status, manual=list(desired))
        return res
    changed = diff_alerts(db, desired)
    if not changed:
        res.update(status=f"moligod 上的 {len(desired)} 个提醒与今天的计划一致，无需改动。", ok=True)
        return res
    if dry_run:
        res.update(status=f"dry-run：本应自动设置 {len(changed)} 个提醒，未实际操作。", manual=changed)
        return res
    try:
        from playwright.sync_api import TimeoutError as PWTimeout  # type: ignore
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        res.update(status="未安装 Playwright（pip install playwright && python -m playwright install chromium），"
                          "已改为手动设置。", manual=changed)
        return res

    max_ops = int(cfg.get("moligod.max_ops_per_run", 20))
    timeout = int(cfg.get("moligod.timeout_sec", 20)) * 1000
    gap = (float(cfg.get("moligod.min_gap_sec", 4)), float(cfg.get("moligod.max_gap_sec", 8)))
    todo, overflow = changed[:max_ops], changed[max_ops:]
    done: list[dict] = []
    reason = ""
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(profile_dir(cfg)), headless=bool(cfg.get("moligod.headless", False)))
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(timeout)
                page.goto(cfg.get("moligod.url"), wait_until="domcontentloaded")
                buttons = page.get_by_role("button", name=BTN_PATTERN)
                try:
                    buttons.first.wait_for(state="visible")
                except PWTimeout:
                    raise SyncAbort("页面上没有找到“设置 XXX 的提醒”按钮（未登录，或页面改版）") from None
                available = []
                for i in range(buttons.count()):
                    label = buttons.nth(i).get_attribute("aria-label") or buttons.nth(i).inner_text()
                    m = BTN_PATTERN.match((label or "").strip())
                    if m:
                        available.append(m.group(1))
                name_map = db.kv_get("moligod_name_map", {}) or {}
                for idx, a in enumerate(todo):
                    conf = a.get("moligod_name") or name_map.get(a["name"])
                    mname, cands = match_name(a["name"], conf, available)
                    if mname is None:
                        res["unmatched"].append({**a, "candidates": cands})
                        continue
                    if idx > 0:
                        sleep(random.uniform(*gap))
                    _set_one(page, mname, a, PWTimeout)
                    sleep(random.uniform(1.0, 2.0))
                    low, high = _read_one(page, mname, PWTimeout)
                    if not (_same(low, a["low"]) and _same(high, a["high"])):
                        raise SyncAbort(f"「{mname}」保存后读回的值（低 {low} / 高 {high}）"
                                        f"与期望（低 {a['low']:.0f} / 高 {a['high']:.0f}）不一致")
                    db.execute(
                        "INSERT INTO moligod_alerts(name,low,high,synced_at,verified) VALUES(?,?,?,?,1) "
                        "ON CONFLICT(name) DO UPDATE SET low=excluded.low, high=excluded.high, "
                        "synced_at=excluded.synced_at, verified=1",
                        (a["name"], a["low"], a["high"], timeutil.fmt(timeutil.now())))
                    if mname != a["name"]:
                        name_map[a["name"]] = mname
                        db.kv_set("moligod_name_map", name_map)
                    done.append({**a, "moligod_name": mname})
                    event(log, logging.INFO, "提醒已设置并校验", node="sync_alerts", item=a["name"],
                          low=a["low"], high=a["high"])
            finally:
                ctx.close()
    except SyncAbort as e:
        reason = str(e)
    except Exception as e:  # Playwright 的各种异常：一律视为失败，不重试
        reason = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"

    done_names = {d["name"] for d in done}
    unmatched_names = {u["name"] for u in res["unmatched"]}
    res["synced"] = done
    res["manual"] = [a for a in changed if a["name"] not in done_names] if reason else \
        [a for a in changed if a["name"] in unmatched_names] + overflow
    parts = []
    if done:
        parts.append(f"已自动设置并校验 {len(done)} 个提醒")
    if reason:
        parts.append(f"同步中止：{reason}。未完成的请按下面手动设置")
        event(log, logging.WARNING, f"moligod 同步中止: {reason}", node="sync_alerts")
    if res["unmatched"]:
        for u in res["unmatched"]:
            c = "、".join(u["candidates"]) or "无相近名称"
            parts.append(f"「{u['name']}」在 moligod 上没匹配到（候选：{c}），"
                         f"请在 config.yaml 的 watchlist 里给它加 moligod_name")
    if overflow:
        parts.append(f"单次操作上限 {max_ops}，还有 {len(overflow)} 个留到下次")
    res["status"] = "；".join(parts) + "。" if parts else "无改动。"
    res["ok"] = not reason
    return res


def _open_dialog(page, mname: str, PWTimeout):
    btn = page.get_by_role("button", name=f"设置 {mname} 的提醒", exact=True)
    if btn.count() == 0:
        raise SyncAbort(f"找不到按钮“设置 {mname} 的提醒”")
    btn.first.scroll_into_view_if_needed()
    btn.first.click()
    low = page.get_by_label(LABEL_LOW, exact=False)
    high = page.get_by_label(LABEL_HIGH, exact=False)
    try:
        low.first.wait_for(state="visible")
        high.first.wait_for(state="visible")
    except PWTimeout:
        raise SyncAbort("提醒弹窗里没有找到“低于(≤)提醒 / 高于(≥)提醒”输入框（可能未登录或页面改版）") from None
    return low.first, high.first


def _set_one(page, mname: str, a: dict, PWTimeout) -> None:
    low, high = _open_dialog(page, mname, PWTimeout)
    low.fill(str(int(round(a["low"]))))
    high.fill(str(int(round(a["high"]))))
    _choose_trigger_mode(page)
    save = page.get_by_role("button", name=LABEL_SAVE)
    if save.count() == 0:
        raise SyncAbort("找不到“保存提醒”按钮")
    save.first.click()
    try:
        low.wait_for(state="hidden", timeout=8000)
    except PWTimeout:
        page.keyboard.press("Escape")       # 弹窗没自动关：手动关掉，后面靠读回校验判断是否保存成功


def _choose_trigger_mode(page) -> None:
    """提醒模式选“触发”（不要选“持续”）。控件类型未知，按常见的三种形态依次尝试。"""
    radio = page.get_by_role("radio", name=MODE_TRIGGER)
    if radio.count():
        radio.first.check()
        return
    combo = page.get_by_label("提醒模式")
    if combo.count():
        try:
            combo.first.select_option(label=MODE_TRIGGER)
            return
        except Exception:
            combo.first.click()
            opt = page.get_by_role("option", name=MODE_TRIGGER)
            if opt.count():
                opt.first.click()
                return
    btn = page.get_by_role("button", name=MODE_TRIGGER, exact=True)
    if btn.count():
        btn.first.click()
        return
    raise SyncAbort("找不到提醒模式“触发”选项")


def _read_one(page, mname: str, PWTimeout) -> tuple[float | None, float | None]:
    low, high = _open_dialog(page, mname, PWTimeout)
    vals = _parse_value(low.input_value()), _parse_value(high.input_value())
    page.keyboard.press("Escape")
    return vals


def login(cfg) -> str:
    """打开浏览器让你手动登录一次；登录状态保存在本机的浏览器配置目录里，程序不接触账号密码。

    自动同步停用期间直接返回 UNAVAILABLE_TEXT，不打开浏览器（命令行据此返回非 0）。
    """
    if not AUTO_SYNC_AVAILABLE:
        return UNAVAILABLE_TEXT
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return "未安装 Playwright。请先执行：pip install playwright  然后  python -m playwright install chromium"
    pdir = profile_dir(cfg)
    pdir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(pdir), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg.get("moligod.url"), wait_until="domcontentloaded")
        print("浏览器已打开。请在浏览器里手动登录 moligod，确认能看到“设置 XXX 的提醒”按钮后，")
        input("回到这个窗口按回车键结束（不要直接关浏览器）……")
        ctx.close()
    return f"登录状态已保存到 {pdir}（该目录已被 .gitignore 排除，不要分享给别人）。"
