"""执行器插件接口（借鉴 MAAFramework：引擎只管调度，具体“怎么执行”交给可替换的执行器）。

本项目只实现一种执行器：manual —— 把指令写进邮件，由人进游戏手动操作。

`executor.mode: auto` 只保留注册入口，**没有任何实现**，也不应该在本仓库里实现：
硬性边界是不操作游戏客户端（不读内存、不注入输入、不截游戏画面、不模拟点击）。
选中 auto 而没有已注册的插件时，会报出清楚的错误并回退到 manual。

如果将来出现了合规的自动执行方式（例如官方开放了交易接口），接入方法是：

    from dfq.executors import Executor, Report, register_executor

    class MyExecutor(Executor):
        name = "auto"
        def execute(self, ins):            # ins: Instruction
            ...
            return Report(ins, status="done", detail="...")

    register_executor("auto", MyExecutor)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from ..log import event, get_logger

log = get_logger("executor")


@dataclass
class Instruction:
    """一条计划指令。"""
    name: str            # 品种（游戏内准确名称）
    side: str            # buy / sell
    price: float         # 买入：最高可接受价；卖出：最低可接受价
    qty: int
    deadline: str        # 截止时间（过期作废）
    oid: int | None = None
    stop_price: float | None = None
    note: str = ""


@dataclass
class Report:
    """执行回报。"""
    instruction: Instruction
    status: str          # notified = 已通知人工；done / failed 留给将来的自动执行器
    detail: str = ""
    extra: dict = field(default_factory=dict)


class Executor(ABC):
    name = "base"

    def __init__(self, cfg, **deps):
        self.cfg = cfg
        self.deps = deps

    @abstractmethod
    def execute(self, instruction: Instruction) -> Report:
        """执行一条指令并返回回报。"""

    def flush(self) -> dict:
        """批量型执行器在这里统一提交（manual 在这里发那一封合并邮件）。"""
        return {}


class ManualExecutor(Executor):
    """人工执行：只收集指令，由通知环节合并成一封邮件发给你。"""
    name = "manual"

    def __init__(self, cfg, **deps):
        super().__init__(cfg, **deps)
        self.pending: list[Instruction] = []

    def execute(self, instruction: Instruction) -> Report:
        self.pending.append(instruction)
        return Report(instruction, status="notified", detail="已写入当晚的计划邮件，等待人工执行")


_REGISTRY: dict[str, Callable[..., Executor]] = {"manual": ManualExecutor}


def register_executor(mode: str, factory: Callable[..., Executor]) -> None:
    _REGISTRY[mode] = factory


def get_executor(cfg, **deps) -> tuple[Executor, str | None]:
    """按 executor.mode 取执行器。返回 (执行器, 回退说明)。"""
    mode = cfg.get("executor.mode", "manual")
    if mode in _REGISTRY:
        return _REGISTRY[mode](cfg, **deps), None
    msg = (f"executor.mode 设成了 {mode!r}，但没有已注册的 {mode} 执行器插件。"
           f"本项目不提供自动操作游戏的实现，已回退为 manual（邮件通知人工执行）。")
    event(log, logging.ERROR, msg, node="notify")
    return ManualExecutor(cfg, **deps), msg


def plan_to_instructions(plan: dict) -> list[Instruction]:
    out = []
    for s in plan.get("sells") or []:
        out.append(Instruction(name=s["name"], side="sell", price=s["sell_price"], qty=s["qty"],
                               deadline=plan["valid_until"], oid=s.get("oid"),
                               stop_price=s.get("stop_price"), note=s.get("note", "")))
    for i in plan["items"]:
        if i["action"] == "buy" and i.get("qty"):
            out.append(Instruction(name=i["name"], side="buy", price=i["buy_price"], qty=i["qty"],
                                   deadline=plan["valid_until"], oid=i["oid"],
                                   stop_price=i.get("stop_price"), note=i.get("reason", "")))
    return out
