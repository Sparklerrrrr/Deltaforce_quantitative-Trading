"""“设置”页里可以直接修改的 config.yaml 配置项（声明式清单）。

界面按这份清单自动生成表单；后端的 Api.set_config 也只接受这里列出的键（白名单）。
这里的 min / max 只是给界面的提示，**权威校验是 config_check**：改动后的整份配置会按启动时同样的严格规则
再校验一遍，不通过就不写盘。所以这里写宽了不会放进坏值，写窄了只会让界面多拦一次。

每项字段：
  key           配置项（点号路径）
  group         所属分组 id（见 GROUPS）
  label / hint  显示名、说明
  type          bool / text / int / float / percent（小数，界面按百分比显示）/ choice
  min/max/step  数值范围提示
  choices       choice 的选项 [[值, 显示名], ...]
  placeholder   文本框的占位提示
  confirm_on    切到这个值时需要用户确认；confirm_text 是确认文字
"""
from __future__ import annotations

GROUPS: list[dict] = [
    {"id": "trade", "title": "记账与计划", "hint": "模拟 / 实盘、运行频率、计划有效期"},
    {"id": "fee", "title": "费率", "hint": "卖出时交易行扣的费用，回本价按它计算"},
    {"id": "risk", "title": "风控限额", "hint": "代码强制执行，大模型绕不过"},
    {"id": "xtc", "title": "行情接口", "hint": "小涛查按次计费：开得越多，每天用的次数越多"},
    {"id": "llm", "title": "大模型", "hint": "API Key 在“密钥”一栏单独填写，不写进配置文件"},
    {"id": "news", "title": "新闻搜索", "hint": "需要在“密钥”一栏填写新闻搜索 Key"},
    {"id": "mail", "title": "邮件", "hint": "授权码在“密钥”一栏单独填写，不写进配置文件"},
]

SPEC: list[dict] = [
    # —— 记账与计划 ——
    {"key": "general.paper_mode", "group": "trade", "label": "模拟模式", "type": "bool",
     "hint": "开着时计划标“模拟”，邮件回复和界面记账默认记入模拟账本，不影响真实持仓和余额",
     "confirm_on": False,
     "confirm_text": "切换到实盘记账？之后的计划不再标‘模拟’，邮件回复和界面记账默认记入真实账本。"},
    {"key": "general.run_every_days", "group": "trade", "label": "每几天运行一次", "type": "int",
     "min": 1, "max": 30, "step": 1, "hint": "计划任务每晚都会触发，没到日子程序会自己跳过"},
    {"key": "general.plan_valid_hours", "group": "trade", "label": "计划有效期（小时）", "type": "int",
     "min": 1, "max": 72, "step": 1, "hint": "过期的计划作废，不能超过风控里的最长有效期"},
    # —— 费率 ——
    {"key": "fees.sell_fee_rate", "group": "fee", "label": "卖出费率", "type": "percent",
     "min": 0, "max": 0.5, "step": 0.001,
     "hint": "13% 是默认值，不是官方公告。可以运行“核对费率”，或卖出时填写实际到手金额让程序校准"},
    # —— 风控 ——
    {"key": "risk.assumed_capital", "group": "risk", "label": "假定总资金", "type": "int",
     "min": 0, "step": 100000, "hint": "还没填余额时，按这个数估算仓位"},
    {"key": "risk.max_daily_spend_pct", "group": "risk", "label": "每日最大投入", "type": "percent",
     "min": 0.01, "max": 1, "step": 0.01, "hint": "占总资产的比例"},
    {"key": "risk.max_position_pct", "group": "risk", "label": "单品种最大持仓", "type": "percent",
     "min": 0.01, "max": 1, "step": 0.01, "hint": "占总资产的比例"},
    {"key": "risk.max_total_exposure_pct", "group": "risk", "label": "总仓位上限", "type": "percent",
     "min": 0.01, "max": 1, "step": 0.01, "hint": "占总资产的比例"},
    {"key": "risk.llm_max_adjust_pct", "group": "risk", "label": "大模型调整幅度上限", "type": "percent",
     "min": 0, "max": 0.5, "step": 0.01, "hint": "模型对规则给出的价格最多能调多少"},
    {"key": "risk.max_loss_per_trade_pct", "group": "risk", "label": "单笔最大亏损", "type": "percent",
     "min": 0.001, "max": 0.5, "step": 0.005,
     "hint": "买入后如果打到止损线，实亏（含卖出费）不超过总资产的这个比例，超了自动减少数量"},
    {"key": "risk.max_holding_days", "group": "risk", "label": "最长持有天数", "type": "int",
     "min": 1, "max": 365, "step": 1, "hint": "超过后卖出线降到回本价；超过两倍时建议按现价卖出"},
    {"key": "risk.max_cumulative_loss_pct", "group": "risk", "label": "累计亏损上限", "type": "percent",
     "min": 0.01, "max": 1, "step": 0.01, "hint": "统计窗口内已实现亏损超过资金的这个比例，就暂停新的买入建议，直到你确认"},
    {"key": "risk.loss_window_days", "group": "risk", "label": "累计亏损统计窗口（天）", "type": "int",
     "min": 1, "max": 365, "step": 1, "hint": "累计亏损按最近多少天计算"},
    {"key": "risk.halt_after_losses", "group": "risk", "label": "连续亏损几笔后暂停", "type": "int",
     "min": 1, "max": 100, "step": 1, "hint": "最近连续这么多笔平仓都亏，就暂停新的买入建议（策略可能失效）"},
    {"key": "risk.abnormal_move_pct", "group": "risk", "label": "异常波动阈值（%）", "type": "float",
     "min": 1, "max": 100, "step": 0.5, "hint": "今日涨跌或偏离近 7 日中位价超过这个百分数，当天不买（写 12 表示 12%）"},
    # —— 行情接口 ——
    {"key": "xtc.daily_limit", "group": "xtc", "label": "每日请求上限", "type": "int",
     "min": 1, "max": 2000, "step": 1, "hint": "超过即停并报警。正常每晚约 13～15 次"},
    {"key": "xtc.fetch_minute", "group": "xtc", "label": "拉取分钟曲线", "type": "bool",
     "hint": "接口只给最近 24 小时，必须每晚拉才能攒出历史"},
    {"key": "xtc.fetch_hour_forecast", "group": "xtc", "label": "拉取小时曲线和平台预测", "type": "bool",
     "hint": "每个品种每次多 1 次请求"},
    {"key": "watch.enabled", "group": "xtc", "label": "每小时异常波动检查", "type": "bool",
     "hint": "每天约多 48 次请求，开启前先调高每日请求上限"},
    {"key": "watch.move_pct", "group": "xtc", "label": "异常波动幅度（%）", "type": "float",
     "min": 1, "max": 50, "step": 0.5, "hint": "写 8 表示 8%"},
    # —— 大模型 ——
    {"key": "llm.enabled", "group": "llm", "label": "启用大模型", "type": "bool",
     "hint": "需要先填好服务地址、模型名和 API Key"},
    {"key": "llm.base_url", "group": "llm", "label": "服务地址", "type": "text",
     "placeholder": "https://你的服务商/v1", "hint": "必须是 https://，不要带 /chat/completions，也不要把 Key 写进地址"},
    {"key": "llm.model", "group": "llm", "label": "模型名", "type": "text", "placeholder": "服务商给的模型名"},
    {"key": "llm.max_tokens", "group": "llm", "label": "最大输出长度（token）", "type": "int",
     "min": 256, "max": 200000, "step": 256},
    {"key": "llm.json_mode", "group": "llm", "label": "要求返回 JSON", "type": "bool",
     "hint": "服务商不支持 response_format 时关掉"},
    # —— 新闻搜索 ——
    {"key": "intel.news.enabled", "group": "news", "label": "启用新闻搜索", "type": "bool"},
    {"key": "intel.news.max_results", "group": "news", "label": "每次最多几条", "type": "int", "min": 1, "max": 20, "step": 1},
    {"key": "intel.news.days", "group": "news", "label": "搜索最近几天", "type": "int", "min": 1, "max": 30, "step": 1},
    # —— 邮件 ——
    {"key": "mail.enabled", "group": "mail", "label": "启用邮件", "type": "bool",
     "hint": "关闭时计划只写到 data/outbox/latest.txt"},
    {"key": "mail.owner_address", "group": "mail", "label": "你的邮箱", "type": "text",
     "placeholder": "you@example.com", "hint": "计划发到这里；程序也只认这个地址发来的回复"},
    {"key": "mail.from_address", "group": "mail", "label": "发件邮箱", "type": "text",
     "placeholder": "可以和上面相同：自己发给自己"},
    {"key": "mail.smtp_host", "group": "mail", "label": "发信服务器", "type": "text", "placeholder": "smtp.qq.com"},
    {"key": "mail.smtp_port", "group": "mail", "label": "发信端口", "type": "int", "min": 1, "max": 65535, "step": 1},
    {"key": "mail.smtp_user", "group": "mail", "label": "发信账号", "type": "text", "placeholder": "一般就是发件邮箱地址"},
    {"key": "mail.imap_host", "group": "mail", "label": "收信服务器", "type": "text", "placeholder": "imap.qq.com"},
    {"key": "mail.imap_port", "group": "mail", "label": "收信端口", "type": "int", "min": 1, "max": 65535, "step": 1},
    {"key": "mail.imap_user", "group": "mail", "label": "收信账号", "type": "text", "placeholder": "不填则同发信账号"},
    {"key": "mail.confirm_success", "group": "mail", "label": "记账成功后单独回信", "type": "bool",
     "hint": "默认不回，回执合并进当晚的计划邮件"},
    {"key": "mail.reply_max_age_days", "group": "mail", "label": "回复有效期（天）", "type": "float",
     "min": 0.5, "max": 30, "step": 0.5,
     "hint": "计划邮件发出超过这么多天后，对它的回复不再记账（防止旧邮件被重放），改用界面或命令行记账"},
]

SPEC_BY_KEY: dict[str, dict] = {s["key"]: s for s in SPEC}
TYPES = ("bool", "text", "int", "float", "percent", "choice")
TEXT_MAX = 300
