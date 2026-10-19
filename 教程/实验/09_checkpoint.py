"""单元 09 实验：checkpointer、thread 与恢复。

用法（在 ops-assistant 目录执行）：
    uv run python ../教程/实验/09_checkpoint.py            # 第一次运行：写入并展示
    uv run python ../教程/实验/09_checkpoint.py --resume   # 第二次运行：跨进程恢复

实验把 checkpoint 写到 ../教程/实验/_output/checkpoints.db（相对脚本位置）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
DB_PATH = OUTPUT_DIR / "checkpoints.db"
THREAD_ID = "demo-09"


class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    turn: int
    status: str


def node_reply(state: ChatState) -> dict:
    last = state["messages"][-1]
    turn = state.get("turn", 0) + 1
    return {
        "messages": [AIMessage(content=f"第 {turn} 轮收到：{last.content}")],
        "turn": turn,
        "status": "answered",
    }


def build_memory_graph():
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=InMemorySaver())


def build_sqlite_graph(db_path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 注意：SqliteSaver 需要 sqlite3 连接；具体构造方式以你的版本官方示例为准
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=saver), conn


def config_for(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def demo_memory_isolation() -> None:
    print("\n== 第一段：InMemorySaver 的 thread 隔离 ==")
    graph = build_memory_graph()
    a = config_for("thread-a")
    b = config_for("thread-b")

    graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, a)
    graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, a)
    graph.invoke({"messages": [HumanMessage(content="另一个会话的问题")]}, b)

    state_a = graph.get_state(a)
    state_b = graph.get_state(b)
    print("thread-a 消息数：", len(state_a.values["messages"]), "turn：", state_a.values["turn"])
    print("thread-b 消息数：", len(state_b.values["messages"]), "turn：", state_b.values["turn"])
    print("thread-a 内容：", [message.content for message in state_a.values["messages"]])
    print("→ 不同 thread_id 完全隔离。")

    print("\n-- get_state_history：从新到旧 --")
    history = list(graph.get_state_history(a))
    print("快照数：", len(history))
    for snapshot in history[:3]:
        print("  next =", snapshot.next, "turn =", snapshot.values.get("turn"),
              "created_at =", getattr(snapshot, "created_at", "N/A"))


def demo_sqlite_roundtrip() -> None:
    print("\n== 第二段：SqliteSaver 写入（本轮进程内） ==")
    graph, conn = build_sqlite_graph(DB_PATH)
    config = config_for(THREAD_ID)

    existing = graph.get_state(config)
    already = existing.values.get("turn", 0)
    print("已有快照 turn：", already, "（首次运行应为 0/None）")

    graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, config)
    graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, config)
    state = graph.get_state(config)
    print("本轮结束后 turn：", state.values["turn"])
    print("消息数：", len(state.values["messages"]))
    print("checkpoint 文件：", DB_PATH, "存在：", DB_PATH.exists())
    conn.close()


def resume_after_restart() -> None:
    print("\n== 第三段：新进程按同一 thread_id 恢复 ==")
    if not DB_PATH.exists():
        print("[跳过] 没有 checkpoint 文件，请先不带 --resume 运行一次。")
        return
    graph, conn = build_sqlite_graph(DB_PATH)
    config = config_for(THREAD_ID)
    before = graph.get_state(config)
    print("恢复到的 turn：", before.values.get("turn"))
    print("恢复到的消息：", [message.content for message in before.values["messages"]])

    graph.invoke({"messages": [HumanMessage(content="重启后继续问：那华南呢？")]}, config)
    after = graph.get_state(config)
    print("继续一轮后 turn：", after.values["turn"])
    print("消息数：", len(after.values["messages"]))
    print("→ 进程重启后仍能接上文，这就是 SqliteSaver 与 InMemorySaver 的关键差别。")
    conn.close()


def demo_update_state() -> None:
    print("\n== 第四段：update_state 人工修正（谨慎使用） ==")
    graph = build_memory_graph()
    config = config_for("thread-fix")
    graph.invoke({"messages": [HumanMessage(content="原始问题")]}, config)
    graph.update_state(config, {"turn": 99, "status": "manually_fixed"})
    state = graph.get_state(config)
    print("修正后 turn：", state.values["turn"], "status：", state.values["status"])
    print("→ update_state 会产生新快照，不是“原地修改”。审计场景要记录谁改了什么。")


def main() -> int:
    print("== 单元 09 实验：checkpointer、thread 与恢复 ==")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    demo_memory_isolation()
    if "--resume" in sys.argv:
        resume_after_restart()
    else:
        demo_sqlite_roundtrip()
        demo_update_state()
        print("\n下一步：运行 `uv run python ../教程/实验/09_checkpoint.py --resume` 体验跨进程恢复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
