"""大模型分析（兼容 OpenAI 格式的 /chat/completions）。

- base_url / model 在 config.yaml 里配，api_key 走环境变量 DFQ_LLM_API_KEY。
- 只喂结构化特征和整理后的观点摘要，不喂原始网页。
- 要求模型输出严格 JSON；解析或校验失败就整体回退到规则基线。
- 模型的输出只是“建议”，之后还要过 guardrails.apply()，它绕不过护栏。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import timeutil
from .http import HttpError, Transport, request_json
from .log import event, get_logger

log = get_logger("llm")

PROMPT_VER = "v1"

SYSTEM_PROMPT = """你是《三角洲行动》交易行的子弹低买高卖分析助手。用户每晚收到你的计划，第二天手动在游戏里挂单。

你会收到一个 JSON：每个关注品种的统计特征和“规则基线”给出的买入线/卖出线/止损线，以及近期复盘结果、各信源的历史信用分、最新的观点摘要、资金与风控上限。

你的任务：在规则基线的基础上做有依据的调整，并写明理由。要求：
1. 卖出要扣综合费率（见 fee_rate）。回本卖价 = 买入价 ÷ (1 − 费率)。卖出线必须高于回本价，否则把该品种设为 watch。
2. 调整阈值要有具体依据（分位、趋势、时段规律、复盘里反复出现的错误、可信信源的观点）。没有依据就沿用规则线，不要为了显得有用而乱调。
3. 信源观点要按信用分加权：信用分低或样本少的只作参考；数据站站长、主播可能有利益相关，不能默认可信。
4. 数据不足（basis 为 daily、data_days 很少）时降低置信度，宁可观望。
5. 数量 qty 不要超过给出的风控上限；不确定就填 null，由程序按上限分配。
6. 所有价格为整数（哈夫币/发）。理由用中文，每条不超过 60 字，要能事后用真实价格检验。

只输出一个 JSON 对象，不要输出任何其他文字，不要用代码块包裹。结构如下：
{
  "market_view": "一两句话的整体判断",
  "items": [
    {"oid": 整数, "action": "buy" 或 "watch", "buy_price": 整数, "sell_price": 整数, "stop_price": 整数,
     "qty": 整数或null, "reason": "理由", "risk": "主要风险", "confidence": 0到1的小数}
  ],
  "short_term": [{"date": "YYYY-MM-DD", "note": "当日要点"}],
  "long_term": [{"week": "第几周或日期范围", "note": "本周思路"}],
  "next_run": "YYYY-MM-DD HH:MM",
  "next_run_reason": "为什么建议这个时间再跑"
}
items 必须覆盖输入里的每一个品种。"""

REFLECT_PROMPT = """你是这个子弹交易决策系统的复盘员。下面的 JSON 是最近一段时间每条建议的真实结果、最终建议与纯规则基线的对比、当前规则参数、各信源的命中情况。

请诚实地总结系统错在哪里，并提出修改建议。建议只会存档，由用户确认后才生效，所以请具体、保守、可验证。

只输出一个 JSON 对象：
{
  "summary": "一段话总结这段时间的表现",
  "mistakes": ["具体错误 1", "具体错误 2"],
  "param_suggestions": [
    {"key": "rules.buy_quantile", "current": 0.15, "suggested": 0.12, "reason": "依据"}
  ],
  "prompt_suggestions": ["对分析提示词的修改建议"],
  "watchlist_suggestions": ["关于关注清单的建议"]
}
param_suggestions 的 key 只能取：rules.buy_quantile、rules.sell_quantile、rules.min_margin、rules.stop_pct、rules.trend_block_pct、features.window_days。样本太少时请直接说“样本不足，不建议修改”，并让 param_suggestions 为空数组。"""

EXTRACT_PROMPT = """下面是一段关于《三角洲行动》交易行的文字（视频字幕、新闻或群聊）。请只提取其中对具体子弹价格走向的明确观点。

可选的子弹（只能从这里选，写不出对应品种的观点请忽略）：
{items}

只输出一个 JSON 对象：
{{"digest": "三句话以内的要点摘要（中文）",
  "opinions": [{{"item": "上面列表里的品种全名", "direction": "up 或 down", "horizon_days": 1到14的整数, "claim": "原意概括，30字内"}}]}}
没有明确观点时 opinions 为空数组。不要编造。"""


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, cfg, api_key: str | None, transport: Transport | None = None):
        self.cfg = cfg
        self.api_key = api_key
        self.transport = transport
        self.base_url = str(cfg.get("llm.base_url") or "").rstrip("/")
        self.model = str(cfg.get("llm.model") or "")

    @property
    def ready(self) -> bool:
        return bool(self.cfg.get("llm.enabled") and self.base_url and self.model and self.api_key)

    def why_not_ready(self) -> str:
        if not self.cfg.get("llm.enabled"):
            return "llm.enabled 为 false"
        if not self.base_url or not self.model:
            return "llm.base_url 或 llm.model 未配置"
        if not self.api_key:
            return "未设置环境变量 DFQ_LLM_API_KEY"
        return ""

    def chat_json(self, system: str, user: str) -> dict:
        if not self.ready:
            raise LLMError(self.why_not_ready())
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": int(self.cfg.get("llm.max_tokens", 4000)),
        }
        if self.cfg.get("llm.temperature") is not None:
            body["temperature"] = float(self.cfg.get("llm.temperature"))
        if self.cfg.get("llm.json_mode", True):
            body["response_format"] = {"type": "json_object"}
        try:
            resp = request_json(
                "POST", f"{self.base_url}/chat/completions", json_body=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=float(self.cfg.get("llm.timeout", 120)),
                retries=int(self.cfg.get("llm.retries", 1)), transport=self.transport)
        except HttpError as e:
            raise LLMError(f"模型请求失败: {e}") from None
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("模型响应结构不符合 OpenAI 格式") from None
        if isinstance(content, list):   # 个别兼容实现把内容拆成片段
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = resp.get("usage") or {}
        event(log, logging.INFO, "模型调用完成", model=self.model,
              prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        return parse_json_loose(content)


def parse_json_loose(text: str) -> dict:
    """容错解析：去掉代码块围栏、截取第一个完整的 {...}。"""
    if not isinstance(text, str) or not text.strip():
        raise LLMError("模型返回为空")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        start = s.find("{")
        if start < 0:
            raise LLMError("模型返回里没有 JSON 对象") from None
        depth, end, in_str, esc = 0, -1, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            raise LLMError("模型返回的 JSON 不完整（可能被截断）") from None
        try:
            obj = json.loads(s[start:end + 1])
        except json.JSONDecodeError as e:
            raise LLMError(f"模型返回的 JSON 无法解析: {e.msg}") from None
    if not isinstance(obj, dict):
        raise LLMError("模型返回的不是 JSON 对象")
    return obj


def _int(v) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def validate_analysis(obj: dict, rule: dict[int, dict]) -> dict:
    """校验并规整模型输出。缺的品种用规则基线补齐；多出来的品种丢弃。"""
    items_in = obj.get("items")
    if not isinstance(items_in, list):
        raise LLMError("模型输出缺少 items 数组")
    seen: dict[int, dict] = {}
    for raw in items_in:
        if not isinstance(raw, dict):
            continue
        oid = _int(raw.get("oid"))
        if oid not in rule or oid in seen:
            continue
        action = str(raw.get("action", "")).lower().strip()
        conf = raw.get("confidence")
        try:
            conf = min(max(float(conf), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = rule[oid].get("confidence", 0.3)
        seen[oid] = {
            "oid": oid, "name": rule[oid]["name"],
            "action": action if action in ("buy", "watch") else "watch",
            "buy_price": _int(raw.get("buy_price")), "sell_price": _int(raw.get("sell_price")),
            "stop_price": _int(raw.get("stop_price")), "qty": _int(raw.get("qty")),
            "reason": str(raw.get("reason") or "")[:200], "risk": str(raw.get("risk") or "")[:200],
            "confidence": conf, "from": "llm",
        }
    missing = [oid for oid in rule if oid not in seen]
    for oid in missing:
        seen[oid] = {**rule[oid], "qty": None, "from": "rule"}

    def notes(key: str, k1: str) -> list[dict]:
        out = []
        for n in obj.get(key) or []:
            if isinstance(n, dict) and n.get("note"):
                out.append({k1: str(n.get(k1) or "")[:40], "note": str(n["note"])[:200]})
        return out[:10]

    next_run = None
    if obj.get("next_run"):
        try:
            next_run = timeutil.fmt_min(timeutil.parse_dt(str(obj["next_run"])))
        except ValueError:
            next_run = None
    return {
        "items": [seen[oid] for oid in rule],
        "missing": missing,
        "market_view": str(obj.get("market_view") or "")[:300],
        "short_term": notes("short_term", "date"),
        "long_term": notes("long_term", "week"),
        "next_run": next_run,
        "next_run_reason": str(obj.get("next_run_reason") or "")[:200],
    }


def build_analysis_input(cfg, feats: dict[int, dict], rule: dict[int, dict], state: dict,
                         recent: dict, sources: list[dict], digests: list[dict],
                         hints: list[dict], caps: dict | None = None) -> str:
    keep = ("current", "pct_rank", "basis", "data_days", "minute_days", "q", "window_min",
            "window_max", "amp_avg", "low_hours", "high_hours", "trend_7d", "trend_30d",
            "change_today", "implied_fee", "stale_hours", "warnings")
    items = []
    for oid, f in feats.items():
        items.append({"oid": oid, "name": f["name"], "grade": f.get("grade"),
                      "features": {k: f[k] for k in keep if f.get(k) not in (None, [], {})},
                      "rule": {k: rule[oid].get(k) for k in
                               ("action", "buy_price", "sell_price", "stop_price", "breakeven", "reason")}})
    payload = {
        "now": timeutil.fmt_min(timeutil.now()),
        "fee_rate": cfg.fee,
        "paper_mode": cfg.paper,
        "run_every_days": cfg.get("general.run_every_days"),
        "capital": {"cash": round(state["cash"]), "cash_known": state["cash_known"],
                    "positions": {n: {"qty": p["qty"], "avg_cost": round(p["avg_cost"])}
                                  for n, p in state["positions"].items()}},
        "risk_limits": {k: cfg.get(f"risk.{k}") for k in
                        ("max_daily_spend_pct", "max_position_pct", "max_total_exposure_pct",
                         "llm_max_adjust_pct", "lot_size")},
        "items": items,
        "recent_review": recent,
        "source_credibility": sources,
        "opinion_digests": digests,
        "platform_hints": hints,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)
