"""LLM 客户端：惰性初始化聊天模型，统一记录调用次数、token 与错误。
无密钥时 enabled=False，所有调用返回 None 而不抛异常，主流程可继续走纯规则模式。
支持 OpenAI 兼容端点（base_url）与 with_structured_output 结构化输出。
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from pydantic import BaseModel

from ops_assistant.config import Settings
from ops_assistant.knowledge.embeddings import redact_secret

logger = logging.getLogger(__name__)

# 结构化输出泛型：要求是 pydantic BaseModel 子类。
ModelT = TypeVar("ModelT", bound=BaseModel)


def content_to_text(content: Any) -> str:
    """把消息 content 统一转换为纯文本。

    参数：
        content: 字符串、内容块列表（多模态）或其他对象。

    返回：
        str：拼接后的文本；无法识别时返回空字符串。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


class LLMClient:
    """聊天模型客户端封装（线程内使用，不做跨线程共享）。

    设计要点：
        - 首次调用时才用 init_chat_model 创建模型（惰性初始化）；
        - 模型按 temperature 缓存，避免每次调用重复构造；
        - 任何异常都转成 last_error，不让上层崩溃；
        - API key 绝不写入日志，错误摘要统一脱敏。

    统计口径（重要）：
        usage() 只统计本 LLMClient 自身的调用。simple_agent 使用
        create_agent 时，模型由框架内部直接调用，那些调用的次数与 token
        不计入本对象，usage 中不会体现；这是已知限制，不是统计遗漏。
    """

    def __init__(self, settings: Settings) -> None:
        """保存配置，不创建任何网络客户端。

        参数：
            settings: 项目配置（model/model_provider/api_key/base_url 等）。
        """
        self._settings = settings
        self._models: dict[float, Any] = {}
        self._init_failed = False
        self._last_error: str | None = None
        self._model_calls = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._errors = 0

    @property
    def enabled(self) -> bool:
        """模型是否可用：配置齐全且初始化没有失败。"""
        return bool(self._settings.llm_enabled and not self._init_failed)

    def _redact(self, text: str) -> str:
        """对错误信息做密钥脱敏与截断。"""
        return redact_secret(text, self._settings.api_key)

    def redact(self, text: str) -> str:
        """公开的脱敏方法，供 Agent 层记录日志时复用。"""
        return self._redact(text)

    def _record_error(self, prefix: str, exc: BaseException) -> None:
        """记录一次错误（计数 + last_error + WARNING 日志）。"""
        self._errors += 1
        self._last_error = self._redact(f"{prefix}：{type(exc).__name__}: {exc}")
        logger.warning(self._last_error)

    def _get_model(self, temperature: float) -> Any | None:
        """按 temperature 惰性获取聊天模型；失败返回 None。

        参数：
            temperature: 采样温度；分析类任务默认 0。

        返回：
            聊天模型实例或 None。
        """
        key = float(temperature)
        if key in self._models:
            return self._models[key]
        if self._init_failed:
            return None
        if not self._settings.llm_enabled:
            if self._last_error is None:
                self._last_error = "模型未启用：缺少 OPS_MODEL 或 OPS_API_KEY，当前为纯规则模式"
            return None

        model_name = self._settings.model
        if not model_name:
            self._last_error = "模型未配置：OPS_MODEL 为空"
            return None

        try:
            from langchain.chat_models import init_chat_model
        except Exception as exc:
            self._init_failed = True
            self._record_error("导入 init_chat_model 失败", exc)
            return None

        kwargs: dict[str, Any] = {}
        provider = self._settings.model_provider
        if provider and ":" not in model_name:
            kwargs["model_provider"] = provider
        if self._settings.base_url:
            kwargs["base_url"] = self._settings.base_url
        if self._settings.api_key:
            kwargs["api_key"] = self._settings.api_key

        try:
            try:
                model = init_chat_model(model_name, temperature=key, **kwargs)
            except Exception:
                # 部分 provider 不接受 temperature 构造参数，退化为使用默认温度。
                model = init_chat_model(model_name, **kwargs)
        except Exception as exc:
            self._init_failed = True
            self._record_error("模型初始化失败", exc)
            return None

        self._models[key] = model
        return model

    def get_chat_model(self, *, temperature: float = 0.0) -> Any | None:
        """返回底层聊天模型，供 create_agent 或能力探测使用。

        参数：
            temperature: 采样温度。

        返回：
            聊天模型实例或 None（未配置/初始化失败）。
        """
        return self._get_model(temperature)

    def _as_messages(self, messages: list[Any]) -> list[BaseMessage]:
        """把 dict/字符串形式的简化消息转换为 LangChain 消息对象。

        参数：
            messages: 消息列表，元素可以是 BaseMessage、dict（role/content）或 str。

        返回：
            list[BaseMessage]：转换后的消息列表。

        异常：
            TypeError: 入参不是列表。
        """
        if not isinstance(messages, list):
            raise TypeError("messages 必须是消息列表")
        normalized: list[BaseMessage] = []
        for item in messages:
            if isinstance(item, BaseMessage):
                normalized.append(item)
                continue
            if isinstance(item, str):
                normalized.append(HumanMessage(content=item))
                continue
            if isinstance(item, dict):
                role = str(item.get("role", "user")).strip().lower()
                content = item.get("content", "")
                if role == "system":
                    normalized.append(SystemMessage(content=content))
                elif role in ("assistant", "ai"):
                    normalized.append(AIMessage(content=content))
                else:
                    normalized.append(HumanMessage(content=content))
                continue
            raise TypeError(f"无法识别的消息类型：{type(item).__name__}")
        return normalized

    def _accumulate_usage(self, response: Any) -> None:
        """从模型响应中累计 token 用量（拿不到就保持 0，不做估算）。"""
        usage = getattr(response, "usage_metadata", None)
        if isinstance(usage, dict):
            self._prompt_tokens += _safe_int(usage.get("input_tokens"))
            self._completion_tokens += _safe_int(usage.get("output_tokens"))
            return
        metadata = getattr(response, "response_metadata", None)
        if isinstance(metadata, dict):
            token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
            if isinstance(token_usage, dict):
                self._prompt_tokens += _safe_int(token_usage.get("prompt_tokens"))
                self._completion_tokens += _safe_int(token_usage.get("completion_tokens"))

    def invoke_text(self, messages: list[Any], *, temperature: float = 0) -> str | None:
        """执行一次文本调用。

        参数：
            messages: 消息列表（BaseMessage/dict/str）。
            temperature: 采样温度，默认 0。

        返回：
            str | None：模型文本输出；未启用、失败或空输出时返回 None。
        """
        model = self._get_model(temperature)
        if model is None:
            return None
        self._model_calls += 1
        try:
            normalized = self._as_messages(messages)
            response = model.invoke(normalized)
        except Exception as exc:
            self._record_error("文本调用失败", exc)
            return None

        self._accumulate_usage(response)
        text = content_to_text(getattr(response, "content", response)).strip()
        if not text:
            self._last_error = "模型返回空文本"
            logger.warning(self._last_error)
            return None
        return text

    def invoke_structured(
        self,
        schema: type[ModelT],
        messages: list[Any],
    ) -> ModelT | None:
        """执行一次结构化输出调用（with_structured_output）。

        参数：
            schema: pydantic BaseModel 子类，描述期望的结构。
            messages: 消息列表（BaseMessage/dict/str）。

        返回：
            ModelT | None：校验后的结构对象；未启用或失败时返回 None。

        说明：
            结构化调用的 token 用量通常不在返回对象里，因此这里只计 model_calls，
            不估算 prompt/completion token。
        """
        model = self._get_model(0.0)
        if model is None:
            return None
        self._model_calls += 1
        try:
            structured = model.with_structured_output(schema)
        except Exception as exc:
            self._record_error("with_structured_output 不可用", exc)
            return None

        try:
            normalized = self._as_messages(messages)
            result = structured.invoke(normalized)
        except Exception as exc:
            self._record_error("结构化输出调用失败", exc)
            return None

        self._accumulate_usage(result)
        if result is None:
            self._last_error = "结构化输出为空"
            logger.warning(self._last_error)
            return None
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            try:
                return schema.model_validate(result)
            except Exception as exc:
                self._record_error("结构化输出校验失败", exc)
                return None
        try:
            return schema.model_validate(result)
        except Exception as exc:
            self._record_error("结构化输出校验失败", exc)
            return None

    def usage(self) -> dict[str, Any]:
        """返回调用统计（可直接 JSON 序列化）。

        返回：
            dict：model_calls/prompt_tokens/completion_tokens/errors，
            以及 enabled/model/last_error 等辅助信息。

        注意：
            只统计本 LLMClient 自身的调用；create_agent 内部的模型调用
            （simple_agent 路径）不经过这里，不会出现在统计中。
        """
        return {
            "model_calls": self._model_calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "errors": self._errors,
            "enabled": self.enabled,
            "model": self._settings.model,
            "last_error": self._last_error,
        }

    def last_error(self) -> str | None:
        """返回最近一次错误摘要（已脱敏）；没有错误时返回 None。"""
        return self._last_error


def _safe_int(value: Any) -> int:
    """把 token 计数转换为非负整数；非法值按 0 处理。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def build_llm(settings: Settings) -> LLMClient:
    """构造 LLMClient（惰性，无网络请求）。

    参数：
        settings: 项目配置。

    返回：
        LLMClient：可用于文本/结构化调用的客户端。
    """
    return LLMClient(settings)
