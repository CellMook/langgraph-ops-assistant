"""单元 04 实验：手写工具循环（不依赖 create_agent）。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/04_tool_loop.py
默认使用“假模型”（脚本内置的确定性逻辑），不需要密钥；
配置了 OPS_MODEL 与 OPS_API_KEY 时会额外跑一遍真实模型循环。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "ops-assistant" / ".env"
MAX_TOOL_CALLS = 4

FAKE_MONTHS = {
    "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500,
                "order_count": 57, "has_orders": True},
    "2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000,
                "order_count": 61, "has_orders": True},
    "2025-01": {"gross_cents": 0, "refund_cents": 0, "net_cents": 0,
                "order_count": 0, "has_orders": False},
}


def error_payload(code: str, message: str, hint: str) -> str:
    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message}, "hint": hint},
        ensure_ascii=False,
    )


@tool
def query_month_sales(month: str) -> str:
    """查询某个月的销售汇总。

    Args:
        month: 月份，格式必须是 YYYY-MM，例如 "2026-09"。

    Returns:
        JSON 字符串；ok=false 时说明原因，不抛异常。
    """
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        return error_payload("INVALID_MONTH", "月份格式必须是 YYYY-MM", "可用月份：2025-01 ～ 2026-09")
    data = FAKE_MONTHS.get(month)
    if data is None:
        return error_payload("NO_DATA", f"没有 {month} 的数据", "先调用 list_available_periods")
    return json.dumps(
        {
            "ok": True,
            "data": data,
            "unit": "cents",
            "sources": [
                {
                    "type": "sql",
                    "sql": "SELECT SUM(quantity * unit_price_cents) FROM orders WHERE order_date LIKE ?",
                    "params": {"month": f"{month}%"},
                }
            ],
        },
        ensure_ascii=False,
    )


@tool
def calculate_change(current_cents: int, previous_cents: int) -> str:
    """计算两期净销售额的差额与变化率。

    Args:
        current_cents: 本期净销售额，单位分。
        previous_cents: 上期净销售额，单位分。

    Returns:
        JSON 字符串；previous_cents 为 0 时 pct_change 为 null。
    """
    delta = current_cents - previous_cents
    if previous_cents == 0:
        pct = None
        base_zero = True
    else:
        pct = round(delta / abs(previous_cents), 4)
        base_zero = False
    return json.dumps(
        {
            "ok": True,
            "data": {"delta_cents": delta, "pct_change": pct, "base_zero": base_zero},
            "unit": "cents",
            "sources": [{"type": "calc", "formula": "delta = current - previous"}],
        },
        ensure_ascii=False,
    )


TOOLS = [query_month_sales, calculate_change]
TOOL_MAP = {item.name: item for item in TOOLS}


def execute_tool_calls(tool_calls: list[dict]) -> tuple[list[ToolMessage], list[dict]]:
    """真正执行工具的地方：校验 → 执行 → 包成 ToolMessage。"""
    messages: list[ToolMessage] = []
    trace: list[dict] = []
    for call in tool_calls:
        name = call.get("name", "")
        args = call.get("args", {})
        call_id = call.get("id", "")
        entry = {"tool": name, "args": args, "ok": False, "summary": ""}
        if name not in TOOL_MAP:
            content = error_payload(
                "UNKNOWN_TOOL",
                f"没有名为 {name} 的工具",
                f"可用工具：{sorted(TOOL_MAP)}",
            )
            entry["summary"] = "unknown_tool"
        else:
            try:
                content = TOOL_MAP[name].invoke(args)
                payload = json.loads(content)
                entry["ok"] = bool(payload.get("ok"))
                entry["summary"] = "ok" if entry["ok"] else payload.get("error", {}).get("code", "error")
            except Exception as exc:  # 程序 bug 才走到这里；记录而不是静默
                content = error_payload("TOOL_EXCEPTION", f"{type(exc).__name__}: {exc}", "请检查工具实现")
                entry["summary"] = "exception"
        messages.append(ToolMessage(content=content, tool_call_id=call_id))
        trace.append(entry)
    return messages, trace


def fake_model_decide(messages: list, step: int, max_tool_calls: int) -> AIMessage:
    """确定性“假模型”：按固定剧本返回工具申请，最后给结论。

    它的存在是为了证明：循环由程序驱动，模型只是“提议下一步”。
    """
    if step >= max_tool_calls:
        return AIMessage(content="工具调用达到上限，停止。请人工检查。")
    if step == 0:
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "query_month_sales", "args": {"month": "2026-09"},
                 "id": f"call_{step}_a", "type": "tool_call"},
                {"name": "query_month_sales", "args": {"month": "2026-08"},
                 "id": f"call_{step}_b", "type": "tool_call"},
            ],
        )
    if step == 1:
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate_change", "args": {"current_cents": 1286500, "previous_cents": 1190000},
                 "id": f"call_{step}_a", "type": "tool_call"},
                {"name": "query_month_sales", "args": {"month": "2026-13"},
                 "id": f"call_{step}_b", "type": "tool_call"},
            ],
        )
    return AIMessage(
        content=(
            "2026 年九月净销售额 1286500 分，八月 1190000 分，差额 96500 分，"
            "变化率 8.11%。注意：该结论来自程序计算的工具结果，不是模型心算。"
        )
    )


def run_loop(question: str, *, max_tool_calls: int = MAX_TOOL_CALLS) -> dict:
    messages: list = [
        SystemMessage(content="你可以调用工具。只使用工具返回的数字，不要自行加减。"),
        HumanMessage(content=question),
    ]
    tool_call_count = 0
    steps: list[dict] = []
    for step in range(max_tool_calls + 2):
        reply = fake_model_decide(messages, step, max_tool_calls)
        messages.append(reply)
        if not reply.tool_calls:
            return {"answer": reply.content, "steps": steps, "tool_calls": tool_call_count}
        tool_call_count += len(reply.tool_calls)
        tool_messages, trace = execute_tool_calls(reply.tool_calls)
        messages.extend(tool_messages)
        steps.append({"step": step, "tool_calls": len(reply.tool_calls), "results": trace})
        if tool_call_count > max_tool_calls:
            messages.append(AIMessage(content="预算耗尽：工具调用超过上限，转入失败出口。"))
            return {
                "answer": "预算耗尽（tool_calls > max_tool_calls）",
                "steps": steps,
                "tool_calls": tool_call_count,
                "status": "budget_exceeded",
            }
    return {"answer": "达到最大步数仍未得出结论", "steps": steps, "tool_calls": tool_call_count}


def mask(secret: str | None) -> str:
    if not secret:
        return "未设置"
    return f"{secret[:4]}…{secret[-2:]}"


def load_real_model():
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    raw_model = (os.getenv("OPS_MODEL") or "").strip()
    api_key = (os.getenv("OPS_API_KEY") or "").strip() or os.getenv("OPENAI_API_KEY")
    base_url = (os.getenv("OPS_BASE_URL") or "").strip() or None
    if not raw_model or not api_key:
        return None
    provider, model_name = "openai", raw_model
    if ":" in raw_model:
        provider, _, model_name = raw_model.partition(":")
    from langchain_openai import ChatOpenAI

    print(f"[真实模型] {provider}:{model_name}，key={mask(api_key)}")
    return ChatOpenAI(
        model=model_name, api_key=api_key, base_url=base_url,
        temperature=0.0, timeout=60.0, max_retries=0,
    )


def run_real_loop(model, question: str, *, max_rounds: int = 4) -> dict:
    bound = model.bind_tools(TOOLS)
    messages: list = [
        SystemMessage(content="需要数字时必须调用工具，禁止心算。工具返回 ok=false 时不要编造数据。"),
        HumanMessage(content=question),
    ]
    trace: list[dict] = []
    for _ in range(max_rounds):
        reply = bound.invoke(messages)
        messages.append(reply)
        if not reply.tool_calls:
            return {"answer": reply.content, "trace": trace}
        tool_messages, entries = execute_tool_calls(reply.tool_calls)
        messages.extend(tool_messages)
        trace.extend(entries)
    return {"answer": "达到最大轮数仍未给出最终答案", "trace": trace}


def main() -> int:
    question = "2026 年九月净销售额比八月变化多少？"
    print("== 单元 04 实验：手写工具循环 ==")
    print(f"问题：{question}")
    print(f"工具：{sorted(TOOL_MAP)}；每次执行前都会校验参数，上限 {MAX_TOOL_CALLS} 次\n")

    print("--- 第一段：假模型驱动（不需要密钥）---")
    result = run_loop(question)
    for step in result["steps"]:
        print(f"step {step['step']}：发起 {step['tool_calls']} 个调用")
        for entry in step["results"]:
            print(f"    {entry['tool']}({entry['args']}) → ok={entry['ok']} summary={entry['summary']}")
    print(f"\n最终回答：{result['answer']}")
    print(f"累计工具调用：{result['tool_calls']}；状态：{result.get('status', 'ok')}")

    print("\n--- 第二段：预算上限压到 2 次 ---")
    tight = run_loop(question, max_tool_calls=2)
    print(f"最终回答：{tight['answer']}；累计调用：{tight['tool_calls']}；状态：{tight.get('status', 'ok')}")

    print("\n--- 第三段：真实模型（可选）---")
    model = load_real_model()
    if model is None:
        print("[跳过] 未配置 OPS_MODEL / OPS_API_KEY；假模型已经演示了完整循环。")
    else:
        real = run_real_loop(model, question)
        print(f"最终回答：{real['answer']}")
        for entry in real["trace"]:
            print(f"    {entry['tool']}({entry['args']}) → ok={entry['ok']} summary={entry['summary']}")

    print("\n[结论] 工具执行发生在 execute_tool_calls()；模型只提供 name/args。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
