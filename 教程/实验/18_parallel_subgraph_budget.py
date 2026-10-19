"""实验 18：并行分支 + reducer、子图、预算耗尽。"""

from __future__ import annotations

import dataclasses
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from ops_assistant.config import load_settings
from ops_assistant.workflow.persistence import AppRuntime

QUESTION = "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"


# ---------- 1) 并行：两个分支各写一个字段，用 reducer 合并 ----------
class ParallelState(TypedDict, total=False):
    question: str
    notes: Annotated[list[str], operator.add]   # reducer：追加而不是覆盖


def branch_sql(state: ParallelState) -> dict:
    return {"notes": ["sql: 查了 2026-09 与 2026-08 的月度汇总"]}


def branch_doc(state: ParallelState) -> dict:
    return {"notes": ["doc: 检索了指标口径与退款归属"]}


def join(state: ParallelState) -> dict:
    return {"notes": [f"join: 共收到 {len(state.get('notes') or [])} 条上游结果"]}


def build_parallel_graph():
    builder = StateGraph(ParallelState)
    builder.add_node("sql", branch_sql)
    builder.add_node("doc", branch_doc)
    builder.add_node("join", join)
    # START 同时连两条边：两条分支在同一 superstep 被触发
    builder.add_edge(START, "sql")
    builder.add_edge(START, "doc")
    builder.add_edge("sql", "join")
    builder.add_edge("doc", "join")
    builder.add_edge("join", END)
    return builder.compile()


# ---------- 2) 子图：把两个节点打包成一个节点 ----------
class SubState(TypedDict, total=False):
    value: int
    log: Annotated[list[str], operator.add]


def increment(state: SubState) -> dict:
    return {"value": int(state.get("value") or 0) + 1, "log": ["subgraph: +1"]}


def double(state: SubState) -> dict:
    return {"value": int(state.get("value") or 0) * 2, "log": ["subgraph: *2"]}


def build_subgraph():
    builder = StateGraph(SubState)
    builder.add_node("increment", increment)
    builder.add_node("double", double)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "double")
    builder.add_edge("double", END)
    return builder.compile()


class ParentState(TypedDict, total=False):
    value: int
    log: Annotated[list[str], operator.add]


def parent_start(state: ParentState) -> dict:
    return {"value": int(state.get("value") or 0), "log": ["parent: 开始"]}


def build_parent_graph():
    sub = build_subgraph()
    builder = StateGraph(ParentState)
    builder.add_node("start", parent_start)
    builder.add_node("calc", sub)          # 子图直接当一个节点用
    builder.add_edge(START, "start")
    builder.add_edge("start", "calc")
    builder.add_edge("calc", END)
    return builder.compile()


def main() -> None:
    # --- 并行 ---
    parallel = build_parallel_graph()
    result = parallel.invoke({"question": "demo"})
    print("并行分支写入的 notes：")
    for note in result["notes"]:
        print("  -", note)

    # --- 子图 ---
    parent = build_parent_graph()
    sub_result = parent.invoke({"value": 5})
    print("子图执行后 value =", sub_result["value"], "（期望 (5+1)*2 = 12）")
    print("子图 log =", sub_result["log"])

    # --- 预算耗尽 ---
    settings = load_settings()
    tight = dataclasses.replace(settings, max_steps=2)
    runtime = AppRuntime.create(tight, use_memory_checkpointer=True)
    try:
        turn = runtime.invoke_turn(QUESTION)
        print("预算受限时的状态：", turn["status"])
        print("错误信息：", turn["errors"])
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
