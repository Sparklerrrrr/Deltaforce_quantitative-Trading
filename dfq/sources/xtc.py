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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import timeutil
from ..http import HttpError, Transport, request_json
from ..log import event, get_logger, register_secret

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
                if 0 <= age < ttl:
                    self.cache_hits += 1
                    event(log, logging.DEBUG, "缓存命中", endpoint=endpoint, age_sec=int(age))
                    resp = cached["response"]
                    resp["_fetched_at"] = cached["fetched_at"]
                    resp["_from_cache"] = True
                    return resp
            except (ValueError, KeyError, json.JSONDecodeError):
                pass  # 缓存坏了就当没有

        if self.cache_only:
            raise XtcError(f"{endpoint}：dry-run 模式不发真实请求（缓存里没有可用数据）")
        if not self.token:
            raise TokenMissing("未设置环境变量 DFQ_XTC_TOKEN，无法请求小涛查接口")

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
            event(log, logging.WARNING, f"请求失败: {e}", endpoint=endpoint)
            raise XtcError(f"{endpoint} 请求失败: {e}") from None

        if not isinstance(resp, dict):
            raise XtcError(f"{endpoint} 响应不是对象")
        code = resp.get("code")
        if code != 0:
            msg = resp.get("msg") or resp.get("message") or ""
            event(log, logging.WARNING, "接口返回错误", endpoint=endpoint, code=code, api_msg=str(msg)[:120])
            raise XtcError(f"{endpoint} 返回 code={code}: {str(msg)[:120]}")

        fetched = timeutil.fmt(now)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"fetched_at": fetched, "endpoint": endpoint, "params": params, "response": resp},
                ensure_ascii=False), encoding="utf-8")
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


# ---------- 响应解析（纯函数，便于测试） ----------
def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_minute_curve(resp: dict, ref: datetime) -> list[tuple[datetime, float]]:
    data = resp.get("data") or {}
    labels, prices = data.get("a") or [], data.get("b") or []
    times = timeutil.parse_minute_labels([str(x) for x in labels], ref)
    out: dict[datetime, float] = {}
    for t, p in zip(times, prices):
        pv = _num(p)
        if t is not None and pv is not None and pv > 0:
            out[t] = pv
    return sorted(out.items())


def parse_hour_curve(resp: dict, ref: datetime) -> list[dict]:
    data = resp.get("data") or {}
    labels = data.get("a") or []
    b, mx, mn = data.get("b") or [], data.get("max") or [], data.get("min") or []
    yc = data.get("yc") or []
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
            # 开启预测时结构会变（文档原话）；未实测。这里按“超出 b 长度的标签对应 yc”处理。
            j = i - len(b)
            pv = _num(yc[j]) if 0 <= j < len(yc) else None
            if pv is not None:
                rows.append({"ts": t, "price": pv, "pmin": None, "pmax": None, "forecast": 1})
    return rows


def parse_day_curve(resp: dict, ref: datetime) -> list[dict]:
    data = resp.get("data") or {}
    labels = data.get("a") or []
    b, mx, mn = data.get("b") or [], data.get("max") or [], data.get("min") or []
    mxd, mnd = data.get("maxD") or [], data.get("minD") or []
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
    oid = _num(d.get("oid"))
    name = d.get("name")
    if not oid or not name:
        return None  # oid 为 0 = 不能在交易行交易
    sst = parse_shop_sell_type(d.get("ShopSellType"))
    return {
        "oid": int(oid), "name": str(name).strip(),
        "grade": int(_num(d.get("grade")) or 0), "legacy_id": int(_num(d.get("id")) or 0),
        "price": _num(d.get("price")), "bl": _num(d.get("bl")),
        "day7_price": _num(d.get("day_7_price")), "day7_bl": _num(d.get("day_7_bl")),
        "day30_price": _num(d.get("day_30_price")), "day30_bl": _num(d.get("day_30_bl")),
        "sell_method": sst["method"], "sell_level": sst["level"], "quick_price": sst["quick_price"],
    }


def parse_ammo_day(resp: dict) -> list[dict]:
    """今日低价预测：data = [{hour: 0..23, data: [{oid,name,grade,price}, ...]}, ...]"""
    out = []
    for bucket in resp.get("data") or []:
        if not isinstance(bucket, dict):
            continue
        hour = _num(bucket.get("hour"))
        for it in bucket.get("data") or []:
            if isinstance(it, dict) and it.get("name"):
                out.append({"oid": int(_num(it.get("oid")) or 0), "name": str(it["name"]),
                            "grade": int(_num(it.get("grade")) or 0),
                            "hour": int(hour) if hour is not None else None,
                            "price": _num(it.get("price"))})
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
