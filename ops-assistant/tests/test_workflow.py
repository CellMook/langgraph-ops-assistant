"""T20–T21：工作流主链路、多轮记忆、会话隔离与预算（工程规格 6.1）。

覆盖点：
- T20 主链路：问题 -> 解析 -> 检索 -> 取数 -> 计算 -> 报告 -> 等待审批；
  报告结构、来源、数值来源都可检查；拒绝审批不产出文件。
- T21 多轮与隔离：同 thread 追问沿用上一轮期间；不同 thread 互不影响；
  无期间且无上下文时走澄清分支；预算耗尽走统一失败出口。

说明：
    本文件不依赖真实模型（``settings`` 夹具里 ``llm_enabled=False``），
    因此验证的是"确定性内核 + LangGraph 编排"，这正是本项目声称可复现的部分。

诚实声明：未在编写环境中执行；状态值依据 ``workflow/nodes.py`` 的写入逻辑推导。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from ops_assistant.workflow.persistence import AppRuntime

MAIN_QUESTION = (
    "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
)


def _nodes(turn: dict[str, Any]) -> list[str]:
    """从 TurnResult.trace 里提取去重后的节点顺序。"""
    order: list[str] = []
    for item in turn.get("trace") or []:
        node = str(item.get("node") or "")
        if node and (not order or order[-1] != node):
            order.append(node)
    return order


# ---------------------------------------------------------------------------
# T20 主链路
# ---------------------------------------------------------------------------


def test_t20_main_question_stops_at_approval(runtime: AppRuntime) -> None:
    """T20：主问题跑完后状态是 awaiting_approval，而不是直接导出。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    assert turn["status"] == "awaiting_approval"
    assert turn["interrupt"] is not None
    assert turn["export_path"] is None


def test_t20_report_has_required_sections(runtime: AppRuntime) -> None:
    """T20：报告包含规格 5.9 要求的关键小节（结构而不是措辞）。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    report = turn["report"]
    assert isinstance(report, str) and report.strip()
    for heading in ("## 结论", "## 关键数字", "## 地区差额分解", "## 口径与依据", "## 来源"):
        assert heading in report, f"报告缺少小节：{heading}"


def test_t20_trace_shows_expected_node_order(runtime: AppRuntime) -> None:
    """T20：轨迹里能看到 understand -> knowledge -> data -> compute -> draft。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    nodes = _nodes(turn)
    for expected in ("understand", "knowledge", "data", "compute", "draft"):
        assert expected in nodes, f"轨迹缺少节点 {expected}，实际 {nodes}"
    assert nodes[0] == "understand"


def test_t20_sources_include_sql_and_documents(runtime: AppRuntime) -> None:
    """T20：来源要同时包含 SQL 取数与知识文档，且每条都有可读标识。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    sources = turn["sources"]
    assert sources, "报告必须带来源"
    kinds = {str(item.get("type") or item.get("kind") or "") for item in sources}
    assert any("sql" in kind.lower() for kind in kinds), f"缺少 SQL 来源：{kinds}"
    assert any(
        word in kind.lower() for kind in kinds for word in ("doc", "knowledge")
    ), f"缺少文档来源：{kinds}"


def test_t20_tables_and_chart_payload(runtime: AppRuntime) -> None:
    """T20：表格数据可用于 UI 渲染；图表负载存在且为 JSON 友好结构。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    assert turn["tables"], "应有地区分解表格"
    assert any(item.get("rows") for item in turn["tables"])
    chart = turn["chart"]
    assert isinstance(chart, dict)
    assert chart.get("type") in {"bar", "line"}


def test_t20_budget_within_limits(runtime: AppRuntime) -> None:
    """T20：正常一轮不应撞到预算上限（工具调用数不超过配置上限）。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    metrics = turn["metrics"]
    assert metrics["tool_calls"] <= runtime.settings.max_tool_calls
    assert metrics["elapsed_ms"] >= 0


def test_t20_usage_reports_no_model_calls(runtime: AppRuntime) -> None:
    """T20：纯规则模式下不应有模型调用，但仍要有耗时。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    usage = turn["usage"]
    assert usage["model_calls"] == 0
    assert "elapsed_ms" in usage


def test_t20_reject_produces_no_export(runtime: AppRuntime) -> None:
    """T20：拒绝审批后状态为 rejected，且不写任何文件。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    thread_id = turn["thread_id"]
    rejected = runtime.resume_turn(thread_id, "reject", comment="先不导出")
    assert rejected["status"] == "rejected"
    assert rejected["export_path"] is None
    assert not (rejected["report"] or "").strip()


# ---------------------------------------------------------------------------
# T21 多轮、隔离、澄清与预算
# ---------------------------------------------------------------------------


def test_t21_follow_up_inherits_period_and_filters_region(runtime: AppRuntime) -> None:
    """T21：追问“那华东地区呢？”沿用上一轮期间，并只保留华东。"""
    first = runtime.invoke_turn(MAIN_QUESTION)
    thread_id = first["thread_id"]
    runtime.resume_turn(thread_id, "reject")

    second = runtime.invoke_turn("那华东地区呢？", thread_id=thread_id)
    assert second["status"] == "awaiting_approval", second.get("clarification")

    spec = second["spec"]
    assert spec["current_month"] == "2026-09"
    assert spec["previous_month"] == "2026-08"
    assert list(spec["regions"]) == ["R_EAST"]

    calculation = second["calculation"]
    regions = calculation.get("regions") or []
    assert len(regions) == 1
    assert regions[0]["region_id"] == "R_EAST"


def test_t21_follow_up_on_fresh_thread_requires_clarification(runtime: AppRuntime) -> None:
    """T21：没有上下文的新会话问“那华东地区呢？”只能澄清，不能凭空给数。"""
    turn = runtime.invoke_turn("那华东地区呢？")
    assert turn["status"] == "clarification"
    assert turn["clarification"]


def test_t21_threads_are_isolated(runtime: AppRuntime) -> None:
    """T21：两个 thread 的状态互不覆盖（导出文件也按 thread 命名）。"""
    first = runtime.invoke_turn(MAIN_QUESTION)
    second = runtime.invoke_turn(MAIN_QUESTION)
    assert first["thread_id"] != second["thread_id"]

    approved = runtime.resume_turn(first["thread_id"], "approve")
    assert approved["status"] == "approved"
    export_path = approved["export_path"]
    assert export_path
    assert Path(export_path).name == f"report-{first['thread_id']}.md"

    # 第二个 thread 仍在等待审批，不受影响
    pending = runtime.read_thread(second["thread_id"])
    assert pending["status"] == "awaiting_approval"


def test_t21_missing_year_goes_to_clarification(runtime: AppRuntime) -> None:
    """T21：缺少年份的问题进入澄清分支，且不产生报告。"""
    turn = runtime.invoke_turn("九月销售额是多少？")
    assert turn["status"] == "clarification"
    assert "年份" in turn["clarification"]
    assert turn["report"] == ""


def test_t21_invalid_month_fails_with_readable_error(runtime: AppRuntime) -> None:
    """T21：非法月份走失败出口，errors 里要有可读原因。"""
    turn = runtime.invoke_turn("2026 年 13 月销售额比 12 月变化多少？")
    assert turn["status"] in {"clarification", "failed"}
    assert turn["clarification"] or turn["errors"]


def test_t21_budget_exhaustion_uses_fail_exit(settings) -> None:
    """T21：把 max_steps 调到很小，图应停在统一失败出口而不是抛递归异常。"""
    tight = dataclasses.replace(settings, max_steps=2)
    runtime = AppRuntime.create(tight, use_memory_checkpointer=True)
    try:
        turn = runtime.invoke_turn(MAIN_QUESTION)
        assert turn["status"] == "budget_exceeded"
        assert turn["export_path"] is None
        assert any("预算" in message for message in turn["errors"])
    finally:
        runtime.close()


def test_t21_request_id_is_echoed(runtime: AppRuntime) -> None:
    """T21：调用方传入的 request_id 会原样返回，便于日志串联。"""
    turn = runtime.invoke_turn(MAIN_QUESTION, request_id="req-test-001")
    assert turn["request_id"] == "req-test-001"
