"""简化版工具调用 Agent：用 create_agent 让模型自行选择工具。
本模块刻意保持“模型选工具”的最小形态，用于和 workflow 图中“固定代码步骤”对照：
create_agent 由模型决定调用哪个工具、调用几轮；workflow 由代码固定节点与路由。
没有模型时返回 None / status=unavailable，不影响纯规则主流程。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ops_assistant.config import Settings

from .llm import LLMClient, build_llm, content_to_text
from .tools import make_tools

logger = logging.getLogger(__name__)

# 系统提示：强调工具优先、数字必须来自工具、资料不足要直说。
SYSTEM_PROMPT = (
    "你是经营数据分析助手，负责回答销售额、退款、地区对比与公司指标口径问题。"
    "涉及数字时必须先调用工具，不得凭记忆或估算作答；工具返回 JSON，单位是分。"
    "问题缺少月份时先向用户澄清；不支持“渠道”维度时明确说明。"
    "回答中写清口径（默认净销售额 = 毛销售额 - 已批准退款）与时间范围。"
    "资料或数据不足时直接说明不足，禁止编造原因或因果结论。"
    "最终回答使用简体中文，给出关键数字与来源。"
)


def _build_agent_with_llm(
    settings: Settings,
    conn: Any,
    kb: Any,
) -> tuple[Any | None, LLMClient]:
    """内部实现：构造 create_agent 并返回其使用的 LLMClient。

    参数：
        settings: 项目配置。
        conn: SQLite 连接。
        kb: 知识库实例。

    返回：
        tuple[Any | None, LLMClient]：(agent 或 None, LLMClient)。
    """
    llm = build_llm(settings)
    model = llm.get_chat_model(temperature=0.0)
    if model is None:
        logger.warning(
            "模型不可用（未配置或初始化失败），simple_agent 返回 None；请使用纯规则 workflow。"
        )
        return None, llm

    tools = make_tools(conn, kb, settings)
    try:
        # 延迟导入：即使当前 LangChain 版本没有 create_agent，也不影响 agent 包其它模块导入。
        from langchain.agents import create_agent

        agent = create_agent(model, tools=tools, system_prompt=SYSTEM_PROMPT)
    except Exception as exc:
        logger.warning(
            "create_agent 构造失败：%s；simple_agent 返回 None",
            llm.redact(f"{type(exc).__name__}: {exc}"),
        )
        return None, llm
    return agent, llm


def build_simple_agent(settings: Settings, conn: Any, kb: Any) -> Any | None:
    """构造基于 create_agent 的简单 Agent。

    参数：
        settings: 项目配置。
        conn: 已打开的 SQLite 连接。
        kb: 知识库实例。

    返回：
        Any | None：编译后的 agent 图；无模型或构造失败时返回 None。
    """
    agent, _llm = _build_agent_with_llm(settings, conn, kb)
    return agent


def _parse_tool_payload(content: Any) -> dict[str, Any] | None:
    """解析工具返回的 JSON 字符串；解析失败返回 None。"""
    text = content_to_text(content).strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _json_safe(value: Any) -> Any:
    """保证值可 JSON 序列化；不可序列化时退化为字符串。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def _collect_sources(
    payloads: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> None:
    """把工具结果里的 sources 去重后追加到列表（原地修改）。"""
    seen = {
        json.dumps(source, ensure_ascii=False, sort_keys=True)
        for source in sources
        if isinstance(source, dict)
    }
    for payload in payloads:
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            continue
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            key = json.dumps(source, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)


def run_simple_agent(
    question: str,
    *,
    settings: Settings,
    conn: Any,
    kb: Any,
    thread_id: str = "demo",
) -> dict[str, Any]:
    """执行一次简单 Agent 问答，返回可 JSON 序列化的结果。

    说明（重要）：
        这里的工具调用由“模型”决定（模型输出 tool_calls，框架执行工具后把结果回灌），
        与 workflow 图中由代码固定节点顺序的“固定代码步骤”不同：路径由模型选择，
        因此更灵活，但调用次数、延迟与成本不可完全预测。

    参数：
        question: 用户问题。
        settings: 项目配置。
        conn: SQLite 连接。
        kb: 知识库实例。
        thread_id: 会话标识，仅回显；本简化版不带 checkpointer，不保存多轮状态。

    返回：
        dict：包含 thread_id/status/answer/report/clarification/interrupt/sources/
        tables/chart/trace/usage/errors/export_path，全部可 JSON 序列化。

    已知限制：
        usage 统计的是 LLMClient 自身的调用；create_agent 内部直接调用模型，
        其 token 与调用次数不在 usage 中（tool_calls 为工具调用次数）。不要
        把这里的 usage 当作完整的模型成本统计。
    """
    started = time.perf_counter()
    result: dict[str, Any] = {
        "thread_id": thread_id,
        "status": "failed",
        "answer": "",
        "report": None,
        "clarification": None,
        "interrupt": None,
        "sources": [],
        "tables": [],
        "chart": None,
        "trace": [],
        "usage": {},
        "errors": [],
        "export_path": None,
    }

    text = (question or "").strip()
    if not text:
        result["answer"] = "问题不能为空。请输入具体业务问题。"
        result["errors"] = ["EMPTY_QUESTION"]
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    agent, llm = _build_agent_with_llm(settings, conn, kb)
    if agent is None:
        result["status"] = "unavailable"
        result["answer"] = (
            "未配置模型或模型初始化失败：simple_agent 需要真实模型。"
            "纯规则流程请使用 workflow（无需 API key）。"
        )
        result["errors"] = ["MODEL_UNAVAILABLE"]
        result["usage"] = llm.usage()
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    recursion_limit = max(2, int(settings.max_steps) * 2)
    try:
        state = agent.invoke(
            {"messages": [HumanMessage(content=text)]},
            config={"recursion_limit": recursion_limit},
        )
    except Exception as exc:
        message = llm.redact(f"{type(exc).__name__}: {exc}")
        error_name = type(exc).__name__
        if "Recursion" in error_name or "recursion limit" in str(exc).lower():
            result["status"] = "budget_exceeded"
            result["answer"] = (
                f"简单 Agent 达到步数上限（recursion_limit={recursion_limit}），已终止。"
                "请缩小问题范围或提高 OPS_MAX_STEPS。"
            )
            result["errors"] = ["GRAPH_RECURSION_LIMIT"]
        else:
            result["status"] = "failed"
            result["answer"] = "简单 Agent 执行失败，请查看 usage.last_error 或稍后重试。"
            result["errors"] = [message]
        logger.warning("run_simple_agent 执行异常：%s", message)
        result["usage"] = llm.usage()
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result

    messages = state.get("messages", []) if isinstance(state, dict) else []
    answer = ""
    tool_calls = 0
    tool_payloads: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}

    for message in messages:
        if isinstance(message, AIMessage):
            calls = list(getattr(message, "tool_calls", None) or [])
            tool_calls += len(calls)
            for call in calls:
                call_id = str(call.get("id", ""))
                call_name = str(call.get("name", "tool"))
                if call_id:
                    call_names[call_id] = call_name
                trace.append(
                    {
                        "step": len(trace) + 1,
                        "node": "simple_agent",
                        "action": "tool_call",
                        "tool": call_name,
                        "args": _json_safe(call.get("args", {})),
                    }
                )
            content = content_to_text(getattr(message, "content", "")).strip()
            if content and not calls:
                answer = content
        elif isinstance(message, ToolMessage):
            payload = _parse_tool_payload(getattr(message, "content", ""))
            tool_name = getattr(message, "name", None) or call_names.get(
                str(getattr(message, "tool_call_id", "")),
                "tool",
            )
            ok = bool(payload.get("ok")) if payload is not None else None
            error_code: str | None = None
            if payload is not None and payload.get("ok") is False:
                raw_error = payload.get("error")
                if isinstance(raw_error, dict):
                    error_code = str(raw_error.get("code") or "") or None
            trace.append(
                {
                    "step": len(trace) + 1,
                    "node": "simple_agent",
                    "action": "tool_result",
                    "tool": tool_name,
                    "ok": ok,
                    "error_code": error_code,
                }
            )
            if payload is not None:
                tool_payloads.append(payload)

    _collect_sources(tool_payloads, result["sources"])
    result["trace"] = trace
    result["usage"] = llm.usage()
    result["usage"]["tool_calls"] = tool_calls
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)

    if answer:
        result["status"] = "answered"
        result["answer"] = answer
    else:
        result["status"] = "failed"
        result["answer"] = "模型未返回最终文本，可能只完成了工具调用；请重试或换一种问法。"
        result["errors"] = ["NO_FINAL_ANSWER"]

    return result
