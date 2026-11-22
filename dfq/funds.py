"""资金查询。

默认方式（已实现）：手动输入余额 + 按成交记录自动推算，见 ledger.balance()。
    python -m dfq balance set 5000000
期初库存（用本程序之前就有的子弹，python -m dfq inventory add）不动余额：那笔钱是以前花的。

可选方式（**未实现，只保留接口和说明**）：通过三角洲官方网页接口查询余额和流水。
没有实现的原因：
1. 你给的社区文档 https://df-api.apifox.cn/ 目前公开的只剩“每日密码”一个接口，
   “货币资产查询”“流水查询”的文档已经不在目录里，作者也声明不再维护；
2. 该文档明确写着“Openid 和 Accesstoken 请自行抓包获取，不提供登陆的接口和流程”，
   也就是说没有正规的登录方式，接口细节我无法核实；
3. 用抓包得到的凭证调用官方未公开接口，账号有被风控的可能，不值得为了省一次手动输入去冒险。

如果以后有了可核实、合规的查询方式，实现 OfficialFunds.query_balance() 并把
config.yaml 的 funds.official_enabled 设为 true 即可；查询失败时会自动回退到手动余额。
"""
from __future__ import annotations

from . import ledger


class FundsUnavailable(Exception):
    pass


class OfficialFunds:
    """官方接口查询（占位）。登录必须由你本人扫码完成，程序只复用登录状态。"""

    def __init__(self, cfg):
        self.cfg = cfg

    def query_balance(self) -> float:
        raise FundsUnavailable("官方资金查询未实现（接口未核实），请用 python -m dfq balance set 手动输入")

    def query_flows(self, days: int = 1) -> list[dict]:
        raise FundsUnavailable("官方流水查询未实现（接口未核实）")


def current_balance(cfg, db) -> dict:
    """返回 {known, amount, source, note}。官方查询不可用时回退到手动余额。"""
    note = ""
    if cfg.get("funds.official_enabled"):
        try:
            amount = OfficialFunds(cfg).query_balance()
            ledger.set_balance(db, amount)
            return {"known": True, "amount": amount, "source": "official", "note": ""}
        except FundsUnavailable as e:
            note = f"官方查询不可用，已回退到手动余额：{e}"
    # 按品种取费率（W2 的 cfg.fee_for）；旧配置对象没有它时退回统一费率
    bal = ledger.balance(db, getattr(cfg, "fee_for", None) or cfg.fee)
    return {"known": bal["known"], "amount": bal["amount"], "source": "manual", "note": note,
            "base": bal["base"], "base_ts": bal["base_ts"]}
