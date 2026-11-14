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
        "sell_fee_rate": 0.13,
        "warn_diff": 0.03,           # 反推费率与配置偏差超过它就提示
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
    """读取配置。path 不存在时用默认值（方便首次试跑），并不会报错。"""
    p = Path(path) if path else Path("config.yaml")
    data = copy.deepcopy(DEFAULTS)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        if not isinstance(user, dict):
            raise ValueError(f"{p} 顶层必须是键值对")
        data = deep_merge(data, user)
    cfg = Config(data, p.resolve())
    if overrides:
        cfg.data = deep_merge(cfg.data, overrides)
    ov = cfg.data_dir / "overrides.yaml"
    if ov.exists():
        with open(ov, encoding="utf-8") as f:
            extra = yaml.safe_load(f) or {}
        for key, val in _flatten(extra).items():
            if key in OVERRIDE_WHITELIST:
                lo, hi = OVERRIDE_WHITELIST[key]
                try:
                    num = float(val)
                except (TypeError, ValueError):
                    continue
                if lo <= num <= hi:
                    cfg.set(key, int(num) if key.endswith("window_days") else num)
    validate(cfg)
    return cfg


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def validate(cfg: Config) -> list[str]:
    """基本合法性检查；返回提示信息（不致命的问题只提示，不抛错）。"""
    notes = []
    fee = cfg.fee
    if not (0 <= fee < 0.9):
        raise ValueError(f"fees.sell_fee_rate 不合理: {fee}")
    bq, sq = cfg.get("rules.buy_quantile"), cfg.get("rules.sell_quantile")
    if not (0 < bq < sq < 1):
        raise ValueError("rules.buy_quantile 必须小于 rules.sell_quantile，且都在 (0,1) 内")
    n = len(cfg.watchlist)
    if n == 0:
        raise ValueError("watchlist 不能为空")
    if n > 10:
        notes.append(f"关注清单有 {n} 个品种，建议控制在 5～10 个")
    mode = cfg.get("executor.mode", "manual")
    if mode not in ("manual", "auto"):
        raise ValueError("executor.mode 只能是 manual 或 auto")
    return notes
