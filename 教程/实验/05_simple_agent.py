"""单元 05 实验：用 create_agent 搭第一版问答助手（含无密钥回退）。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/05_simple_agent.py
有密钥：真实运行 agent；无密钥：打印 agent 组成 + 走确定性规则回答。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "ops-assistant" / ".env"

FAKE_MONTHS = {
    "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500,
                "order_count": 57, "has_orders": True},
    "2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000,
                "order_count": 61, "has_orders": True},
}


@tool
def query_month_sales(month: str) -> str:
    """查询某个月的销售汇总；月份格式 YYYY-MM，金额单位分。

    Args:
        month: 月份，例如 "2026-09"。
    """
    data = FAKE_MONTHS.get(month)
    if data is None:
        return json.dumps(
            {
                "ok": False,
                "error": {"code": "NO_DATA", "message": f"没有 {month} 的数据"},
                "hint": "可用月份：2026-08、2026-09",
            },
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, "data": data, "unit": "cents"}, ensure_ascii=False)


@tool
def calculate_change(current_cents: int, previous_cents: int) -> str:
    """计算两期净销售额的差额与变化率；previous 为 0 时 pct_change 为 null。

    Args:
        current_cents: 本期净销售额，单位分。
        previous_cents: 上期净销售额，单位分。
    """
    delta = current_cents - previous_cents
    pct = None if previous_cents == 0 else round(delta / abs(previous_cents), 4)
    return json.dumps(
        {"ok": True, "data": {"delta_cents": delta, "pct_change": pct}, "unit": "cents"},
        ensure_ascii=False,
    )


TOOLS = [query_month_sales, calculate_change]
SYSTEM_PROMPT = (
    "你是经营数据分析助手。规则："
    "1) 金额单位是分；2) 需要数字时必须调用工具，禁止心算；"
    "3) 工具返回 ok=false 时如实告知，不要编造；"
    "4) 回答先给结论，再给数字依据。"
)
QUESTION = "2026 年九月净销售额比八月变化多少？"


def mask(secret: str | None) -> str:
    if not secret:
        return "未设置"
    return f"{secret[:4]}…{secret[-2:]}"


def load_model():
    """返回 (model, description)；不可用时 model 为 None。"""
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    raw_model = (os.getenv("OPS_MODEL") or "").strip()
    api_key = (os.getenv("OPS_API_KEY") or "").strip() or os.getenv("OPENAI_API_KEY")
    base_url = (os.getenv("OPS_BASE_URL") or "").strip() or None
    if not raw_model or not api_key:
        return None, "未配置 OPS_MODEL / OPS_API_KEY"
    provider, model_name = "openai", raw_model
    if ":" in raw_model:
        provider, _, model_name = raw_model.partition(":")
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=model_name, api_key=api_key, base_url=base_url,
        temperature=0.0, timeout=60.0, max_retries=0,
    )
    return model, f"{provider}:{model_name}（key={mask(api_key)}）"


def rule_fallback(question: str) -> dict:
    """无模型时的确定性回答：解析月份 → 查数 → 算差额 → 组装文字。"""
    current = "2026-09"
    previous = "2026-08"
    cur = FAKE_MONTHS[current]["net_cents"]
    prev = FAKE_MONTHS[previous]["net_cents"]
    delta = cur - prev
    pct = round(delta / prev, 4)
    answer = (
        f"【规则模式·used_fallback=True】{current} 净销售额 {cur} 分，"
        f"{previous} 净销售额 {prev} 分，差额 {delta} 分，变化率 {pct:.2%}。"
        f"口径：净销售额 = 毛销售额 − 已批准退款；本条回答由程序计算，未调用模型。"
    )
    sources = [
        {"type": "sql", "month": current, "field": "net_cents", "value": cur},
        {"type": "sql", "month": previous, "field": "net_cents", "value": prev},
        {"type": "calc", "formula": "delta = current - previous", "value": delta},
    ]
    return {"answer": answer, "used_fallback": True, "sources": sources}


def describe_agent(model_description: str) -> None:
    print("== Agent 组成（Model + Harness）==")
    print(f"  模型（Model）：{model_description}")
    print(f"  工具（tools）：{[item.name for item in TOOLS]}")
    print(f"  系统提示词长度：{len(SYSTEM_PROMPT)} 字")
    print("  Harness 提供：工具绑定 / 工具调用循环 / 消息管理 / 可选 checkpointer")
    print("  Harness 不提供：业务校验 / 预算 / 审批 / 失败出口 / 报告导出")


def run_with_agent(model) -> dict:
    from langchain.agents import create_agent

    agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
    result = agent.invoke({"messages": [{"role": "user", "content": QUESTION}]})
    messages = result["messages"]
    tool_calls = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append({"name": call.get("name"), "args": call.get("args")})
    return {
        "answer": messages[-1].content,
        "used_fallback": False,
        "message_count": len(messages),
        "tool_calls": tool_calls,
    }


def main() -> int:
    print("== 单元 05 实验：create_agent 第一版问答助手 ==")
    print(f"问题：{QUESTION}\n")

    model, description = load_model()
    describe_agent(description)

    if model is None:
        print(f"\n[回退] {description}；运行规则模式，仍可得到正确业务结论：")
        result = rule_fallback(QUESTION)
        print(f"\n回答：{result['answer']}")
        print(f"used_fallback={result['used_fallback']}")
        for source in result["sources"]:
            print(f"  source: {source}")
        print("\n[提示] 配置密钥后重跑，可看到 create_agent 的真实输出与工具调用记录。")
        return 0

    print("\n[运行] create_agent + invoke ...")
    try:
        result = run_with_agent(model)
    except Exception as exc:
        print(f"[失败] {type(exc).__name__}: {exc}")
        print("排查：1) create_agent 参数名是否与你的版本一致；2) 模型是否支持工具调用；3) 端点是否可用。")
        return 1

    print(f"\n消息条数：{result['message_count']}")
    print("工具调用记录：")
    for call in result["tool_calls"]:
        print(f"  - {call['name']}({call['args']})")
    print(f"\n最终回答：{result['answer']}")
    print("\n[观察] 回答里的数字应当来自工具结果；若模型自己算了，说明提示词约束不够，需加强校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
