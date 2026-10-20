"""API 数据模型（Pydantic v2）：请求体、响应体与统一错误结构。

约定：
- 所有响应都带 ``request_id``，方便和日志（``ops`` 结构化字段）对上；
- 错误统一为 ``{"detail": {"code", "message", "request_id", "detail"}}``，
  UI 只需要读 ``detail.code`` 与 ``detail.message``；
- 金额一律用"分"的整数表达，展示换算放在 UI/报告层完成，接口不返回浮点金额。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """统一错误体。"""

    code: str = Field(description="机器可读错误码，例如 VALIDATION_ERROR / THREAD_NOT_FOUND")
    message: str = Field(description="人能看懂的中文错误说明")
    request_id: str = Field(default="", description="请求追踪 id")
    detail: Any | None = Field(default=None, description="可选的补充细节（校验错误列表等）")


class ErrorResponse(BaseModel):
    """统一错误响应：``{"detail": ErrorBody}``。"""

    detail: ErrorBody


class CreateThreadRequest(BaseModel):
    """新建会话请求（只分配 id，真正的第一轮由 /api/chat 触发）。"""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(default="demo-user", min_length=1, max_length=64, description="用户标识")
    question: str | None = Field(default=None, max_length=2000, description="可选的首个问题")


class ThreadCreated(BaseModel):
    """新建会话的返回。"""

    thread_id: str
    user_id: str
    status: str = Field(default="empty", description="尚未开始对话，因此没有检查点")
    created_at: str
    message: str = Field(default="会话已创建，请调用 /api/chat 开始第一轮问答。")


class ThreadSummary(BaseModel):
    """会话列表里的一行。"""

    thread_id: str
    user_id: str = ""
    question: str = ""
    status: str = ""
    updated_at: str = ""


class ThreadListResponse(BaseModel):
    """会话列表响应。"""

    items: list[ThreadSummary] = Field(default_factory=list)
    count: int = 0


class ChatRequest(BaseModel):
    """发起一轮问答。"""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=2000, description="自然语言问题")
    thread_id: str | None = Field(default=None, description="留空则新建会话（多轮追问要带上它）")
    user_id: str = Field(default="demo-user", min_length=1, max_length=64, description="用户标识")


class ResumeRequest(BaseModel):
    """人工审批结论。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str | None = Field(default=None, max_length=128)
    report_id: str | None = Field(default=None, max_length=128)
    content_hash: str | None = Field(default=None, max_length=64)

    decision: Literal["approve", "reject", "edit"] = Field(
        description="approve 导出 / reject 不导出 / edit 按修改稿导出"
    )
    comment: str = Field(default="", max_length=2000, description="审批备注（可选）")
    edited_report: str | None = Field(
        default=None,
        description="decision=edit 时的修改稿；只能改措辞，不能改数字",
    )
    user_id: str = Field(default="demo-user", min_length=1, max_length=64, description="用户标识")


class TraceEntry(BaseModel):
    """一条执行轨迹（前端展开"执行轨迹"用）。"""

    node: str
    action: str
    ok: bool
    duration_ms: int | float = 0
    tool: str | None = None
    retryable: bool | None = None
    detail: dict[str, Any] | None = None


class InterruptPayload(BaseModel):
    """等待人工处理的 interrupt。"""

    id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class TurnUsage(BaseModel):
    """用量与耗时快照。

    ``steps`` / ``tool_calls`` 是**本轮**的图状态计数（来自 ``budget`` 通道，
    规格第 5.10 节"usage 合并图状态计数与 MetricsCollector"）；
    进程级累计值看 ``metrics``（跨请求累加）。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    elapsed_ms: float = 0.0
    steps: int = 0
    max_steps: int = 0
    tool_calls: int = 0
    max_tool_calls: int = 0


class TurnResponse(BaseModel):
    """一轮问答/审批的结果（也是读取会话状态的返回）。"""

    thread_id: str
    report_id: str = ""
    approval_interrupt_id: str = ""
    status: str
    answer: str = ""
    report: str = ""
    clarification: str = ""
    interrupt: InterruptPayload | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)
    chart: dict[str, Any] | None = None
    trace: list[TraceEntry] = Field(default_factory=list)
    usage: TurnUsage = Field(default_factory=TurnUsage)
    metrics: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    export_path: str | None = None
    request_id: str = ""
    messages: list[dict[str, str]] = Field(default_factory=list)
    spec: dict[str, Any] = Field(default_factory=dict)
    calculation: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


class ReportResponse(BaseModel):
    """已导出报告的下载内容。"""

    thread_id: str
    filename: str
    path: str
    markdown: str


class PreferencesResponse(BaseModel):
    """用户偏好（已与默认值合并）。"""

    user_id: str
    prefs: dict[str, Any] = Field(default_factory=dict)
    supported_keys: list[str] = Field(default_factory=list)


class PreferencesUpdate(BaseModel):
    """偏好更新请求；``value=None`` 表示删除该键并回退默认值。"""

    model_config = ConfigDict(extra="ignore")

    user_id: str = Field(default="demo-user", min_length=1, max_length=64)
    prefs: dict[str, Any] = Field(default_factory=dict, description="只允许 state.PREFERENCE_KEYS 里的键")


class HealthResponse(BaseModel):
    """健康检查。"""

    status: str
    model_enabled: bool
    kb_chunks: int = 0
    db_ok: bool = True
    max_steps: int = 12
    max_tool_calls: int = 8
    checkpointer: str = ""
    closed: bool = False


class UsageResponse(BaseModel):
    """用量统计（进程级累计 + 最近一次请求的指标快照）。"""

    usage: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
