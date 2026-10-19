"""Agent 层公共入口：LLM 客户端、统一 JSON 工具与 create_agent 简化版 Agent。
外部代码通常从这里导入 build_llm、make_tools 与 build_simple_agent/run_simple_agent。
全部模块在无模型时保持可用：LLMClient.enabled=False，Agent 返回 None，不抛异常。
"""

from __future__ import annotations

from .llm import LLMClient, build_llm, content_to_text
from .simple_agent import (
    SYSTEM_PROMPT,
    build_simple_agent,
    run_simple_agent,
)
from .tools import make_tools

__all__ = [
    "LLMClient",
    "SYSTEM_PROMPT",
    "build_llm",
    "build_simple_agent",
    "content_to_text",
    "make_tools",
    "run_simple_agent",
]
