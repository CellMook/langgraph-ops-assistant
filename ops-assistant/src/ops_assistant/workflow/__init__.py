"""工作流包：LangGraph 编排（10 节点 + 3 条件路由 + SQLite 检查点 + 偏好存储）。

对外只暴露稳定的入口，方便 API / UI / CLI 三方复用：

- ``build_graph``：装配并编译图；
- ``AppRuntime``：运行时（连接、知识库、模型、检查点、锁）；
- ``PreferenceStore``：SQLite 偏好存储（同时充当 Store 适配器）；
- 状态常量与工具函数：``initial_state`` / ``effective_preferences`` / ``jsonable``；
- 三个路由函数：纯函数，可脱离图单独测试。
"""

from ops_assistant.workflow.graph import NODE_NAMES, build_graph
from ops_assistant.workflow.persistence import (
    AppRuntime,
    PreferenceItem,
    PreferenceStore,
    ThreadNotFoundError,
)
from ops_assistant.workflow.routing import (
    MAX_REPAIR_ATTEMPTS,
    route_after_data,
    route_after_review,
    route_after_understand,
)
from ops_assistant.workflow.state import (
    DEFAULT_AMOUNT_UNIT,
    DEFAULT_DIMENSION,
    DEFAULT_USER_ID,
    AssistantContext,
    AssistantState,
    effective_preferences,
    initial_state,
    jsonable,
)

__all__ = [
    "AppRuntime",
    "AssistantContext",
    "AssistantState",
    "DEFAULT_AMOUNT_UNIT",
    "DEFAULT_DIMENSION",
    "DEFAULT_USER_ID",
    "MAX_REPAIR_ATTEMPTS",
    "NODE_NAMES",
    "PreferenceItem",
    "PreferenceStore",
    "ThreadNotFoundError",
    "build_graph",
    "effective_preferences",
    "initial_state",
    "jsonable",
    "route_after_data",
    "route_after_review",
    "route_after_understand",
]
