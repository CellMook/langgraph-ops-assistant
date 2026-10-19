"""单元 01 实验 B：探测当前模型是否支持 文本 / 工具调用 / 结构化输出。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/01_probe_model.py
无密钥时打印“跳过”并返回 0；有密钥时逐项探测并打印耗时（耗时随网络波动）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "ops-assistant" / ".env"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "query_month_sales",
        "description": "查询某个月的净销售额。月份格式必须是 YYYY-MM，金额单位是分。",
        "parameters": {
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "月份，格式 YYYY-MM，例如 2026-09",
                }
            },
            "required": ["month"],
        },
    },
}


class MonthFact(BaseModel):
    """结构化输出探测用的最小模式。"""

    month: str = Field(description="月份，格式 YYYY-MM")
    net_sales_cents: int = Field(description="净销售额，单位分，整数")
    contains_refund: bool = Field(description="该净销售额是否已扣除退款")


def load_config() -> dict[str, str | None] | None:
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    raw_model = (os.getenv("OPS_MODEL") or "").strip()
    provider_hint = (os.getenv("OPS_MODEL_PROVIDER") or "").strip()
    api_key = (os.getenv("OPS_API_KEY") or "").strip() or os.getenv("OPENAI_API_KEY")
    base_url = (os.getenv("OPS_BASE_URL") or "").strip() or None
    if not raw_model or not api_key:
        return None
    if ":" in raw_model:
        provider, _, model_name = raw_model.partition(":")
    else:
        provider, model_name = provider_hint or "openai", raw_model
    return {
        "provider": provider.strip(),
        "model_name": model_name.strip(),
        "api_key": api_key,
        "base_url": base_url,
    }


def probe(name: str, fn) -> tuple[bool, str]:
    started = time.perf_counter()
    try:
        detail = fn()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return True, f"{detail}（耗时 {elapsed_ms} ms，数值随网络波动）"
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return False, f"{type(exc).__name__}: {exc}（耗时 {elapsed_ms} ms）"


def main() -> int:
    print("== 单元 01 实验 B：模型能力探测 ==")
    config = load_config()
    if config is None:
        print("[跳过] 未配置 OPS_MODEL / OPS_API_KEY，本脚本不联网。")
        print("       没有密钥是允许的：后续单元 06～09 完全不需要模型也能学完。")
        return 0

    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=config["model_name"],
        api_key=config["api_key"],
        base_url=config["base_url"],
        temperature=0.0,
        timeout=60.0,
        max_retries=0,
    )
    print(f"[配置] {config['provider']}:{config['model_name']} @ {config['base_url'] or 'OpenAI 默认'}")

    def probe_text() -> str:
        answer = model.invoke([HumanMessage(content="只回复两个字：收到。")])
        text = (answer.content or "").strip()
        if not text:
            raise ValueError("返回内容为空")
        return f"content={text[:30]!r}"

    def probe_tool_call() -> str:
        bound = model.bind_tools([TOOL_SPEC])
        answer = bound.invoke(
            [HumanMessage(content="2026 年九月的净销售额是多少？请调用工具查询，不要自己猜。")]
        )
        calls = getattr(answer, "tool_calls", None) or []
        if not calls:
            raise ValueError("模型没有返回 tool_calls（可能不支持工具调用）")
        first = calls[0]
        return f"tool_calls[0].name={first.get('name')} args={first.get('args')}"

    def probe_structured() -> str:
        structured = model.with_structured_output(MonthFact)
        result = structured.invoke(
            [
                HumanMessage(
                    content=(
                        "从这句话里抽取结构化信息：2026 年九月净销售额 1286500 分，"
                        "该数字已扣除退款。"
                    )
                )
            ]
        )
        if not isinstance(result, MonthFact):
            raise TypeError(f"返回类型是 {type(result).__name__}，不是 MonthFact")
        return f"month={result.month} net={result.net_sales_cents} contains_refund={result.contains_refund}"

    results = [
        ("文本生成", probe_text),
        ("工具调用", probe_tool_call),
        ("结构化输出", probe_structured),
    ]

    failures = 0
    print("\n== 探测结果 ==")
    for label, fn in results:
        ok, detail = probe(label, fn)
        print(f"[{'支持' if ok else '不支持/失败'}] {label}：{detail}")
        if not ok:
            failures += 1

    print("\n== 结论 ==")
    if failures == 0:
        print("三项都通过。可以按教程完整学习；注意模型行为仍可能波动。")
    else:
        print(f"有 {failures} 项失败。请按第 8 节排查；失败不妨碍继续学单元 06～09。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
