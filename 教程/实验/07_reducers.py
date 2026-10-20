"""单元 07 实验：默认覆盖 vs 追加 reducer vs add_messages。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/07_reducers.py
不需要模型；数字来自样例数据（与项目口径一致，单位分）。
"""

from __future__ import annotations

import json
import operator
import sys
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

SAMPLE_MONTHS = {
    "2026-09": {"net_cents": 1286500},
    "2026-08": {"net_cents": 1190000},
}


class OpsState(TypedDict, total=False):
    """对照状态：trace 用 operator.add，messages 用 add_messages，status 默认覆盖。"""

    question: str
    spec: dict
    calculation: dict
    status: str
    trace: Annotated[list[dict], operator.add]
    messages: Annotated[list, add_messages]


def node_parse(state: OpsState) -> dict:
    spec = {"current_month": "2026-09", "previous_month": "2026-08", "metric": "net_sales"}
    return {
        "spec": spec,
        "status": "running",
        "trace": [{"node": "parse", "action": "parsed_spec"}],
        "messages": [HumanMessage(content=state.get("question", ""), id="q1")],
    }


def node_compute(state: OpsState) -> dict:
    current = SAMPLE_MONTHS["2026-09"]["net_cents"]
    previous = SAMPLE_MONTHS["2026-08"]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    return {
        "calculation": {
            "current_cents": current,
            "previous_cents": previous,
            "delta_cents": delta,
            "pct_change": pct,
        },
        "status": "answered",
        "trace": [{"node": "compute", "action": "calculated", "delta_cents": delta}],
        "messages": [AIMessage(content=f"差额 {delta} 分，变化率 {pct:.2%}。", id="a1")],
    }


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("parse", node_parse)
    builder.add_node("compute", node_compute)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", "compute")
    builder.add_edge("compute", END)
    return builder.compile()


def demo_messages() -> None:
    print("\n== 第二段：add_messages 的“同 id 替换” ==")

    class ChatState(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    def node_first(state: ChatState) -> dict:
        return {"messages": [HumanMessage(content="原始问题", id="m1")]}

    def node_edit(state: ChatState) -> dict:
        return {"messages": [HumanMessage(content="编辑后的问题", id="m1")]}

    def node_drop(state: ChatState) -> dict:
        return {"messages": [RemoveMessage(id="m1")]}

    builder = StateGraph(ChatState)
    builder.add_node("first", node_first)
    builder.add_node("edit", node_edit)
    builder.add_edge(START, "first")
    builder.add_edge("first", "edit")
    builder.add_edge("edit", END)
    result = builder.compile().invoke({"messages": []})
    print("同 id 写入后条数：", len(result["messages"]))
    print("内容：", [message.content for message in result["messages"]])

    builder2 = StateGraph(ChatState)
    builder2.add_node("first", node_first)
    builder2.add_node("drop", node_drop)
    builder2.add_edge(START, "first")
    builder2.add_edge("first", "drop")
    builder2.add_edge("drop", END)
    dropped = builder2.compile().invoke({"messages": [HumanMessage(content="历史消息", id="old")]})
    print("RemoveMessage 之后条数：", len(dropped["messages"]))
    print("内容：", [message.content for message in dropped["messages"]])


def demo_operator_add_vs_plain() -> None:
    print("\n== 第三段：operator.add vs 默认覆盖 ==")

    class PlainState(TypedDict, total=False):
        plain: list[str]
        added: Annotated[list[str], operator.add]

    def node_a(state: PlainState) -> dict:
        return {"plain": ["a"], "added": ["a"]}

    def node_b(state: PlainState) -> dict:
        return {"plain": ["b"], "added": ["b"]}

    builder = StateGraph(PlainState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    result = builder.compile().invoke({"plain": [], "added": []})
    print(json.dumps(result, ensure_ascii=False))


def main() -> int:
    print("== 单元 07 实验：reducer 与消息状态 ==")
    graph = build_graph()
    final = graph.invoke(
        {
            "question": "2026 年九月净销售额比八月变化多少？",
            "status": "running",
            "trace": [],
            "messages": [],
        }
    )

    print("\n== 第一段：trace 用 operator.add 后不再被覆盖 ==")
    print("trace 条数：", len(final["trace"]))
    for entry in final["trace"]:
        print("  ", json.dumps(entry, ensure_ascii=False))
    print("status（默认覆盖）：", final["status"])
    print("messages 条数：", len(final["messages"]))
    for message in final["messages"]:
        print(f"  {type(message).__name__}(id={message.id}) {message.content}")

    demo_messages()
    demo_operator_add_vs_plain()
    print("\n[结论] 默认覆盖，Annotated 声明合并；消息用 add_messages，轨迹用 operator.add。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
