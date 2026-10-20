"""流式输出：``AppRuntime.stream_turn`` 与 ``GET /api/chat/stream``（SSE）。

覆盖点：
- 事件契约：``start → node* → interrupt? → final|error``，终止事件恰好一个；
- ``final.turn`` 与 ``invoke_turn`` / ``POST /api/chat`` 的返回体**同构**（键集合相同）；
- ``interrupt`` 事件与 ``TurnResult.interrupt`` 同构（同 id、同 payload 键）；
- 锁语义：完整消费与中途 ``close()`` 之后都必须释放（``RLock`` 按线程归属，跨线程
  逐个 ``next()`` 会在释放时抛 RuntimeError，这两条断言就是那道护栏）；
- 审批语义未被流式改变：``interrupt`` 之后仍须 ``resume`` 才导出；
- 传输层：SSE 帧格式合规、待审批 409、校验 422、越权 404 都发生在**开流之前**；
- 逐帧送达：人为放慢取数节点后，早期帧必须远早于总耗时到达（证明不是一次性缓冲）。

实现说明：与 test_api.py 相同，不启动 uvicorn、不走 lifespan，直接把测试运行时塞进
``app.state.runtime``；逐帧送达断言用 TestClient 的流式接口即可复现。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import ops_assistant.workflow.nodes as nodes
from ops_assistant.api.main import app
from ops_assistant.workflow.graph import NODE_NAMES
from ops_assistant.workflow.persistence import AppRuntime

MAIN_QUESTION = "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
CLARIFY_QUESTION = "利润是多少？"
TERMINAL_EVENTS = {"final", "error"}


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


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本解析成 ``[(event 名, data 字典)]``。"""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name: str | None = None
        data: dict[str, Any] | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        assert name is not None and data is not None, f"SSE 帧不完整：{block!r}"
        events.append((name, data))
    return events


def stream(client: TestClient, **params: Any) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """请求 SSE 端点，返回 (响应对象, 解析后的事件列表)。"""
    with client.stream("GET", "/api/chat/stream", params=params) as response:
        body = "".join(response.iter_text())
        headers = dict(response.headers)
        status_code = response.status_code
    return type("R", (), {"status_code": status_code, "headers": headers})(), parse_sse(body)


# ---------------------------------------------------------------------------
# 运行时层：stream_turn 的事件契约
# ---------------------------------------------------------------------------


def test_stream_turn_event_sequence_ends_with_single_terminal(runtime: AppRuntime) -> None:
    events = list(runtime.stream_turn(MAIN_QUESTION))
    names = [event["event"] for event in events]
    assert names[0] == "start"
    assert names[-1] in TERMINAL_EVENTS
    assert sum(name in TERMINAL_EVENTS for name in names) == 1, names
    assert "interrupt" in names, names


def test_stream_turn_node_events_use_real_node_names_in_order(runtime: AppRuntime) -> None:
    events = list(runtime.stream_turn(MAIN_QUESTION))
    seen = [event["node"] for event in events if event["event"] == "node"]
    assert seen, "没有任何 node 事件"
    assert set(seen) <= set(NODE_NAMES), seen
    # 节点顺序必须与图上的实际执行顺序一致，不能乱序或重复
    assert seen == sorted(set(seen), key=seen.index)
    assert len(seen) == len(set(seen)), seen


def test_stream_turn_final_turn_is_isomorphic_to_invoke_turn(runtime: AppRuntime) -> None:
    """流式不得改变对外契约：final.turn 的键集合与非流式返回值完全相同。"""
    streamed = list(runtime.stream_turn(MAIN_QUESTION))
    turn = next(event["turn"] for event in streamed if event["event"] == "final")
    reference = runtime.invoke_turn(MAIN_QUESTION)
    assert set(turn) == set(reference), set(turn) ^ set(reference)
    assert turn["status"] == reference["status"] == "awaiting_approval"


def test_stream_turn_interrupt_event_matches_turn_interrupt(runtime: AppRuntime) -> None:
    """SSE 的 interrupt 载荷与 TurnResult.interrupt 必须同构，UI 才能复用同一套逻辑。"""
    events = list(runtime.stream_turn(MAIN_QUESTION))
    interrupt = next(event for event in events if event["event"] == "interrupt")
    turn = next(event["turn"] for event in events if event["event"] == "final")
    assert interrupt["id"] == turn["interrupt"]["id"]
    assert set(interrupt["payload"]) == set(turn["interrupt"]["payload"])
    assert interrupt["payload"]["type"] == "report_approval"


def test_stream_turn_events_are_json_serializable(runtime: AppRuntime) -> None:
    for event in runtime.stream_turn(MAIN_QUESTION):
        json.dumps(event, ensure_ascii=False)


def test_stream_turn_clarification_has_no_interrupt(runtime: AppRuntime) -> None:
    events = list(runtime.stream_turn(CLARIFY_QUESTION))
    names = [event["event"] for event in events]
    assert "interrupt" not in names, names
    turn = next(event["turn"] for event in events if event["event"] == "final")
    assert turn["status"] == "clarification"
    assert not turn["report"]


def test_stream_turn_refuses_new_turn_while_approval_pending(runtime: AppRuntime) -> None:
    """与 invoke_turn 一致：待审批时抛 ValueError，而不是静默开新一轮。"""
    events = list(runtime.stream_turn(MAIN_QUESTION))
    thread_id = events[0]["thread_id"]
    with pytest.raises(ValueError, match="PENDING_APPROVAL"):
        list(runtime.stream_turn("2026 年 8 月销售额是多少？", thread_id=thread_id))


def test_stream_turn_approval_semantics_are_unchanged(runtime: AppRuntime) -> None:
    """流式只改变进度怎么送达：报告仍只在 resume 批准后才有导出路径。"""
    events = list(runtime.stream_turn(MAIN_QUESTION))
    turn = next(event["turn"] for event in events if event["event"] == "final")
    interrupt = next(event for event in events if event["event"] == "interrupt")
    assert turn["export_path"] is None

    resumed = runtime.resume_turn(
        turn["thread_id"],
        "approve",
        interrupt_id=interrupt["id"],
        report_id=turn["report_id"],
        content_hash=interrupt["payload"]["content_hash"],
    )
    assert resumed["status"] == "approved"
    assert resumed["export_path"]


def test_stream_turn_releases_lock_after_full_consumption(runtime: AppRuntime) -> None:
    list(runtime.stream_turn(MAIN_QUESTION))
    assert runtime.lock.acquire(blocking=False), "流结束后锁未释放"
    runtime.lock.release()


def test_stream_turn_releases_lock_when_consumer_abandons_midway(runtime: AppRuntime) -> None:
    """消费端中途放弃（生成器 close）也必须放锁，否则整个运行时会死锁。"""
    generator = runtime.stream_turn(MAIN_QUESTION)
    next(generator)  # start
    next(generator)  # 进入持锁区间后的第一个 node 事件
    generator.close()
    assert runtime.lock.acquire(blocking=False), "中途放弃后锁未释放"
    runtime.lock.release()


# ---------------------------------------------------------------------------
# 传输层：GET /api/chat/stream
# ---------------------------------------------------------------------------


def test_sse_response_headers_and_frame_format(client: TestClient) -> None:
    response, events = stream(client, question=MAIN_QUESTION)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("cache-control") == "no-cache"
    # 反向代理会缓冲响应，SSE 必须显式关掉
    assert response.headers.get("x-accel-buffering") == "no"
    for name, data in events:
        assert name == data["event"], f"event 行与 data.event 不一致：{name} / {data.get('event')}"


def test_sse_terminal_event_is_exactly_one_final(client: TestClient) -> None:
    _, events = stream(client, question=MAIN_QUESTION)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert sum(name in TERMINAL_EVENTS for name in names) == 1, names
    assert names[-1] == "final"


def test_sse_final_turn_matches_post_chat_response(client: TestClient) -> None:
    """SSE 的 final.turn 与 POST /api/chat 的响应体同构，既有客户端可直接复用。"""
    _, events = stream(client, question=MAIN_QUESTION)
    turn = next(data["turn"] for name, data in events if name == "final")
    reference = client.post("/api/chat", json={"question": MAIN_QUESTION}).json()
    assert set(turn) == set(reference), set(turn) ^ set(reference)


def test_sse_pending_approval_is_http_409_before_stream_opens(client: TestClient) -> None:
    """SSE 一旦发出 200 就改不了状态码，所以待审批必须在开流前变成标准 409。"""
    first = client.post("/api/chat", json={"question": MAIN_QUESTION}).json()
    response = client.get(
        "/api/chat/stream",
        params={"question": "2026 年 8 月销售额是多少？", "thread_id": first["thread_id"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PENDING_APPROVAL"
    assert response.headers["content-type"].startswith("application/json")


def test_sse_empty_question_is_validation_error(client: TestClient) -> None:
    response = client.get("/api/chat/stream", params={"question": ""})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_sse_other_users_thread_is_404(client: TestClient) -> None:
    first = client.post("/api/chat", json={"question": MAIN_QUESTION, "user_id": "owner"}).json()
    response = client.get(
        "/api/chat/stream",
        params={"question": MAIN_QUESTION, "thread_id": first["thread_id"], "user_id": "intruder"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "THREAD_NOT_FOUND"


def test_sse_stream_can_be_resumed_through_normal_endpoint(client: TestClient) -> None:
    """流式开的会话，审批仍走原来的 resume 端点并真的导出。"""
    _, events = stream(client, question=MAIN_QUESTION, user_id="stream-user")
    turn = next(data["turn"] for name, data in events if name == "final")
    interrupt = next(data for name, data in events if name == "interrupt")
    response = client.post(
        f"/api/threads/{turn['thread_id']}/resume",
        json={
            "decision": "approve",
            "user_id": "stream-user",
            "interrupt_id": interrupt["id"],
            "report_id": turn["report_id"],
            "content_hash": interrupt["payload"]["content_hash"],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["export_path"]


def test_sse_frames_arrive_incrementally_not_buffered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """决定性断言：把取数节点放慢后，早期帧必须远早于总耗时到达。

    若实现是"跑完再一次性吐出"，首帧到达时间会约等于总耗时。
    """
    delay = 0.5
    original = nodes.get_month_sales
    calls = {"count": 0}

    def slow_get_month_sales(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        time.sleep(delay)
        return original(*args, **kwargs)

    monkeypatch.setattr(nodes, "get_month_sales", slow_get_month_sales)

    started = time.perf_counter()
    arrivals: list[tuple[float, str]] = []
    with client.stream("GET", "/api/chat/stream", params={"question": MAIN_QUESTION}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event: "):
                arrivals.append(((time.perf_counter() - started) * 1000, line[len("event: ") :]))

    assert calls["count"] >= 1, "慢节点没有被调用，本断言失去意义"
    total = arrivals[-1][0]
    first = arrivals[0][0]
    assert total > delay * 1000, f"总耗时 {total}ms 未体现注入的延迟，实验无效"
    # 逐帧送达：首帧必须显著早于末帧（缓冲实现会让两者几乎相等）
    assert first < total * 0.5, f"首帧 {first}ms / 总耗时 {total}ms，看起来是缓冲到最后才吐出"
    # data 节点之前的帧应当在慢节点触发前就已送达
    node_times = [ms for ms, name in arrivals if name == "node"]
    assert max(node_times) - min(node_times) > delay * 500, node_times
