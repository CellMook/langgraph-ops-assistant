"""工作流状态：LangGraph 状态图的状态定义、运行时上下文与初始状态构造。

``AssistantState`` 是图的状态通道（含 reducer 字段），``AssistantContext`` 是
运行时依赖（settings / 连接 / 知识库 / 模型 / 偏好 / 指标）。
依赖通过 ``graph.invoke(..., context=...)`` 注入，节点内用 ``Runtime[AssistantContext]`` 读取。
"""

from __future__ import annotations

import dataclasses
import math
import operator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages
from langgraph.types import Overwrite

from ops_assistant.agent.llm import LLMClient
from ops_assistant.config import Settings
from ops_assistant.knowledge.retrieve import KnowledgeBase
from ops_assistant.observability import MetricsCollector

if TYPE_CHECKING:  # pragma: no cover - 只影响类型检查器
    from ops_assistant.workflow.persistence import PreferenceStore
else:  # 运行时占位：TypedDict 注解在 get_type_hints 时需要一个真实可求值的对象
    PreferenceStore = Any

# 偏好键与默认值：PreferenceStore 里没有记录时使用这些默认值
DEFAULT_USER_ID = "demo-user"
DEFAULT_AMOUNT_UNIT = "元"
DEFAULT_DIMENSION = "region"
PREFERENCE_KEYS = ("amount_unit", "default_dimension")
DEFAULT_PREFERENCES: dict[str, Any] = {
    "amount_unit": DEFAULT_AMOUNT_UNIT,
    "default_dimension": DEFAULT_DIMENSION,
}


def effective_preferences(values: dict[str, Any] | None) -> dict[str, Any]:
    """把存储里的偏好与默认值合并，并过滤掉不认识的键。

    参数：
        values: ``PreferenceStore.all_for_user`` 返回的键值对（可能为空）。

    返回：
        dict：至少包含 ``amount_unit`` 与 ``default_dimension`` 两个键；
        非法取值会被回退成默认值（避免坏数据把报告渲染搞崩）。

    说明：
        取值不是字符串（例如历史脏数据写入了 dict）时同样回退默认值，
        绝不让偏好层把整轮工作流带崩。
    """
    merged = dict(DEFAULT_PREFERENCES)
    for key, value in dict(values or {}).items():
        if key in DEFAULT_PREFERENCES and isinstance(value, str) and value:
            merged[key] = value
    if merged.get("amount_unit") not in {"元", "万元"}:
        merged["amount_unit"] = DEFAULT_AMOUNT_UNIT
    if merged.get("default_dimension") not in {"region"}:
        merged["default_dimension"] = DEFAULT_DIMENSION
    return merged


def jsonable(value: Any) -> Any:
    """把任意对象递归转换为可 JSON 序列化的结构。

    参数：
        value: 任意 Python 对象（dict/list/tuple/set/dataclass/Path/基础类型...）。

    返回：
        Any：只含 str/int/float/bool/None/list/dict 的结构。

    说明：
        LangGraph 的 ``interrupt()`` 载荷、SQLite 检查点与 FastAPI 响应都要求
        JSON 可序列化；这里统一兜底，避免在业务代码里到处写 ``json.dumps``。
        非有限浮点（NaN/Infinity）会被替换成 None，因为标准 JSON 不支持它们。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return jsonable(model_dump())
        except Exception:  # pragma: no cover - pydantic 模型 dump 失败时退化为字符串
            return str(value)
    return str(value)


class AssistantState(TypedDict, total=False):
    """LangGraph 状态图的通道定义。

    字段与规格 5.6 一致；``total=False`` 表示每个字段都可以缺省（节点按需写入）。
    带 reducer 的字段：
        messages: ``add_messages``，按消息 id 追加；
        trace: ``operator.add``，轮内累加执行轨迹，新轮用 Overwrite 清空；
        errors: ``operator.add``，轮内累加错误信息，新轮用 Overwrite 清空。
    """

    # --- 对话与身份 ---
    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    thread_id: str
    user_id: str

    # --- 理解与检索 ---
    spec: dict[str, Any]
    route: str
    knowledge_hits: list[dict[str, Any]]
    policy_context: str

    # --- 数据与计算 ---
    data_result: dict[str, Any] | None
    calculation: dict[str, Any] | None

    # --- 报告与人工审批 ---
    draft_report: str | None
    sources: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    chart: dict[str, Any] | None
    review: dict[str, Any] | None
    final_report: str | None
    export_path: str | None
    report_id: str

    # --- 可观测性与控制 ---
    trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    budget: dict[str, int]
    status: str
    clarification: str | None

    # --- 会话级（跨轮）字段 ---
    # 注意：last_spec 故意不放进 initial_state，否则每轮都会被空值覆盖，
    # 追问时就读不到上一轮的解析结果了（非 reducer 通道会被输入字典替换）。
    last_spec: dict[str, Any] | None


class AssistantContext(TypedDict):
    """图运行时的依赖集合（通过 ``graph.invoke(..., context=...)`` 注入）。

    这样节点无需访问模块级全局变量，测试时可以注入临时目录/临时数据库。
    """

    settings: Settings
    conn: Any
    kb: KnowledgeBase
    llm: LLMClient
    prefs: PreferenceStore
    metrics: MetricsCollector


def initial_state(question: str, thread_id: str, user_id: str, settings: Settings) -> dict[str, Any]:
    """构造一轮对话的初始状态增量。

    参数：
        question: 本轮用户问题。
        thread_id: 会话 id（跨轮稳定，用于检查点与线程隔离）。
        user_id: 用户 id（用于读取会话级偏好）。
        settings: 应用配置（提供默认预算 max_steps / max_tool_calls）。

    返回：
        dict：作为 ``graph.invoke`` 的输入。

    说明：
        1. 这里显式重置"本轮"字段（spec/sources/tables/review/errors 等），
           避免上一轮的内容串到本轮；trace 与 errors 用 Overwrite 显式清空，历史仍保留在检查点中。
        2. 不包含 ``last_spec``：它需要跨轮保留在检查点里（见规格 5.7 追问要求）。
    """
    from uuid import uuid4

    max_steps = int(getattr(settings, "max_steps", 12))
    max_tool_calls = int(getattr(settings, "max_tool_calls", 8))
    return {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "thread_id": thread_id,
        "user_id": user_id or DEFAULT_USER_ID,
        "spec": {},
        "route": "",
        "knowledge_hits": [],
        "policy_context": "",
        "data_result": None,
        "calculation": None,
        "draft_report": None,
        "sources": [],
        "tables": [],
        "chart": None,
        "review": None,
        "final_report": None,
        "export_path": None,
        "report_id": uuid4().hex,
        "approval_interrupt_id": "",
        "trace": Overwrite([]),
        "errors": Overwrite([]),
        "budget": {
            "steps": 0,
            "max_steps": max_steps,
            "tool_calls": 0,
            "max_tool_calls": max_tool_calls,
        },
        "status": "running",
        "clarification": None,
    }
