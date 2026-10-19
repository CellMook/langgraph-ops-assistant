"""工作流节点：understand/clarify/knowledge/data/repair/compute/draft/review/export/fail。

节点约定：
1. 依赖一律从 ``Runtime[AssistantContext]`` 读取（settings/conn/kb/llm/prefs/metrics），
   不读模块级全局，测试可以注入临时库；
2. 预算：进入节点先 ``spend_step()``，用尽或拿不到"下一个节点"的余量时写
   ``status="budget_exceeded"``，由路由交给 fail 出口，绝不让图跑成 GraphRecursionError；
3. 工具预算只统计"分析/检索类"调用（知识检索、月份查询、地区查询、修复探测），
   可用月份/地区列表这类元数据读取与确定性纯计算不计入，避免单轮被误判超预算；
4. 所有写进 interrupt 载荷与 SQLite 检查点的数据都必须 JSON 可序列化（用 ``jsonable``）；
5. 副作用（写文件）只允许出现在 export 节点，review 节点在 interrupt 之前只有纯读/纯算。
"""

import dataclasses
import inspect
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from ops_assistant.data.queries import (
    get_month_sales,
    get_region_breakdown,
    list_available_months,
    list_regions,
)
from ops_assistant.domain.metrics import compare_months
from ops_assistant.domain.question import parse_question
from ops_assistant.knowledge.qa import answer_from_knowledge
from ops_assistant.observability import (
    MetricsCollector,
    elapsed_ms_since,
    get_logger,
    log_structured,
)
from ops_assistant.report.export import export_report
from ops_assistant.report.format import format_cents, format_pct, render_analysis_report
from ops_assistant.workflow.state import (
    DEFAULT_DIMENSION,
    AssistantContext,
    AssistantState,
    effective_preferences,
    jsonable,
)

logger = get_logger("workflow.nodes")

# 可重试错误统一前缀：data 节点写进 errors，路由据此决定是否进入 repair
RETRYABLE_PREFIX = "retryable:"
LEGAL_MONTH = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")
TIME_HINT = re.compile(
    r"(20\d{2}\s*年|\d{1,2}\s*月份?|[一二三四五六七八九十]{1,3}\s*月份?"
    r"|本月|这个月|上个月|上月|今年|去年|季度)"
)
DIGITS = re.compile(r"\d")
CAUSAL_WORDS = ("因为", "由于", "所以", "导致", "造成", "引起")
KNOWLEDGE_HINTS = re.compile(r"(口径|退款|归属|定义|是否扣|算不算|规则|政策|怎么算)")

METRIC_LABELS = {"net_sales": "净销售额", "gross_sales": "毛销售额", "refunds": "退款额"}
DIMENSION_LABELS = {"region": "地区", "product": "商品", "month": "月份", "channel": "渠道"}

# 来源段用的"等价 SQL"：实际执行在 data/queries.py，这里只用于把口径写进报告
MONTH_SALES_SQL = (
    "-- 等价 SQL：按订单日期所在月份聚合毛销售额与订单数\n"
    "SELECT strftime('%Y-%m', order_date) AS month,\n"
    "       SUM(quantity * unit_price_cents) AS gross_cents,\n"
    "       COUNT(*) AS order_count\n"
    "FROM orders\n"
    "WHERE strftime('%Y-%m', order_date) = :month\n"
    "GROUP BY month"
)
REFUND_SQL = (
    "-- 等价 SQL：只统计 approved 退款，并按退款发生月归属\n"
    "SELECT COALESCE(SUM(amount_cents), 0) AS refund_cents,\n"
    "       COUNT(*) AS approved_refund_count\n"
    "FROM refunds\n"
    "WHERE status = 'approved'\n"
    "  AND strftime('%Y-%m', refund_date) = :month"
)
REGION_SQL = (
    "-- 等价 SQL：按月、按地区聚合，订单表关联地区表\n"
    "SELECT r.region_id, r.region_name,\n"
    "       SUM(o.quantity * o.unit_price_cents) AS gross_cents,\n"
    "       COUNT(o.order_id) AS order_count\n"
    "FROM orders AS o\n"
    "JOIN regions AS r ON r.region_id = o.region_id\n"
    "WHERE strftime('%Y-%m', o.order_date) = :month\n"
    "GROUP BY r.region_id, r.region_name"
)
COMPARE_SQL = (
    "-- 等价 SQL：两期净额（毛销售额 − approved 退款）\n"
    "SELECT month, gross_cents, refund_cents, gross_cents - refund_cents AS net_cents\n"
    "FROM (\n"
    "  SELECT strftime('%Y-%m', order_date) AS month,\n"
    "         SUM(quantity * unit_price_cents) AS gross_cents\n"
    "  FROM orders GROUP BY month\n"
    ") AS g\n"
    "JOIN (\n"
    "  SELECT strftime('%Y-%m', refund_date) AS month,\n"
    "         SUM(amount_cents) AS refund_cents\n"
    "  FROM refunds WHERE status = 'approved' GROUP BY month\n"
    ") AS r USING (month)\n"
    "WHERE month IN (:current, :previous)"
)

SYSTEM_POLISH_PROMPT = (
    "你是中文经营分析报告的润色助手。只允许把用户给出的那一句中文改写得更通顺；"
    "禁止新增或修改任何数字、日期、百分比；禁止使用“因为/由于/所以/导致/造成/引起”等因果措辞；"
    "禁止编造事实与建议。只输出改写后的那一句话，不要解释。"
)


# ---------------------------------------------------------------------------
# 依赖解析与通用小工具
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """节点依赖集合（从 Runtime context 取出后的只读视图）。"""

    settings: Any
    conn: Any
    kb: Any
    llm: Any
    prefs: Any
    metrics: MetricsCollector


class _MemoryPreferences:
    """PreferenceStore 的最小内存替身。

    只在"context 没传 prefs 且 runtime.store 也没有"这种异常装配下兜底，
    保证节点不会因为偏好存储缺失而崩；真实运行永远走 SQLite 的 PreferenceStore。
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def all_for_user(self, user_id: str) -> dict[str, Any]:
        """返回该用户的全部偏好（内存版，进程退出即丢）。"""
        return dict(self._data.get(str(user_id or ""), {}))

    def set_many(self, user_id: str, values: dict[str, Any]) -> None:
        """批量写入偏好（内存版）。"""
        bucket = self._data.setdefault(str(user_id or ""), {})
        bucket.update({str(key): value for key, value in dict(values or {}).items()})


_MEMORY_PREFS = _MemoryPreferences()


def _resolve_deps(runtime: Any) -> _Deps:
    """从 Runtime 里解析出节点依赖。

    参数：
        runtime: LangGraph 注入的 ``Runtime[AssistantContext]``。

    返回：
        _Deps：settings/conn/kb/llm/prefs/metrics 的只读视图。

    异常：
        RuntimeError：没有注入 context（说明调用方式不对，越早失败越好）。
    """
    context = getattr(runtime, "context", None)
    if context is None and isinstance(runtime, dict):
        context = runtime
    if context is None:
        raise RuntimeError(
            "工作流节点缺少 Runtime context：请用 graph.invoke(..., context=AssistantContext(...)) 注入依赖。"
        )

    def _get(name: str) -> Any:
        if isinstance(context, dict):
            return context.get(name)
        return getattr(context, name, None)

    prefs = _get("prefs")
    if prefs is None:
        store = getattr(runtime, "store", None)
        if store is not None and hasattr(store, "all_for_user"):
            prefs = store
    if prefs is None:
        prefs = _MEMORY_PREFS
    metrics = _get("metrics")
    if metrics is None:
        metrics = MetricsCollector()
    return _Deps(
        settings=_get("settings"),
        conn=_get("conn"),
        kb=_get("kb"),
        llm=_get("llm"),
        prefs=prefs,
        metrics=metrics,
    )


def _status(state: AssistantState) -> str:
    """读取规范化后的状态字符串。"""
    return str(state.get("status") or "").strip().lower()


def _as_dict(value: Any) -> dict[str, Any]:
    """把 dataclass / dict / 普通对象统一成 dict（None 返回空 dict）。"""
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {"value": value}


def _opt_int(value: Any) -> int | None:
    """尽量转成 int，转不了返回 None（不抛异常，避免脏数据带崩报告）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    """尽量转成 float，转不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    """返回 UTC 的 ISO8601 时间字符串（秒精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry(
    node: str,
    action: str,
    ok: bool,
    started: float,
    detail: dict[str, Any] | None = None,
    *,
    tool: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """构造一条执行轨迹（trace）条目。

    参数：
        node: 节点名。
        action: 动作名（例如 parse_question / query_month_sales）。
        ok: 是否成功。
        started: 动作起点（time.perf_counter），用于算耗时。
        detail: 结果摘要（会做 JSON 化）。
        tool: 工具名（可选）。
        retryable: 是否可重试（仅 data/repair 相关条目需要）。

    返回：
        dict：可直接累加进 ``state["trace"]`` 的条目。
    """
    entry: dict[str, Any] = {
        "node": node,
        "action": action,
        "ok": bool(ok),
        "duration_ms": elapsed_ms_since(started),
    }
    if tool:
        entry["tool"] = tool
    if retryable is not None:
        entry["retryable"] = bool(retryable)
    if detail:
        entry["detail"] = jsonable(detail)
    return entry


@contextmanager
def _tool_span(
    metrics: MetricsCollector,
    tool: str,
    *,
    thread_id: str,
    node: str,
    params: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """工具调用计时/计数上下文。

    用法::

        with _tool_span(metrics, "query_month_sales", thread_id=tid, node="data") as span:
            data = get_month_sales(conn, month)
            span["summary"] = {"orders": data.order_count}

    参数：
        metrics: 指标采集器。
        tool: 工具名。
        thread_id: 会话 id。
        node: 节点名。
        params: 入参摘要（会写进结构化日志）。

    异常路径会记录一次失败工具调用并重新抛出，由调用方决定是否降级。
    """
    started = time.perf_counter()
    box: dict[str, Any] = {}
    try:
        yield box
    except Exception as exc:
        metrics.record_tool(
            tool=tool,
            ok=False,
            thread_id=thread_id,
            node=node,
            duration_ms=elapsed_ms_since(started),
            error_code=type(exc).__name__,
            params=params,
        )
        raise
    metrics.record_tool(
        tool=tool,
        ok=True,
        thread_id=thread_id,
        node=node,
        duration_ms=elapsed_ms_since(started),
        params=params,
        summary=box.get("summary") if isinstance(box.get("summary"), dict) else None,
    )


def _call_with_supported_kwargs(func: Any, *args: Any, **kwargs: Any) -> Any:
    """只传目标函数签名支持的 kwargs 再调用（兼容同组模块的参数演进）。

    参数：
        func: 目标函数。
        *args: 位置参数。
        **kwargs: 期望的关键字参数，签名不支持会被丢弃。

    返回：
        目标函数的返回值。

    说明：
        工作流是跨模块调用方（domain/knowledge/data 三组并行实现），
        参数名可能有细微差异；这里做一次签名过滤，避免因为多传一个 ``metric``
        就整轮失败，同时不掩盖真正的参数错误（位置参数仍然原样传递）。
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    parameters = list(signature.parameters.values())
    if any(item.kind == item.VAR_KEYWORD for item in parameters):
        return func(*args, **kwargs)
    allowed = {
        item.name
        for item in parameters
        if item.kind in (item.POSITIONAL_OR_KEYWORD, item.KEYWORD_ONLY)
    }
    return func(*args, **{key: value for key, value in kwargs.items() if key in allowed})


def _prefs_for_user(deps: _Deps, user_id: str, *, thread_id: str) -> dict[str, Any]:
    """读取用户偏好并合并默认值；读失败只记日志，不影响主流程。"""
    try:
        values = deps.prefs.all_for_user(user_id) or {}
    except Exception as exc:  # 偏好读失败不应让整轮失败
        deps.metrics.record_error(
            code="PREF_READ_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            thread_id=thread_id,
            node="understand",
        )
        values = {}
    return effective_preferences(values if isinstance(values, dict) else {})


class BudgetTracker:
    """单轮预算累计器：步数与工具调用次数分开统计。

    步数语义：每进入一个节点 +1；``spend_step()`` 返回 False 表示已超上限，
    调用方应立即写 ``status="budget_exceeded"``；``has_step_room()`` 用于确认
    "本轮还剩至少一步给下一个节点"，避免在最后一格预算里进入死路。
    """

    def __init__(self, state: AssistantState, settings: Any) -> None:
        raw = state.get("budget") or {}
        if not isinstance(raw, dict):
            raw = {}
        self.max_steps = int(raw.get("max_steps") or getattr(settings, "max_steps", 12) or 12)
        self.max_tool_calls = int(raw.get("max_tool_calls") or getattr(settings, "max_tool_calls", 8) or 8)
        self.steps = int(raw.get("steps") or 0)
        self.tool_calls = int(raw.get("tool_calls") or 0)

    def spend_step(self) -> bool:
        """消耗一步；返回是否仍在预算内。"""
        self.steps += 1
        return self.steps <= self.max_steps

    def has_step_room(self) -> bool:
        """是否还有余量（至少能再走一步）。"""
        return self.steps < self.max_steps

    def spend_tools(self, count: int = 1) -> bool:
        """消耗 count 次工具调用；返回是否仍在预算内。"""
        self.tool_calls += max(int(count), 0)
        return self.tool_calls <= self.max_tool_calls

    def has_tool_room(self, count: int = 1) -> bool:
        """是否还能再调用 count 次工具。"""
        return self.tool_calls + max(int(count), 0) <= self.max_tool_calls

    def as_dict(self) -> dict[str, int]:
        """导出可写入状态的预算快照。"""
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.max_tool_calls,
        }


def _budget_exceeded_update(
    state: AssistantState,
    budget: BudgetTracker,
    *,
    node: str,
    started: float,
    action: str = "budget_check",
    route: str | None = None,
) -> dict[str, Any]:
    """构造"预算用尽"的状态增量（status=budget_exceeded，交给 fail 出口）。"""
    message = (
        f"BUDGET_EXCEEDED: 单轮预算已用尽（steps={budget.steps}/{budget.max_steps}，"
        f"tool_calls={budget.tool_calls}/{budget.max_tool_calls}），工作流提前结束。"
        "可以缩小问题范围（例如只问一个地区），或调高 OPS_MAX_STEPS / OPS_MAX_TOOL_CALLS。"
    )
    update: dict[str, Any] = {
        "status": "budget_exceeded",
        "budget": budget.as_dict(),
        "errors": [message],
        "trace": [
            _entry(
                node,
                action,
                False,
                started,
                {
                    "steps": budget.steps,
                    "max_steps": budget.max_steps,
                    "tool_calls": budget.tool_calls,
                    "max_tool_calls": budget.max_tool_calls,
                },
            )
        ],
    }
    if route is not None:
        update["route"] = route
    return update


def _guard_step(
    state: AssistantState,
    deps: _Deps,
    *,
    node: str,
    started: float,
) -> tuple[BudgetTracker, dict[str, Any] | None]:
    """步数预算守卫：返回 (累计器, 失败增量或 None)。

    判定规则：消耗一步后仍在上限内、且还剩至少一步给下一个节点，才允许继续。
    """
    budget = BudgetTracker(state, deps.settings)
    if budget.spend_step() and budget.has_step_room():
        return budget, None
    return budget, _budget_exceeded_update(state, budget, node=node, started=started)


def _guard_tools(
    state: AssistantState,
    deps: _Deps,
    *,
    node: str,
    started: float,
    budget: BudgetTracker,
    count: int = 1,
) -> dict[str, Any] | None:
    """工具预算守卫：超限时返回失败增量，否则返回 None。"""
    if budget.spend_tools(count):
        return None
    return _budget_exceeded_update(
        state,
        budget,
        node=node,
        started=started,
        action="tool_budget_check",
    )


def _previous_month_of(month: str) -> str | None:
    """返回上一个自然月（YYYY-MM）；无法解析时返回 None。"""
    match = LEGAL_MONTH.match(str(month or ""))
    if not match:
        return None
    year = int(match.group(1))
    number = int(match.group(2))
    if number == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{number - 1:02d}"


def _has_explicit_time(question: str) -> bool:
    """问题里是否出现了明确的时间措辞（出现但解析失败时应澄清，而不是沿用历史）。"""
    return bool(TIME_HINT.search(str(question or "")))


def _spec_to_dict(spec: Any) -> dict[str, Any]:
    """把 QuestionSpec 转成 JSON 友好的 dict。"""
    data = jsonable(spec)
    if isinstance(data, dict):
        return dict(data)
    return {"raw": data}


def _range_hint(min_month: str | None, max_month: str | None, regions: list[str]) -> str:
    """拼一句"请补充时间范围"的澄清话术，附上可用的数据范围与地区。"""
    text = "请补充要分析的时间范围，例如“2026 年九月销售额比八月变化多少”。"
    extras: list[str] = []
    if min_month and max_month:
        extras.append(f"可用数据范围：{min_month} ~ {max_month}")
    if regions:
        extras.append("可用地区：" + "、".join(regions[:8]))
    if extras:
        text += "；" + "；".join(extras) + "。"
    return text


def _sql_source(sql: str, params: dict[str, Any], *, note: str) -> dict[str, Any]:
    """构造一条 SQL 来源（写进报告第 8 段与 TurnResult.sources）。"""
    return {"type": "sql", "sql": sql, "params": jsonable(params), "note": note}


def _citation_dict(item: Any) -> dict[str, Any]:
    """把知识层返回的引用（dict / SearchHit / dataclass）统一成 dict。"""
    if isinstance(item, dict):
        return {
            "doc_id": str(item.get("doc_id") or ""),
            "chunk_id": str(item.get("chunk_id") or ""),
            "title": str(item.get("title") or ""),
            "snippet": str(item.get("snippet") or ""),
            "score": _opt_float(item.get("score")),
            "method": str(item.get("method") or ""),
        }
    citation = getattr(item, "citation", None)
    if callable(citation):
        try:
            return _citation_dict(dict(citation()))
        except Exception:  # pragma: no cover - 引用对象异常时退化为字段读取
            pass
    chunk = getattr(item, "chunk", None)
    return {
        "doc_id": str(getattr(item, "doc_id", None) or getattr(chunk, "doc_id", "") or ""),
        "chunk_id": str(getattr(item, "chunk_id", None) or getattr(chunk, "chunk_id", "") or ""),
        "title": str(getattr(item, "title", None) or getattr(chunk, "title", "") or ""),
        "snippet": str(getattr(item, "snippet", None) or getattr(chunk, "text", "") or ""),
        "score": _opt_float(getattr(item, "score", None)),
        "method": str(getattr(item, "method", "") or ""),
    }


def _doc_sources(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把引用列表转成来源条目，并按 (doc_id, chunk_id) 去重。"""
    seen: set[tuple[str, str]] = set()
    sources: list[dict[str, Any]] = []
    for item in items:
        doc_id = str(item.get("doc_id") or "").strip()
        chunk_id = str(item.get("chunk_id") or "").strip()
        if not doc_id and not chunk_id:
            continue
        key = (doc_id, chunk_id)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "type": "doc",
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "title": str(item.get("title") or ""),
                "snippet": str(item.get("snippet") or "")[:300],
                "score": item.get("score"),
                "method": str(item.get("method") or ""),
                "ref": f"[{doc_id}#{chunk_id}]",
            }
        )
    return sources


def _knowledge_query(question: str, spec: dict[str, Any]) -> str:
    """拼知识库检索用的 query：问题 + 口径关键词 + 维度提示。"""
    parts = [question.strip()]
    parts.append("退款口径 净销售额定义")
    regions = [str(item) for item in (spec.get("regions") or []) if str(item).strip()]
    if regions:
        parts.append("地区 " + "、".join(regions[:4]))
    return " ".join(part for part in parts if part)[:240]


def _call_answer_from_knowledge(query: str, kb: Any, llm: Any) -> Any:
    """调用知识层问答函数，兼容位置参数与关键字参数两种签名。"""
    try:
        signature = inspect.signature(answer_from_knowledge)
    except (TypeError, ValueError):
        return answer_from_knowledge(query, kb, llm)
    parameters = list(signature.parameters.values())
    if len(parameters) >= 2 and parameters[1].kind in (
        parameters[1].POSITIONAL_ONLY,
        parameters[1].POSITIONAL_OR_KEYWORD,
    ):
        return answer_from_knowledge(query, kb, llm)
    return answer_from_knowledge(query, kb=kb, llm=llm)


# ---------------------------------------------------------------------------
# 1) understand 节点
# ---------------------------------------------------------------------------


def understand_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """理解节点：解析问题、维护追问上下文、决定下一步 route。

    读取：``question`` / ``last_spec`` / ``user_id`` / ``budget``。
    写入：``spec`` / ``route`` / ``clarification`` / ``status`` / ``budget`` / ``trace``，
    必要时同时写入 ``last_spec``（跨轮保留，供追问使用）。

    规则（规格 5.7）：
        - 问题缺少时间范围时沿用上一轮的时间范围，并在 trace 记一条
          ``inherit_period_from_last_turn``；
        - 问题里出现地区词时覆盖历史地区，没出现则沿用历史地区（记
          ``inherit_regions_from_last_turn``）；
        - 带时间措辞但解析不出具体月份（例如只写"九月"）时走澄清，不猜年份。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    budget, blocked = _guard_step(state, deps, node="understand", started=started)
    if blocked is not None:
        blocked["route"] = "fail"
        return blocked

    question = str(state.get("question") or "").strip()
    trace: list[dict[str, Any]] = []
    if not question:
        trace.append(_entry("understand", "validate_question", False, started, {"reason": "empty"}))
        return {
            "status": "failed",
            "route": "fail",
            "budget": budget.as_dict(),
            "errors": [
                "EMPTY_QUESTION: 问题为空，请描述要分析的时间范围与指标，"
                "例如“2026 年九月销售额比八月变化多少，并按地区分解”。"
            ],
            "trace": trace,
        }

    # 元数据只用于解析校验与澄清话术，不占用工具预算（见模块说明第 3 条）
    try:
        raw_months = list_available_months(deps.conn) or []
        raw_regions = list_regions(deps.conn) or []
    except Exception as exc:
        trace.append(_entry("understand", "load_metadata", False, started, {"error": type(exc).__name__}))
        return {
            "status": "failed",
            "route": "fail",
            "budget": budget.as_dict(),
            "errors": [f"METADATA_ERROR: 读取可用月份/地区失败（{type(exc).__name__}），无法解析问题。"],
            "trace": trace,
        }
    months = [str(item) for item in raw_months if str(item).strip()]
    region_dicts = [_as_dict(item) for item in raw_regions]
    # 解析层需要 [{"region_id", "region_name"}]（命中后返回 region_id）；
    # 澄清话术里则展示地区中文名，两者不要混用。
    region_names = [
        str(item.get("region_name") or item.get("region_id") or "")
        for item in region_dicts
    ]
    region_names = [name for name in region_names if name]
    min_month = min(months) if months else None
    max_month = max(months) if months else None

    try:
        spec = _call_with_supported_kwargs(
            parse_question,
            question,
            known_regions=region_dicts,
            min_month=min_month,
            max_month=max_month,
        )
    except Exception as exc:
        trace.append(_entry("understand", "parse_question", False, started, {"error": type(exc).__name__}))
        return {
            "status": "failed",
            "route": "fail",
            "budget": budget.as_dict(),
            "errors": [f"PARSE_ERROR: 问题解析失败（{type(exc).__name__}），请换一种说法再试。"],
            "trace": trace,
        }
    spec_dict = _spec_to_dict(spec)
    trace.append(
        _entry(
            "understand",
            "parse_question",
            True,
            started,
            {
                "dimension": spec_dict.get("dimension"),
                "metric": spec_dict.get("metric"),
                "current_month": spec_dict.get("current_month"),
                "regions": spec_dict.get("regions"),
            },
        )
    )

    last_spec = state.get("last_spec")
    if not isinstance(last_spec, dict):
        last_spec = None
    current_month = str(spec_dict.get("current_month") or "").strip()
    regions_in_spec = [str(item) for item in (spec_dict.get("regions") or []) if str(item).strip()]
    needs_clarification = bool(spec_dict.get("needs_clarification"))

    if last_spec is not None:
        if not current_month and not _has_explicit_time(question) and last_spec.get("current_month"):
            inherited_month = str(last_spec.get("current_month") or "").strip()
            if LEGAL_MONTH.match(inherited_month):
                spec_dict["current_month"] = inherited_month
                spec_dict["previous_month"] = (
                    str(last_spec.get("previous_month") or "").strip()
                    or _previous_month_of(inherited_month)
                )
                current_month = inherited_month
                needs_clarification = False
                trace.append(
                    _entry(
                        "understand",
                        "inherit_period_from_last_turn",
                        True,
                        started,
                        {
                            "current_month": inherited_month,
                            "previous_month": spec_dict.get("previous_month"),
                        },
                    )
                )
        if not regions_in_spec:
            inherited_regions = [str(item) for item in (last_spec.get("regions") or []) if str(item).strip()]
            if inherited_regions:
                spec_dict["regions"] = inherited_regions
                regions_in_spec = inherited_regions
                trace.append(
                    _entry(
                        "understand",
                        "inherit_regions_from_last_turn",
                        True,
                        started,
                        {"regions": inherited_regions},
                    )
                )

    user_id = str(state.get("user_id") or "")
    prefs_values = _prefs_for_user(deps, user_id, thread_id=thread_id)
    dimension = str(spec_dict.get("dimension") or "").strip()
    if dimension not in {"region", "product", "month", "channel"}:
        spec_dict["dimension"] = str(prefs_values.get("default_dimension") or DEFAULT_DIMENSION)
        trace.append(
            _entry(
                "understand",
                "apply_dimension_preference",
                True,
                started,
                {"dimension": spec_dict["dimension"]},
            )
        )

    wants_comparison = bool(spec_dict.get("wants_comparison", True))
    previous_month = str(spec_dict.get("previous_month") or "").strip()
    if current_month and not previous_month and wants_comparison:
        derived = _previous_month_of(current_month)
        if derived:
            spec_dict["previous_month"] = derived
            previous_month = derived
            trace.append(
                _entry(
                    "understand",
                    "derive_previous_month",
                    True,
                    started,
                    {"current_month": current_month, "previous_month": derived},
                )
            )

    dimension = str(spec_dict.get("dimension") or "region")
    route = "data"
    status = "running"
    clarification: str | None = None
    if dimension == "channel":
        trace.append(
            _entry("understand", "flag_unsupported_dimension", True, started, {"dimension": "channel"})
        )

    if needs_clarification or not current_month:
        route = "clarify"
        status = "clarification"
        clarification = str(spec_dict.get("clarification") or "").strip() or _range_hint(
            min_month, max_month, region_names
        )
    elif not LEGAL_MONTH.match(current_month):
        route = "clarify"
        status = "clarification"
        clarification = (
            f"时间范围“{current_month}”不是合法的年月，请按“2026 年九月”这类说法提问"
            f"（可用数据范围 {min_month or '未知'} ~ {max_month or '未知'}）。"
        )
    elif min_month and max_month and (current_month < min_month or current_month > max_month):
        route = "clarify"
        status = "clarification"
        clarification = f"可用数据范围是 {min_month} ~ {max_month}，您问的 {current_month} 不在范围内，请换一个月份。"
    elif months and current_month not in months:
        # 在范围内但该月没有订单：不澄清，交给 data 层用"无记录"口径说明
        trace.append(_entry("understand", "month_without_records", True, started, {"month": current_month}))

    if route == "data" and KNOWLEDGE_HINTS.search(question):
        route = "knowledge"
        trace.append(_entry("understand", "route_to_knowledge", True, started, {"reason": "口径类问题"}))

    update: dict[str, Any] = {
        "spec": spec_dict,
        "route": route,
        "status": status,
        "clarification": clarification,
        "budget": budget.as_dict(),
        "trace": trace,
    }
    if spec_dict.get("current_month") or spec_dict.get("regions"):
        update["last_spec"] = dict(spec_dict)
    return update


# ---------------------------------------------------------------------------
# 2) clarify 节点
# ---------------------------------------------------------------------------


def clarify_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """澄清节点：把缺少的信息直接问回用户，不猜、不编造。

    读取：``clarification``；写入：``status="clarification"``、``messages``、``trace``。
    """
    _resolve_deps(runtime)  # 确认 Runtime context 已注入（缺依赖时应尽早报错）
    started = time.perf_counter()
    clarification = str(state.get("clarification") or "").strip() or "请补充要分析的时间范围与地区，我再继续。"
    message = f"需要您补充一点信息：{clarification}"
    return {
        "status": "clarification",
        "clarification": clarification,
        "messages": [AIMessage(content=message)],
        "trace": [_entry("clarify", "emit_clarification", True, started, {"clarification": clarification})],
    }


# ---------------------------------------------------------------------------
# 3) knowledge 节点
# ---------------------------------------------------------------------------


def knowledge_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """知识检索节点：为口径类问题补充可引用的资料片段。

    读取：``question`` / ``spec``；写入：``policy_context`` / ``knowledge_hits`` /
    ``sources``（追加 doc 来源）/ ``trace``。

    降级策略：知识库或模型不可用时只记一条 ``KNOWLEDGE_ERROR``，
    报告仍会生成（"口径与依据"段会说明本轮没有知识库结论）。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    budget, blocked = _guard_step(state, deps, node="knowledge", started=started)
    if blocked is not None:
        return blocked
    spec = state.get("spec") if isinstance(state.get("spec"), dict) else {}
    question = str(state.get("question") or "")
    query = _knowledge_query(question, spec)
    blocked = _guard_tools(state, deps, node="knowledge", started=started, budget=budget)
    if blocked is not None:
        return blocked

    trace: list[dict[str, Any]] = []
    errors: list[str] = []
    policy_context = ""
    citations: list[dict[str, Any]] = []
    sources = list(state.get("sources") or [])
    try:
        llm_arg = deps.llm if getattr(deps.llm, "enabled", False) else None
        with _tool_span(
            deps.metrics, "search_knowledge", thread_id=thread_id, node="knowledge", params={"query": query[:80]}
        ) as span:
            answer = _call_answer_from_knowledge(query, deps.kb, llm_arg)
            raw_citations = list(getattr(answer, "citations", None) or [])
            span["summary"] = {"citation_count": len(raw_citations)}
        policy_context = str(getattr(answer, "answer", "") or "").strip()
        citations = [_citation_dict(item) for item in raw_citations]
        if not citations:
            citations = [_citation_dict(item) for item in (getattr(answer, "hits", None) or [])]
        insufficient = bool(getattr(answer, "insufficient", False))
        trace.append(
            _entry(
                "knowledge",
                "answer_from_knowledge",
                True,
                started,
                {
                    "citation_count": len(citations),
                    "insufficient": insufficient,
                    "chars": len(policy_context),
                },
            )
        )
    except Exception as exc:  # 知识层失败不阻断主流程（规格 5.12 的可降级要求）
        trace.append(_entry("knowledge", "answer_from_knowledge", False, started, {"error": type(exc).__name__}))
        errors.append(
            f"KNOWLEDGE_ERROR: 知识库检索失败（{type(exc).__name__}），本轮报告不包含知识库依据，其余流程继续。"
        )
        log_structured(
            event="knowledge.error",
            thread_id=thread_id,
            node="knowledge",
            ok=False,
            error_code=type(exc).__name__,
        )

    sources.extend(_doc_sources(citations))
    return {
        "policy_context": policy_context,
        "knowledge_hits": citations,
        "sources": sources,
        "trace": trace,
        "errors": errors,
        "budget": budget.as_dict(),
    }


# ---------------------------------------------------------------------------
# 4) data 节点
# ---------------------------------------------------------------------------


def data_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """数据查询节点：按月份取两期销量、按地区取分解明细。

    读取：``spec`` / ``sources``；写入：``data_result`` / ``tables`` / ``sources``（追加 SQL 来源）/
    ``trace``；失败时写 ``errors``，可重试错误会带 ``retryable:`` 前缀。

    注意：SQLite 的 ``OperationalError`` 被视为可重试（例如临时锁、表刚被重建），
    其它异常（包括表结构缺失）视为不可重试，直接走 fail 出口并给出可读原因。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    status = _status(state)
    if status in {"failed", "budget_exceeded"}:
        return {
            "status": status,
            "trace": [_entry("data", "skip", False, started, {"reason": status})],
        }
    budget, blocked = _guard_step(state, deps, node="data", started=started)
    if blocked is not None:
        return blocked

    spec = state.get("spec") if isinstance(state.get("spec"), dict) else {}
    current_month = str(spec.get("current_month") or "").strip()
    previous_month = str(spec.get("previous_month") or "").strip() or None
    trace: list[dict[str, Any]] = []

    if not LEGAL_MONTH.match(current_month):
        trace.append(_entry("data", "validate_month", False, started, {"month": current_month}))
        return {
            "status": "failed",
            "data_result": None,
            "budget": budget.as_dict(),
            "errors": [
                f"INVALID_MONTH: 无法识别的月份“{current_month or '（空）'}”，"
                "请使用 YYYY-MM 或“2026 年九月”这类说法。"
            ],
            "trace": trace,
        }
    if previous_month and not LEGAL_MONTH.match(previous_month):
        fixed = _previous_month_of(current_month)
        trace.append(_entry("data", "fix_previous_month", True, started, {"previous_month": fixed}))
        previous_month = fixed

    blocked = _guard_tools(state, deps, node="data", started=started, budget=budget, count=2)
    if blocked is not None:
        return blocked

    sources = list(state.get("sources") or [])
    try:
        with _tool_span(
            deps.metrics,
            "query_month_sales",
            thread_id=thread_id,
            node="data",
            params={"current_month": current_month, "previous_month": previous_month},
        ) as span:
            current = _as_dict(get_month_sales(deps.conn, current_month))
            previous = _as_dict(get_month_sales(deps.conn, previous_month)) if previous_month else None
            span["summary"] = {
                "current_orders": _opt_int(current.get("order_count")) or 0,
                "has_previous": previous is not None,
            }
        trace.append(
            _entry(
                "data",
                "query_month_sales",
                True,
                started,
                {
                    "current_month": current_month,
                    "previous_month": previous_month,
                    "current_has_orders": bool(current.get("has_orders")),
                },
            )
        )

        blocked = _guard_tools(state, deps, node="data", started=started, budget=budget, count=2)
        if blocked is not None:
            return blocked
        with _tool_span(
            deps.metrics,
            "query_region_breakdown",
            thread_id=thread_id,
            node="data",
            params={"current_month": current_month, "previous_month": previous_month},
        ) as span:
            current_regions = [_as_dict(item) for item in (get_region_breakdown(deps.conn, current_month) or [])]
            previous_regions = (
                [_as_dict(item) for item in (get_region_breakdown(deps.conn, previous_month) or [])]
                if previous_month
                else []
            )
            span["summary"] = {
                "current_region_count": len(current_regions),
                "previous_region_count": len(previous_regions),
            }
        trace.append(
            _entry(
                "data",
                "query_region_breakdown",
                True,
                started,
                {"current_region_count": len(current_regions), "previous_region_count": len(previous_regions)},
            )
        )
    except sqlite3.Error as exc:
        message = (
            f"{RETRYABLE_PREFIX}sqlite3.{type(exc).__name__}: 数据查询失败（{exc}）；"
            "将进入 repair 节点修复并重试一次。"
        )
        trace.append(
            _entry("data", "query_failed", False, started, {"error": type(exc).__name__}, retryable=True)
        )
        log_structured(
            event="data.error",
            thread_id=thread_id,
            node="data",
            ok=False,
            error_code=type(exc).__name__,
        )
        return {
            "status": "running",
            "data_result": None,
            "errors": [message],
            "trace": trace,
            "budget": budget.as_dict(),
        }
    except Exception as exc:
        trace.append(
            _entry("data", "query_failed", False, started, {"error": type(exc).__name__}, retryable=False)
        )
        return {
            "status": "failed",
            "data_result": None,
            "errors": [
                f"DATA_QUERY_ERROR: 数据查询失败（{type(exc).__name__}: {exc}），"
                "本轮无法生成报告；请先运行 scripts/bootstrap.py 初始化演示数据。"
            ],
            "trace": trace,
            "budget": budget.as_dict(),
        }

    sources.extend(
        [
            _sql_source(MONTH_SALES_SQL, {"month": current_month}, note="本期毛销售额与订单数"),
            _sql_source(REFUND_SQL, {"month": current_month}, note="本期 approved 退款（按退款月归属）"),
            _sql_source(REGION_SQL, {"month": current_month}, note="本期按地区聚合，用于维度分解"),
        ]
    )
    tables = list(state.get("tables") or [])
    if current_regions:
        tables = [
            {
                "title": f"{current_month} 地区明细（单位：分，未换算）",
                "columns": ["地区编号", "地区", "毛销售额(分)", "退款额(分)", "净销售额(分)", "订单数"],
                "rows": [
                    [
                        item.get("region_id"),
                        item.get("region_name"),
                        _opt_int(item.get("gross_cents")),
                        _opt_int(item.get("refund_cents")),
                        _opt_int(item.get("net_cents")),
                        _opt_int(item.get("order_count")),
                    ]
                    for item in current_regions
                ],
            }
        ]

    data_result = {
        "current_month": current_month,
        "previous_month": previous_month,
        "current": current,
        "previous": previous,
        "current_regions": current_regions,
        "previous_regions": previous_regions,
        "dimension": spec.get("dimension"),
        "metric": spec.get("metric"),
        "regions_filter": [str(item) for item in (spec.get("regions") or [])],
        "fetched_at": _now_iso(),
    }
    return {
        "data_result": data_result,
        "tables": tables,
        "sources": sources,
        "trace": trace,
        "budget": budget.as_dict(),
    }


# ---------------------------------------------------------------------------
# 5) repair 节点
# ---------------------------------------------------------------------------


def repair_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """修复节点：对可重试的数据错误做一次探活修复，然后回到 data 重试。

    读取：``trace``（统计已修复次数）；写入：``errors``（失败时带可重试前缀）/ ``trace``。

    说明：
        本节点不做任何写库操作，只做只读探活（``SELECT 1``）并把"重试过一次"
        记录进轨迹；真正的重试由路由回到 data 节点完成。这样即使修复失败，
        历史也不会被破坏。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    budget, blocked = _guard_step(state, deps, node="repair", started=started)
    if blocked is not None:
        return blocked
    blocked = _guard_tools(state, deps, node="repair", started=started, budget=budget, count=1)
    if blocked is not None:
        return blocked

    attempts = (
        sum(
            1
            for item in (state.get("trace") or [])
            if isinstance(item, dict) and item.get("node") == "repair"
        )
        + 1
    )
    trace: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with _tool_span(
            deps.metrics,
            "repair_probe",
            thread_id=thread_id,
            node="repair",
            params={"attempt": attempts},
        ) as span:
            row = deps.conn.execute("SELECT 1").fetchone()
            span["summary"] = {"probe": "ok" if row else "empty"}
        trace.append(
            _entry(
                "repair",
                "retry_data_query",
                True,
                started,
                {"attempt": attempts, "probe": "ok"},
            )
        )
    except sqlite3.Error as exc:
        trace.append(
            _entry(
                "repair",
                "retry_data_query",
                False,
                started,
                {"attempt": attempts, "error": type(exc).__name__},
                retryable=False,
            )
        )
        errors.append(
            f"{RETRYABLE_PREFIX}sqlite3.{type(exc).__name__}: 修复探活仍然失败（{exc}）；"
            "下一次失败将走 fail 出口，不再重试。"
        )
    return {"errors": errors, "trace": trace, "budget": budget.as_dict()}


# ---------------------------------------------------------------------------
# 6) compute 节点
# ---------------------------------------------------------------------------


def _region_delta_dict(item: Any) -> dict[str, Any]:
    """把指标层的地区分解结果统一成报告需要的字段（兼容字段别名）。"""
    raw = _as_dict(item)
    region_id = str(raw.get("region_id") or raw.get("id") or raw.get("region") or "")
    return {
        "region_id": region_id,
        "region_name": str(raw.get("region_name") or raw.get("name") or region_id),
        "current_net_cents": _opt_int(raw.get("current_net_cents", raw.get("current"))),
        "previous_net_cents": _opt_int(raw.get("previous_net_cents", raw.get("previous"))),
        "delta_cents": _opt_int(raw.get("delta_cents", raw.get("delta"))),
        "share_of_delta": _opt_float(raw.get("share_of_delta", raw.get("share"))),
    }


def _matches_regions(region: dict[str, Any], selected: list[str]) -> bool:
    """判断一条地区记录是否命中用户指定的地区过滤（编号或名称任一命中即可）。"""
    if not selected:
        return True
    region_id = str(region.get("region_id") or "")
    region_name = str(region.get("region_name") or "")
    return region_id in selected or region_name in selected


def _display_amount(cents: int | None, unit: str) -> float | None:
    """把"分"换算成图表用的展示数值（元或万元，浮点）。"""
    if cents is None:
        return None
    divisor = 100.0 if unit == "元" else 1_000_000.0
    return round(int(cents) / divisor, 2)


def compute_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """计算节点：调用指标层算两期差额与维度分解，整理成报告输入。

    读取：``spec`` / ``data_result``；写入：``calculation`` / ``tables`` / ``chart`` /
    ``sources``（追加对比 SQL）/ ``trace`` / ``errors``。

    说明：
        指标层负责地区分解与合计校验；净额、差额、变化率在本节点按
        ``net_cents`` 再算一次，保证报告里"差额 = 本期 − 上期"严格自洽。
        两者不一致时只记一条 ``COMPUTE_WARNING``，不改口径、不猜原因。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    status = _status(state)
    if status in {"failed", "budget_exceeded"}:
        return {"status": status, "trace": [_entry("compute", "skip", False, started, {"reason": status})]}
    budget, blocked = _guard_step(state, deps, node="compute", started=started)
    if blocked is not None:
        return blocked

    spec = state.get("spec") if isinstance(state.get("spec"), dict) else {}
    data_result = state.get("data_result") if isinstance(state.get("data_result"), dict) else {}
    if not data_result.get("current"):
        return {
            "status": "failed",
            "errors": ["COMPUTE_ERROR: 缺少数据查询结果，无法执行指标计算。"],
            "trace": [_entry("compute", "validate_input", False, started, {"has_data": False})],
            "budget": budget.as_dict(),
        }

    current_month = str(data_result.get("current_month") or spec.get("current_month") or "")
    previous_month = (
        str(data_result.get("previous_month") or spec.get("previous_month") or "")
        or _previous_month_of(current_month)
        or ""
    )
    requested_dimension = str(spec.get("dimension") or DEFAULT_DIMENSION)
    if requested_dimension not in {"region", "product", "month", "channel"}:
        requested_dimension = DEFAULT_DIMENSION
    compare_dimension = "region" if requested_dimension == "channel" else requested_dimension
    unsupported = "channel" if requested_dimension == "channel" else ""
    regions_filter = [str(item) for item in (spec.get("regions") or []) if str(item).strip()]
    metric = str(spec.get("metric") or "net_sales")
    if metric not in METRIC_LABELS:
        # 解析层理论上只会给出这三种指标；这里再兜一层，避免未知指标把指标层打崩
        metric = "net_sales"

    user_id = str(state.get("user_id") or "")
    prefs_values = _prefs_for_user(deps, user_id, thread_id=thread_id)
    unit = str(prefs_values.get("amount_unit") or "元")
    if unit not in {"元", "万元"}:
        unit = "元"

    trace: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with _tool_span(
            deps.metrics,
            "compare_months",
            thread_id=thread_id,
            node="compute",
            params={
                "current_month": current_month,
                "previous_month": previous_month,
                "dimension": compare_dimension,
                "regions": regions_filter,
                "metric": metric,
            },
        ) as span:
            result = _call_with_supported_kwargs(
                compare_months,
                deps.conn,
                current_month,
                previous_month,
                dimension=compare_dimension,
                region_ids=tuple(regions_filter) or None,
                metric=metric,
            )
            span["summary"] = {"region_count": len(list(getattr(result, "regions", None) or []))}
        trace.append(
            _entry(
                "compute",
                "compare_months",
                True,
                started,
                {"dimension": compare_dimension, "requested_dimension": requested_dimension},
            )
        )
    except Exception as exc:
        trace.append(_entry("compute", "compare_months", False, started, {"error": type(exc).__name__}))
        return {
            "status": "failed",
            "errors": [f"COMPUTE_ERROR: 指标计算失败（{type(exc).__name__}: {exc}），本轮无法生成报告。"],
            "trace": trace,
            "budget": budget.as_dict(),
        }

    current_sales = _as_dict(getattr(result, "current", None)) or _as_dict(data_result.get("current"))
    previous_sales = _as_dict(getattr(result, "previous", None)) or _as_dict(data_result.get("previous"))
    raw_regions = getattr(result, "regions", None)
    if raw_regions is None:
        raw_regions = getattr(result, "region_deltas", None)
    region_deltas = [_region_delta_dict(item) for item in list(raw_regions or [])]

    current_net = _opt_int(current_sales.get("net_cents")) or 0
    previous_net = _opt_int(previous_sales.get("net_cents")) or 0
    delta_net = current_net - previous_net
    base_zero = previous_net == 0
    pct_change = None if base_zero else delta_net / abs(previous_net)

    metric_field = {"gross_sales": "gross_cents", "refunds": "refund_cents"}.get(metric, "net_cents")
    metric_current = _opt_int(current_sales.get(metric_field)) or 0
    metric_previous = _opt_int(previous_sales.get(metric_field)) or 0
    metric_delta = metric_current - metric_previous
    metric_base_zero = metric_previous == 0
    metric_pct = None if metric_base_zero else metric_delta / abs(metric_previous)

    reported_delta = _opt_int(getattr(result, "delta_net_cents", None))
    if reported_delta is not None and reported_delta != delta_net:
        errors.append(
            f"COMPUTE_WARNING: 指标层给出的净额差额（{reported_delta} 分）与本节点按净额重算的结果"
            f"（{delta_net} 分）不一致，报告以重算值为准。"
        )
        trace.append(
            _entry(
                "compute",
                "delta_mismatch",
                True,
                started,
                {"metrics": reported_delta, "recomputed": delta_net},
            )
        )

    for item in region_deltas:
        if item.get("share_of_delta") is None and delta_net != 0 and item.get("delta_cents") is not None:
            item["share_of_delta"] = float(item["delta_cents"]) / float(delta_net)
    metrics_region_sum = _opt_int(getattr(result, "region_delta_sum_cents", None))
    region_delta_sum = (
        metrics_region_sum
        if metrics_region_sum is not None
        else sum(int(item["delta_cents"]) for item in region_deltas if item.get("delta_cents") is not None)
    )

    current_regions = [_as_dict(item) for item in (data_result.get("current_regions") or [])]
    previous_regions = [_as_dict(item) for item in (data_result.get("previous_regions") or [])]
    if regions_filter:
        current_regions = [item for item in current_regions if _matches_regions(item, regions_filter)]
        previous_regions = [item for item in previous_regions if _matches_regions(item, regions_filter)]

    current_has_orders = bool(current_sales.get("has_orders"))
    order_count = _opt_int(current_sales.get("order_count")) or 0
    if not order_count and current_regions:
        order_count = sum(_opt_int(item.get("order_count")) or 0 for item in current_regions)
    previous_order_count = _opt_int(previous_sales.get("order_count"))
    if previous_order_count is None and previous_regions:
        previous_order_count = sum(_opt_int(item.get("order_count")) or 0 for item in previous_regions)
    current_gross = _opt_int(current_sales.get("gross_cents")) or 0
    refund_cents = _opt_int(current_sales.get("refund_cents")) or 0
    pending_refund_count = _opt_int(current_sales.get("pending_refund_count")) or 0
    refund_count = _opt_int(current_sales.get("approved_refund_count")) or 0

    zero_order_current = [
        str(item.get("region_name") or item.get("region_id") or "")
        for item in current_regions
        if (_opt_int(item.get("order_count")) or 0) == 0
    ]
    zero_order_previous = [
        str(item.get("region_name") or item.get("region_id") or "")
        for item in previous_regions
        if (_opt_int(item.get("order_count")) or 0) == 0
    ]
    zero_order_current = [name for name in zero_order_current if name]
    zero_order_previous = [name for name in zero_order_previous if name]

    refund_flagged = current_sales.get("has_any_refund_records")
    edge_flags = {
        "current_no_records": (not current_has_orders) and order_count == 0,
        "true_zero": order_count == 0 and current_gross == 0 and refund_cents == 0 and current_net == 0,
        "previous_no_records": previous_order_count == 0 and previous_net == 0 and not previous_sales.get("has_orders"),
        "base_zero": base_zero,
        "negative_net": current_net < 0 or previous_net < 0,
        "pending_refunds": pending_refund_count,
        # 优先用数据层给出的"是否存在退款记录"（含 pending），拿不到时退回启发式判断
        "any_refund_records": bool(refund_flagged)
        if refund_flagged is not None
        else (refund_count > 0 or refund_cents != 0),
        "current_has_orders": current_has_orders,
        "zero_order_regions_current": zero_order_current,
        "zero_order_regions_previous": zero_order_previous,
        "delta_sum_matches": (region_delta_sum == delta_net) if region_deltas else True,
    }

    tables = [item for item in (state.get("tables") or []) if isinstance(item, dict)]
    tables.append(
        {
            "title": f"两期指标对比（单位：{unit}）",
            "columns": ["指标", f"本期 {current_month}", f"上期 {previous_month}", "差额"],
            "rows": [
                [
                    "毛销售额",
                    format_cents(_opt_int(current_sales.get("gross_cents")), unit),
                    format_cents(_opt_int(previous_sales.get("gross_cents")), unit),
                    format_cents(
                        (_opt_int(current_sales.get("gross_cents")) or 0)
                        - (_opt_int(previous_sales.get("gross_cents")) or 0),
                        unit,
                    ),
                ],
                [
                    "退款额（approved）",
                    format_cents(refund_cents, unit),
                    format_cents(_opt_int(previous_sales.get("refund_cents")), unit),
                    format_cents(refund_cents - (_opt_int(previous_sales.get("refund_cents")) or 0), unit),
                ],
                [
                    "净销售额",
                    format_cents(current_net, unit),
                    format_cents(previous_net, unit),
                    format_cents(delta_net, unit),
                ],
            ],
        }
    )
    chart: dict[str, Any] | None = None
    if region_deltas:
        chart_labels: list[str] = []
        chart_values: list[float] = []
        for item in region_deltas:
            value = _display_amount(item.get("delta_cents"), unit)
            if value is None:
                continue
            chart_labels.append(str(item.get("region_name") or item.get("region_id") or "（未知）"))
            chart_values.append(value)
        if chart_labels:
            chart = {
                "type": "bar",
                "x": DIMENSION_LABELS.get(requested_dimension, "维度"),
                "y": f"差额（{unit}）",
                "labels": chart_labels,
                "values": chart_values,
            }
            tables.append(
                {
                    "title": f"{DIMENSION_LABELS.get(requested_dimension, '维度')}差额（单位：{unit}）",
                    "columns": [
                        "维度",
                        f"本期{METRIC_LABELS.get(metric, '净销售额')}（{unit}）",
                        f"上期{METRIC_LABELS.get(metric, '净销售额')}（{unit}）",
                        f"差额（{unit}）",
                    ],
                    "rows": [
                        [
                            str(item.get("region_name") or item.get("region_id") or "（未知）"),
                            format_cents(item.get("current_net_cents"), unit),
                            format_cents(item.get("previous_net_cents"), unit),
                            format_cents(item.get("delta_cents"), unit),
                        ]
                        for item in region_deltas
                    ],
                }
            )

    sources = list(state.get("sources") or [])
    sources.append(
        _sql_source(
            COMPARE_SQL,
            {"current": current_month, "previous": previous_month},
            note="两期净额与维度分解由 domain/metrics.compare_months 在内存中完成，等价口径如上",
        )
    )

    calculation = {
        "unit": unit,
        "metric": metric,
        "dimension": requested_dimension,
        "current_month": current_month,
        "previous_month": previous_month,
        "current": current_sales,
        "previous": previous_sales,
        "metric_current_cents": metric_current,
        "metric_previous_cents": metric_previous,
        "metric_delta_cents": metric_delta,
        "metric_pct": metric_pct,
        "metric_base_zero": metric_base_zero,
        "delta_net_cents": delta_net,
        "pct_change": pct_change,
        "base_zero": base_zero,
        "region_deltas": region_deltas,
        "region_delta_sum_cents": region_delta_sum,
        "regions_filter": regions_filter,
        "unsupported_dimension": unsupported,
        "edge_flags": edge_flags,
        "computed_at": _now_iso(),
        "formulas": {
            "net_cents": "gross_cents - refund_cents",
            "delta_net_cents": "本期 net_cents - 上期 net_cents",
            "pct_change": "delta_net_cents / |上期 net_cents|",
        },
    }
    trace.append(
        _entry(
            "compute",
            "build_calculation",
            True,
            started,
            {
                "delta_net_cents": delta_net,
                "region_delta_sum_cents": region_delta_sum,
                "base_zero": base_zero,
            },
        )
    )
    return {
        "calculation": calculation,
        "tables": tables,
        "chart": chart,
        "sources": sources,
        "trace": trace,
        "errors": errors,
        "budget": budget.as_dict(),
    }


# ---------------------------------------------------------------------------
# 7) draft 节点
# ---------------------------------------------------------------------------


def _narrative_seed(calculation: dict[str, Any]) -> str:
    """给模型润色用的"一句话"种子（不含任何数字，避免模型复述/篡改数字）。"""
    metric = str(calculation.get("metric") or "net_sales")
    dimension = str(calculation.get("dimension") or "region")
    return (
        "请把下面这句话改写得更通顺，不要添加任何数字、日期、百分比，也不要解释原因：\n"
        f"本期与上期的{METRIC_LABELS.get(metric, '净销售额')}存在差异，"
        f"按{DIMENSION_LABELS.get(dimension, '地区')}看各明细的表现并不相同，"
        "具体数值以报告表格为准，解读前请先看数据边界与来源。"
    )


def _polish_with_llm(deps: _Deps, *, calculation: dict[str, Any], thread_id: str) -> tuple[str | None, list[str]]:
    """用可选的模型润色一句非数字叙述；失败或被判定越界时返回 (None, 错误说明)。

    约束（规格 5.12）：
        - 只润色措辞，不产生任何数字；
        - 输出里出现数字或因果措辞就整段丢弃；
        - 模型未启用、报错、返回空都只是"不润色"，绝不阻断主流程。
    """
    llm = deps.llm
    if not getattr(llm, "enabled", False):
        return None, []
    invoke_text = getattr(llm, "invoke_text", None)
    if not callable(invoke_text):
        return None, []
    messages = [
        {"role": "system", "content": SYSTEM_POLISH_PROMPT},
        {"role": "user", "content": _narrative_seed(calculation)},
    ]
    started = time.perf_counter()
    try:
        with _tool_span(deps.metrics, "llm_polish", thread_id=thread_id, node="draft") as span:
            text = _call_with_supported_kwargs(invoke_text, messages, temperature=0.0)
            span["summary"] = {"chars": len(str(text or ""))}
        deps.metrics.record_model(
            model=str(getattr(llm, "model", "") or (getattr(deps.settings, "model", "") or "")),
            ok=True,
            thread_id=thread_id,
            node="draft",
            duration_ms=elapsed_ms_since(started),
        )
    except Exception as exc:
        deps.metrics.record_model(
            ok=False,
            thread_id=thread_id,
            node="draft",
            duration_ms=elapsed_ms_since(started),
            error_code=type(exc).__name__,
        )
        return None, [
            f"LLM_POLISH_SKIPPED: 模型润色失败（{type(exc).__name__}），"
            "报告已使用纯模板渲染，主体流程不受影响。"
        ]
    polished = str(text or "").strip()
    if not polished:
        return None, []
    if DIGITS.search(polished):
        return None, [
            "LLM_POLISH_REJECTED: 模型润色结果包含数字，已整段丢弃；"
            "报告中所有数字始终由程序注入。"
        ]
    if any(word in polished for word in CAUSAL_WORDS):
        return None, [
            "LLM_POLISH_REJECTED: 模型润色结果包含因果措辞，已整段丢弃，避免把相关性写成因果。"
        ]
    return f"模型润色（仅措辞，未经人工事实核查）：{polished}", []


def draft_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """起草节点：用纯模板渲染 8 段报告，可选模型只润色一句非数字叙述。

    读取：``calculation`` / ``sources`` / ``policy_context`` / ``spec`` / ``question``；
    写入：``draft_report`` / ``status="awaiting_approval"`` / ``trace`` / ``errors``。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    status = _status(state)
    if status in {"failed", "budget_exceeded"}:
        return {"status": status, "draft_report": "", "trace": [_entry("draft", "skip", False, started, {"reason": status})]}
    budget, blocked = _guard_step(state, deps, node="draft", started=started)
    if blocked is not None:
        return blocked

    calculation = state.get("calculation") if isinstance(state.get("calculation"), dict) else {}
    if not calculation:
        return {
            "status": "failed",
            "draft_report": "",
            "errors": ["DRAFT_ERROR: 缺少计算结果，无法渲染报告。"],
            "trace": [_entry("draft", "validate_input", False, started, {"has_calculation": False})],
            "budget": budget.as_dict(),
        }

    spec = state.get("spec") if isinstance(state.get("spec"), dict) else {}
    notes: list[str] = [str(item) for item in (calculation.get("notes") or []) if str(item).strip()]
    errors: list[str] = []
    polished, polish_errors = _polish_with_llm(deps, calculation=calculation, thread_id=thread_id)
    if polished:
        notes.append(polished)
    errors.extend(polish_errors)

    report = render_analysis_report(
        calculation=calculation,
        policy_context=str(state.get("policy_context") or ""),
        sources=[item for item in (state.get("sources") or []) if isinstance(item, dict)],
        prefs={"amount_unit": str(calculation.get("unit") or "元")},
        question=str(state.get("question") or ""),
        notes=notes,
    )
    trace = [
        _entry(
            "draft",
            "render_analysis_report",
            True,
            started,
            {
                "chars": len(report),
                "sections": report.count("\n## "),
                "polished": bool(polished),
                "question_dimension": spec.get("dimension"),
            },
        )
    ]
    return {
        "draft_report": report,
        "status": "awaiting_approval",
        "trace": trace,
        "errors": errors,
        "budget": budget.as_dict(),
    }


# ---------------------------------------------------------------------------
# 8) review 节点（Human-in-the-loop）
# ---------------------------------------------------------------------------

APPROVAL_OPTIONS = ("approve", "reject", "edit")


def _review_summary(calculation: dict[str, Any]) -> str:
    """给审批人看的三行摘要（全部由程序算好的数字拼装，不含推断）。"""
    unit = str(calculation.get("unit") or "元")
    metric = str(calculation.get("metric") or "net_sales")
    label = METRIC_LABELS.get(metric, "净销售额")
    current_month = str(calculation.get("current_month") or "（未知）")
    previous_month = str(calculation.get("previous_month") or "（未知）")
    metric_current = _opt_int(calculation.get("metric_current_cents"))
    metric_previous = _opt_int(calculation.get("metric_previous_cents"))
    metric_delta = _opt_int(calculation.get("metric_delta_cents"))
    pct = calculation.get("metric_pct")
    lines = [
        f"本期（{current_month}）{label}：{format_cents(metric_current, unit)} {unit}",
        f"上期（{previous_month}）{label}：{format_cents(metric_previous, unit)} {unit}",
        f"差额：{format_cents(metric_delta, unit)} {unit}，变化率："
        f"{format_pct(pct, base_zero=bool(calculation.get('metric_base_zero')))}",
    ]
    return "\n".join(lines)


def _approval_payload(state: AssistantState, *, thread_id: str) -> dict[str, Any]:
    """构造审批卡片的载荷（必须 JSON 可序列化，会写进 SQLite 检查点）。"""
    calculation = state.get("calculation") if isinstance(state.get("calculation"), dict) else {}
    draft = str(state.get("draft_report") or "")
    return {
        "type": "report_approval",
        "thread_id": thread_id,
        "question": str(state.get("question") or ""),
        "current_month": str(calculation.get("current_month") or ""),
        "previous_month": str(calculation.get("previous_month") or ""),
        "summary": _review_summary(calculation),
        "options": list(APPROVAL_OPTIONS),
        "report_markdown": draft,
        "instructions": (
            "请审批本报告：approve 直接导出；edit 提供修改稿后导出（只能改措辞，不能改数字）；"
            "reject 不导出文件。"
        ),
    }


def _parse_resume(value: Any) -> tuple[str, str, str]:
    """解析人工审批的恢复值，返回 (decision, comment, edited_report)。

    兼容三种写法：
        - 裸字符串："approve" / "reject" / "edit"（也接受中文与 approve 的常见同义词）；
        - dict：``{"decision": ..., "comment": ..., "edited_report": ...}``；
        - 其它：视为无法识别（decision 为空串），由调用方按拒绝处理。
    """
    aliases = {
        "approve": "approve",
        "approved": "approve",
        "ok": "approve",
        "yes": "approve",
        "通过": "approve",
        "批准": "approve",
        "reject": "reject",
        "rejected": "reject",
        "no": "reject",
        "拒绝": "reject",
        "驳回": "reject",
        "edit": "edit",
        "modify": "edit",
        "修改": "edit",
        "编辑": "edit",
    }
    if isinstance(value, str):
        return aliases.get(value.strip().lower(), ""), "", ""
    if not isinstance(value, dict):
        return "", "", ""
    decision = str(value.get("decision") or value.get("action") or "").strip().lower()
    comment = str(value.get("comment") or value.get("feedback") or "").strip()
    edited = value.get("edited_report")
    if edited is None:
        edited = value.get("report_markdown")
    if isinstance(edited, dict):
        edited = edited.get("report_markdown")
    return aliases.get(decision, ""), comment, str(edited or "").strip()


def _number_tokens(text: str) -> list[str]:
    """抽出文本里的数字片段（用于校验"编辑只能改措辞，不能改数字"）。"""
    return re.findall(r"\d[\d,]*(?:\.\d+)?", str(text or ""))


def _edited_report_or_draft(edited: str, draft: str) -> tuple[str, list[str]]:
    """决定最终导出的报告文本：优先用人工修改稿，但必须通过"数字未变"校验。

    规格 5.8 要求 edit 只能改措辞、不能改数字，因此这里逐个核对原草稿里的
    数字片段是否仍然出现在修改稿里；只要有一个数字丢失或被改写，就回退原草稿并说明原因。

    参数：
        edited: 人工提交的修改稿。
        draft: 程序渲染的原草稿。

    返回：
        tuple[str, list[str]]：(最终报告文本, 需要写进 errors 的说明)。
    """
    text = str(edited or "").strip()
    if not text:
        return draft, ["REVIEW_WARNING: 您选择了编辑但未提供修改稿，将按原始草稿导出。"]
    draft_tokens = _number_tokens(draft)
    missing = [token for token in draft_tokens if token not in text]
    if missing:
        preview = "、".join(missing[:5])
        return draft, [
            "REVIEW_WARNING: 修改稿缺少原报告中的数字（"
            f"{preview}{'…' if len(missing) > 5 else ''}），为避免数字被改写，已回退为程序渲染的原草稿导出。"
        ]
    injected = [token for token in _number_tokens(text) if token not in draft_tokens]
    if injected:
        preview = "、".join(injected[:5])
        return draft, [
            "REVIEW_WARNING: 修改稿新增了原报告中没有的数字（"
            f"{preview}{'…' if len(injected) > 5 else ''}），"
            "为避免把未经计算的数字写进报告，已回退为程序渲染的原草稿导出。"
        ]
    return text, []


def review_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """审批节点：先把载荷准备好，再 ``interrupt`` 等人工决定，恢复后只改状态。

    关键约束（规格 5.8）：
        - ``interrupt()`` 之前只做纯读/纯算，不写文件、不写库，保证节点重跑安全；
        - 恢复后本节点不再执行任何带副作用的操作，真正的落盘由 export 节点负责；
        - 载荷必须是 JSON 可序列化对象（写进 SQLite 检查点再读回来）。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    status = _status(state)
    if status in {"failed", "budget_exceeded"}:
        return {"status": status, "trace": [_entry("review", "skip", False, started, {"reason": status})]}
    budget, blocked = _guard_step(state, deps, node="review", started=started)
    if blocked is not None:
        return blocked

    draft = str(state.get("draft_report") or "")
    if not draft.strip():
        return {
            "status": "failed",
            "errors": ["REVIEW_ERROR: 没有可审批的报告草稿，请检查 draft 节点。"],
            "trace": [_entry("review", "validate_input", False, started, {"has_draft": False})],
            "budget": budget.as_dict(),
        }

    payload = jsonable(_approval_payload(state, thread_id=thread_id))
    waited_ms = elapsed_ms_since(started)
    resume_value = interrupt(payload)
    resumed_at = time.perf_counter()

    decision, comment, edited = _parse_resume(resume_value)
    trace = [
        _entry(
            "review",
            "approval_decision",
            True,
            resumed_at,
            {"decision": decision or "（无法识别）", "has_comment": bool(comment), "edited": bool(edited), "waited_ms": waited_ms},
        )
    ]
    errors: list[str] = []
    if decision not in APPROVAL_OPTIONS:
        errors.append(
            f"INVALID_DECISION: 无法识别的审批结论“{decision or '（空）'}”，按拒绝处理以免误导出文件。"
        )
        decision = "reject"

    decided_at = _now_iso()
    if decision == "reject":
        return {
            "status": "rejected",
            "review": {"decision": "reject", "comment": comment, "edited_report": "", "decided_at": decided_at},
            "final_report": "",
            "messages": [
                AIMessage(
                    content="已按您的要求拒绝本次报告，未导出任何文件。"
                    + (f" 备注：{comment}" if comment else "")
                )
            ],
            "trace": trace,
            "errors": errors,
            "budget": budget.as_dict(),
        }
    if decision == "edit":
        final_text, edit_notes = _edited_report_or_draft(edited, draft)
        errors.extend(edit_notes)
        # 只有真正采纳了人工修改稿才把它写进 review 记录，回退时该字段留空
        accepted_edit = final_text if final_text != draft else ""
        return {
            "status": "approved",
            "review": {
                "decision": "edit",
                "comment": comment,
                "edited_report": accepted_edit,
                "decided_at": decided_at,
            },
            "final_report": final_text,
            "messages": [
                AIMessage(
                    content="已收到您的修改稿（或原始草稿），将按其导出报告文件。"
                    + (f" 备注：{comment}" if comment else "")
                )
            ],
            "trace": trace,
            "errors": errors,
            "budget": budget.as_dict(),
        }
    return {
        "status": "approved",
        "review": {"decision": "approve", "comment": comment, "edited_report": "", "decided_at": decided_at},
        "final_report": draft,
        "messages": [
            AIMessage(
                content="报告已批准，正在导出文件。" + (f" 备注：{comment}" if comment else "")
            )
        ],
        "trace": trace,
        "errors": errors,
        "budget": budget.as_dict(),
    }


# ---------------------------------------------------------------------------
# 9) export 节点
# ---------------------------------------------------------------------------


def export_node(state: AssistantState, runtime: Runtime[AssistantContext]) -> dict[str, Any]:
    """导出节点：唯一的落盘节点，文件名固定为 ``report-<thread_id>.md``。

    写入方式：同目录临时文件 → 写入 → fsync → ``os.replace`` 原子替换，
    因此重复导出永远命中同一路径，也不会出现"半截文件"。

    读取：``draft_report`` / ``final_report`` / ``review``；写入：``export_path`` /
    ``final_report``（与落盘内容严格一致）/ ``status="approved"`` / ``trace``。
    """
    deps = _resolve_deps(runtime)
    started = time.perf_counter()
    thread_id = str(state.get("thread_id") or "")
    status = _status(state)
    if status in {"failed", "budget_exceeded"}:
        return {"status": status, "trace": [_entry("export", "skip", False, started, {"reason": status})]}
    budget, blocked = _guard_step(state, deps, node="export", started=started)
    if blocked is not None:
        return blocked

    review = state.get("review") if isinstance(state.get("review"), dict) else {}
    decision = str(review.get("decision") or "").strip().lower()
    draft = str(state.get("draft_report") or "")
    final = str(state.get("final_report") or draft)
    if decision == "reject":
        # 兜底：正常情况下 review 的路由不会把 reject 送到这里，万一来了也绝不落盘
        return {
            "status": "rejected",
            "export_path": None,
            "trace": [_entry("export", "skip", False, started, {"reason": "rejected"})],
            "budget": budget.as_dict(),
        }
    # review 已经把"是否采纳修改稿 + 数字校验"做完了（结果落在 final_report），
    # 这里直接用它，避免绕过校验把未校验的编辑稿写盘。
    content = final if final.strip() else draft
    if not content.strip():
        return {
            "status": "failed",
            "export_path": None,
            "errors": ["EXPORT_ERROR: 没有可导出的报告内容。"],
            "trace": [_entry("export", "validate_input", False, started, {"has_content": False})],
            "budget": budget.as_dict(),
        }

    export_dir = getattr(deps.settings, "export_dir", None)
    if export_dir is None:
        return {
            "status": "failed",
            "export_path": None,
            "errors": ["EXPORT_ERROR: 配置缺少 export_dir，无法确定报告导出目录。"],
            "trace": [_entry("export", "validate_settings", False, started, {"has_export_dir": False})],
            "budget": budget.as_dict(),
        }
    try:
        directory = Path(export_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = export_report(content, thread_id=thread_id, export_dir=directory)
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "export_path": None,
            "errors": [f"EXPORT_ERROR: 写入报告文件失败（{type(exc).__name__}: {exc}）。"],
            "trace": [_entry("export", "write_report", False, started, {"error": type(exc).__name__})],
            "budget": budget.as_dict(),
        }

    log_structured(
        event="report.exported",
        thread_id=thread_id,
        node="export",
        ok=True,
        detail={"path": str(path), "chars": len(content), "edited": decision == "edit"},
    )
    trace = [
        _entry(
            "export",
            "write_report",
            True,
            started,
            {"path": str(path), "chars": len(content), "edited": decision == "edit"},
        )
    ]
    return {
        "status": "approved",
        "export_path": str(path),
        "final_report": content,
        "trace": trace,
        "budget": budget.as_dict(),
    }


# ---------------------------------------------------------------------------
# 10) fail 节点
# ---------------------------------------------------------------------------


def fail_node(state: AssistantState) -> dict[str, Any]:
    """失败出口：把错误收敛成一段人能看懂的中文提示，状态保持失败语义。

    读取：``status`` / ``errors``；写入：``messages`` / ``trace``。
    本节点不依赖 Runtime context，也不做任何 IO，保证任何失败路径都能走到这里。
    """
    started = time.perf_counter()
    status = _status(state)
    if status not in {"failed", "budget_exceeded", "rejected"}:
        status = "failed"
    errors = [str(item) for item in (state.get("errors") or []) if str(item).strip()]
    if status == "budget_exceeded":
        message = "本轮分析因预算限制提前结束。"
        if errors:
            message += " " + errors[-1]
        message += (
            " 建议缩小问题范围（例如只问一个地区或一个指标），"
            "或调高 OPS_MAX_STEPS / OPS_MAX_TOOL_CALLS 后重试。"
        )
    else:
        message = "本轮分析未能完成。"
        if errors:
            message += " 原因：" + errors[-1]
        message += " 可以换一种说法，或先运行 scripts/bootstrap.py 检查演示数据是否就绪。"
    return {
        "status": status,
        "messages": [AIMessage(content=message)],
        "trace": [
            _entry(
                "fail",
                "emit_failure_message",
                True,
                started,
                {"status": status, "error_count": len(errors)},
            )
        ],
    }



