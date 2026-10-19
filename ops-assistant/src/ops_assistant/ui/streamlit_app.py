"""Streamlit 前端：会话列表、对话、执行轨迹/来源展开、表格图表、审批与下载。

设计约束（规格 5.11）：
    1. UI **只**通过 HTTP 调后端 API，不直接访问图/数据库，保证"刷新即从 API 拉状态"；
    2. ``st.session_state`` 里只保留 ``thread_id`` / ``user_id`` 这类会话标识，
       业务状态每次都从 API 重新读取，避免前端缓存与检查点不一致；
    3. 审批三按钮（approve / reject / edit）调用 ``/api/threads/{id}/resume``；
    4. 没有待审批 interrupt 时，审批区不渲染，避免误发请求。

启动::

    uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
    streamlit run src/ops_assistant/ui/streamlit_app.py

环境变量：
    OPS_API_BASE_URL: 后端地址，默认 ``http://127.0.0.1:8000``
    OPS_USER_ID: 默认用户 id，默认 ``demo-user``
"""

import json
import os
from typing import Any
from urllib.parse import quote

import httpx
import streamlit as st

API_BASE_URL = os.environ.get("OPS_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_USER_ID = os.environ.get("OPS_USER_ID", "demo-user")
REQUEST_TIMEOUT = 180.0

STATUS_LABELS = {
    "running": "执行中",
    "awaiting_approval": "等待审批",
    "approved": "已批准（已导出）",
    "rejected": "已拒绝（未导出）",
    "clarification": "需要补充信息",
    "failed": "失败",
    "budget_exceeded": "超出预算（已中止）",
    "empty": "新会话",
    "unknown": "未知状态",
}

DIMENSION_LABELS = {
    "region": "地区",
    "product": "商品",
    "month": "月份",
}
DIMENSION_BY_LABEL = {label: key for key, label in DIMENSION_LABELS.items()}

AMOUNT_UNITS = ("元", "万元")


class ApiError(RuntimeError):
    """后端接口调用失败（网络错误或业务错误）。"""

    def __init__(self, message: str, *, code: str = "", status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发起一次 API 调用，并把统一错误结构转换成 ApiError。"""
    url = f"{API_BASE_URL}{path}"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.request(method, url, json=json_body, params=params)
    except httpx.ConnectError as exc:
        raise ApiError(
            f"无法连接后端 API（{API_BASE_URL}）。请先启动："
            "uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000"
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiError(f"请求后端失败：{type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        code = ""
        message = f"HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                code = str(detail.get("code") or "")
                message = str(detail.get("message") or message)
            elif isinstance(detail, str):
                message = detail
        raise ApiError(message, code=code, status_code=response.status_code)

    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError("后端返回的不是合法 JSON。") from exc
    if not isinstance(data, dict):
        raise ApiError("后端返回结构异常（期望 JSON 对象）。")
    return data


def api_health() -> dict[str, Any]:
    """健康检查。"""
    return _request("GET", "/api/health")


def api_list_threads(user_id: str, *, limit: int = 30) -> dict[str, Any]:
    """会话列表。"""
    return _request("GET", "/api/threads", params={"user_id": user_id, "limit": limit})


def api_read_thread(thread_id: str, user_id: str) -> dict[str, Any]:
    """读取会话状态。"""
    return _request(
        "GET",
        f"/api/threads/{quote(thread_id, safe='')}",
        params={"user_id": user_id},
    )


def api_chat(question: str, *, thread_id: str | None, user_id: str) -> dict[str, Any]:
    """发起一轮问答。"""
    return _request(
        "POST",
        "/api/chat",
        json_body={"question": question, "thread_id": thread_id, "user_id": user_id},
    )


def api_resume(
    thread_id: str,
    decision: str,
    *,
    comment: str,
    edited_report: str,
    user_id: str,
) -> dict[str, Any]:
    """提交审批结论。"""
    return _request(
        "POST",
        f"/api/threads/{quote(thread_id, safe='')}/resume",
        json_body={
            "decision": decision,
            "comment": comment,
            "edited_report": edited_report or None,
            "user_id": user_id,
        },
    )


def api_preferences(user_id: str) -> dict[str, Any]:
    """读取偏好。"""
    return _request("GET", "/api/preferences", params={"user_id": user_id})


def api_put_preferences(user_id: str, prefs: dict[str, Any]) -> dict[str, Any]:
    """更新偏好。"""
    return _request("PUT", "/api/preferences", json_body={"user_id": user_id, "prefs": prefs})


def api_report(thread_id: str, user_id: str) -> dict[str, Any]:
    """获取已导出报告。"""
    return _request(
        "GET",
        f"/api/threads/{quote(thread_id, safe='')}/report",
        params={"user_id": user_id},
    )


# ---------------------------------------------------------------------------
# 页面装配
# ---------------------------------------------------------------------------


def _rerun() -> None:
    """兼容新旧 Streamlit 的刷新调用。"""
    rerun = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
    if callable(rerun):
        rerun()


def _init_state() -> None:
    """初始化会话级状态（只放标识，不放业务数据）。"""
    st.session_state.setdefault("user_id", DEFAULT_USER_ID)
    st.session_state.setdefault("thread_id", None)
    st.session_state.setdefault("turn", None)
    st.session_state.setdefault("flash", "")


def _status_label(status: str) -> str:
    """把状态码翻译成中文标签。"""
    return STATUS_LABELS.get(str(status or ""), str(status or "未知状态"))


def _load_health() -> dict[str, Any] | None:
    """读取后端健康状态；失败时在侧边栏提示启动方式。"""
    try:
        return api_health()
    except ApiError as exc:
        st.sidebar.error(f"后端不可用：{exc}")
        with st.sidebar.expander("如何启动后端", expanded=True):
            st.code(
                "cd ops-assistant\n"
                "uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000",
                language="bash",
            )
        return None


def _render_sidebar(health: dict[str, Any] | None) -> None:
    """渲染侧边栏：状态、用户、偏好、会话列表。"""
    sidebar = st.sidebar
    sidebar.title("经营数据分析助手")
    sidebar.caption(f"后端：{API_BASE_URL}")

    if health is not None:
        badge = "正常" if health.get("status") == "ok" else "降级"
        sidebar.markdown(f"**后端状态：** {badge}")
        sidebar.caption(
            f"模型：{'已启用' if health.get('model_enabled') else '未启用（纯规则模式）'} · "
            f"知识片段：{health.get('kb_chunks', 0)} · "
            f"数据库：{'可用' if health.get('db_ok') else '不可用'}"
        )
        sidebar.caption(
            f"步数上限：{health.get('max_steps', 12)} · "
            f"工具调用上限：{health.get('max_tool_calls', 8)} · "
            f"检查点：{health.get('checkpointer', '')}"
        )

    sidebar.markdown("---")
    sidebar.text_input("用户 ID", key="user_id")

    _render_preferences(sidebar)

    sidebar.markdown("---")
    sidebar.subheader("会话")
    if sidebar.button("＋ 新建会话", key="new_thread_button"):
        st.session_state.thread_id = None
        st.session_state.turn = None
        _rerun()

    _render_thread_list(sidebar)


def _render_preferences(sidebar: Any) -> None:
    """偏好设置：金额单位与默认维度。"""
    user_id = str(st.session_state.get("user_id") or DEFAULT_USER_ID)
    try:
        payload = api_preferences(user_id)
    except ApiError as exc:
        sidebar.warning(f"读取偏好失败：{exc}")
        return
    prefs = payload.get("prefs") if isinstance(payload.get("prefs"), dict) else {}
    unit_index = AMOUNT_UNITS.index(prefs.get("amount_unit")) if prefs.get("amount_unit") in AMOUNT_UNITS else 0
    dimension = prefs.get("default_dimension") if prefs.get("default_dimension") in DIMENSION_LABELS else "region"
    dimension_index = list(DIMENSION_LABELS).index(dimension)

    with sidebar.form("preferences_form"):
        st.caption("偏好会注入工作流（金额单位、默认维度）")
        unit = st.selectbox("金额单位", AMOUNT_UNITS, index=unit_index, key="pref_unit")
        dimension_label = st.selectbox(
            "默认维度",
            list(DIMENSION_LABELS.values()),
            index=dimension_index,
            key="pref_dimension",
        )
        submitted = st.form_submit_button("保存偏好")

    if not submitted:
        return
    try:
        api_put_preferences(
            user_id,
            {
                "amount_unit": unit,
                "default_dimension": DIMENSION_BY_LABEL.get(dimension_label, "region"),
            },
        )
    except ApiError as exc:
        sidebar.error(f"保存偏好失败：{exc}")
        return
    st.session_state.flash = "偏好已保存。"
    _rerun()


def _render_thread_list(sidebar: Any) -> None:
    """会话列表：选择后从 API 重新拉取该会话状态。"""
    user_id = str(st.session_state.get("user_id") or DEFAULT_USER_ID)
    try:
        payload = api_list_threads(user_id)
        items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
    except ApiError as exc:
        sidebar.warning(f"读取会话列表失败：{exc}")
        return

    options: list[str] = []
    labels: dict[str, str] = {}
    for item in items:
        thread_id = str(item.get("thread_id") or "")
        if not thread_id:
            continue
        options.append(thread_id)
        question = str(item.get("question") or "（尚无问题）")
        if len(question) > 18:
            question = question[:18] + "…"
        labels[thread_id] = f"{question} · {_status_label(item.get('status', ''))}"

    current = st.session_state.get("thread_id")
    if current and current not in options:
        options.insert(0, current)
        labels[current] = "当前会话（尚未出现在列表中）"
    if not options:
        sidebar.caption("还没有历史会话，直接提问即可创建。")
        return

    index = options.index(current) if current in options else 0
    selected = sidebar.selectbox("选择会话", options, index=index, format_func=lambda value: labels.get(value, value))
    if not selected or selected == st.session_state.get("thread_id"):
        return
    try:
        turn = api_read_thread(selected, user_id)
    except ApiError as exc:
        sidebar.warning(f"读取会话失败：{exc}")
        return
    st.session_state.thread_id = selected
    st.session_state.turn = turn


# ---------------------------------------------------------------------------
# 主区域
# ---------------------------------------------------------------------------


def _render_turn(turn: dict[str, Any]) -> None:
    """渲染一轮结果的完整视图。"""
    thread_id = str(turn.get("thread_id") or "")
    status = str(turn.get("status") or "")
    st.markdown(f"**会话：** `{thread_id}` · **状态：** {_status_label(status)}")

    messages = [item for item in (turn.get("messages") or []) if isinstance(item, dict)]
    for message in messages:
        role = str(message.get("role") or "assistant")
        if role not in {"user", "assistant"}:
            role = "assistant"
        content = str(message.get("content") or "")
        if not content.strip():
            continue
        with st.chat_message(role):
            st.markdown(content)

    if turn.get("clarification"):
        st.warning(str(turn.get("clarification")))
    if turn.get("errors"):
        st.error("；".join(str(item) for item in turn.get("errors") or []))

    report = str(turn.get("report") or "")
    if report:
        with st.expander("报告 Markdown", expanded=(status == "awaiting_approval")):
            st.markdown(report)

    _render_tables(turn)
    _render_chart(turn)
    _render_trace(turn)
    _render_sources(turn)
    _render_approval(turn)
    _render_download(turn)


def _render_tables(turn: dict[str, Any]) -> None:
    """把 tables（columns + rows）逐张渲染成表格。"""
    tables = [item for item in (turn.get("tables") or []) if isinstance(item, dict)]
    if not tables:
        return
    for position, table in enumerate(tables, start=1):
        title = str(table.get("title") or f"明细表 {position}")
        columns = [str(column) for column in (table.get("columns") or [])]
        rows = [row for row in (table.get("rows") or []) if isinstance(row, (list, tuple))]
        if not columns or not rows:
            continue
        data: dict[str, list[Any]] = {}
        for index, column in enumerate(columns):
            data[column] = [
                row[index] if index < len(row) else ""
                for row in rows
            ]
        with st.expander(title, expanded=False):
            st.dataframe(data)


def _render_chart(turn: dict[str, Any]) -> None:
    """渲染地区差额柱状图（数据来自 compute 节点，单位由计算层决定）。"""
    chart = turn.get("chart")
    if not isinstance(chart, dict):
        return
    labels = [str(item) for item in (chart.get("labels") or [])]
    values = list(chart.get("values") or [])
    if not labels or len(labels) != len(values):
        return
    x_label = str(chart.get("x") or "类别")
    y_label = str(chart.get("y") or "数值")
    st.subheader("地区差额（图表）")
    try:
        st.bar_chart({x_label: labels, y_label: values}, x=x_label, y=y_label)
    except Exception:  # 老版本 Streamlit 对 dict 输入的 x/y 支持不一致，退化为表格
        st.dataframe({x_label: labels, y_label: values})


def _render_trace(turn: dict[str, Any]) -> None:
    """执行轨迹：节点 / 动作 / 成功与否 / 耗时 / 工具。"""
    trace = [item for item in (turn.get("trace") or []) if isinstance(item, dict)]
    if not trace:
        return
    with st.expander(f"执行轨迹（{len(trace)} 条）", expanded=False):
        rows = [
            {
                "节点": entry.get("node") or "",
                "动作": entry.get("action") or "",
                "成功": "是" if entry.get("ok") else "否",
                "耗时(ms)": entry.get("duration_ms") or 0,
                "工具": entry.get("tool") or "",
                "可重试": "是" if entry.get("retryable") else "",
            }
            for entry in trace
        ]
        st.dataframe(rows)


def _render_sources(turn: dict[str, Any]) -> None:
    """来源：SQL（含参数）与知识库片段（doc_id#chunk_id）。"""
    sources = [item for item in (turn.get("sources") or []) if isinstance(item, dict)]
    if not sources:
        return
    sql_sources = [item for item in sources if item.get("type") == "sql"]
    doc_sources = [item for item in sources if item.get("type") == "doc"]
    with st.expander(
        f"来源与依据（SQL {len(sql_sources)} 条 / 知识片段 {len(doc_sources)} 条）",
        expanded=False,
    ):
        for position, source in enumerate(sql_sources, start=1):
            st.caption(str(source.get("note") or f"SQL {position}"))
            st.code(str(source.get("sql") or ""), language="sql")
            params = source.get("params")
            if params:
                st.caption("参数：" + json.dumps(params, ensure_ascii=False))
        for source in doc_sources:
            ref = str(source.get("ref") or "")
            if not ref:
                doc_id = str(source.get("doc_id") or "")
                chunk_id = str(source.get("chunk_id") or "")
                ref = f"[{doc_id}#{chunk_id}]"
            st.markdown(
                f"- `{ref}`《{source.get('title') or ''}》：{source.get('snippet') or ''}"
            )


def _render_approval(turn: dict[str, Any]) -> None:
    """审批区：只有状态为 awaiting_approval 且有 interrupt 时出现。"""
    if str(turn.get("status") or "") != "awaiting_approval":
        return
    interrupt = turn.get("interrupt")
    payload = interrupt.get("payload") if isinstance(interrupt, dict) else None
    if not isinstance(payload, dict) or not payload:
        st.info("工作流停在审批节点，但未取到审批载荷；请刷新会话重试。")
        return
    thread_id = str(turn.get("thread_id") or "")
    st.subheader("人工审批")
    st.markdown(f"**摘要：** {payload.get('summary') or '（无摘要）'}")
    st.caption(str(payload.get("instructions") or ""))

    comment = st.text_input("审批备注（可选）", key=f"comment_{thread_id}")
    edited = st.text_area(
        "修改稿（选择 edit 时使用；只能改措辞，不能改数字）",
        value=str(payload.get("report_markdown") or ""),
        height=260,
        key=f"edited_{thread_id}",
    )
    columns = st.columns(3)
    with columns[0]:
        approve = st.button("✅ 通过并导出", key=f"approve_{thread_id}")
    with columns[1]:
        reject = st.button("⛔ 拒绝（不导出）", key=f"reject_{thread_id}")
    with columns[2]:
        edit = st.button("✏️ 按修改稿导出", key=f"edit_{thread_id}")

    if approve:
        _handle_resume(turn, "approve", comment=comment, edited_report="")
    elif reject:
        _handle_resume(turn, "reject", comment=comment, edited_report="")
    elif edit:
        _handle_resume(turn, "edit", comment=comment, edited_report=edited)


def _render_download(turn: dict[str, Any]) -> None:
    """已导出报告时提供下载。"""
    if not turn.get("export_path"):
        return
    thread_id = str(turn.get("thread_id") or "")
    user_id = str(st.session_state.get("user_id") or DEFAULT_USER_ID)
    try:
        report = api_report(thread_id, user_id)
    except ApiError as exc:
        st.warning(f"读取报告失败：{exc}")
        return
    markdown = str(report.get("markdown") or "")
    filename = str(report.get("filename") or f"report-{thread_id}.md")
    st.download_button(
        "⬇️ 下载 Markdown 报告",
        data=markdown.encode("utf-8"),
        file_name=filename,
        mime="text/markdown",
        key=f"download_{thread_id}",
    )
    st.caption(f"已导出：{report.get('path') or turn.get('export_path')}")


def _handle_chat(question: str) -> None:
    """发起一轮问答并刷新页面。"""
    user_id = str(st.session_state.get("user_id") or DEFAULT_USER_ID)
    try:
        turn = api_chat(question, thread_id=st.session_state.get("thread_id"), user_id=user_id)
    except ApiError as exc:
        st.error(f"问答失败：{exc}")
        return
    st.session_state.turn = turn
    st.session_state.thread_id = turn.get("thread_id")
    _rerun()


def _handle_resume(turn: dict[str, Any], decision: str, *, comment: str, edited_report: str) -> None:
    """提交审批并刷新页面。"""
    thread_id = str(turn.get("thread_id") or "")
    user_id = str(st.session_state.get("user_id") or DEFAULT_USER_ID)
    if decision == "edit" and not str(edited_report or "").strip():
        st.warning("选择 edit 时必须提供修改稿；未导出任何文件。")
        return
    try:
        result = api_resume(
            thread_id,
            decision,
            comment=str(comment or ""),
            edited_report=str(edited_report or "") if decision == "edit" else "",
            user_id=user_id,
        )
    except ApiError as exc:
        st.error(f"审批失败：{exc}")
        return
    st.session_state.turn = result
    st.session_state.flash = {
        "approve": "已通过审批。",
        "reject": "已拒绝，未导出文件。",
        "edit": "已按修改稿导出。",
    }.get(decision, "审批已提交。")
    _rerun()


def main() -> None:
    """页面入口。"""
    st.set_page_config(page_title="经营数据分析助手", page_icon="📊", layout="wide")
    _init_state()

    health = _load_health()
    _render_sidebar(health)

    st.title("经营数据分析助手")
    st.caption("自然语言提问 → 结构化口径 → 取数计算 → 生成报告 → 人工审批 → 导出 Markdown")

    if st.session_state.get("flash"):
        st.success(str(st.session_state.flash))
        st.session_state.flash = ""

    if health is None:
        st.error(
            "后端 API 不可用。请先启动："
            "`uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000`，再刷新本页面。"
        )
        return

    turn = st.session_state.get("turn")
    if isinstance(turn, dict) and turn:
        _render_turn(turn)
    else:
        st.info("在下方输入问题开始，例如：“2026 年九月销售额比八月变化多少？”")

    question = st.chat_input("输入你的问题…")
    if question:
        _handle_chat(question)


if __name__ == "__main__":
    main()
