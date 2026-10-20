"""FastAPI 应用入口：会话、问答、人工审批、偏好与健康检查接口。

启动方式（单 worker，**不要**加 ``--workers N``）::

    uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000

为什么必须单 worker：
    本项目的运行时（SQLite 连接、检查点、偏好存储、``threading.Lock``）是
    **进程内单例**。多 worker 时每个进程各有一份运行时、各写各的检查点文件，
    同一 thread 的历史会互相覆盖。真实多实例部署需要把检查点/偏好换成
    Postgres 等共享后端，并去掉进程内锁。

阻塞说明：
    工作流 ``graph.invoke`` 是同步阻塞调用（内部还有 SQLite 查询与可选的模型调用），
    因此所有触图接口都写成同步 ``def``，由 FastAPI 丢进线程池执行，不阻塞事件循环；
    线程安全由 ``AppRuntime.lock`` 串行化，DB 连接以 ``check_same_thread=False`` 打开。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from ops_assistant.api.schemas import (
    ChatRequest,
    CreateThreadRequest,
    HealthResponse,
    PreferencesResponse,
    PreferencesUpdate,
    ReportResponse,
    ResumeRequest,
    ThreadCreated,
    ThreadListResponse,
    ThreadSummary,
    TurnResponse,
    UsageResponse,
)
from ops_assistant.config import load_settings
from ops_assistant.observability import get_logger, log_structured, new_request_id
from ops_assistant.workflow.persistence import AppRuntime, ThreadNotFoundError
from ops_assistant.workflow.state import (
    DEFAULT_USER_ID,
    PREFERENCE_KEYS,
    effective_preferences,
    jsonable,
)

logger = get_logger("api")

VERSION = "0.1.0"

# 偏好允许的取值（与 workflow.state 的默认值保持一致）
AMOUNT_UNITS = ("元", "万元")
DIMENSIONS = ("region",)


def _now_iso() -> str:
    """返回 UTC 的 ISO8601 时间字符串。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _request_id(request: Request | None) -> str:
    """取本次请求的追踪 id（中间件写入；缺失时返回空串）。"""
    if request is None:
        return ""
    return str(getattr(request.state, "request_id", "") or "")


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    request: Request | None = None,
    detail: Any = None,
) -> JSONResponse:
    """构造统一错误响应：``{"detail": {"code", "message", "request_id", "detail"}}``。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "detail": jsonable(detail),
            }
        },
    )


def _sse(event: dict[str, Any], request_id: str = "") -> str:
    """把一个事件字典格式化成 SSE 帧：``event: <name>\\ndata: <json>\\n\\n``。

    ``json.dumps`` 会把字符串内的换行转义成 ``\\n``，所以 data 一定是单行，
    不需要按 SSE 规范把它拆成多个 ``data:`` 行。``ensure_ascii=False`` 让中文
    原样传输（帧以 UTF-8 编码）。
    """
    body = dict(event)
    if request_id and not body.get("request_id"):
        body["request_id"] = request_id
    name = str(body.get("event") or "message")
    return f"event: {name}\ndata: {json.dumps(jsonable(body), ensure_ascii=False)}\n\n"


def get_runtime(request: Request) -> AppRuntime:
    """从 app.state 取运行时；未就绪时返回 503。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "RUNTIME_NOT_READY", "message": "运行时尚未初始化完成，请稍后重试。"},
        )
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动装配运行时，关闭时释放 SQLite/偏好连接。"""
    settings = load_settings()
    runtime = AppRuntime.create(settings)
    app.state.runtime = runtime
    app.state.settings = settings
    logger.info("ops-assistant API 启动完成：%s", runtime.health().get("status"))
    try:
        yield
    finally:
        runtime.close()
        app.state.runtime = None
        logger.info("ops-assistant API 已关闭，连接已释放")


app = FastAPI(
    title="经营数据分析助手 API",
    version=VERSION,
    description=(
        "多轮经营数据分析助手：自然语言 → 结构化口径 → SQL 取数 → 程序计算 → "
        "Markdown 报告 → 人工审批 → 导出文件。\n\n"
        "注意：必须单 worker 运行；工作流调用为同步阻塞实现。"
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 中间件与异常处理
# ---------------------------------------------------------------------------


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """为每个请求生成/透传 ``X-Request-ID``，并记录起始时间。"""
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    request.state.request_id = request_id
    request.state.started_at = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """把 Pydantic 校验错误也包装成统一错误结构（422）。"""
    return _error(
        422,
        "VALIDATION_ERROR",
        "请求参数校验失败，请检查字段类型与取值范围。",
        request=request,
        detail=exc.errors(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """统一 HTTPException 的响应结构（业务代码用 ``detail={"code","message"}`` 抛错）。"""
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("code"):
        return _error(
            exc.status_code,
            str(detail.get("code")),
            str(detail.get("message") or ""),
            request=request,
            detail=detail.get("detail"),
        )
    return _error(exc.status_code, f"HTTP_{exc.status_code}", str(detail), request=request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理器：不向前端泄露堆栈，只留结构化日志。"""
    log_structured(
        event="api.unhandled_error",
        request_id=_request_id(request),
        ok=False,
        error_code=type(exc).__name__,
        detail={"path": request.url.path},
    )
    return _error(
        500,
        "INTERNAL_ERROR",
        "服务内部错误，请查看服务端日志后重试。",
        request=request,
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    """根路径：给出文档与健康检查地址。"""
    return {"service": "ops-assistant", "version": VERSION, "docs": "/docs", "health": "/api/health"}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@app.post(
    "/api/threads",
    response_model=ThreadCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["threads"],
    summary="新建会话",
)
def create_thread(payload: CreateThreadRequest, request: Request) -> ThreadCreated:
    """分配一个新的 thread_id。

    说明：本接口不写检查点；第一轮 ``/api/chat`` 才会真正产生会话状态。
    因此刚创建的 thread 在 ``GET /api/threads/{id}`` 上会返回 404，直到第一轮跑完。
    """
    get_runtime(request)  # 只要求运行时已就绪
    return ThreadCreated(
        thread_id=new_request_id(),
        user_id=payload.user_id,
        created_at=_now_iso(),
    )


@app.get(
    "/api/threads",
    response_model=ThreadListResponse,
    tags=["threads"],
    summary="会话列表",
)
def list_threads(request: Request, user_id: str | None = None, limit: int = 50) -> ThreadListResponse:
    """按最近更新时间倒序列出会话（检查点来源，进程重启后仍在）。"""
    runtime = get_runtime(request)
    items = runtime.list_threads(user_id=user_id or None, limit=limit)
    return ThreadListResponse(items=[ThreadSummary(**item) for item in items], count=len(items))


@app.get(
    "/api/threads/{thread_id}",
    response_model=TurnResponse,
    tags=["threads"],
    summary="读取会话状态",
)
def read_thread(
    thread_id: str,
    request: Request,
    user_id: str = DEFAULT_USER_ID,
) -> TurnResponse:
    """从检查点读取会话当前状态（UI 刷新时用；不会推进工作流）。"""
    runtime = get_runtime(request)
    try:
        turn = runtime.read_thread(thread_id, user_id=user_id, request_id=_request_id(request))
    except ThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "THREAD_NOT_FOUND", "message": str(exc)},
        ) from exc
    return TurnResponse(**turn)


# ---------------------------------------------------------------------------
# 问答与审批
# ---------------------------------------------------------------------------


@app.post(
    "/api/chat",
    response_model=TurnResponse,
    tags=["chat"],
    summary="发起一轮问答",
)
def chat(payload: ChatRequest, request: Request) -> TurnResponse:
    """跑一轮工作流，直到"需要审批"或终态。

    返回里的 ``status`` 表示工作流层面的结果：
    ``awaiting_approval``（带 ``interrupt`` 载荷）/ ``approved`` / ``rejected`` /
    ``clarification`` / ``failed`` / ``budget_exceeded``。传输层错误才用 HTTP 4xx/5xx。
    """
    runtime = get_runtime(request)
    try:
        turn = runtime.invoke_turn(
            payload.question,
            thread_id=payload.thread_id,
            user_id=payload.user_id,
            request_id=_request_id(request),
        )
    except ThreadNotFoundError as exc:
        raise HTTPException(404, detail={"code": "THREAD_NOT_FOUND", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "PENDING_APPROVAL", "message": str(exc)}) from exc
    log_structured(
        event="api.chat",
        request_id=_request_id(request),
        thread_id=turn.get("thread_id"),
        ok=turn.get("status") not in {"failed", "budget_exceeded"},
        detail={"status": turn.get("status")},
    )
    return TurnResponse(**turn)


@app.get(
    "/api/chat/stream",
    tags=["chat"],
    summary="流式发起一轮问答（SSE）",
)
async def chat_stream(
    request: Request,
    question: str = Query(min_length=1, max_length=2000, description="自然语言问题"),
    thread_id: str | None = Query(default=None, max_length=128, description="留空则新建会话"),
    user_id: str = Query(default=DEFAULT_USER_ID, min_length=1, max_length=64, description="用户标识"),
) -> StreamingResponse:
    """以 SSE 逐节点推送一轮问答；终止事件是 ``final`` 或 ``error``，二者互斥。

    事件契约与 :meth:`AppRuntime.stream_turn` 一致::

        event: start      data: {"event","thread_id","request_id","question"}
        event: node       data: {"event","node","update"}
        event: interrupt  data: {"event","id","payload"}
        event: final      data: {"event","turn"}   # turn 与 POST /api/chat 响应体同构
        event: error      data: {"event","code","message","request_id"}

    ``final.turn`` 与 ``POST /api/chat`` 的 ``TurnResponse`` 同构，既有客户端逻辑可直接
    复用；``node`` / ``interrupt`` 只是过程信息，丢弃不影响终态正确性。

    为什么是 GET + 查询参数：浏览器原生 ``EventSource`` 只支持 GET、不能带请求体。
    非浏览器客户端用任意 HTTP 库读 ``text/event-stream`` 即可。

    **审批语义没有被流式改变**：收到 ``interrupt`` 之后仍必须调用
    ``POST /api/threads/{thread_id}/resume`` 才会导出报告；流式只改变"进度怎么送达"，
    不改变"谁能落盘"。

    并发与线程说明：``stream_turn`` 在整个流期间持有运行时锁，而 ``threading.RLock``
    按线程归属，所以这里用**单独一个工作线程**（``asyncio.to_thread``）迭代生成器，再经
    ``asyncio.Queue`` 把帧交回事件循环。不能把同步生成器直接交给 ``StreamingResponse``：
    Starlette 会在线程池里逐个 ``next()``，相邻两次可能落在不同线程，释放锁时抛
    ``RuntimeError: cannot release un-acquired lock``。

    客户端断开**不会**中止工作流：本轮会跑完并写入检查点（避免留下半截状态），
    只是后续帧不再发送。
    """
    runtime = get_runtime(request)
    request_id = _request_id(request)
    # 开流之前拦截：SSE 一旦发出 200 与响应头就无法再改状态码，
    # 所以待审批必须在这里变成标准 409（与 POST /api/chat 一致）。
    try:
        pending = runtime.pending_approval(thread_id, user_id=user_id)
    except ThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "THREAD_NOT_FOUND", "message": str(exc)},
        ) from exc
    if pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "PENDING_APPROVAL", "message": "请先批准、编辑或拒绝当前报告，再开始新一轮。"},
        )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()

    def pump() -> None:
        """在单一线程里迭代生成器：锁的获取与释放都发生在这个线程。"""
        try:
            for event in runtime.stream_turn(
                question, thread_id=thread_id, user_id=user_id, request_id=request_id
            ):
                loop.call_soon_threadsafe(queue.put_nowait, _sse(event, request_id))
        except ThreadNotFoundError as exc:
            loop.call_soon_threadsafe(
                queue.put_nowait, _sse({"event": "error", "code": "THREAD_NOT_FOUND", "message": str(exc)}, request_id)
            )
        except ValueError as exc:
            # 预检与开流之间的竞态：权威拦截在 stream_turn 内部（锁内完成）。
            loop.call_soon_threadsafe(
                queue.put_nowait, _sse({"event": "error", "code": "PENDING_APPROVAL", "message": str(exc)}, request_id)
            )
        except Exception as exc:
            logger.warning("SSE 流式问答失败：%s: %s", type(exc).__name__, exc)
            loop.call_soon_threadsafe(
                queue.put_nowait, _sse({"event": "error", "code": type(exc).__name__, "message": str(exc)}, request_id)
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    async def body() -> AsyncIterator[str]:
        worker = asyncio.ensure_future(asyncio.to_thread(pump))
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                yield item
        finally:
            # 等工作线程收尾，保证锁已释放；即使这里被取消，pump 线程仍会自行跑完并放锁。
            await worker

    log_structured(event="api.chat_stream", request_id=request_id, thread_id=thread_id, ok=True)
    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 反向代理默认会缓冲响应，SSE 必须关掉才能逐帧送达
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/threads/{thread_id}/resume",
    response_model=TurnResponse,
    tags=["chat"],
    summary="提交人工审批",
)
def resume_thread(thread_id: str, payload: ResumeRequest, request: Request) -> TurnResponse:
    """把审批结论送回 interrupt，继续执行导出或结束。"""
    runtime = get_runtime(request)
    try:
        current = runtime.read_thread(thread_id, user_id=payload.user_id, request_id=_request_id(request))
    except ThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "THREAD_NOT_FOUND", "message": str(exc)},
        ) from exc
    already_exported = current.get("status") == "approved" and bool(current.get("export_path"))
    retry_export = current.get("status") == "failed" and any(
        e.startswith("EXPORT_ERROR:") for e in current.get("errors", [])
    )
    if current.get("interrupt") is None and not already_exported and not retry_export:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "NO_PENDING_INTERRUPT",
                "message": "该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。",
            },
        )
    if not payload.interrupt_id or not payload.report_id:
        raise HTTPException(409, detail={"code": "STALE_APPROVAL", "message": "请提交当前 interrupt_id 与 report_id。"})
    try:
        turn = runtime.resume_turn(
            thread_id,
            payload.decision,
            comment=payload.comment,
            edited_report=payload.edited_report or "",
            user_id=payload.user_id,
            request_id=_request_id(request),
            interrupt_id=payload.interrupt_id,
            report_id=payload.report_id,
            content_hash=payload.content_hash,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409 if str(exc).startswith("STALE_APPROVAL:") else 422,
            detail={
                "code": "STALE_APPROVAL" if str(exc).startswith("STALE_APPROVAL:") else "INVALID_RESUME",
                "message": str(exc),
            },
        ) from exc
    log_structured(
        event="api.resume",
        request_id=_request_id(request),
        thread_id=thread_id,
        ok=turn.get("status") not in {"failed", "budget_exceeded"},
        detail={"decision": payload.decision, "status": turn.get("status")},
    )
    return TurnResponse(**turn)


@app.get("/api/threads/{thread_id}/reports", tags=["chat"])
def list_report_versions(thread_id: str, request: Request, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    runtime = get_runtime(request)
    try:
        runtime.read_thread(thread_id, user_id=user_id)
        return {"items": runtime.list_reports(thread_id, user_id)}
    except ThreadNotFoundError as exc:
        raise HTTPException(404, detail={"code": "THREAD_NOT_FOUND", "message": str(exc)}) from exc


@app.get(
    "/api/threads/{thread_id}/report",
    response_model=ReportResponse,
    tags=["chat"],
    summary="获取已导出的报告",
)
def get_report(
    thread_id: str,
    request: Request,
    user_id: str = DEFAULT_USER_ID,
    report_id: str | None = None,
) -> ReportResponse:
    """读取已导出的 Markdown 报告内容（供 UI 预览与下载）。"""
    runtime = get_runtime(request)
    try:
        turn = runtime.read_thread(thread_id, user_id=user_id, request_id=_request_id(request))
    except ThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "THREAD_NOT_FOUND", "message": str(exc)},
        ) from exc
    try:
        export_path = (
            runtime.report_path(thread_id, report_id, user_id) if report_id else str(turn.get("export_path") or "")
        )
    except ThreadNotFoundError as exc:
        raise HTTPException(404, detail={"code": "REPORT_NOT_FOUND", "message": str(exc)}) from exc
    if not export_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "REPORT_NOT_FOUND",
                "message": "该会话还没有导出报告：请先审批通过（approve/edit）后再下载。",
            },
        )
    path = Path(export_path)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "REPORT_FILE_MISSING",
                "message": f"报告记录存在但文件已丢失：{export_path}",
            },
        )
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "REPORT_READ_ERROR",
                "message": f"读取报告文件失败（{type(exc).__name__}）。",
            },
        ) from exc
    return ReportResponse(thread_id=thread_id, filename=path.name, path=str(path), markdown=markdown)


# ---------------------------------------------------------------------------
# 偏好 / 健康 / 用量
# ---------------------------------------------------------------------------


@app.get(
    "/api/preferences",
    response_model=PreferencesResponse,
    tags=["preferences"],
    summary="读取用户偏好",
)
def get_preferences(request: Request, user_id: str = DEFAULT_USER_ID) -> PreferencesResponse:
    """返回与默认值合并后的偏好（含 ``amount_unit`` 与 ``default_dimension``）。"""
    runtime = get_runtime(request)
    values = runtime.prefs.all_for_user(user_id)
    return PreferencesResponse(
        user_id=user_id,
        prefs=effective_preferences(values),
        supported_keys=list(PREFERENCE_KEYS),
    )


@app.put(
    "/api/preferences",
    response_model=PreferencesResponse,
    tags=["preferences"],
    summary="更新用户偏好",
)
def put_preferences(payload: PreferencesUpdate, request: Request) -> PreferencesResponse:
    """写入偏好；``value=None`` 表示删除该键并回退默认值。"""
    runtime = get_runtime(request)
    unknown = [key for key in payload.prefs if key not in PREFERENCE_KEYS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "UNKNOWN_PREFERENCE_KEY",
                "message": f"不支持的偏好键：{', '.join(unknown)}；可用：{', '.join(PREFERENCE_KEYS)}。",
            },
        )
    for key, value in payload.prefs.items():
        if value is None:
            continue
        if key == "amount_unit" and str(value) not in AMOUNT_UNITS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_PREFERENCE_VALUE",
                    "message": f"amount_unit 只能是 {' / '.join(AMOUNT_UNITS)}，收到：{value}。",
                },
            )
        if key == "default_dimension" and str(value) not in DIMENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_PREFERENCE_VALUE",
                    "message": f"default_dimension 只能是 {' / '.join(DIMENSIONS)}，收到：{value}。",
                },
            )
    runtime.prefs.set_many(payload.user_id, payload.prefs)
    return PreferencesResponse(
        user_id=payload.user_id,
        prefs=effective_preferences(runtime.prefs.all_for_user(payload.user_id)),
        supported_keys=list(PREFERENCE_KEYS),
    )


@app.get("/api/health", response_model=HealthResponse, tags=["ops"], summary="健康检查")
def health(request: Request) -> HealthResponse:
    """返回运行时与数据/知识库/模型的状态。"""
    runtime = get_runtime(request)
    return HealthResponse(**runtime.health())


@app.get("/api/usage", response_model=UsageResponse, tags=["ops"], summary="用量统计")
def usage(request: Request) -> UsageResponse:
    """返回进程级累计用量（模型调用、工具调用、token 与耗时）。"""
    runtime = get_runtime(request)
    return UsageResponse(**runtime.usage_stats())
