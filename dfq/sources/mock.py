"""模拟行情源：没有 token 时用它把整条流程跑通（`--mock`）。

做法是冒充小涛查的“传输层”：按公开文档里的返回结构造数据，
这样真正被执行的还是 XtcClient 的解析、缓存、记账、限额代码，
只有“数据是假的”这一点不同。

价格是 (品种, 时刻) 的纯函数：基准价 × (1 + 慢波动 + 日内周期 + 噪声)。
日内周期设成凌晨低、傍晚高 —— 文档里“低价预测”示例的低点也集中在 0～8 点。
同一时刻无论哪天来取都得到同一个价，所以可以按天推进时间来模拟多日运行。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .. import timeutil
from .catalog import AMMO_REFERENCE

_EPOCH = datetime(2026, 1, 1)
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _unit(seed: str) -> float:
    """[0,1) 的确定性伪随机数。"""
    h = hashlib.md5(seed.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def base_price(oid: int, grade: int) -> float:
    lo, hi = {5: (2600, 6200), 4: (1100, 2300), 3: (340, 620)}.get(grade, (100, 300))
    return lo + (hi - lo) * _unit(f"base{oid}")


def mock_price(oid: int, grade: int, t: datetime) -> int:
    days = (t - _EPOCH).total_seconds() / 86400
    hour = t.hour + t.minute / 60
    amp = 0.05 + 0.05 * _unit(f"amp{oid}")                 # 日内振幅 5%～10%
    low_hour = 3 + 4 * _unit(f"low{oid}")                  # 低点在 3～7 点
    seasonal = -amp * math.cos(2 * math.pi * (hour - low_hour) / 24)
    slow = (0.05 * math.sin(2 * math.pi * days / (7 + 6 * _unit(f"p1{oid}")) + 6.28 * _unit(f"ph1{oid}"))
            + 0.03 * math.sin(2 * math.pi * days / (19 + 9 * _unit(f"p2{oid}")) + 6.28 * _unit(f"ph2{oid}")))
    bucket = int((t - _EPOCH).total_seconds() // 300)
    noise = 0.02 * (_unit(f"n{oid}:{bucket}") - 0.5)
    return max(1, int(round(base_price(oid, grade) * (1 + seasonal + slow + noise))))


class MockXtcTransport:
    """可直接当 XtcClient(transport=...) 用。"""

    def __init__(self, fee: float = 0.13):
        self.fee = fee
        self.calls: list[tuple[str, dict]] = []
        self.catalog = {oid: (name, grade, lid) for oid, name, grade, lid in AMMO_REFERENCE}

    def __call__(self, method, url, params, body, headers, timeout):
        endpoint = urlparse(url).path.rstrip("/").split("/")[-1]
        params = dict(params or {})
        params.pop("token", None)
        self.calls.append((endpoint, params))
        handler = getattr(self, f"_h_{endpoint}", None)
        if handler is None:
            return 200, json.dumps({"code": 404, "msg": "模拟源不支持该接口"}).encode()
        payload = {"code": 0, "msg": "成功", "message": "成功", **handler(params)}
        return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # ---- 各接口 ----
    def _h_item_list_pro_key(self, p):
        return {"count": 0, "data": [
            {"label": "全部", "value": 100, "extra": 0, "children": [{"label": "全部", "value": 101, "extra": 0}]},
            {"label": "子弹", "value": 20, "extra": 2, "children": [{"label": "全部", "value": 21, "extra": 0}]},
        ]}

    def _row(self, oid: int, now: datetime) -> dict:
        name, grade, lid = self.catalog[oid]
        price = mock_price(oid, grade, now)
        start = mock_price(oid, grade, now.replace(hour=0, minute=0))
        p7 = mock_price(oid, grade, now - timedelta(days=7))
        p30 = mock_price(oid, grade, now - timedelta(days=30))
        return {
            "ShopSellType": ["交易行上架", "3", str(int(price * (1 - self.fee) * 0.995))],
            "bl": round((price / start - 1) * 100, 2),
            "day_7_price": p7, "day_7_bl": round((price / p7 - 1) * 100, 2),
            "day_30_price": p30, "day_30_bl": round((price / p30 - 1) * 100, 2),
            "grade": grade, "id": lid, "isGet": "1", "length": 1, "width": 1,
            "name": name, "oid": oid, "pic": "", "price": price,
        }

    def _h_item_list_pro(self, p):
        now = timeutil.now()
        oids = sorted(self.catalog, key=lambda o: -mock_price(o, self.catalog[o][1], now))
        if p.get("grade") not in (None, ""):
            oids = [o for o in oids if self.catalog[o][1] == int(p["grade"])]
        if p.get("n"):
            oids = [o for o in oids if str(p["n"]) in self.catalog[o][0]]
        page, limit = int(p.get("p", 1)), int(p.get("limit", 20))
        chunk = oids[(page - 1) * limit: page * limit]
        return {"count": len(oids), "data": [self._row(o, now) for o in chunk]}

    def _series(self, oid: int, start: datetime, end: datetime, step_min: int):
        grade = self.catalog.get(oid, ("", 4, 0))[1]
        t = start
        while t <= end:
            yield t, mock_price(oid, grade, t)
            t += timedelta(minutes=step_min)

    def _h_minute(self, p):
        oid = int(p["id"])
        now = timeutil.now()
        end = now.replace(second=0, microsecond=0)
        end -= timedelta(minutes=end.minute % 5)
        pts = list(self._series(oid, end - timedelta(hours=24) + timedelta(minutes=5), end, 5))
        # 标签写法与真实接口一致（2026-11-16 实测："11-15 周日 23:51分"），让演示 / 测试走和真实数据相同的解析路径
        return {"data": {"a": [f"{t:%m-%d} {_WEEKDAYS[t.weekday()]} {t:%H:%M}分" for t, _ in pts],
                         "b": [v for _, v in pts], "zb": [0 for _ in pts]}}

    def _h_hour(self, p):
        oid = int(p["id"])
        now = timeutil.now().replace(minute=0, second=0, microsecond=0)
        a, b, mx, mn, mxd, mnd = [], [], [], [], [], []
        for i in range(71, -1, -1):
            h = now - timedelta(hours=i)
            pts = list(self._series(oid, h, h + timedelta(minutes=55), 5))
            hi, lo = max(pts, key=lambda x: x[1]), min(pts, key=lambda x: x[1])
            a.append(f"{h:%m-%d} {_WEEKDAYS[h.weekday()]} {h:%H}时"); b.append(pts[-1][1])   # 实测写法："11-15 周六 20时"
            mx.append(hi[1]); mn.append(lo[1])
            mxd.append(f"{hi[0]:%m-%d %H:%M}分"); mnd.append(f"{lo[0]:%m-%d %H:%M}分")
        data = {"a": a, "b": b, "max": mx, "maxD": mxd, "min": mn, "minD": mnd, "zb": [0] * len(a)}
        if str(p.get("isYC", "0")) == "1":
            grade = self.catalog.get(oid, ("", 4, 0))[1]
            for i in range(1, 13):
                h = now + timedelta(hours=i)
                data["a"].append(f"{h:%m-%d} {_WEEKDAYS[h.weekday()]} {h:%H}时")
                data.setdefault("yc", []).append(mock_price(oid, grade, h))
        return {"data": data}

    def _h_day(self, p):
        oid = int(p["id"])
        today = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0)
        a, b, mx, mn, mxd, mnd = [], [], [], [], [], []
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            end = min(d + timedelta(hours=23, minutes=55), timeutil.now())
            pts = list(self._series(oid, d, end, 15)) or [(d, 1)]
            hi, lo = max(pts, key=lambda x: x[1]), min(pts, key=lambda x: x[1])
            a.append(f"{d:%m-%d}"); b.append(int(sum(v for _, v in pts) / len(pts)))
            mx.append(hi[1]); mn.append(lo[1])
            mxd.append(f"{hi[0]:%m-%d %H:%M}分"); mnd.append(f"{lo[0]:%m-%d %H:%M}分")
        return {"data": {"a": a, "b": b, "max": mx, "maxD": mxd, "min": mn, "minD": mnd,
                         "zb": [0] * len(a)}}

    def _h_ammo_day(self, p):
        grade = int(p.get("grade", 0) or 0)
        today = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = {h: [] for h in range(24)}
        for oid, (name, g, lid) in self.catalog.items():
            if grade and g != grade:
                continue
            pts = [(h, mock_price(oid, g, today + timedelta(hours=h))) for h in range(24)]
            h, price = min(pts, key=lambda x: x[1])
            buckets[h].append({"grade": g, "id": lid, "name": name, "oid": oid, "pic": "", "price": price})
        return {"data": [{"hour": h, "data": buckets[h]} for h in range(24)]}

    def _h_ammo_zr_yc(self, p):
        # 真实结构未知（文档无示例），这里只造一个合理的形状供解析器演练
        grade = int(p.get("grade", 0) or 0)
        y = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        rows = []
        for oid, (name, g, lid) in self.catalog.items():
            if grade and g != grade:
                continue
            pts = [mock_price(oid, g, y + timedelta(minutes=15 * i)) for i in range(96)]
            lo, hi = min(pts), max(pts)
            rows.append({"grade": g, "id": lid, "name": name, "oid": oid,
                         "min_price": lo, "max_price": hi,
                         "profit": int(hi * (1 - self.fee) - lo)})
        rows.sort(key=lambda r: -r["profit"])
        return {"data": rows[:10]}

    def _h_ammo_pack(self, p):
        return {"data": {}}

    # 平台“军需处兑换”按 13% 收手续费（2026-11-16 实测 9 种 2～4 级子弹都是 12.998%～13.000%）。
    # 这里故意用固定的 13%，而不是 self.fee：否则 fee-check --mock 只会“核对出”你自己配置的数，毫无意义。
    PLATFORM_FEE = 0.13

    def _h_exchange_pay(self, p):
        now = timeutil.now()
        rows = []
        picks = [o for o, (_, g, _) in sorted(self.catalog.items()) if g in (2, 3, 4)][:9]
        for oid in picks:
            name, grade, lid = self.catalog[oid]
            count = 60 if grade >= 4 else 120
            total = mock_price(oid, grade, now) * count
            rows.append({"oid": oid, "name": name, "grade": grade, "count": count, "priceMax": total,
                         "sxf": int(round(total * self.PLATFORM_FEE)), "is_pay": 1, "isGet": "1", "pic": ""})
        return {"data": rows}
