"""单元 02 实验：观察上下文增长对成本、延迟与稳定性的影响。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/02_context_growth.py
无密钥时使用“本地假模型”，仍然能观察 token 估算与截断策略。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "ops-assistant" / ".env"
MAX_HISTORY_MESSAGES = 6


def mask(secret: str | None) -> str:
    if not secret:
        return "未设置"
    return f"{secret[:4]}…{secret[-2:]}"


def load_config() -> dict[str, str | None]:
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    raw_model = (os.getenv("OPS_MODEL") or "").strip()
    provider_hint = (os.getenv("OPS_MODEL_PROVIDER") or "").strip()
    api_key = (os.getenv("OPS_API_KEY") or "").strip() or os.getenv("OPENAI_API_KEY")
    base_url = (os.getenv("OPS_BASE_URL") or "").strip() or None
    provider, model_name = ("openai", raw_model)
    if ":" in raw_model:
        provider, _, model_name = raw_model.partition(":")
    elif provider_hint:
        provider = provider_hint
    return {
        "provider": provider.strip(),
        "model_name": model_name.strip(),
        "api_key": api_key,
        "base_url": base_url,
    }


def build_history(turns: int) -> list[dict[str, str]]:
    """构造多轮历史；用 dict 表示，便于无密钥时也不依赖 langchain。"""
    system = {
        "role": "system",
        "content": (
            "你是经营数据分析助手。规则：金额单位是分；只复述给定数字；"
            "不确定时说“不确定”；不要编造地区数据。"
        ),
    }
    history: list[dict[str, str]] = [system]
    for index in range(turns):
        history.append(
            {
                "role": "user",
                "content": f"第 {index + 1} 轮：2026 年九月净销售额 {1286500 + index * 1000} 分，八月 1190000 分。",
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": f"第 {index + 1} 轮记录：九月 {1286500 + index * 1000} 分，八月 1190000 分。",
            }
        )
    history.append({"role": "user", "content": "那华东地区呢？"})
    return history


def estimate_tokens(text: str) -> int:
    """极简 token 估算：中文按 1 字 1 token，英文按 4 字符 1 token。

    教学简化：真实分词器（tokenizer）不同，本函数只用于展示“趋势”。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    others = len(text) - cjk
    return cjk + max(1, others // 4)


def total_tokens(history: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(item["content"]) for item in history)


def truncate_history(history: list[dict[str, str]], max_messages: int) -> list[dict[str, str]]:
    """保留 system + 最近 max_messages 条非 system 消息。

    规则显式：system 永远保留（否则规则丢失）；其余按“越旧越先丢”。
    """
    system_messages = [item for item in history if item["role"] == "system"]
    others = [item for item in history if item["role"] != "system"]
    kept = others[-max_messages:]
    return system_messages + kept


def main() -> int:
    print("== 单元 02 实验：上下文增长与截断 ==")
    config = load_config()

    for turns in (1, 5, 20):
        history = build_history(turns)
        tokens = total_tokens(history)
        truncated = truncate_history(history, MAX_HISTORY_MESSAGES)
        print(
            f"轮数={turns:>2} 消息数={len(history):>3} 估算输入token={tokens:>5} "
            f"截断后消息数={len(truncated):>2} 截断后token={total_tokens(truncated):>5}"
        )

    print("\n== 同一问题的两种问法 ==")
    print("A 带上下文：", build_history(1)[-2:])
    print("B 不带上下文：", [{"role": "user", "content": "那华东地区呢？"}])
    print("→ A 能让模型知道“哪个月、什么指标”；B 不能。上下文不会自己保留。")

    enabled = bool(config["model_name"]) and bool(config["api_key"])
    if not enabled:
        print("\n[规则模式] 未配置模型，跳过真实调用。下面用本地确定性函数模拟回复：")
        print("    假模型回复：本轮问题缺少时间与指标，请你补充“哪个月、哪个指标”。")
        print("\n[结论] 即使没密钥，你也能观察：历史越长 token 越多、截断会丢信息。")
        return 0

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=config["model_name"],
        api_key=config["api_key"],
        base_url=config["base_url"],
        temperature=0.0,
        timeout=60.0,
        max_retries=0,
    )

    def ask(messages: list[dict[str, str]]) -> str:
        converted = []
        for item in messages:
            if item["role"] == "system":
                converted.append(SystemMessage(content=item["content"]))
            elif item["role"] == "assistant":
                converted.append(AIMessage(content=item["content"]))
            else:
                converted.append(HumanMessage(content=item["content"]))
        reply = model.invoke(converted)
        usage = getattr(reply, "usage_metadata", None)
        return f"{reply.content}\n    [usage] {usage}"

    print("\n--- 带上下文提问 ---")
    print(ask(build_history(2)))

    print("\n--- 只发最后一句 ---")
    print(ask([{"role": "user", "content": "那华东地区呢？"}]))

    print("\n--- 截断后再问 ---")
    print(ask(truncate_history(build_history(20), MAX_HISTORY_MESSAGES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
