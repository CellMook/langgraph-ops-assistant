"""条件路由：understand / data / review 之后的三处分支判断。

本模块只读状态、不写状态、不访问数据库、不调用模型，因此可以脱离图单独测试。
返回值为"节点名"或 "end"（由 ``graph.py`` 映射到 ``END``）；
任何无法判断的异常状态都收敛到 ``fail``，绝不让图卡在半路。
"""

from __future__ import annotations

from typing import Any

from ops_assistant.workflow.state import AssistantState

# understand 节点允许写入的三个业务去向 + fail 兜底
UNDERSTAND_ROUTES = ("clarify", "knowledge", "data", "fail")

# 数据查询失败后最多修复一次（规格 5.7：失败只允许重试一次，避免死循环）
MAX_REPAIR_ATTEMPTS = 1

# 终态：这些状态不再继续业务流程
TERMINAL_STATUSES = frozenset({"failed", "budget_exceeded"})


def _status(state: AssistantState) -> str:
    """读取并规范化当前状态字符串（小写、去空格）。"""
    return str(state.get("status") or "").strip().lower()


def _trace_entries(state: AssistantState) -> list[dict[str, Any]]:
    """只取结构合法的 trace 条目（脏数据直接忽略，避免路由被历史数据带崩）。"""
    entries = state.get("trace") or []
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def route_after_understand(state: AssistantState) -> str:
    """understand 之后的路由。

    判断顺序：
        1. 已经是 failed / budget_exceeded → ``fail``；
        2. ``route`` 字段不在允许集合里（模型未产出或脏数据）→ ``fail``；
        3. 其余情况按 ``route`` 走 ``clarify`` / ``knowledge`` / ``data``。

    参数：
        state: 当前图状态。

    返回：
        str：``clarify`` / ``knowledge`` / ``data`` / ``fail`` 之一。
    """
    if _status(state) in TERMINAL_STATUSES:
        return "fail"
    route = str(state.get("route") or "").strip().lower()
    if route not in UNDERSTAND_ROUTES:
        return "fail"
    return route


def route_after_data(state: AssistantState) -> str:
    """data 之后的路由：成功进 compute，可重试错误进 repair，其余进 fail。

    约定（由 nodes.data 写入最近一条 node="data" 的 trace 条目）：
        ``ok=True``        → 查询成功，进入 ``compute``；
        ``retryable=True`` → 可重试错误，若还没修复过则进入 ``repair``；
        其它               → 不可重试错误，进入 ``fail``。

    参数：
        state: 当前图状态。

    返回：
        str：``compute`` / ``repair`` / ``fail`` 之一。
    """
    if _status(state) in TERMINAL_STATUSES:
        return "fail"
    trace = _trace_entries(state)
    data_events = [item for item in trace if item.get("node") == "data"]
    if not data_events:
        # 没有任何 data 轨迹：只有在拿到结果时才允许继续，否则视为失败
        return "compute" if state.get("data_result") else "fail"
    last_event = data_events[-1]
    if bool(last_event.get("ok")):
        return "compute"
    if bool(last_event.get("retryable")):
        repaired = sum(1 for item in trace if item.get("node") == "repair")
        return "repair" if repaired < MAX_REPAIR_ATTEMPTS else "fail"
    return "fail"


def route_after_review(state: AssistantState) -> str:
    """review 之后的路由：批准或编辑后导出，拒绝/未审批则结束。

    参数：
        state: 当前图状态。

    返回：
        str：``export`` 或 ``end``。
    """
    if _status(state) in TERMINAL_STATUSES:
        return "end"
    review = state.get("review") or {}
    if not isinstance(review, dict):
        return "end"
    decision = str(review.get("decision") or "").strip().lower()
    if decision in {"approve", "edit"}:
        return "export"
    return "end"
