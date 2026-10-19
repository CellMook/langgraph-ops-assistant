#!/usr/bin/env python
"""模型能力自检：检查配置是否存在，并各做一次文本调用、bind_tools 与结构化输出探测。
只向 stdout 输出 JSON 报告，不打印任何密钥，也不假设模型服务一定可用。
运行方式：uv run python scripts/check_model.py
"""

from __future__ import annotations

import json
import logging
import platform
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# 允许在未安装项目（未 uv sync）时也能从 src 目录导入。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from ops_assistant.agent.llm import LLMClient, build_llm  # noqa: E402
from ops_assistant.config import load_settings  # noqa: E402
from ops_assistant.knowledge.embeddings import redact_secret  # noqa: E402


class EchoArgs(BaseModel):
    """bind_tools 探测用的最小工具参数模型。"""

    text: str = Field(description="要回显的文本")


class Verdict(BaseModel):
    """with_structured_output 探测用的最小结构化输出模型。"""

    summary: str = Field(description="一句话摘要，不超过 20 字")
    is_positive: bool = Field(description="是否为正面评价")


def _safe_url(url: str | None) -> str | None:
    """只展示协议、主机与端口，去掉可能包含账号或查询参数的敏感部分。"""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "<无法解析的 base_url>"
    host = parts.hostname or ""
    if port:
        host = f"{host}:{port}"
    if not host:
        return "<已配置 base_url>"
    return urlunsplit((parts.scheme or "https", host, "", "", ""))


def _redact(text: str, llm: LLMClient) -> str:
    """错误摘要脱敏。"""
    return llm.redact(text)


def _check_text_invocation(llm: LLMClient) -> dict[str, Any]:
    """执行一次最小文本调用。"""
    messages = [
        SystemMessage(content="你是自检助手，只输出简短中文。"),
        HumanMessage(content="请回答：1+1 等于几？只输出答案。"),
    ]
    text = llm.invoke_text(messages, temperature=0)
    if not text:
        return {
            "ok": False,
            "skipped": False,
            "error": llm.last_error() or "文本调用没有返回内容",
        }
    return {
        "ok": True,
        "skipped": False,
        "preview": text[:160],
        "chars": len(text),
    }


def _check_bind_tools(llm: LLMClient) -> dict[str, Any]:
    """探测 bind_tools 是否可用，并观察模型是否真的返回工具调用。"""
    model = llm.get_chat_model(temperature=0.0)
    if model is None:
        return {
            "ok": False,
            "skipped": True,
            "error": llm.last_error() or "模型不可用",
        }
    try:
        bound = model.bind_tools([EchoArgs])
        response = bound.invoke(
            [
                SystemMessage(content="你需要通过工具完成请求。"),
                HumanMessage(content="请调用 EchoArgs 工具，参数 text 设为 ping。"),
            ]
        )
    except Exception as exc:
        return {
            "ok": False,
            "skipped": False,
            "error": _redact(f"{type(exc).__name__}: {exc}", llm),
        }

    tool_calls = list(getattr(response, "tool_calls", None) or [])
    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "bind_ok": True,
        "tool_calls_seen": bool(tool_calls),
        "tool_call_count": len(tool_calls),
        "tools": [str(call.get("name", "")) for call in tool_calls if isinstance(call, dict)],
    }
    if not tool_calls:
        result["note"] = (
            "bind_tools 调用成功，但模型本次没有返回 tool_calls；"
            "这不等于 bind_tools 不可用，可能是模型/提示词未触发工具调用。"
        )
    return result


def _check_structured_output(llm: LLMClient) -> dict[str, Any]:
    """探测 with_structured_output 是否可用。"""
    messages = [
        SystemMessage(content="请严格按给定结构输出。"),
        HumanMessage(content="评价这句话：“这个功能很好用。”"),
    ]
    verdict = llm.invoke_structured(Verdict, messages)
    if verdict is None:
        return {
            "ok": False,
            "skipped": False,
            "error": llm.last_error() or "结构化输出没有返回结果",
        }
    return {
        "ok": True,
        "skipped": False,
        "summary": verdict.summary,
        "is_positive": verdict.is_positive,
    }


def build_report() -> dict[str, Any]:
    """加载配置并执行全部探测，返回可 JSON 序列化的报告。"""
    settings = load_settings()
    llm = build_llm(settings)
    checks: dict[str, Any] = {}

    configured = bool(settings.llm_enabled)
    checks["configuration"] = {
        "ok": configured,
        "skipped": False,
        "detail": (
            "检测到 OPS_MODEL 与 OPS_API_KEY，模型调用已启用"
            if configured
            else "未检测到完整的模型配置；项目将以纯规则模式运行（这是允许的）"
        ),
    }

    if not configured:
        for name in ("text_invocation", "bind_tools", "structured_output"):
            checks[name] = {
                "ok": False,
                "skipped": True,
                "error": "模型未配置，跳过探测",
            }
    else:
        checks["text_invocation"] = _check_text_invocation(llm)
        checks["bind_tools"] = _check_bind_tools(llm)
        checks["structured_output"] = _check_structured_output(llm)

    required = ("text_invocation", "bind_tools", "structured_output")
    report: dict[str, Any] = {
        "ok": bool(
            checks["configuration"].get("ok")
            and all(bool(checks[name].get("ok")) for name in required)
        ),
        "model_configured": configured,
        "model": settings.model,
        "model_provider": settings.model_provider,
        "base_url": _safe_url(settings.base_url),
        "checks": checks,
        "usage": llm.usage(),
        "python_version": platform.python_version(),
        "project_root": str(PROJECT_ROOT),
        "notes": [
            "本报告只反映本次探测结果；网络不可达、配额不足或模型名错误都会表现为失败，"
            "不代表项目代码不可用。",
            "usage 只统计文本调用与结构化输出调用；bind_tools 探测为直接调用，未计入 token。",
            "未配置模型时 enabled=False 是预期行为，纯规则 workflow 仍可离线运行。",
        ],
    }
    return report


def main() -> int:
    """入口：打印 JSON 报告。成功/失败都返回 0，除非脚本自身崩溃。"""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        report = build_report()
    except Exception as exc:
        payload = {
            "ok": False,
            "fatal": True,
            "error": redact_secret(f"{type(exc).__name__}: {exc}"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
