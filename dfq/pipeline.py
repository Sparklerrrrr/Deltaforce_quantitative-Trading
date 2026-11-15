"""任务流水线引擎（借鉴 MAAFramework 的思想：流程写在配置里，引擎负责调度，节点负责干活）。

配置里的每个节点：
    - id: collect_market     # 节点名（对应 nodes.py 里注册的实现）
      retries: 1             # 失败后重试次数
      on_fail: degrade       # 重试用尽后怎么办：
                             #   degrade  调用节点自己的降级逻辑（例如改用库里旧数据）
                             #   skip     跳过这个节点，继续往下
                             #   abort    中止后续节点（标了 always 的节点仍会执行，用来发通知）
                             #   goto:xxx 跳到名为 xxx 的节点
      always: false          # 即使流程已中止也要执行
      enabled: true

每个节点声明自己需要的输入（requires）和产出（provides），引擎在运行前后检查，
输入不齐就不运行该节点，并按 on_fail 处理。每一步都写结构化日志。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .log import event, get_logger, redact

log = get_logger("pipeline")


class NodeSkip(Exception):
    """节点主动表示“这次不需要我”（不算失败）。"""


@dataclass
class Context:
    cfg: Any
    db: Any
    now: Any
    run_id: int | None = None
    offline: bool = False
    dry_run: bool = False
    mock: bool = False
    data: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    services: dict = field(default_factory=dict)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def warn(self, msg: str) -> None:
        if msg and msg not in self.warnings:
            self.warnings.append(msg)


class Node:
    id = ""
    title = ""
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()

    def run(self, ctx: Context) -> dict | None:
        raise NotImplementedError

    def degrade(self, ctx: Context, err: Exception) -> dict | None:
        """降级逻辑。默认没有：直接把原错误抛回去，引擎按“失败”处理。"""
        raise err


REGISTRY: dict[str, type[Node]] = {}


def register(cls: type[Node]) -> type[Node]:
    REGISTRY[cls.id] = cls
    return cls


@dataclass
class NodeResult:
    id: str
    status: str            # ok / degraded / skipped / failed / not_run
    seconds: float = 0.0
    attempts: int = 0
    detail: str = ""
    info: dict = field(default_factory=dict)


def run_pipeline(ctx: Context, specs: list[dict]) -> list[NodeResult]:
    results: list[NodeResult] = []
    index = {s["id"]: i for i, s in enumerate(specs)}
    aborted = False
    visited: dict[str, int] = {}
    i = 0
    while i < len(specs):
        spec = specs[i]
        i += 1
        nid = spec["id"]
        visited[nid] = visited.get(nid, 0) + 1
        if visited[nid] > 2:                       # 防止 goto 写成死循环
            results.append(NodeResult(nid, "failed", detail="节点被重复进入，疑似 goto 循环"))
            aborted = True
            continue
        if spec.get("enabled", True) is False:
            results.append(NodeResult(nid, "skipped", detail="配置中已关闭"))
            continue
        if aborted and not spec.get("always"):
            results.append(NodeResult(nid, "not_run", detail="流程已中止"))
            continue
        cls = REGISTRY.get(nid)
        if cls is None:
            results.append(NodeResult(nid, "failed", detail="未知节点（配置里的 id 写错了？）"))
            event(log, logging.ERROR, "未知节点", node=nid)
            continue
        node = cls()
        policy = str(spec.get("on_fail", "abort"))
        missing = [k for k in node.requires if k not in ctx]
        if missing and not spec.get("always"):
            detail = f"缺少输入 {missing}（上游节点失败或被跳过）"
            results.append(NodeResult(nid, "skipped", detail=detail))
            event(log, logging.WARNING, detail, node=nid, event="skip")
            if policy == "abort":
                aborted = True
            continue

        retries = int(spec.get("retries", 0))
        t0 = time.time()
        err: Exception | None = None
        info: dict | None = None
        status = "ok"
        attempts = 0
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                event(log, logging.INFO, f"开始 {node.title or nid}", node=nid, event="start",
                      attempt=attempts, dry_run=ctx.dry_run)
                info = node.run(ctx)
                err = None
                break
            except NodeSkip as e:
                status, err = "skipped", None
                info = {"reason": str(e)}
                break
            except Exception as e:      # 节点内任何异常都不能让整条流水线崩掉
                err = e
                event(log, logging.WARNING, f"节点出错: {type(e).__name__}: {redact(e)}", node=nid,
                      event="error", attempt=attempts)
        detail = ""
        goto: str | None = None
        if err is not None:
            detail = f"{type(err).__name__}: {redact(err)}"[:300]
            if policy == "degrade":
                try:
                    info = node.degrade(ctx, err)
                    status = "degraded"
                except Exception as e2:
                    status = "failed"
                    detail = f"{type(e2).__name__}: {redact(e2)}"[:300]
            elif policy == "skip":
                status = "skipped"
            elif policy.startswith("goto:") and policy[5:] in index:
                status, goto = "failed", policy[5:]
            else:
                status = "failed"
                aborted = True
            if status in ("failed", "degraded", "skipped"):
                ctx.warn(f"环节「{node.title or nid}」{_STATUS_CN[status]}：{detail}")
        elif status == "skipped":
            detail = (info or {}).get("reason", "")

        if status in ("ok", "degraded"):
            lost = [k for k in node.provides if k not in ctx]
            if lost:
                detail = (detail + "；" if detail else "") + f"未产出 {lost}"
                event(log, logging.WARNING, f"节点没有产出声明的输出 {lost}", node=nid)
        res = NodeResult(nid, status, round(time.time() - t0, 2), attempts, detail, info or {})
        results.append(res)
        event(log, logging.INFO if status == "ok" else logging.WARNING,
              f"结束 {node.title or nid}: {_STATUS_CN[status]}", node=nid, event="end", status=status,
              seconds=res.seconds, **{k: v for k, v in (info or {}).items()
                                      if isinstance(v, (int, float, str, bool))})
        if goto:
            i = index[goto]
    return results


_STATUS_CN = {"ok": "成功", "degraded": "已降级", "skipped": "已跳过", "failed": "失败", "not_run": "未执行"}


def summarize(results: list[NodeResult]) -> str:
    return " → ".join(f"{r.id}:{_STATUS_CN[r.status]}" for r in results)


def overall_status(results: list[NodeResult]) -> str:
    if any(r.status == "failed" for r in results):
        return "failed"
    if any(r.status in ("degraded",) for r in results):
        return "degraded"
    return "ok"
