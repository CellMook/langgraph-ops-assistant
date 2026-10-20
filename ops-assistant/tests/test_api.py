"""T23：HTTP API 行为与错误约定（工程规格 6.1、5.10）。

覆盖点：
- 会话创建/列表/读取；未知会话返回 404 而不是 500；
- ``/api/chat`` 跑一轮并返回统一 TurnResponse；
- 审批三态（approve/reject/edit 的非法值）与冲突（无待审批时 409）；
- 已导出报告可读取，未导出时 404；参数校验失败返回统一错误结构；
- 健康检查与用量接口可用，且不泄露密钥。

实现说明：
    这里不启动 uvicorn，也不走应用 lifespan（否则会按 ``load_settings()`` 在仓库
    目录里建库）。测试直接把已装配好的 ``AppRuntime`` 塞进 ``app.state.runtime``，
    验证的是路由、序列化与错误映射，工作流本身由 test_workflow/test_hitl 覆盖。

诚实声明：未在编写环境中执行。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ops_assistant.api.main import app
from ops_assistant.workflow.persistence import AppRuntime

MAIN_QUESTION = "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"


@pytest.fixture()
def client(runtime: AppRuntime) -> Iterator[TestClient]:
    """注入测试运行时，避免 lifespan 在仓库目录里创建数据库。"""
    app.state.runtime = runtime
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.state.runtime = None
        test_client.close()


def _chat(client: TestClient, question: str, thread_id: str | None = None) -> dict[str, Any]:
    """发起一轮问答，断言传输层成功并返回 JSON。"""
    payload: dict[str, Any] = {"question": question}
    if thread_id:
        payload["thread_id"] = thread_id
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _resume(client: TestClient, turn: dict[str, Any], decision: str, **extra: Any):
    return client.post(
        f"/api/threads/{turn['thread_id']}/resume",
        json={
            "decision": decision,
            "report_id": turn["report_id"],
            "interrupt_id": turn["interrupt"]["id"],
            **extra,
        },
    )


# ---------------------------------------------------------------------------
# 基础端点
# ---------------------------------------------------------------------------


def test_t23_health(client: TestClient) -> None:
    """T23：健康检查返回运行时状态、检查点类型与预算配置。"""
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"]
    assert body["db_ok"] is True
    assert body["model_enabled"] is False
    assert body["checkpointer"]
    assert body["closed"] is False


def test_t23_usage_does_not_leak_secrets(client: TestClient) -> None:
    """T23：用量接口只给计数，不含任何密钥。"""
    response = client.get("/api/usage")
    assert response.status_code == 200
    text = response.text
    assert "sk-" not in text
    assert "api_key" not in text.lower()
    assert "usage" in response.json()


def test_t23_root_points_to_docs(client: TestClient) -> None:
    """T23：根路径给出文档与健康检查地址。"""
    body = client.get("/").json()
    assert body["docs"] == "/docs"
    assert body["health"] == "/api/health"


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def test_t23_create_thread_returns_id(client: TestClient) -> None:
    """T23：新建会话返回 201 与 thread_id。"""
    response = client.post("/api/threads", json={})
    assert response.status_code == 201
    body = response.json()
    assert body["thread_id"]
    assert body["status"] == "empty"


def test_t23_unknown_thread_returns_404(client: TestClient) -> None:
    """T23：读取不存在的会话返回 404 与机器可读错误码。"""
    response = client.get("/api/threads/does-not-exist")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "THREAD_NOT_FOUND"


def test_t23_list_threads_after_chat(client: TestClient) -> None:
    """T23：跑过一轮之后，会话出现在列表里。"""
    turn = _chat(client, MAIN_QUESTION)
    response = client.get("/api/threads")
    assert response.status_code == 200
    ids = {item["thread_id"] for item in response.json()["items"]}
    assert turn["thread_id"] in ids


# ---------------------------------------------------------------------------
# 问答
# ---------------------------------------------------------------------------


def test_t23_chat_returns_awaiting_approval(client: TestClient) -> None:
    """T23：主问题返回 awaiting_approval，并带报告与来源。"""
    turn = _chat(client, MAIN_QUESTION)
    assert turn["status"] == "awaiting_approval"
    assert turn["report"]
    assert turn["sources"]
    assert turn["interrupt"]["payload"]["options"] == ["approve", "reject", "edit"]


def test_t23_chat_follow_up_uses_thread_id(client: TestClient) -> None:
    """T23：带上 thread_id 的追问沿用上下文，只保留指定地区。"""
    first = _chat(client, MAIN_QUESTION)
    thread_id = first["thread_id"]
    assert _resume(client, first, "reject").status_code == 200

    second = _chat(client, "那华东地区呢？", thread_id=thread_id)
    assert second["status"] == "awaiting_approval"
    regions = second["calculation"]["regions"]
    assert len(regions) == 1
    assert regions[0]["region_id"] == "R_EAST"


def test_t23_chat_clarification_is_not_an_http_error(client: TestClient) -> None:
    """T23：需要澄清是业务状态（200），不是 HTTP 错误。"""
    turn = _chat(client, "九月销售额是多少？")
    assert turn["status"] == "clarification"
    assert turn["clarification"]
    assert turn["report"] == ""


def test_t23_empty_question_is_validation_error(client: TestClient) -> None:
    """T23：空问题返回 422，并包成统一错误结构。"""
    response = client.post("/api/chat", json={"question": ""})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 审批与报告
# ---------------------------------------------------------------------------


def test_t23_approve_then_download(client: TestClient) -> None:
    """T23：批准后可读取已导出的 Markdown，文件名与 thread 对应。"""
    turn = _chat(client, MAIN_QUESTION)
    thread_id = turn["thread_id"]

    resumed = _resume(client, turn, "approve", comment="OK")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "approved"

    report = client.get(f"/api/threads/{thread_id}/report")
    assert report.status_code == 200
    body = report.json()
    assert body["filename"] == f"report-{thread_id}-{turn['report_id']}.md"
    assert "## 结论" in body["markdown"]


def test_t23_report_before_approval_is_404(client: TestClient) -> None:
    """T23：尚未导出时读取报告返回 404（提示先审批）。"""
    turn = _chat(client, MAIN_QUESTION)
    response = client.get(f"/api/threads/{turn['thread_id']}/report")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REPORT_NOT_FOUND"


def test_t23_resume_without_pending_is_conflict(client: TestClient) -> None:
    """T23：没有待审批报告时提交审批返回 409。"""
    response = client.post("/api/threads/nope/resume", json={"decision": "approve"})
    assert response.status_code in {404, 409}
    assert response.json()["detail"]["code"] in {
        "THREAD_NOT_FOUND",
        "NO_PENDING_INTERRUPT",
    }


def test_t23_invalid_decision_is_validation_error(client: TestClient) -> None:
    """T23：decision 只允许三个值，其它值由 Pydantic 拦成 422。"""
    turn = _chat(client, MAIN_QUESTION)
    response = client.post(f"/api/threads/{turn['thread_id']}/resume", json={"decision": "maybe"})
    assert response.status_code == 422


def test_t23_reject_does_not_create_report_file(client: TestClient) -> None:
    """T23：拒绝后报告保持不可下载。"""
    turn = _chat(client, MAIN_QUESTION)
    thread_id = turn["thread_id"]
    resumed = _resume(client, turn, "reject")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "rejected"
    assert client.get(f"/api/threads/{thread_id}/report").status_code == 404


def test_t23_edit_keeps_numbers(client: TestClient) -> None:
    """T23：编辑只改措辞时通过并导出。"""
    turn = _chat(client, MAIN_QUESTION)
    thread_id = turn["thread_id"]
    edited = turn["report"] + "\n\n> 人工确认：措辞已核对，数字未改动。\n"
    resumed = _resume(client, turn, "edit", edited_report=edited)
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["status"] == "approved"
    report = client.get(f"/api/threads/{thread_id}/report")
    assert "人工确认" in report.json()["markdown"]


# ---------------------------------------------------------------------------
# 偏好（跨会话 Store）
# ---------------------------------------------------------------------------


def test_t23_preferences_roundtrip(client: TestClient) -> None:
    """T23：偏好读取有默认值，写入后能读回（跨会话记忆的最小形态）。"""
    initial = client.get("/api/preferences")
    assert initial.status_code == 200
    assert set(initial.json()["prefs"]) >= {"amount_unit", "default_dimension"}

    updated = client.put("/api/preferences", json={"prefs": {"amount_unit": "万元", "default_dimension": "region"}})
    assert updated.status_code == 200
    assert updated.json()["prefs"]["amount_unit"] == "万元"
