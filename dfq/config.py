"""配置加载。

优先级（后者覆盖前者）：内置默认值 → config.yaml → data/overrides.yaml（你确认过的反思建议）。
密钥不在配置文件里：统一用 `secret("DFQ_XXX")` 从环境变量读。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from .log import SECRET_ENV_NAMES, register_secret

# 默认关注清单。oid 取自小涛查公开文档里的示例数据（未实测）；
# 实际运行时以“名称”匹配接口返回的最新 oid，两者不一致会在日志和邮件里提示。
DEFAULT_WATCHLIST = [
    {"name": "12.7x55mm PS12B", "oid": 626, "alias": ["PS12B"]},
    {"name": "5.7x28mm SS190", "oid": 640, "alias": ["SS190"]},
    {"name": "5.56x45mm M995", "oid": 635, "alias": ["M995"]},
    {"name": "5.45x39mm BS", "oid": 629, "alias": ["BS"]},
    {"name": "7.62x39mm AP", "oid": 652, "alias": ["762AP", "39AP"]},
    {"name": "7.62x51mm M62", "oid": 657, "alias": ["M62"]},
    {"name": "6.8x51mm Hybrid", "oid": 648, "alias": ["Hybrid", "68H"]},
    {"name": "5.8x42mm DVC12", "oid": 646, "alias": ["DVC12"]},
]

DEFAULTS: dict[str, Any] = {
    "general": {
        "data_dir": "data",
        "paper_mode": True,          # 冷启动：只记录打分，不计入真实持仓
        "run_every_days": 1,         # 每 N 天跑一次
        "plan_valid_hours": 24,      # 计划有效期
    },
    "watchlist": DEFAULT_WATCHLIST,
    "fees": {
        "sell_fee_rate": 0.13,       # 默认值，不是实测：用 fee-check 或卖出时填实际到手金额来核对
        "warn_diff": 0.03,           # 按你记录的实际到手金额反推的费率，与配置相差超过它就提示
        "overrides": {},             # 个别品种费率不同时单独设置：{品种名: 费率}
    },
    "xtc": {
        "enabled": True,
        "base_url": "https://orzice.com/workApi",
        "daily_limit": 60,
        "timeout": 15,
        "retries": 2,
        "ammo_key1": "子弹",          # 分类索引，未实测；probe 会从筛选项接口确认
        "ammo_key2": "全部",
        "list_limit": 20,
        "list_max_pages": 8,
        "grades": "auto",            # 低价预测 / 昨日收益 按品质请求；auto = 关注清单里出现的品质
        "fetch_minute": True,
        "fetch_day_bootstrap": True,  # 首次为每个关注品种拉一次天曲线（一次性）
        "fetch_hour_forecast": False, # 小时曲线 + 未来预测（每品种 1 次，默认关）
        "fetch_ammo_day": True,
        "fetch_ammo_yesterday": True,
        "cost": {"item_info_all": 20, "item_price_all": 20, "item_list_pro_key": 0},
        "cache_ttl": {                # 秒
            "item_list_pro": 600, "minute": 600, "hour": 1800, "day": 21600,
            "ammo_day": 3600, "ammo_zr_yc": 21600, "ammo_pack": 3600,
            "item_list_pro_key": 2592000,
            "exchange_pay": 21600,    # fee-check 用：6 小时内重复核对不再花次数
        },
    },
    "features": {
        "window_days": 7,
        "min_minute_points": 200,
    },
    "rules": {
        "buy_quantile": 0.15,
        "sell_quantile": 0.85,
        "min_margin": 0.03,          # 卖出线至少比回本价高 3%
        "stop_pct": 0.10,            # 止损不高于 买入线 × (1 − 10%)
        "stop_buffer": 0.02,         # 且不高于 近期最低价 × (1 − 2%)
        "trend_block_pct": 15.0,     # 7 日跌幅超过 15% → 观望
        "cold_shrink": 0.15,         # 只有日线数据时，阈值向均价收缩的比例
    },
    "risk": {
        "assumed_capital": 3000000,  # 没填余额时用它估算仓位
        "max_daily_spend_pct": 0.5,
        "max_position_pct": 0.25,
        "max_total_exposure_pct": 0.7,
        "lot_size": 10,
        "min_qty": 20,
        "llm_max_adjust_pct": 0.10,  # 模型对规则阈值的调整幅度上限
        "max_valid_hours": 72,
        # —— 资金损失约束（代码强制，在模型输出之后执行）——
        "max_loss_per_trade_pct": 0.03,   # 单笔买入打到止损线的实亏（含卖出费）不超过总资产的 3%
        "max_holding_days": 7,            # 持仓超过 7 天：卖出线降到回本价；超过 14 天：建议按现价卖出
        "max_cumulative_loss_pct": 0.10,  # 窗口内已实现亏损超过资金基准的 10% → 暂停新的买入
        "loss_window_days": 30,
        "halt_after_losses": 3,           # 最近连续 3 笔平仓都亏 → 暂停新的买入（策略可能失效）
        "abnormal_move_pct": 12.0,        # 今日涨跌或相对近 7 日中位价偏离超过 12% → 当天不买
    },
    "llm": {
        "enabled": False,
        "base_url": "",
        "model": "",
        "timeout": 120,
        "retries": 1,
        "max_tokens": 4000,
        "temperature": None,
        "json_mode": True,
        "allow_http_local": False,   # 只有模型跑在本机（localhost/127.0.0.1）时才可能需要打开
        "extra_body": {},            # 服务商专有的请求参数，原样合并进请求体（如 DeepSeek 关闭思考模式）
    },
    "intel": {
        "bilibili": {
            "enabled": False,
            "ups": [
                {"name": "moligod", "mid": None},
                {"name": "十七-_-三角洲行动", "mid": None},
            ],
            "max_videos_per_up": 3,
            "min_interval_sec": 5,
        },
        "news": {
            "enabled": False,
            "provider": "tavily",
            "queries": ["三角洲行动 子弹 平衡调整 公告", "三角洲行动 版本更新 交易行"],
            "max_results": 5,
            "days": 3,
        },
        "manual_dir": "manual_notes",
        "opinion_horizon_days": 3,
        "opinion_min_move_pct": 2.0,
    },
    "mail": {
        "enabled": False,
        "backend": "smtp_imap",      # smtp_imap | file
        "owner_address": "",         # 你的邮箱：计划发到这里，也只认这个地址的回复
        "from_address": "",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_user": "",
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "imap_user": "",
        "imap_folder": "INBOX",
        "auth": {
            # 你的邮箱服务商写在 Authentication-Results 邮件头开头的标识。留空 = 不采信任何验证结果
            # = 邮件回复一律不记账（只能用命令行记账）。用 mail-poll --dry-run 查看实际标识。
            "trusted_authserv": [],
        },
        "confirm_success": False,
        "poll_lookback_days": 3,
        # 计划邮件发出超过这么多天后，对它的回复不再记账（防止旧邮件被翻出来重放）；请改用界面或命令行记账
        "reply_max_age_days": 3,
    },
    "executor": {"mode": "manual"},
    "moligod": {
        "enabled": False,
        "url": "https://moligod.com/ammo-realtime",
        "profile_dir": "",           # 留空 = <data_dir>/moligod_profile
        "headless": False,
        "min_gap_sec": 4,
        "max_gap_sec": 8,
        "max_ops_per_run": 20,
        "timeout_sec": 20,
    },
    "funds": {"official_enabled": False},
    "review": {
        "sell_horizon_hours": 48,
        "reflect_every_days": 7,
    },
    "watch": {"enabled": False, "move_pct": 8.0},
    "pipeline": {
        "nodes": [
            {"id": "mail_poll", "on_fail": "skip"},
            {"id": "collect_market", "retries": 1, "on_fail": "degrade"},
            {"id": "collect_intel", "on_fail": "skip"},
            {"id": "review", "on_fail": "skip"},
            {"id": "reflect", "on_fail": "skip"},
            {"id": "features", "on_fail": "abort"},
            {"id": "rule_baseline", "on_fail": "abort"},
            {"id": "llm_analyze", "retries": 1, "on_fail": "degrade"},
            {"id": "guardrails", "on_fail": "abort"},
            {"id": "write_plan", "on_fail": "abort"},
            {"id": "sync_alerts", "on_fail": "degrade"},
            {"id": "notify", "retries": 1, "on_fail": "degrade", "always": True},
        ]
    },
}

# 反思建议被你确认后，允许自动写入 overrides 的参数白名单及取值范围
OVERRIDE_WHITELIST: dict[str, tuple[float, float]] = {
    "rules.buy_quantile": (0.02, 0.45),
    "rules.sell_quantile": (0.55, 0.98),
    "rules.min_margin": (0.0, 0.2),
    "rules.stop_pct": (0.02, 0.3),
    "rules.trend_block_pct": (3.0, 60.0),
    "features.window_days": (2, 30),
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict, path: Path | None = None):
        self.data = data
        self.path = path
        self.notes: list[str] = []          # 校验时给出的不致命提示

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        cur = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    @property
    def data_dir(self) -> Path:
        p = Path(self.get("general.data_dir", "data"))
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent / p
        return p

    @property
    def fee(self) -> float:
        return float(self.get("fees.sell_fee_rate", 0.13))

    def fee_for(self, name: str | None) -> float:
        """某个品种的卖出费率：fees.overrides 里单独设置了就用它（名称不区分大小写、忽略首尾空白），否则用 fees.sell_fee_rate。"""
        ov = self.get("fees.overrides") or {}
        if name and isinstance(ov, dict) and ov:
            key = str(name).strip().lower()
            for k, v in ov.items():
                if str(k).strip().lower() == key:
                    return float(v)
        return self.fee

    @property
    def paper(self) -> bool:
        return bool(self.get("general.paper_mode", True))

    @property
    def watchlist(self) -> list[dict]:
        out = []
        for w in self.get("watchlist", []) or []:
            if isinstance(w, str):
                w = {"name": w}
            if not w.get("name"):
                continue
            out.append({
                "name": str(w["name"]),
                "oid": int(w["oid"]) if w.get("oid") not in (None, "") else None,
                "alias": [str(a) for a in (w.get("alias") or [])],
                "moligod_name": w.get("moligod_name"),
                "search": w.get("search") or str(w["name"]),
            })
        return out


def secret(name: str) -> str | None:
    """从环境变量读密钥，并登记到脱敏表。"""
    if name not in SECRET_ENV_NAMES:
        raise ValueError(f"未登记的密钥名: {name}")
    val = os.environ.get(name)
    if val:
        val = val.strip()
        register_secret(val)
    return val or None


def load_config(path: str | Path | None = None, *, overrides: dict | None = None) -> Config:
    """读取并严格校验配置。任何一项不合法都抛 ConfigError（拒绝运行），不会悄悄改用默认值。

    path 不存在时用内置默认值（方便首次试跑）。
    """
    from . import config_check

    p = Path(path) if path else Path("config.yaml")
    data = copy.deepcopy(DEFAULTS)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                user = yaml.safe_load(f)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f"第 {mark.line + 1} 行" if mark is not None else ""
            raise config_check.ConfigError([f"不是合法的 YAML{where}：{getattr(e, 'problem', e)}"], str(p)) from None
        if user is None:
            user = {}
        if not isinstance(user, dict):
            raise config_check.ConfigError(["顶层必须是键值对"], str(p))
        data = deep_merge(data, user)
    cfg = Config(data, p.resolve())
    if overrides:
        cfg.data = deep_merge(cfg.data, overrides)
    # 先保证 data_dir 本身合法，才能去找 overrides.yaml
    dd = cfg.get("general.data_dir")
    if not isinstance(dd, str) or not dd.strip():
        config_check.check(cfg.data, str(p))
    ov = cfg.data_dir / "overrides.yaml"
    if ov.exists():
        try:
            with open(ov, encoding="utf-8") as f:
                extra = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise config_check.ConfigError([f"不是合法的 YAML：{getattr(e, 'problem', e)}"], str(ov)) from None
        for key, val in config_check.check_overrides(extra, OVERRIDE_WHITELIST, str(ov)).items():
            cfg.set(key, int(val) if key.endswith("window_days") else float(val))
    cfg.notes = config_check.check(cfg.data, str(p) if p.exists() else "内置默认配置")
    return cfg


def validate(cfg: Config) -> list[str]:
    """校验一个已加载的配置对象；不合法抛 ConfigError，返回不致命的提示。"""
    from . import config_check
    return config_check.check(cfg.data)
