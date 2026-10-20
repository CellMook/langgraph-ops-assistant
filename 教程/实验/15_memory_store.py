"""单元 15 实验：State / Checkpointer / thread / Store / Runtime Context 的分工。

本脚本不导入 ops_assistant，也不联网：用一个"迷你图"复刻主项目的四个机制，
方便单独观察每一层的行为。运行前不需要任何环境变量。

用法：
    cd ops-assistant
    uv run python ../教程/实验/15_memory_store.py
"""

from __future__ import annotations

import operator
import sqlite3
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
PREFS_DB = OUTPUT_DIR / "preferences.db"

DEFAULT_PREFS: dict[str, str] = {"amount_unit": "元", "default_dimension": "region"}
VALID_DIMENSIONS = ("region", "product", "month")


class MiniContext(TypedDict):
    """运行时依赖：与主项目 AssistantContext 同构，但只保留必需的三项。"""

    store: "MiniStore"
    user_id: str
    note: str


class MiniState(TypedDict, total=False):
    """图状态：messages/trace 带 reducer，last_spec 是跨轮字段。"""

    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    spec: dict[str, Any]
    last_spec: dict[str, Any] | None
    status: str
    trace: Annotated[list[dict[str, Any]], operator.add]


class MiniStore:
    """教学版 Store：SQLite 两列键值表。

    只实现 get/put/all_for_user 三个方法，足以说明"跨 thread 的长期记忆"；
    不是官方 BaseStore 的完整实现（没有 search/向量检索/TTL/batch）。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS prefs (namespace TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, PRIMARY KEY (namespace, key))"
        )
        self._conn.commit()

    @staticmethod
    def namespace_for(user_id: str) -> str:
        return f"users/{user_id}/preferences"

    def put(self, user_id: str, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO prefs (namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (self.namespace_for(user_id), str(key), str(value)),
        )
        self._conn.commit()

    def all_for_user(self, user_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM prefs WHERE namespace = ?", (self.namespace_for(user_id),)
        ).fetchall()
        return {str(key): str(value) for key, value in rows}

    def close(self) -> None:
        self._conn.close()


def effective_prefs(store: MiniStore, user_id: str) -> dict[str, str]:
    """合并默认值与已存偏好；非法值回退成默认（与主项目同口径）。"""
    merged = dict(DEFAULT_PREFS)
    for key, value in store.all_for_user(user_id).items():
        if key in merged and value:
            merged[key] = value
    if merged["default_dimension"] not in VALID_DIMENSIONS:
        merged["default_dimension"] = DEFAULT_PREFS["default_dimension"]
    if merged["amount_unit"] not in {"元", "万元"}:
        merged["amount_unit"] = DEFAULT_PREFS["amount_unit"]
    return merged


def parse_mini(question: str) -> dict[str, Any]:
    """极简解析：只认两个月份、两个地区和"渠道"这个词。"""
    spec: dict[str, Any] = {"current_month": None, "regions": [], "dimension": None}
    for token, month in (("九月", "2026-09"), ("八月", "2026-08")):
        if token in question:
            spec["current_month"] = month
    for region in ("华东", "西南"):
        if region in question:
            spec["regions"].append(region)
    if "渠道" in question:
        spec["dimension"] = "channel"
    elif spec["regions"]:
        spec["dimension"] = "region"
    return spec


def understand_mini(state: MiniState, runtime: Runtime[MiniContext]) -> dict[str, Any]:
    """迷你版 understand：短期记忆（last_spec）+ 长期记忆（store）+ 本轮解析。"""
    context = runtime.context
    store = context["store"]
    user_id = context["user_id"]
    prefs = effective_prefs(store, user_id)
    question = str(state.get("question") or "")

    trace: list[dict[str, Any]] = []
    spec = parse_mini(question)
    last_spec = state.get("last_spec")
    if not isinstance(last_spec, dict):
        last_spec = None

    if spec["current_month"] is None and last_spec and last_spec.get("current_month"):
        spec["current_month"] = str(last_spec["current_month"])
        trace.append({"node": "understand", "action": "inherit_period", "from": spec["current_month"]})

    if not spec["regions"] and last_spec and last_spec.get("regions"):
        spec["regions"] = list(last_spec["regions"])
        trace.append({"node": "understand", "action": "inherit_regions", "from": spec["regions"]})

    if spec["dimension"] is None:
        spec["dimension"] = prefs["default_dimension"]
        trace.append(
            {
                "node": "understand",
                "action": "used_preference",
                "key": "default_dimension",
                "value": prefs["default_dimension"],
            }
        )

    needs_month = spec["current_month"] is None
    if needs_month:
        answer = "请说明要分析的月份（例如“2026 年九月”）。"
        status = "clarification"
    else:
        answer = f"已记录：{spec['current_month']} / {spec['dimension']} / {spec['regions']}"
        status = "answered"

    update: dict[str, Any] = {
        "spec": spec,
        "status": status,
        "messages": [AIMessage(content=answer)],
        "trace": trace,
    }
    if spec["current_month"]:
        update["last_spec"] = dict(spec)
    return update


def build_graph(checkpointer: Any | None, store: MiniStore | None) -> tuple[Any, bool]:
    """装配迷你图；返回 (编译后的图, 是否成功把 store 传进了 compile)。"""
    builder = StateGraph(MiniState, context_schema=MiniContext)
    builder.add_node("understand", understand_mini)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", END)

    if store is not None:
        try:
            return builder.compile(checkpointer=checkpointer, store=store), True
        except Exception:
            # 教学适配器不是官方 BaseStore 时会走到这里：退化为只注入 checkpointer，
            # 节点仍然从 context 读到同一个 store，读写行为不变。
            pass
    return builder.compile(checkpointer=checkpointer), False


def config_for(thread_id: str, user_id: str) -> dict[str, Any]:
    """一次 invoke 的配置：thread_id 决定检查点归属，user_id 只影响偏好。"""
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def run_turn(
    graph: Any,
    store: MiniStore,
    *,
    thread_id: str,
    user_id: str,
    question: str,
) -> dict[str, Any]:
    """跑一轮并返回图状态（含 last_spec 与 trace）。"""
    context: MiniContext = {"store": store, "user_id": user_id, "note": "本对象不入检查点"}
    config = config_for(thread_id, user_id)
    return graph.invoke(
        {"question": question, "messages": [HumanMessage(content=question)]},
        config=config,
        context=context,
    )


def main() -> int:
    """按五段演示：跨轮继承 → 长期偏好 → 快照内容 → 无检查点 → store 注入结果。"""
    print("== 单元 15 实验：多轮记忆、Store 与 Runtime Context ==")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if PREFS_DB.exists():
        PREFS_DB.unlink()
    store = MiniStore(PREFS_DB)
    store.put("demo-user", "default_dimension", "region")

    graph, store_injected = build_graph(InMemorySaver(), store)

    print("\n== 1) 同一 thread 的追问：last_spec 从检查点里读回 ==")
    first = run_turn(
        graph, store, thread_id="thread-a", user_id="demo-user",
        question="2026 年九月销售额比八月变化多少？",
    )
    follow = run_turn(
        graph, store, thread_id="thread-a", user_id="demo-user",
        question="那华东地区呢？",
    )
    print("A1 spec =", first["spec"])
    print("A1 trace =", first["trace"])
    print("A2 spec =", follow["spec"])
    print("A2 trace =", follow["trace"])

    print("\n== 2) 另一个 thread 的同一句追问：没有 last_spec 可继承 ==")
    lonely = run_turn(
        graph, store, thread_id="thread-b", user_id="demo-user",
        question="那华东地区呢？",
    )
    print("B1 status =", lonely["status"])
    print("B1 spec =", lonely["spec"])

    print("\n== 3) 长期记忆：换 thread 也生效（按 user_id，不看 thread） ==")
    store.put("vip-user", "default_dimension", "product")
    print("demo-user 偏好 =", effective_prefs(store, "demo-user"))
    print("vip-user  偏好 =", effective_prefs(store, "vip-user"))
    vip = run_turn(
        graph, store, thread_id="thread-c", user_id="vip-user",
        question="2026 年九月销售额多少？",
    )
    print("C1 spec =", vip["spec"])
    print("C1 trace =", vip["trace"])

    print("\n== 4) 检查点里到底存了什么（context 不在里面） ==")
    snapshot = graph.get_state(config_for("thread-a", "demo-user"))
    print("状态通道 =", sorted(snapshot.values.keys()))
    print("context 是否进了状态 =", "store" in snapshot.values)
    print("next =", snapshot.next)

    print("\n== 5) 没有 checkpointer：同一段追问立刻失忆 ==")
    stateless, _ = build_graph(None, store)
    run_turn(stateless, store, thread_id="thread-d", user_id="demo-user",
             question="2026 年九月销售额比八月变化多少？")
    lost = run_turn(stateless, store, thread_id="thread-d", user_id="demo-user",
                    question="那华东地区呢？")
    print("D2 status =", lost["status"])
    print("D2 spec =", lost["spec"])

    print("\n== 6) 把教学版 Store 传进 compile() 的结果 ==")
    if store_injected:
        print("store 注入 compile：成功（你的版本接受了这个适配器）")
    else:
        print("store 注入 compile：回退为只注入 checkpointer（适配器不是官方 BaseStore）")
    print("→ 无论哪条分支，节点都从 context 读同一个 store，偏好行为不变。")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
