"""工作流图装配：10 个节点 + 4 处条件路由 + 检查点/Store 注入。

节点与路由一览（对应规格 5.6）：

    START → understand ─┬→ clarify → END
                        ├→ knowledge ─┬→ END（纯知识问答）
                        │             └→ data（混合问题）
                        └→ data ─┬→ compute → draft → review ─┬→ export → END
                                 ├→ repair → data              └→ END
                                 └→ fail → END

    understand / knowledge / data / review 四处走条件边，其余都是固定边；
    任何节点的失败最终都收敛到 fail 出口，不会把图留在中间状态。
"""

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ops_assistant.workflow.nodes import (
    clarify_node,
    compute_node,
    data_node,
    draft_node,
    export_node,
    fail_node,
    knowledge_node,
    repair_node,
    review_node,
    understand_node,
)
from ops_assistant.workflow.routing import (
    route_after_data,
    route_after_review,
    route_after_understand,
)
from ops_assistant.workflow.state import AssistantContext, AssistantState

# 规格 5.6 规定的 10 个节点名（顺序即文档顺序）
NODE_NAMES = (
    "understand",
    "clarify",
    "knowledge",
    "data",
    "repair",
    "compute",
    "draft",
    "review",
    "export",
    "fail",
)

# 主要条件路由的目标映射（review 的 "end" 映射到 END）
UNDERSTAND_TARGETS = {"clarify": "clarify", "knowledge": "knowledge", "data": "data", "fail": "fail"}
DATA_TARGETS = {"compute": "compute", "repair": "repair", "fail": "fail"}
REVIEW_TARGETS = {"export": "export", "end": END}


def build_graph(
    *,
    settings: Any | None = None,
    checkpointer: Any | None = None,
    store: Any | None = None,
) -> Any:
    """构建并编译工作流图。

    参数：
        settings: 配置对象；节点在运行时从 context 读取，这里只作为装配信号保留。
        checkpointer: 检查点实现；缺省用 ``InMemorySaver``（仅测试/实验路径，
            生产与 API 路径由 ``AppRuntime.create`` 注入 ``SqliteSaver``）。
        store: Store 适配器（本项目为 ``PreferenceStore``）；不传则节点只依赖
            context 注入的偏好存储，功能不受影响。

    返回：
        编译后的图对象，支持 ``invoke`` / ``get_state`` / ``list``。
    """
    del settings  # 节点一律从 Runtime context 取配置，避免图里藏全局状态

    builder = StateGraph(AssistantState, context_schema=AssistantContext)
    builder.add_node("understand", understand_node)
    builder.add_node("clarify", clarify_node)
    builder.add_node("knowledge", knowledge_node)
    builder.add_node("data", data_node)
    builder.add_node("repair", repair_node)
    builder.add_node("compute", compute_node)
    builder.add_node("draft", draft_node)
    builder.add_node("review", review_node)
    builder.add_node("export", export_node)
    builder.add_node("fail", fail_node)

    builder.add_edge(START, "understand")
    builder.add_conditional_edges("understand", route_after_understand, UNDERSTAND_TARGETS)
    builder.add_conditional_edges(
        "knowledge", lambda state: "end" if state.get("status") == "answered" else "data", {"end": END, "data": "data"}
    )
    builder.add_conditional_edges("data", route_after_data, DATA_TARGETS)
    builder.add_edge("repair", "data")
    builder.add_edge("compute", "draft")
    builder.add_edge("draft", "review")
    builder.add_conditional_edges("review", route_after_review, REVIEW_TARGETS)
    builder.add_edge("export", END)
    builder.add_edge("clarify", END)
    builder.add_edge("fail", END)

    if checkpointer is None:
        # 仅测试/实验：进程退出即丢历史，不能跨进程恢复 interrupt
        checkpointer = InMemorySaver()

    if store is not None:
        try:
            return builder.compile(checkpointer=checkpointer, store=store)
        except Exception:  # 教学版 Store 适配器不是官方 BaseStore 时，退化为只注入检查点；
            # 节点仍然从 context 拿到同一个 PreferenceStore，读写行为不变。
            pass
    return builder.compile(checkpointer=checkpointer)
