"""阶段 0：接口实测探测（python -m dfq probe）。

用最少的真实请求确认公开文档里的信息，硬上限 15 次（超过立即停止）。
结果写到 docs/接口探测报告-实测.md（不含任何密钥）和 data/probe/ 下的原始响应。

探测顺序（预计 11～13 次）：
  筛选项 0 次 → 子弹列表 3～5 次 → 分钟曲线 1 → 小时曲线 2（含/不含预测）→ 天曲线 1
  → 今日低价预测 2（grade=5 与 grade=0）→ 昨日最高收益 1 → 自选包收益 1
"""
from __future__ import annotations

import json
from pathlib import Path

from . import timeutil
from .sources import xtc as X

HARD_LIMIT = 15


def _shape(o, depth=0):
    """只保留结构和少量样例，避免报告里堆满数据。"""
    if isinstance(o, dict):
        if depth > 4:
            return "{…}"
        return {k: _shape(v, depth + 1) for k, v in list(o.items())[:20] if not str(k).startswith("_")}
    if isinstance(o, list):
        if not o:
            return []
        return [_shape(o[0], depth + 1), f"…共 {len(o)} 项"]
    return o


def run(cfg, db, token: str | None, out_dir: Path, transport=None) -> dict:
    """执行探测。返回 {"ok", "used", "report_path", "lines"}。"""
    if not token and transport is None:
        return {"ok": False, "used": 0, "report_path": None,
                "lines": ["未设置环境变量 DFQ_XTC_TOKEN，探测没有执行，也没有消耗任何次数。",
                          "Windows 设置方法（PowerShell，设置后重开终端）：",
                          '  [Environment]::SetEnvironmentVariable("DFQ_XTC_TOKEN", "你的token", "User")']}
    cfg.set("xtc.retries", 0)                      # 探测不重试：重试也会扣次
    client = X.XtcClient(cfg, db, token or "MOCK", transport=transport)
    before = db.usage_on(client.provider)
    budget = min(HARD_LIMIT, client.remaining_today())
    # 把客户端的日上限临时压到“已用 + 15”，即使代码有疏漏也不可能超
    client.daily_limit = min(client.daily_limit, before + HARD_LIMIT)
    raw_dir = cfg.data_dir / "probe"
    raw_dir.mkdir(parents=True, exist_ok=True)
    now = timeutil.now()
    R: list[str] = [f"# 小涛查接口探测报告（实测）", "",
                    f"探测时间：{timeutil.fmt_min(now)}（北京时间）", ""]
    findings: dict = {}

    def used() -> int:
        return db.usage_on(client.provider) - before

    def step(title: str, endpoint: str, fn, cost: int = 1):
        """执行一步探测；额度不够或出错都只记录，不中断后面的步骤。"""
        R.append(f"## {title}")
        if cost and used() + cost > budget:
            R.extend([f"- 跳过：会超过本次探测上限 {budget} 次", ""])
            return None
        try:
            resp = fn()
        except X.DailyLimitExceeded as e:
            R.extend([f"- 跳过：{e}", ""])
            return None
        except X.XtcError as e:
            R.extend([f"- **失败**：{e}", ""])
            return None
        (raw_dir / f"{endpoint}_{now:%Y%m%d%H%M}.json").write_text(
            json.dumps({k: v for k, v in resp.items() if not k.startswith("_")}, ensure_ascii=False),
            encoding="utf-8")
        R.append(f"- 路径：`GET /v1/sjz_api/{endpoint}`　结果：成功（code=0）"
                 + ("　来自缓存，未扣次" if resp.get("_from_cache") else ""))
        return resp

    # 1) 筛选项
    resp = step("1. 全物品列表 Pro - 筛选项", "item_list_pro_key", client.filter_options_raw, cost=0)
    key1, key2 = str(cfg.get("xtc.ammo_key1")), str(cfg.get("xtc.ammo_key2"))
    if resp is not None:
        opts = resp.get("data") or []
        R.append(f"- 一级分类：{'、'.join(str(o.get('label')) for o in opts if isinstance(o, dict))}")
        cats = X.find_category(opts, "子弹") or X.find_category(opts, "弹")
        if cats:
            key1 = str(cats[0]["key1"])
            k2 = [k for k in cats[0]["key2"] if k]
            key2 = "全部" if "全部" in k2 or not k2 else str(k2[0])
            R.append(f"- **子弹分类索引**：key1=`{key1}`，key2 可选：{'、'.join(map(str, k2)) or '（无）'}")
            findings["ammo_key1"], findings["ammo_key2"] = key1, key2
        else:
            R.append("- **没有找到名字含“子弹”的分类**，下面的列表探测沿用配置值，结果可能为空")
        R.append(f"- 是否扣次：本步前后计数未变（文档称不扣次；以平台后台为准）")
    R.append("")

    # 2) 子弹列表
    R.append("## 2. 全物品列表 Pro（子弹分类）")
    items: list[dict] = []
    try:
        if used() + 1 > budget:
            raise X.DailyLimitExceeded("超过本次探测上限")
        cfg.set("xtc.list_max_pages", min(int(cfg.get("xtc.list_max_pages", 8)), 5))
        raw = client.list_all(key1, key2)
        items = [it for it in (X.parse_list_item(d) for d in raw) if it]
        (raw_dir / f"item_list_pro_{now:%Y%m%d%H%M}.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        R.append(f"- 参数：key1=`{key1}` key2=`{key2}` limit={cfg.get('xtc.list_limit')} sort=`2-2`")
        R.append(f"- 返回 {len(raw)} 条，其中可在交易行交易（oid≠0）的 {len(items)} 条")
        if raw:
            R.append(f"- 字段：{'、'.join(sorted(raw[0].keys()))}")
        grades: dict[int, int] = {}
        for it in items:
            grades[it["grade"]] = grades.get(it["grade"], 0) + 1
        R.append(f"- 各品质数量：{'，'.join(f'{g}级 {n} 种' for g, n in sorted(grades.items()))}")
        findings["ammo_count"] = len(items)
    except X.XtcError as e:
        R.append(f"- **失败**：{e}")
    R.append("")

    sample = None
    watch_names = {w["name"].lower(): w for w in cfg.watchlist}
    for it in items:
        if it["name"].lower() in watch_names:
            sample = it
            break
    if sample is None and items:
        sample = items[0]
    if sample is None:
        w = cfg.watchlist[0]
        sample = {"oid": w["oid"], "name": w["name"]}
        R += [f"（列表没拿到，后续用配置里的 {w['name']} oid={w['oid']} 试探）", ""]
    oid = sample["oid"]

    # 3) 曲线
    resp = step(f"3. 价格曲线 - 分钟刻度（{sample['name']}，oid={oid}）", "minute", lambda: client.minute(oid))
    if resp is not None:
        pts = X.parse_minute_curve(resp, now)
        d = resp.get("data") or {}
        R.append(f"- 数据键：{'、'.join(d.keys())}；标签示例：`{(d.get('a') or ['—'])[0]}` … `{(d.get('a') or ['—'])[-1]}`")
        if pts:
            span = (pts[-1][0] - pts[0][0]).total_seconds() / 3600
            step_min = span * 60 / max(len(pts) - 1, 1)
            R.append(f"- 共 {len(pts)} 个点，时间范围 {timeutil.fmt_min(pts[0][0])} → {timeutil.fmt_min(pts[-1][0])}"
                     f"（约 {span:.1f} 小时，平均间隔 {step_min:.1f} 分钟）")
            R.append(f"- 时间补全校验：最后一个点距探测时刻 {(now - pts[-1][0]).total_seconds() / 60:.0f} 分钟")
    R.append("")

    resp = step("4. 价格曲线 - 小时刻度（不带预测）", "hour", lambda: client.hour(oid, False))
    if resp is not None:
        d = resp.get("data") or {}
        R.append(f"- 数据键：{'、'.join(d.keys())}；{len(d.get('a') or [])} 个标签，"
                 f"`{(d.get('a') or ['—'])[0]}` … `{(d.get('a') or ['—'])[-1]}`")
    R.append("")
    resp = step("5. 价格曲线 - 小时刻度（isYC=1，带未来预测）", "hour", lambda: client.hour(oid, True))
    if resp is not None:
        d = resp.get("data") or {}
        R.append(f"- 数据键：{'、'.join(d.keys())}")
        R.append(f"- 各数组长度：{ {k: len(v) for k, v in d.items() if isinstance(v, list)} }")
        rows = X.parse_hour_curve(resp, now)
        fc = [r for r in rows if r["forecast"]]
        R.append(f"- 本程序解析出 {len(rows)} 行，其中预测 {len(fc)} 行"
                 + (f"（{timeutil.fmt_min(fc[0]['ts'])} → {timeutil.fmt_min(fc[-1]['ts'])}）" if fc else
                    "。**如果上面有 yc 数组但这里是 0 行，说明预测结构与假设不符，需要改 parse_hour_curve**"))
    R.append("")
    resp = step("6. 价格曲线 - 天刻度", "day", lambda: client.day(oid))
    if resp is not None:
        rows = X.parse_day_curve(resp, now)
        d = resp.get("data") or {}
        R.append(f"- 数据键：{'、'.join(d.keys())}；共 {len(rows)} 天"
                 + (f"，{rows[0]['d']} → {rows[-1]['d']}" if rows else ""))
    R.append("")

    # 4) 子弹分析类
    resp5 = step("7. 今日倒子弹低价预测（grade=5）", "ammo_day", lambda: client.ammo_day(5))
    if resp5 is not None:
        rows = X.parse_ammo_day(resp5)
        R.append(f"- 解析出 {len(rows)} 条；涉及品质：{sorted({r['grade'] for r in rows})}；"
                 f"小时分布：{sorted({r['hour'] for r in rows if r['hour'] is not None})}")
    R.append("")
    resp0 = step("8. 今日倒子弹低价预测（grade=0，看是否代表“全部”）", "ammo_day", lambda: client.ammo_day(0))
    if resp0 is not None:
        rows = X.parse_ammo_day(resp0)
        R.append(f"- 解析出 {len(rows)} 条；涉及品质：{sorted({r['grade'] for r in rows})}")
        R.append("- 结论：" + ("grade=0 返回多个品质，可以一次拉全，配置里 xtc.grades 可设为 [0]"
                              if len({r['grade'] for r in rows}) > 1 else
                              "grade=0 不是“全部”，xtc.grades 需要按品质分别请求"))
    R.append("")
    resp = step("9. 昨日倒子弹最高收益（grade=5）", "ammo_zr_yc", lambda: client.ammo_yesterday(5))
    if resp is not None:
        rows = X.parse_generic_items(resp)
        R.append(f"- 文档没有给返回示例。实际结构（节选）：")
        R.append("```json")
        R.append(json.dumps(_shape(resp.get("data")), ensure_ascii=False, indent=1)[:1500])
        R.append("```")
        R.append(f"- 通用解析器识别出 {len(rows)} 条带名称的记录"
                 + (f"，字段：{'、'.join(sorted(rows[0].keys()))}" if rows else ""))
    R.append("")
    resp = step("10. 子弹自选包收益（grade=5）", "ammo_pack", lambda: client.ammo_pack(5))
    if resp is not None:
        d = resp.get("data")
        R.append(f"- 顶层：{list(d.keys()) if isinstance(d, dict) else type(d).__name__}")
    R.append("")

    # 5) 汇总
    R.append("## 汇总")
    R.append(f"- **本次探测消耗 {used()} 次**（硬上限 {HARD_LIMIT}；本程序口径：每个发出的请求按文档扣次规则计）")
    R.append("- 请到小涛查控制台核对实际扣次，和上面的数字不一致时告诉我。")
    if findings.get("ammo_key1") and (findings["ammo_key1"] != cfg.get("xtc.ammo_key1")):
        R.append(f"- **请修改 config.yaml**：`xtc.ammo_key1: {findings['ammo_key1']}`，"
                 f"`xtc.ammo_key2: {findings.get('ammo_key2')}`")
    if items:
        R.append("")
        R.append("### 关注清单核对")
        by_name = {it["name"].lower(): it for it in items}
        for w in cfg.watchlist:
            live = by_name.get(w["name"].lower())
            if live is None:
                R.append(f"- ❌ 「{w['name']}」不在接口返回的子弹列表里，请核对名称")
            elif w["oid"] is not None and live["oid"] != w["oid"]:
                R.append(f"- ⚠ 「{w['name']}」配置 oid={w['oid']}，实际 oid={live['oid']}（运行时会自动按实际值）")
            else:
                R.append(f"- ✅ 「{w['name']}」oid={live['oid']}，{live['grade']} 级，现价 {live['price']:.0f}")
        R += ["", f"### 子弹 oid 清单（共 {len(items)} 种）", "", "| oid | 名称 | 品质 | 现价 | 旧ID |",
              "|---:|---|---:|---:|---:|"]
        for it in sorted(items, key=lambda x: (-x["grade"], x["name"])):
            R.append(f"| {it['oid']} | {it['name']} | {it['grade']} | {it['price'] or 0:.0f} | {it['legacy_id']} |")
            db.upsert_item(it["oid"], it["name"], it["grade"], it["legacy_id"])
    text = "\n".join(R) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "接口探测报告-实测.md"
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "used": used(), "report_path": str(path), "lines": R, "findings": findings}
