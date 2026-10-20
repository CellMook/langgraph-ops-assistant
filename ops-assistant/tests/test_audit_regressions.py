"""Regression tests for the independently reproduced audit counterexamples.

No xfail, no real credentials, no external model calls. A failed acceptance
test is a reproduction, not a repaired defect. Controls demonstrate the harness.
"""

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from conftest import build_settings
from ops_assistant.config import ensure_dirs
from ops_assistant.data.generate import generate_database
from ops_assistant.domain.question import parse_question
from ops_assistant.workflow.nodes import _edited_report_or_draft
from ops_assistant.workflow.persistence import AppRuntime


@pytest.fixture
def settings(tmp_path):
    value = build_settings(tmp_path)
    ensure_dirs(value)
    generate_database(value.db_path)
    return value


@pytest.fixture
def runtime(settings):
    value = AppRuntime.create(settings)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def client(runtime):
    from fastapi.testclient import TestClient

    from ops_assistant.api.main import app

    app.state.runtime = runtime
    value = TestClient(app)
    try:
        yield value
    finally:
        value.close()
        app.state.runtime = None


QUESTION = "2026年9月销售额比8月变化多少？按地区分解，并说明退款口径。"


def finish(runtime, turn):
    if turn.get("interrupt"):
        runtime.resume_turn(turn["thread_id"], "reject")


def test_control_offline_approval_and_idempotence(runtime):
    turn = runtime.invoke_turn(QUESTION)
    assert turn["status"] == "awaiting_approval"
    assert turn["calculation"]["metric_delta_cents"] == -573884
    assert not list(runtime.settings.export_dir.glob("*.md"))
    result = runtime.resume_turn(turn["thread_id"], "approve")
    path = Path(result["export_path"])
    before = path.read_bytes()
    repeated = runtime.resume_turn(turn["thread_id"], "approve")
    assert repeated["export_path"] == str(path)
    assert path.read_bytes() == before


def test_control_rejection_does_not_export(runtime):
    turn = runtime.invoke_turn(QUESTION)
    rejected = runtime.resume_turn(turn["thread_id"], "reject")
    assert rejected["status"] == "rejected"
    assert not rejected["export_path"]
    assert not list(runtime.settings.export_dir.glob("*.md"))


def test_control_restart_pending_approval(settings):
    first = AppRuntime.create(settings)
    turn = first.invoke_turn(QUESTION)
    first.close()
    second = AppRuntime.create(settings)
    try:
        assert second.read_thread(turn["thread_id"])["interrupt"]
        assert second.resume_turn(turn["thread_id"], "approve")["status"] == "approved"
    finally:
        second.close()


def test_control_distinct_threads_keep_periods(runtime):
    a = runtime.invoke_turn("2026年9月销售额是多少？")
    b = runtime.invoke_turn("2026年7月销售额是多少？")
    assert a["thread_id"] != b["thread_id"]
    assert runtime.read_thread(a["thread_id"])["spec"]["current_month"] == "2026-09"
    assert runtime.read_thread(b["thread_id"])["spec"]["current_month"] == "2026-07"


@pytest.mark.parametrize(
    "question,expected_current,expected_previous",
    [
        ("2026年9月同比销售额变化多少？", "2026-09", "2025-09"),
        ("2026年9月比上月销售额变化多少？", "2026-09", "2026-08"),
        ("2026年9月相对于去年9月销售额变化多少？", "2026-09", "2025-09"),
        ("从2026年8月到9月销售额变化多少？", "2026-09", "2026-08"),
    ],
)
def test_period_semantics(question, expected_current, expected_previous):
    result = parse_question(question, today=date(2026, 10, 20))
    assert (result.current_month, result.previous_month) == (expected_current, expected_previous)


def test_unknown_metric_must_not_become_net_sales(runtime):
    turn = runtime.invoke_turn("2026年9月利润是多少？")
    assert turn["status"] in {"clarification", "unsupported", "failed"}, turn["spec"]
    assert not turn["report"]


def test_knowledge_question_needs_no_sales_month(runtime):
    turn = runtime.invoke_turn("净销售额的定义是什么？")
    assert turn["status"] != "clarification", turn["clarification"]
    assert any(source["type"] == "doc" for source in turn["sources"])


def test_followup_preserves_gross_metric(runtime):
    first = runtime.invoke_turn("2026年9月毛销售额比8月变化多少？")
    finish(runtime, first)
    second = runtime.invoke_turn("那华东呢？", thread_id=first["thread_id"])
    assert second["spec"]["metric"] == "gross_sales", second["spec"]


def test_national_followup_clears_region_filter(runtime):
    first = runtime.invoke_turn("2026年9月华东销售额是多少？")
    finish(runtime, first)
    second = runtime.invoke_turn("那全国呢？", thread_id=first["thread_id"])
    assert second["spec"]["regions"] == [], second["spec"]


def test_unknown_region_followup_requires_clarification(runtime):
    first = runtime.invoke_turn("2026年9月华东销售额是多少？")
    finish(runtime, first)
    second = runtime.invoke_turn("那火星地区呢？", thread_id=first["thread_id"])
    assert second["status"] == "clarification", second["spec"]


def test_mixed_valid_and_unknown_regions_are_not_silently_dropped(runtime):
    turn = runtime.invoke_turn("2026年9月华东和火星地区销售额是多少？")
    assert turn["status"] == "clarification", turn["spec"]


@pytest.mark.parametrize("metric", ["毛销售额", "退款额"])
def test_breakdown_uses_same_metric_as_total(runtime, metric):
    turn = runtime.invoke_turn(f"2026年9月{metric}比8月变化多少？")
    calc = turn["calculation"]
    assert calc["region_delta_sum_cents"] == calc["metric_delta_cents"]
    assert calc["edge_flags"]["delta_sum_matches"], turn["errors"]
    assert "校验结果不一致" not in turn["report"]


def test_refund_counts_respect_region_filter(runtime):
    turn = runtime.invoke_turn("2026年9月华东销售额是多少？")
    expected = runtime.conn.execute(
        "SELECT COUNT(*) FROM refunds r JOIN orders o USING(order_id) "
        "WHERE o.region_id = ? AND r.status = 'approved' "
        "AND r.refund_date >= ? AND r.refund_date < ?",
        ("R_EAST", "2026-09-01", "2026-10-01"),
    ).fetchone()[0]
    assert turn["calculation"]["current"]["approved_refund_count"] == expected


def test_true_zero_is_recognized_in_workflow(runtime):
    turn = runtime.invoke_turn("2026年9月西南销售额是多少？")
    calc = turn["calculation"]
    assert calc["current"]["order_count"] > 0
    assert calc["current"]["net_cents"] == 0
    assert calc["edge_flags"]["true_zero"] is True


def test_stale_approval_is_rejected(client):
    first = client.post("/api/chat", json={"question": "2026年9月销售额是多少？"}).json()
    second_response = client.post(
        "/api/chat",
        json={
            "question": "2026年8月销售额是多少？",
            "thread_id": first["thread_id"],
        },
    )
    if second_response.status_code == 409:
        return  # Another valid design: disallow replacing a pending review.
    assert second_response.status_code == 200
    second = second_response.json()
    assert second["interrupt"]["id"] != first["interrupt"]["id"]
    response = client.post(
        f"/api/threads/{first['thread_id']}/resume",
        json={
            "decision": "approve",
            "interrupt_id": first["interrupt"]["id"],
            "comment": "This approval is for September, not August",
        },
    )
    assert response.status_code in {409, 422}, response.json()["spec"]


@pytest.mark.parametrize(
    "edited",
    [
        "华东销售额 +100 元；华南销售额 200 元。",
        "华东销售额 -100 万元；华南销售额 200 元。",
        "华东销售额 200 元；华南销售额 -100 元。",
    ],
)
def test_edit_cannot_change_sign_unit_or_numeric_assignment(edited):
    draft = "华东销售额 -100 元；华南销售额 200 元。"
    accepted, errors = _edited_report_or_draft(edited, draft)
    assert accepted != edited or errors, (accepted, errors)


def test_distinct_unicode_thread_ids_do_not_overwrite_exports(runtime):
    a = runtime.invoke_turn("2026年9月销售额是多少？", thread_id="审查甲")
    out_a = runtime.resume_turn(a["thread_id"], "approve")
    before = Path(out_a["export_path"]).read_bytes()
    b = runtime.invoke_turn("2026年8月销售额是多少？", thread_id="审查乙")
    out_b = runtime.resume_turn(b["thread_id"], "approve")
    assert out_a["export_path"] != out_b["export_path"]
    assert Path(out_a["export_path"]).read_bytes() == before


def test_user_id_does_not_grant_another_users_thread(runtime):
    turn = runtime.invoke_turn(QUESTION, user_id="alice")
    with pytest.raises((LookupError, PermissionError, ValueError)):
        runtime.read_thread(turn["thread_id"], user_id="bob")


def test_configured_model_timeout_reaches_client(settings, monkeypatch):
    import langchain.chat_models

    from ops_assistant.agent.llm import LLMClient

    captured = {}

    def fake_factory(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_factory)
    value = dataclasses.replace(
        settings, llm_enabled=True, model="openai:audit-fake", api_key="not-a-real-key", request_timeout_s=0.25
    )
    assert LLMClient(value).get_chat_model() is not None
    assert captured.get("timeout", captured.get("request_timeout")) == 0.25, captured


class BurstToolModel(BaseChatModel):
    """Real create_agent loop, deterministic fake model; executes real local tools."""

    @property
    def _llm_type(self):
        return "audit-burst"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            response = AIMessage(content="工具完成。")
        else:
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "calculate_change",
                        "args": {"current_cents": 200, "previous_cents": 100},
                        "id": f"call-{i}",
                        "type": "tool_call",
                    }
                    for i in range(9)
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_agent_tool_budget_enforced_before_tools(runtime, monkeypatch):
    from ops_assistant.agent import simple_agent

    executed = []
    from langchain_core.tools import tool

    @tool
    def calculate_change(current_cents: int, previous_cents: int) -> str:
        """Calculate deterministic local change; instrument execution count."""
        executed.append((current_cents, previous_cents))
        return json.dumps({"ok": True, "data": {"delta": current_cents - previous_cents}})

    class FakeClient:
        def get_chat_model(self, **kwargs):
            return BurstToolModel()

        def usage(self):
            return {}

        def redact(self, value):
            return value

    monkeypatch.setattr(simple_agent, "build_llm", lambda settings: FakeClient())
    monkeypatch.setattr(simple_agent, "make_tools", lambda *args: [calculate_change])
    result = simple_agent.run_simple_agent(
        "计算变化",
        settings=dataclasses.replace(runtime.settings, max_tool_calls=2),
        conn=runtime.conn,
        kb=runtime.kb,
    )
    assert len(executed) <= 2, {"executed": len(executed), "status": result["status"]}


def test_citation_id_alone_does_not_prove_claim(runtime):
    from ops_assistant.knowledge.qa import _llm_rewrite

    hits = runtime.kb.search("净销售额口径", mode="hybrid")
    fabricated = f"净销售额就是利润，退款永远不扣除。[{hits[0].chunk.chunk_id}]"

    class UnfaithfulModel:
        enabled = True

        def invoke_text(self, *args, **kwargs):
            return fabricated

    answer, mode = _llm_rewrite("净销售额是什么？", hits, UnfaithfulModel(), "trusted fallback")
    assert answer != fabricated, {"mode": mode, "answer": answer}


def test_failed_eval_is_nonzero(runtime, tmp_path, monkeypatch):
    import importlib.util

    from conftest import PROJECT_DIR as PROJECT

    spec = importlib.util.spec_from_file_location("audit_eval", PROJECT / "eval/run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_settings", lambda: runtime.settings)
    cases = tmp_path / "wrong-expectation.jsonl"
    cases.write_text(
        json.dumps({"id": "negative-control", "question": "2026年9月销售额是多少？", "expect_status": "IMPOSSIBLE"})
        + "\n",
        encoding="utf-8",
    )
    assert module.main(["--cases", str(cases), "--out-dir", str(tmp_path / "out")]) != 0


@pytest.mark.parametrize(
    "question,current,previous",
    [
        ("2026年8月到9月销售额变化多少？", "2026-09", "2026-08"),
        ("2026年9月比去年同期销售额变化多少？", "2026-09", "2025-09"),
        ("2026年9月相对于去年9月销售额变化多少？", "2026-09", "2025-09"),
        ("2026年1月比上个月销售额变化多少？", "2026-01", "2025-12"),
    ],
)
def test_period_anchor_is_independent_of_machine_year(question, current, previous):
    spec = parse_question(question, today=date(2027, 10, 20))
    assert (spec.current_month, spec.previous_month) == (current, previous)


def approval_payload(turn, decision="approve"):
    return {
        "decision": decision,
        "interrupt_id": turn["interrupt"]["id"],
        "report_id": turn["report_id"],
        "content_hash": turn["interrupt"]["payload"]["content_hash"],
    }


def test_report_versions_remain_downloadable_and_stale_approval_cannot_advance(client):
    first = client.post("/api/chat", json={"question": "2026年9月销售额是多少？"}).json()
    tid = first["thread_id"]
    old_approval = approval_payload(first)
    approved = client.post(f"/api/threads/{tid}/resume", json=old_approval)
    assert approved.status_code == 200
    first_file = Path(approved.json()["export_path"])
    first_content = first_file.read_bytes()
    second = client.post("/api/chat", json={"question": "2026年8月销售额是多少？", "thread_id": tid}).json()
    assert second["report_id"] != first["report_id"]
    assert client.post(f"/api/threads/{tid}/resume", json=old_approval).status_code == 409
    assert client.get(f"/api/threads/{tid}").json()["status"] == "awaiting_approval"
    assert client.post(f"/api/threads/{tid}/resume", json=approval_payload(second)).status_code == 200
    assert first_file.read_bytes() == first_content
    old_report = client.get(f"/api/threads/{tid}/report", params={"report_id": first["report_id"]})
    assert old_report.status_code == 200
    assert old_report.json()["markdown"].encode() == first_content
    assert len(client.get(f"/api/threads/{tid}/reports").json()["items"]) == 2


def test_missing_approval_identity_is_rejected(client):
    turn = client.post("/api/chat", json={"question": "2026年9月销售额是多少？"}).json()
    response = client.post(f"/api/threads/{turn['thread_id']}/resume", json={"decision": "approve"})
    assert response.status_code == 409


def test_bad_content_hash_preserves_pending_approval(client):
    turn = client.post("/api/chat", json={"question": "2026年9月销售额是多少？"}).json()
    payload = approval_payload(turn)
    payload["content_hash"] = "wrong"
    response = client.post(f"/api/threads/{turn['thread_id']}/resume", json=payload)
    assert response.status_code == 409
    assert client.get(f"/api/threads/{turn['thread_id']}").json()["interrupt"] is not None


def test_export_failure_is_retryable_without_regenerating_report(runtime, monkeypatch):
    import ops_assistant.workflow.nodes as nodes

    turn = runtime.invoke_turn(QUESTION)
    original = nodes.export_report

    def fail(*args, **kwargs):
        raise OSError("transient disk error")

    monkeypatch.setattr(nodes, "export_report", fail)
    failed = runtime.resume_turn(turn["thread_id"], "approve")
    assert failed["status"] == "failed"
    monkeypatch.setattr(nodes, "export_report", original)
    recovered = runtime.resume_turn(turn["thread_id"], "approve", report_id=turn["report_id"])
    assert recovered["status"] == "approved"
    assert recovered["report_id"] == turn["report_id"]
    assert Path(recovered["export_path"]).read_text() == turn["report"]


def test_old_errors_do_not_appear_in_new_turn(runtime):
    runtime.invoke_turn("", thread_id="recover")
    valid = runtime.invoke_turn("2026年9月销售额是多少？", thread_id="recover")
    assert valid["status"] == "awaiting_approval"
    assert valid["errors"] == []


def test_zero_tool_budget_cannot_be_replaced_with_default(settings):
    instance = AppRuntime.create(dataclasses.replace(settings, max_tool_calls=0))
    try:
        result = instance.invoke_turn("2026年9月销售额是多少？")
        assert result["status"] == "budget_exceeded"
        assert result["usage"]["max_tool_calls"] == 0
        assert not result["report"]
    finally:
        instance.close()


@pytest.mark.parametrize("dimension", ["商品", "月", "渠道"])
def test_unimplemented_dimensions_are_explicit(runtime, dimension):
    turn = runtime.invoke_turn(f"2026年9月按{dimension}分析销售额")
    assert turn["status"] == "clarification"
    assert not turn["report"]


def test_final_report_citation_ids_match_index(runtime):
    import re

    result = runtime.invoke_turn(QUESTION)
    cited = re.findall(r"\[([^\[\]\s]+#[0-9]+)\]", result["report"])
    assert cited
    assert all(ref in runtime.kb.citation_ids() for ref in cited)


def test_no_orders_with_refund_is_not_described_as_missing_amounts(runtime):
    result = runtime.invoke_turn("2025年9月东北销售额是多少？")
    current = result["calculation"]["current"]
    assert not current["has_orders"] and current["refund_cents"] == 13447
    assert "相关金额只代表无数据" not in result["report"]


def test_scoped_detail_rows_do_not_include_other_regions(runtime):
    turn = runtime.invoke_turn("2026年9月华东销售额是多少？")
    assert {row[0] for row in turn["tables"][0]["rows"]} == {"R_EAST"}
