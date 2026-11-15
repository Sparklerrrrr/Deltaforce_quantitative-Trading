"""首页的任务清单（声明式）。

学 MaaEnd 的 interface.json：界面上显示什么任务、每个任务的说明和标签，都写在这里，
界面按这份清单自动生成，不在 HTML/JS 里写死。每个任务的“按钮”对应 api.py 里的一个动作 id。

字段：
  id       任务 id
  icon     图标名（static/app.js 里的 ICONS）
  title    标题
  chips    灰色说明标签
  cost     是否会花钱（真实接口 / 大模型）。界面会显示一个“计费”标记
  actions  [(按钮文字, 动作 id, 样式)]，样式：primary / 空
"""
from __future__ import annotations

HOME_TASKS: list[dict] = [
    {"id": "demo", "icon": "play", "title": "演示一遍", "cost": False,
     "chips": ["模拟数据", "只用规则", "不花接口次数"],
     "actions": [("开始演示", "demo", "primary")]},
    {"id": "run_rules", "icon": "rules", "title": "运行（只用规则）", "cost": True,
     "chips": ["每日流程", "生成计划", "约 13～21 次接口"],
     "actions": [("运行", "run_rules", "")]},
    {"id": "run_llm", "icon": "spark", "title": "运行（规则 + 大模型）", "cost": True,
     "chips": ["每日流程", "调用大模型", "按 token 计费"],
     "actions": [("运行", "run_llm", "")]},
    {"id": "status", "icon": "gauge", "title": "查看状态", "cost": False,
     "chips": ["密钥是否设置", "接口用量", "数据覆盖"],
     "actions": [("查看", "status", "")]},
    {"id": "probe", "icon": "probe", "title": "接口实测", "cost": True,
     "chips": ["约 11～13 次", "上限 15 次", "生成实测报告"],
     "actions": [("实测", "probe", "")]},
    {"id": "mail", "icon": "mail", "title": "邮件", "cost": False,
     "chips": ["回复计划邮件即可记账"],
     "actions": [("检查回复", "mail_check", ""), ("读取并记账", "mail_poll", ""), ("测试邮件", "mail_test", "")]},
]

REVIEW_TASKS: list[dict] = [
    {"id": "perf", "icon": "chart", "title": "绩效", "cost": False,
     "chips": ["全部 / 近 7 天", "到期建议打分"],
     "actions": [("全部绩效", "perf", ""), ("近 7 天", "perf7", ""), ("立即打分", "review", "")]},
    {"id": "reflect", "icon": "refresh", "title": "反思", "cost": False,
     "chips": ["根据结果建议调参", "不用模型"],
     "actions": [("反思记录", "reflect_list", ""), ("现在做一次", "reflect_run", "")]},
]


def as_json() -> dict:
    def conv(tasks):
        return [{**t, "actions": [{"label": a, "action": b, "style": c} for a, b, c in t["actions"]]} for t in tasks]
    return {"home": conv(HOME_TASKS), "review": conv(REVIEW_TASKS)}
