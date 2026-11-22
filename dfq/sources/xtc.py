"""小涛查开放平台客户端（主数据源，按次扣费）。

省钱的三道闸：
1. 所有成功响应落盘缓存，同一请求在有效期内不重复发；
2. 每日请求上限（默认 60），超过即抛 DailyLimitExceeded，由上层停止采集并报警；
3. 每次真正发出的请求都记账（api_usage 表），可随时用 `python -m dfq status` 查。

安全：token 只通过参数传入本类，不写日志、不进缓存文件名和缓存内容。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import timeutil
from ..http import HttpError, Transport, request_json
from ..log import event, get_logger, redact, register_secret, scrub_secrets
from ..untrusted import strip_hidden

log = get_logger("xtc")
PROVIDER = "xtc"


class XtcError(Exception):
    """接口返回 code != 0，或响应结构不对。"""


class DailyLimitExceeded(XtcError):
    """今日请求次数已达上限。"""


class TokenMissing(XtcError):
    """没有设置 DFQ_XTC_TOKEN。"""


class XtcClient:
    def __init__(self, cfg, db, token: str | None, *, transport: Transport | None = None,
                 run_id: int | None = None, provider: str = PROVIDER, cache_only: bool = False):
        self.cfg = cfg
        self.db = db
        self.token = token
        self.transport = transport
        self.run_id = run_id
        self.provider = provider
        self.cache_only = cache_only      # dry-run：只读缓存，绝不发真实请求
        self.base_url = str(cfg.get("xtc.base_url")).rstrip("/")
        self.daily_limit = int(cfg.get("xtc.daily_limit", 60))
        self.cache_dir = cfg.data_dir / "cache" / provider
        self.cost_this_session = 0
        self.cache_hits = 0
        # 网络整体不通（断网、DNS 失败、平台宕机）时的熔断：连续几个接口都因网络失败后，
        # 本次运行剩下的请求直接放弃。否则每个接口都要“超时 + 重试”一遍，每次尝试都先记账，
        # 一晚上能白白记掉几十次额度、拖上十几分钟，后面的运行反而被日上限挡住。
        self.network_failures = 0
        self.max_network_failures = 2
        register_secret(token)

    # ---------- 基础 ----------
    def _cost(self, endpoint: str) -> int:
        return int((self.cfg.get("xtc.cost") or {}).get(endpoint, 1))

    def _ttl(self, endpoint: str) -> int:
        return int((self.cfg.get("xtc.cache_ttl") or {}).get(endpoint, 600))

    def _cache_path(self, endpoint: str, params: dict) -> Path:
        key = json.dumps({"e": endpoint, "p": {k: params[k] for k in sorted(params)}},
                         ensure_ascii=False, sort_keys=True)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        safe = re.sub(r"[^A-Za-z0-9_]", "_", endpoint)
        return self.cache_dir / f"{safe}_{h}.json"

    def remaining_today(self) -> int:
        return self.daily_limit - self.db.usage_on(self.provider)

    def call(self, endpoint: str, params: dict | None = None, *, use_cache: bool = True,
             ttl: int | None = None) -> dict:
        """调用 /v1/sjz_api/<endpoint>，返回完整响应字典（已确认 code == 0）。"""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if "token" in params:
            raise ValueError("token 不要放进 params，由客户端统一附加")
        path = self._cache_path(endpoint, params)
        ttl = self._ttl(endpoint) if ttl is None else ttl
        now = timeutil.now()

        if use_cache and path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = (now - timeutil.parse_dt(cached["fetched_at"])).total_seconds()
                if not isinstance(cached.get("response"), dict):
                    raise ValueError("缓存内容不是对象")
                if 0 <= age < ttl:
                    self.cache_hits += 1
                    event(log, logging.DEBUG, "缓存命中", endpoint=endpoint, age_sec=int(age))
                    resp = cached["response"]
                    resp["_fetched_at"] = cached["fetched_at"]
                    resp["_from_cache"] = True
                    return resp
            except (ValueError, KeyError, TypeError, AttributeError, OSError):
                pass  # 缓存坏了（含 JSONDecodeError，它是 ValueError 的子类）就当没有

        if self.cache_only:
            raise XtcError(f"{endpoint}：dry-run 模式不发真实请求（缓存里没有可用数据）")
        if not self.token:
            raise TokenMissing("未设置 DFQ_XTC_TOKEN（在 DFQ.exe 的“设置”页填写，或写进程序文件夹的 .env），无法请求小涛查接口")
        if self.network_failures >= self.max_network_failures:
            raise XtcError(f"{endpoint}：前面 {self.network_failures} 个请求都因网络问题失败，"
                           "本次不再请求小涛查（避免白白记掉额度）。请检查网络后重新运行")

        cost = self._cost(endpoint)

        def before_send() -> None:
            # 每次真正发请求前检查额度并记账（重试也算，宁可多算不可少算）
            if cost > 0 and self.db.usage_on(self.provider) + cost > self.daily_limit:
                raise DailyLimitExceeded(
                    f"今日小涛查请求已用 {self.db.usage_on(self.provider)}/{self.daily_limit} 次，"
                    f"本次需要 {cost} 次，已停止请求")
            if cost > 0:
                self.db.add_usage(self.provider, endpoint, cost, ok=True, run_id=self.run_id)
                self.cost_this_session += cost

        url = f"{self.base_url}/v1/sjz_api/{endpoint}"
        try:
            resp = request_json(
                "GET", url, params={**params, "token": self.token},
                timeout=float(self.cfg.get("xtc.timeout", 15)),
                retries=int(self.cfg.get("xtc.retries", 2)),
                transport=self.transport, on_attempt=before_send)
        except HttpError as e:
            if e.network:
                self.network_failures += 1
            event(log, logging.WARNING, f"请求失败: {e}", endpoint=endpoint)
            raise XtcError(f"{endpoint} 请求失败: {e}") from None
        self.network_failures = 0

        if not isinstance(resp, dict):
            raise XtcError(f"{endpoint} 响应不是对象")
        code = resp.get("code")
        if code != 0:
            # 先脱敏再截断：服务端可能把收到的 token 写进报错，截断在密钥中间会漏出半截
            msg = redact(resp.get("msg") or resp.get("message") or "")[:120]
            event(log, logging.WARNING, "接口返回错误", endpoint=endpoint, code=redact(code)[:20], api_msg=msg)
            raise XtcError(f"{endpoint} 返回 code={redact(code)[:20]}: {msg}")

        fetched = timeutil.fmt(now)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # 缓存要原样保存接口数据，只把万一被回显的密钥原文换掉（scrub_secrets 不动其他字段）
            path.write_text(scrub_secrets(json.dumps(
                {"fetched_at": fetched, "endpoint": endpoint, "params": params, "response": resp},
                ensure_ascii=False)), encoding="utf-8")
        except OSError as e:
            event(log, logging.WARNING, f"缓存写入失败: {e}", endpoint=endpoint)
        event(log, logging.INFO, "请求成功", endpoint=endpoint, cost=cost,
              used_today=self.db.usage_on(self.provider), limit=self.daily_limit)
        resp["_fetched_at"] = fetched
        resp["_from_cache"] = False
        return resp

    # ---------- 具体接口 ----------
    def filter_options(self) -> list[dict]:
        """全物品列表 Pro - 筛选项（文档：不消耗次数）。"""
        return self.filter_options_raw().get("data") or []

    def filter_options_raw(self) -> dict:
        return self.call("item_list_pro_key")

    def item_list_pro(self, *, p: int = 1, limit: int = 20, key1: str = "全部", key2: str = "全部",
                      sort: str = "1-1", grade: int | None = None, is_pay: int | None = None,
                      n: str | None = None) -> dict:
        return self.call("item_list_pro", {"p": p, "limit": limit, "key1": key1, "key2": key2,
                                           "sort": sort, "grade": grade, "is_pay": is_pay, "n": n})

    def list_all(self, key1: str, key2: str, *, sort: str = "2-2") -> list[dict]:
        """翻页拉完一个分类。每页 1 次。"""
        limit = int(self.cfg.get("xtc.list_limit", 20))
        max_pages = int(self.cfg.get("xtc.list_max_pages", 8))
        items: list[dict] = []
        total = None
        for page in range(1, max_pages + 1):
            resp = self.item_list_pro(p=page, limit=limit, key1=key1, key2=key2, sort=sort)
            data = resp.get("data") or []
            if not isinstance(data, list):
                raise XtcError("item_list_pro 返回的 data 不是列表（接口结构变了？），本页数据没有使用")
            items.extend(d for d in data if isinstance(d, dict))
            try:
                total = int(resp.get("count")) if resp.get("count") is not None else total
            except (TypeError, ValueError):
                pass
            if not data or len(data) < limit or (total is not None and len(items) >= total):
                break
        return items

    def minute(self, oid: int) -> dict:
        return self.call("minute", {"id": oid})

    def hour(self, oid: int, forecast: bool = False) -> dict:
        return self.call("hour", {"id": oid, "isYC": 1 if forecast else 0})

    def day(self, oid: int) -> dict:
        return self.call("day", {"id": oid})

    def ammo_day(self, grade: int) -> dict:
        """今日倒子弹低价预测。"""
        return self.call("ammo_day", {"grade": grade})

    def ammo_yesterday(self, grade: int) -> dict:
        """昨日倒子弹最高收益（文档没有给返回示例，结构未实测）。"""
        return self.call("ammo_zr_yc", {"grade": grade})

    def ammo_pack(self, grade: int) -> dict:
        """子弹自选包收益。"""
        return self.call("ammo_pack", {"grade": grade})

    def exchange_pay(self) -> dict:
        """军需处兑换-子弹收益（fee-check 用来核对费率，1 次额度，默认缓存 6 小时）。

        实测（2026-11-16）：返回 9 种 2～4 级子弹，每项有 priceMax（出售总价）、sxf（手续费）、count、name、grade、oid 等。
        """
        return self.call("exchange_pay")


# ---------- 响应解析（纯函数，便于测试） ----------
def _num(v: Any) -> float | None:
    """接口里的数字。NaN / 无穷大 / 布尔值一律当作没有：float("nan")、float("inf") 都能“成功”，
    一旦当成价格入库，后面的分位数、仓位计算会全部失真。"""
    try:
        if v is None or v == "" or isinstance(v, bool):
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _pos(v: Any) -> float | None:
    """价格：只接受正数。0 和负数是接口出错的表现，当作没有数据。"""
    f = _num(v)
    return f if f is not None and f > 0 else None


def _data_dict(resp: Any) -> dict:
    """取响应里的 data 对象；结构不对（不是对象）时当作空，而不是让整个采集崩掉。"""
    data = resp.get("data") if isinstance(resp, dict) else None
    return data if isinstance(data, dict) else {}


def _arr(data: dict, key: str) -> list:
    v = data.get(key)
    return v if isinstance(v, list) else []


def parse_minute_curve(resp: dict, ref: datetime) -> list[tuple[datetime, float]]:
    data = _data_dict(resp)
    labels, prices = _arr(data, "a"), _arr(data, "b")
    times = timeutil.parse_minute_labels([str(x) for x in labels], ref)
    out: dict[datetime, float] = {}
    for t, p in zip(times, prices):
        pv = _num(p)
        if t is not None and pv is not None and pv > 0:
            out[t] = pv
    return sorted(out.items())


def parse_hour_curve(resp: dict, ref: datetime) -> list[dict]:
    data = _data_dict(resp)
    labels = _arr(data, "a")
    b, mx, mn = _arr(data, "b"), _arr(data, "max"), _arr(data, "min")
    yc = _arr(data, "yc")
    rows = []
    for i, lab in enumerate(labels):
        t = timeutil.parse_md_label(str(lab), ref)
        if t is None:
            continue
        price = _num(b[i]) if i < len(b) else None
        if price is not None:
            rows.append({"ts": t, "price": price,
                         "pmin": _num(mn[i]) if i < len(mn) else None,
                         "pmax": _num(mx[i]) if i < len(mx) else None, "forecast": 0})
        elif yc:
            # 实测（2026-11-15 probe）：isYC=1 时 a 和 yc 等长、按下标一一对应；
            # 历史时段 yc 是字符串 "null"，b 结束之后的标签才是预测值。
            pv = _num(yc[i]) if i < len(yc) else None
            if pv is not None:
                rows.append({"ts": t, "price": pv, "pmin": None, "pmax": None, "forecast": 1})
    return rows


def parse_day_curve(resp: dict, ref: datetime) -> list[dict]:
    data = _data_dict(resp)
    labels = _arr(data, "a")
    b, mx, mn = _arr(data, "b"), _arr(data, "max"), _arr(data, "min")
    mxd, mnd = _arr(data, "maxD"), _arr(data, "minD")
    rows = []
    for i, lab in enumerate(labels):
        t = timeutil.parse_md_label(str(lab), ref)
        if t is None:
            continue

        def at(arr: list) -> str | None:
            if i < len(arr):
                dt = timeutil.parse_md_label(str(arr[i]), ref)
                return timeutil.fmt_min(dt) if dt else None
            return None

        rows.append({"d": timeutil.day_str(t), "price": _num(b[i]) if i < len(b) else None,
                     "pmin": _num(mn[i]) if i < len(mn) else None,
                     "pmax": _num(mx[i]) if i < len(mx) else None,
                     "min_at": at(mnd), "max_at": at(mxd)})
    return rows


def parse_list_item(d: dict) -> dict | None:
    from ..fees import parse_shop_sell_type
    if not isinstance(d, dict):
        return None
    oid = _num(d.get("oid"))
    # 品种名来自外部接口：去掉控制字符和不可见字符、限长（它会进数据库、报告，并参与名称匹配）
    name = strip_hidden(str(d.get("name") or "")).strip()[:100]
    if not oid or oid < 0 or not name:
        return None  # oid 为 0 = 不能在交易行交易
    sst = parse_shop_sell_type(d.get("ShopSellType"))
    return {
        "oid": int(oid), "name": name,
        "grade": int(_num(d.get("grade")) or 0), "legacy_id": int(_num(d.get("id")) or 0),
        "price": _pos(d.get("price")), "bl": _num(d.get("bl")),
        "day7_price": _pos(d.get("day_7_price")), "day7_bl": _num(d.get("day_7_bl")),
        "day30_price": _pos(d.get("day_30_price")), "day30_bl": _num(d.get("day_30_bl")),
        "sell_method": sst["method"], "sell_level": sst["level"], "quick_price": sst["quick_price"],
    }


def parse_ammo_day(resp: dict) -> list[dict]:
    """今日低价预测：data = [{hour: 0..23, data: [{oid,name,grade,price}, ...]}, ...]"""
    out = []
    buckets = resp.get("data") if isinstance(resp, dict) else None
    for bucket in buckets if isinstance(buckets, list) else []:
        if not isinstance(bucket, dict):
            continue
        hour = _num(bucket.get("hour"))
        if hour is not None and not 0 <= hour <= 23:
            hour = None
        for it in _arr(bucket, "data"):
            if isinstance(it, dict) and it.get("name"):
                out.append({"oid": int(_num(it.get("oid")) or 0),
                            "name": strip_hidden(str(it["name"])).strip()[:100],
                            "grade": int(_num(it.get("grade")) or 0),
                            "hour": int(hour) if hour is not None else None,
                            "price": _pos(it.get("price"))})
    return out


def parse_exchange_pay(resp: dict) -> list[dict]:
    """军需处兑换-子弹收益 → [{name, grade, oid, count, price_max, sxf, rate}]，rate = sxf ÷ priceMax。

    不假设 data 的嵌套层次：遍历整个 data，凡是同时带 priceMax 和 sxf 的对象都收下；
    出售总价不是正数、手续费为负或不小于出售价的行视为坏数据，跳过。
    """
    out: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "priceMax" in node and "sxf" in node:
                pm, sxf = _pos(node.get("priceMax")), _num(node.get("sxf"))
                if pm is not None and sxf is not None and 0 <= sxf < pm:
                    out.append({"name": str(node.get("name") or "").strip() or None,
                                "grade": int(_num(node.get("grade")) or 0) or None,
                                "oid": int(_num(node.get("oid")) or 0) or None,
                                "count": _num(node.get("count")), "price_max": pm, "sxf": sxf,
                                "rate": sxf / pm})
                return
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(resp.get("data") if isinstance(resp, dict) else None)
    return out


def parse_generic_items(resp: dict) -> list[dict]:
    """结构未知的分析接口：遍历响应，把所有带 name 的对象收集出来，原样保留字段。"""
    found: list[dict] = []

    def walk(node: Any, group: str | None) -> None:
        if isinstance(node, dict):
            if node.get("name") and (node.get("oid") is not None or node.get("id") is not None):
                row = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
                if group:
                    row["_group"] = group
                found.append(row)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, str(k) if not str(k).startswith("data") else group)
        elif isinstance(node, list):
            for v in node:
                walk(v, group)

    walk(resp.get("data"), None)
    return found


def find_category(options: list[dict], keyword: str = "子弹") -> list[dict]:
    """在筛选项里找名字含 keyword 的一级分类，返回 [{key1, key2 列表}]。"""
    hits = []
    for opt in options or []:
        if isinstance(opt, dict) and keyword in str(opt.get("label", "")):
            hits.append({"key1": opt.get("label"), "value": opt.get("value"),
                         "key2": [c.get("label") for c in opt.get("children") or []
                                  if isinstance(c, dict)]})
    return hits
