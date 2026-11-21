"""把交易计划写成一封邮件。

目标：把人工操作压到最短。
- 标题就是指令，锁屏预览能看完；
- 正文按游戏里的操作顺序写：搜什么、什么价、买多少；
- 每晚只发一封。
注意：正文里凡是以“买/卖”开头的行都带有 ≤ ≥ 「」 符号，回复解析器会跳过它们，
所以即使邮件客户端把原文引用进回复，也不会被误记成成交。
"""
from __future__ import annotations

from .. import timeutil


def short_name(cfg, name: str) -> str:
    for w in cfg.watchlist:
        if w["name"] == name:
            return w["alias"][0] if w["alias"] else name.split()[-1]
    return name.split()[-1]


def _money(x) -> str:
    x = float(x or 0)
    if abs(x) >= 10000:
        return f"{x / 10000:.1f}万"
    return f"{x:.0f}"


def build_subject(cfg, plan: dict) -> str:
    tag = "【模拟计划】" if plan.get("paper") else "【计划】"
    buys = [i for i in plan["items"] if i["action"] == "buy"]
    sells = plan.get("sells") or []
    parts = [f"买 {short_name(cfg, i['name'])} ≤{i['buy_price']} ×{i['qty']}" for i in buys[:3]]
    parts += [f"卖 {short_name(cfg, s['name'])} ≥{s['sell_price']}" for s in sells[:2]]
    more = max(len(buys) - 3, 0) + max(len(sells) - 2, 0)
    if not parts:
        parts = ["今日无操作，全部观望"]
    if more:
        parts.append(f"另 {more} 条见正文")
    parts.append(f"有效至 {timeutil.short(plan['valid_until'])}")
    return tag + "｜".join(parts)


def build_body(cfg, plan: dict, *, manual_alerts: list[dict] | None = None,
               alert_status: str | None = None, receipts: list[str] | None = None,
               review_text: str | None = None, system_lines: list[str] | None = None) -> str:
    L: list[str] = []
    buys = [i for i in plan["items"] if i["action"] == "buy"]
    watches = [i for i in plan["items"] if i["action"] != "buy"]
    sells = plan.get("sells") or []

    if plan.get("paper"):
        L += ["※ 模拟模式：以下建议只记录、用于打分，不计入真实持仓。想实盘时把 config.yaml 的 "
              "general.paper_mode 改成 false。", ""]
    L.append(f"有效期：{timeutil.short(plan['valid_from'])} → {timeutil.short(plan['valid_until'])}"
             f"（过期作废）")
    if plan.get("market_view"):
        L.append(f"整体判断：{plan['market_view']}")
    L.append("")

    n = 0
    if sells:
        L.append("━━ 先卖：持仓挂单" + ("（模拟账本里的持仓）" if plan.get("paper") else "") + " ━━")
        for s in sells:
            n += 1
            L.append(f"{n}. 搜索「{_search(cfg, s['name'])}」 卖出价 ≥{s['sell_price']}  数量 {s['qty']}")
            L.append(f"   持仓均价 {s['avg_cost']:.0f}，回本价 {s['breakeven']}；跌破 ≤{s['stop_price']} 考虑止损")
            if s.get("note"):
                L.append(f"   注意：{s['note']}")
        L.append("")
    if buys:
        L.append("━━ 再买：交易行搜索 → 挂买单 ━━")
        for i in buys:
            n += 1
            L.append(f"{n}. 搜索「{i.get('search') or i['name']}」 买入价 ≤{i['buy_price']}  数量 {i['qty']}"
                     f"（约 {_money(i.get('budget'))}）")
            L.append(f"   买到后：卖出价 ≥{i['sell_price']}（回本价 {i.get('breakeven')}，"
                     f"扣费后约 {i.get('exp_net_return', 0):+.1%}）｜止损 ≤{i['stop_price']}"
                     f"（触发则实亏约 {i.get('stop_net_return', 0):.0%}）")
            hint = []
            if i.get("current"):
                hint.append(f"现价 {i['current']:.0f}")
            if i.get("low_hours"):
                hint.append(f"低点多见于 {'/'.join(map(str, i['low_hours']))} 点")
            if hint:
                L.append(f"   参考：{'，'.join(hint)}")
            L.append(f"   理由：{i.get('reason') or '—'}")
            L.append(f"   风险：{i.get('risk') or '—'}")
            for g in i.get("guard_notes") or []:
                L.append(f"   护栏：{g}")
        L.append("")
    if not buys and not sells:
        L += ["今天没有值得操作的品种，全部观望。", ""]
    if watches:
        L.append("━━ 观望 ━━")
        for i in watches:
            ref = ""
            if i.get("buy_price") and i.get("sell_price"):
                ref = f"（参考线 {i['buy_price']}/{i['sell_price']}）"
            notes = "；".join(i.get("guard_notes") or [])
            L.append(f"· {i['name']}{ref}：{notes or i.get('reason') or '—'}")
        L.append("")

    if plan.get("short_term") or plan.get("long_term"):
        L.append("━━ 计划展望 ━━")
        for s in plan.get("short_term") or []:
            L.append(f"· 短期 {s.get('date', '')}：{s['note']}")
        for s in plan.get("long_term") or []:
            L.append(f"· 长期 {s.get('week', '')}：{s['note']}")
        if plan.get("next_run"):
            L.append(f"· 模型建议下次运行：{plan['next_run']}（{plan.get('next_run_reason') or ''}）")
        L.append("")

    L.append("━━ moligod 价格提醒 ━━")
    if alert_status:
        L.append(alert_status)
    if manual_alerts:
        L.append("请手动设置：打开 moligod 子弹实时价格页，点品种旁的“设置 XXX 的提醒”，"
                 "按下面填“低于(≤)提醒”和“高于(≥)提醒”两个数，提醒模式选“触发”，再点“保存提醒”。")
        for a in manual_alerts:
            lo = f"低于(≤) {a['low']:.0f}" if a.get("low") else "低于(≤) 不设"
            hi = f"高于(≥) {a['high']:.0f}" if a.get("high") else "高于(≥) 不设"
            L.append(f"· {a['name']}：{lo}（{a.get('low_role', '买入线')}）  {hi}（{a.get('high_role', '卖出线')}）")
        roles = {a.get("low_role", "买入线") for a in manual_alerts}
        legend = []
        if "买入线" in roles:
            legend.append("买入线 = 价格跌到这里提醒你，可按计划买入")
        if "止损线" in roles:
            legend.append("止损线 = 你持有这个品种，价格跌到这里提醒你按计划止损卖出")
        legend.append("卖出线 = 价格涨到这里提醒你按计划卖出")
        L.append("说明：" + "；".join(legend) + "。提醒只是通知，买卖仍由你在游戏里手动操作。")
    L.append("")

    L.append("━━ 回复本邮件即可记账 ━━")
    L.append("一行一笔，格式：方向 品种 数量 成交价。方向写 买 或 卖，品种可写简称。")
    if plan["items"]:
        e = buys[0] if buys else plan["items"][0]
        ex, eq = short_name(cfg, e["name"]), e.get("qty") or 100
        eb, es = e.get("buy_price") or 3550, e.get("sell_price") or 4300
    else:
        ex, eq, eb, es = "PS12B", 200, 3550, 4300
    L.append(f"例：「买 {ex} {eq} {eb}」「卖 {ex} {eq} {es}」（回复时不要带引号）")
    L.append("不回复 = 视为没执行。")
    if plan.get("paper"):
        L.append("这是模拟计划：回复它的成交只记入模拟账本，不影响真实持仓、余额和真实盈亏。")
    L.append("")

    if receipts:
        L.append("━━ 记账回执 ━━")
        L += [f"· {r}" for r in receipts]
        L.append("")
    if review_text:
        L.append("━━ 近期复盘 ━━")
        L.append(review_text)
        L.append("")

    warn = list(plan.get("warnings") or []) + list(plan.get("guard_notes") or [])
    if warn:
        L.append("━━ 提示与降级 ━━")
        L += [f"· {w}" for w in warn]
        L.append("")
    # 费率依据 / 风控状态随计划保存（nodes.WritePlan），所以 `dfq plan` 重看旧计划时也有
    system_lines = list(system_lines or []) + [plan[k] for k in ("fee_basis", "risk_status") if plan.get(k)]
    if system_lines:
        L.append("━━ 系统状态 ━━")
        L += [f"· {s}" for s in system_lines]
    return "\n".join(L).rstrip() + "\n"


def _search(cfg, name: str) -> str:
    for w in cfg.watchlist:
        if w["name"] == name:
            return w["search"]
    return name


def build_failure_reply(trades_ok: list[str], errors: list[str]) -> tuple[str, str]:
    subject = "【记账失败】有内容没看懂，请重新回复"
    lines = []
    if trades_ok:
        lines.append("已记账：")
        lines += [f"· {t}" for t in trades_ok]
        lines.append("")
    lines.append("没看懂：")
    lines += [f"· {e}" for e in errors]
    lines += ["", "请按这个格式重新回复没看懂的部分（一行一笔）：",
              "方向 品种 数量 成交价      方向写 买 或 卖",
              "已记账的不用重发。"]
    return subject, "\n".join(lines) + "\n"
