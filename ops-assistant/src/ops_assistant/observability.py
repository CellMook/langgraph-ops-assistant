"""可观测性：线程安全的进程内指标采集与结构化日志字段。

本模块不依赖项目内其他模块，工作流节点、API 与脚本都可以复用。
指标是"进程累计值"；单次请求的 elapsed_ms 必须由调用方传入请求起点，
避免把累计值当成单次延迟对外宣称。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

LOGGER_NAME = "ops_assistant"
_EVENT_LOGGER = logging.getLogger(LOGGER_NAME)


def get_logger(name: str | None = None) -> logging.Logger:
    """返回项目统一 logger。

    参数：
        name: 子 logger 名称；为空时返回根 logger ``ops_assistant``。

    返回：
        logging.Logger：用于记录结构化字段的 logger 实例。
    """
    if not name:
        return _EVENT_LOGGER
    if name == LOGGER_NAME or name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def new_request_id() -> str:
    """生成一个短请求 id，仅用于日志串联，不用于安全用途。"""
    return uuid.uuid4().hex[:12]


def elapsed_ms_since(started_at: float | None) -> float:
    """计算从 ``started_at``（time.perf_counter 值）到现在的毫秒数。

    参数：
        started_at: 起始时间戳；为 None 时返回 0.0（表示"没有测量过"）。

    返回：
        float：保留两位小数的毫秒数。
    """
    if started_at is None:
        return 0.0
    return round((time.perf_counter() - started_at) * 1000.0, 2)


def log_structured(
    *,
    event: str,
    request_id: str = "",
    thread_id: str = "",
    node: str = "",
    tool: str = "",
    ok: bool = True,
    duration_ms: float | None = None,
    error_code: str | None = None,
    level: int = logging.INFO,
    detail: dict[str, Any] | None = None,
) -> None:
    """输出一条结构化日志，字段与规格 5.12 一致。

    参数：
        event: 事件名（例如 chat.start、tool.retry、export.write）。
        request_id: 请求 id，用于串联同一次请求的多条日志。
        thread_id: 会话 id。
        node: 工作流节点名。
        tool: 工具名（非工具事件留空）。
        ok: 本次操作是否成功。
        duration_ms: 本次操作耗时（毫秒），未知时传 None。
        error_code: 失败时的可读错误码，成功时为 None。
        level: 日志级别，默认 INFO。
        detail: 额外字段，会被序列化进日志的 ``ops.detail`` 中。
    """
    payload: dict[str, Any] = {
        "event": event,
        "request_id": request_id,
        "thread_id": thread_id,
        "node": node,
        "tool": tool,
        "ok": bool(ok),
        "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
        "error_code": error_code,
    }
    if detail:
        payload["detail"] = detail
    _EVENT_LOGGER.log(level, "ops_event", extra={"ops": payload})


@dataclass
class MetricsCollector:
    """进程内指标采集器。

    说明：
        1. 所有字段都是"进程累计值"，多用户/多请求共享，因此只适合本地演示与
           教学；生产环境应替换为 Prometheus 等指标后端。
        2. 所有写入都加锁，节点在 LangGraph 的不同执行上下文里调用也是安全的。
    """

    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.perf_counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_model(
        self,
        *,
        model: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        ok: bool = True,
        thread_id: str = "",
        node: str = "",
        duration_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        """记录一次模型调用（成功或失败都计数）。

        参数：
            model: 模型名，仅写日志用。
            prompt_tokens: 提示词 token 数，未知传 0。
            completion_tokens: 生成 token 数，未知传 0。
            ok: 是否成功。
            thread_id: 会话 id。
            node: 发起调用的节点名。
            duration_ms: 耗时毫秒。
            error_code: 失败错误码。
        """
        with self._lock:
            self.model_calls += 1
            self.prompt_tokens += max(int(prompt_tokens or 0), 0)
            self.completion_tokens += max(int(completion_tokens or 0), 0)
            if not ok:
                self.errors += 1
        log_structured(
            event="llm.call",
            thread_id=thread_id,
            node=node,
            tool=model or "",
            ok=ok,
            duration_ms=duration_ms,
            error_code=error_code,
            level=logging.INFO if ok else logging.WARNING,
        )

    def record_tool(
        self,
        *,
        tool: str,
        ok: bool = True,
        thread_id: str = "",
        node: str = "",
        duration_ms: float | None = None,
        error_code: str | None = None,
        params: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """记录一次工具调用。

        参数：
            tool: 工具名（例如 query_month_sales）。
            ok: 是否成功。
            thread_id: 会话 id。
            node: 调用所在节点。
            duration_ms: 耗时毫秒。
            error_code: 失败错误码。
            params: 工具入参摘要（不要放敏感信息）。
            summary: 返回结果摘要。
        """
        with self._lock:
            self.tool_calls += 1
            if not ok:
                self.errors += 1
        detail: dict[str, Any] = {}
        if params:
            detail["params"] = params
        if summary:
            detail["summary"] = summary
        log_structured(
            event="tool.call",
            thread_id=thread_id,
            node=node,
            tool=tool,
            ok=ok,
            duration_ms=duration_ms,
            error_code=error_code,
            level=logging.INFO if ok else logging.WARNING,
            detail=detail or None,
        )

    def record_error(
        self,
        *,
        code: str,
        message: str = "",
        thread_id: str = "",
        node: str = "",
        request_id: str = "",
    ) -> None:
        """记录一次业务错误（只计数并写日志，不抛异常）。"""
        with self._lock:
            self.errors += 1
        log_structured(
            event="error",
            request_id=request_id,
            thread_id=thread_id,
            node=node,
            ok=False,
            error_code=code,
            level=logging.WARNING,
            detail={"message": message} if message else None,
        )

    def snapshot(self, *, started_at: float | None = None) -> dict[str, Any]:
        """导出指标快照。

        参数：
            started_at: 请求起点（time.perf_counter 值）。传入时 elapsed_ms 表示
                本次请求耗时；不传时 elapsed_ms 表示进程启动至今的时长（并在
                字段名 uptime_ms 中同时给出）。

        返回：
            dict：可直接 JSON 序列化的指标字典。
        """
        now = time.perf_counter()
        with self._lock:
            model_calls = int(self.model_calls)
            tool_calls = int(self.tool_calls)
            prompt_tokens = int(self.prompt_tokens)
            completion_tokens = int(self.completion_tokens)
            errors = int(self.errors)
            process_uptime_ms = round((now - self.started_at) * 1000.0, 2)
        elapsed = elapsed_ms_since(started_at) if started_at is not None else process_uptime_ms
        return {
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "errors": errors,
            "elapsed_ms": elapsed,
            "uptime_ms": process_uptime_ms,
        }

    def reset(self) -> None:
        """清零所有累计指标（测试用；进程运行期不建议调用）。"""
        with self._lock:
            self.model_calls = 0
            self.tool_calls = 0
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.errors = 0
            self.started_at = time.perf_counter()
