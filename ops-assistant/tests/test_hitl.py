"""T22：人工审批（HITL）、幂等导出与重启恢复（工程规格 6.1、5.8）。

这一组测试专门验证最容易出错、也最容易被面试追问的语义：

1. ``interrupt()`` 之前的代码在恢复后会**从头重跑**，所以那之前不能有副作用；
2. 真正的落盘放在独立的 ``export`` 节点，导出用"临时文件 + os.replace"，可重入；
3. 重复确认同一份报告返回同一个路径，不产生第二份文件；
4. 审批状态必须能被新进程（新的 AppRuntime）从 SQLite 检查点读出来。

诚实声明：未在编写环境中执行。这里断言的是"源码保证的行为"，
真实运行结果需要用 ``uv run pytest tests/test_hitl.py`` 在学习者机器上确认。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ops_assistant.workflow.persistence import AppRuntime, ThreadNotFoundError

MAIN_QUESTION = (
    "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
)


@pytest.fixture()
def pending(runtime: AppRuntime) -> dict[str, Any]:
    """跑完主问题、停在审批中断上的 TurnResult。"""
    turn = runtime.invoke_turn(MAIN_QUESTION)
    assert turn["status"] == "awaiting_approval", turn.get("clarification") or turn.get("errors")
    return turn


# ---------------------------------------------------------------------------
# 中断载荷与决策
# ---------------------------------------------------------------------------


def test_t22_interrupt_payload_is_json_friendly(pending: dict[str, Any]) -> None:
    """T22：中断载荷只含 JSON 基本类型（它会写进 SQLite 检查点再读回来）。"""
    interrupt = pending["interrupt"]
    assert isinstance(interrupt, dict)
    assert set(interrupt) >= {"id", "payload"}
    payload = interrupt["payload"]
    assert isinstance(payload, dict)
    assert payload.get("type") == "report_approval"
    assert payload.get("report_markdown")
    assert set(payload["options"]) == {"approve", "reject", "edit"}


def test_t22_interrupt_payload_has_no_secret(pending: dict[str, Any]) -> None:
    """T22：中断载荷不能包含 api_key 等敏感字段。"""
    text = str(pending["interrupt"])
    assert "sk-" not in text
    assert "api_key" not in text.lower()


def test_t22_invalid_decision_is_rejected(runtime: AppRuntime, pending: dict[str, Any]) -> None:
    """T22：非法 decision 直接报 ValueError，不推进图。"""
    with pytest.raises(ValueError):
        runtime.resume_turn(pending["thread_id"], "maybe")


def test_t22_resume_without_pending_interrupt_raises(runtime: AppRuntime) -> None:
    """T22：没有待审批报告时恢复审批要报错，而不是返回空结果。"""
    with pytest.raises(ValueError):
        runtime.resume_turn("thread-without-draft", "approve")


# ---------------------------------------------------------------------------
# 批准 / 拒绝 / 编辑
# ---------------------------------------------------------------------------


def test_t22_approve_exports_file(runtime: AppRuntime, pending: dict[str, Any]) -> None:
    """T22：批准后导出 Markdown 文件，内容与 final_report 一致。"""
    result = runtime.resume_turn(pending["thread_id"], "approve", comment="口径确认")
    assert result["status"] == "approved"
    export_path = Path(result["export_path"])
    assert export_path.is_file(), f"导出文件不存在：{export_path}"
    content = export_path.read_text(encoding="utf-8")
    assert content.strip() == (result["report"] or "").strip()
    assert "## 结论" in content


def test_t22_reject_does_not_export(runtime: AppRuntime, pending: dict[str, Any]) -> None:
    """T22：拒绝后不落盘，且状态明确为 rejected。"""
    result = runtime.resume_turn(pending["thread_id"], "reject", comment="数字需要复核")
    assert result["status"] == "rejected"
    assert result["export_path"] is None
    assert result["report"] == ""
    # 该 thread 不应留下导出文件
    export_dir = Path(runtime.settings.export_dir)
    if export_dir.is_dir():
        assert not (export_dir / f"report-{pending['thread_id']}.md").exists()


def test_t22_edit_keeps_numbers_and_exports(runtime: AppRuntime, pending: dict[str, Any]) -> None:
    """T22：编辑只允许改措辞；保留原有数字时通过并导出。"""
    draft = pending["report"]
    edited = draft + "\n\n> 人工备注：本段措辞已经人工确认，数字未改动。\n"
    result = runtime.resume_turn(pending["thread_id"], "edit", edited_report=edited)
    assert result["status"] == "approved"
    export_path = Path(result["export_path"])
    assert export_path.is_file()
    content = export_path.read_text(encoding="utf-8")
    assert "人工备注" in content


def test_t22_edit_that_injects_numbers_falls_back_to_draft(
    runtime: AppRuntime, pending: dict[str, Any]
) -> None:
    """T22：编辑稿新增未计算的数字时必须回退原草稿，不能让审批绕过计算。"""
    draft = pending["report"]
    tampered = draft.replace("## 结论", "## 结论\n\n编造数字：销售额为 99999999 元。", 1)
    result = runtime.resume_turn(pending["thread_id"], "edit", edited_report=tampered)
    assert result["status"] == "approved"
    assert result["export_path"] is not None
    content = Path(result["export_path"]).read_text(encoding="utf-8")
    assert "99999999" not in content, "新增数字不应被导出"
    assert any("新增了原报告中没有的数字" in message for message in result["errors"])


def test_t22_repeat_approval_is_idempotent(
    runtime: AppRuntime, pending: dict[str, Any]
) -> None:
    """T22：同一份报告重复确认，返回同一路径且不产生第二份文件。"""
    first = runtime.resume_turn(pending["thread_id"], "approve")
    second = runtime.resume_turn(pending["thread_id"], "approve")
    assert first["export_path"] == second["export_path"]
    export_dir = Path(runtime.settings.export_dir)
    files = sorted(path.name for path in export_dir.glob(f"report-{pending['thread_id']}*.md"))
    assert files == [f"report-{pending['thread_id']}.md"]


def test_t22_export_uses_atomic_replace(runtime: AppRuntime, pending: dict[str, Any]) -> None:
    """T22：导出目录里不应残留 .tmp 中间文件（原子写完成后要清理）。"""
    result = runtime.resume_turn(pending["thread_id"], "approve")
    export_dir = Path(runtime.settings.export_dir)
    assert not list(export_dir.glob("*.tmp")), "不应留下临时文件"
    assert Path(result["export_path"]).suffix == ".md"


# ---------------------------------------------------------------------------
# 重启恢复
# ---------------------------------------------------------------------------


def test_t22_state_survives_restart(settings, db_summary: dict) -> None:
    """T22：用 SQLite 检查点时，进程重启后仍能读到待审批状态并完成审批。"""
    first_process = AppRuntime.create(settings)
    try:
        turn = first_process.invoke_turn(MAIN_QUESTION)
        thread_id = turn["thread_id"]
        assert turn["status"] == "awaiting_approval"
        draft = turn["report"]
    finally:
        first_process.close()

    # 模拟"服务重启"：新的运行时实例，共享同一个 checkpoints.db
    second_process = AppRuntime.create(settings)
    try:
        reloaded = second_process.read_thread(thread_id)
        assert reloaded["status"] == "awaiting_approval"
        assert reloaded["interrupt"] is not None
        assert reloaded["report"] == draft

        approved = second_process.resume_turn(thread_id, "approve")
        assert approved["status"] == "approved"
        assert Path(approved["export_path"]).is_file()
    finally:
        second_process.close()


def test_t22_memory_checkpointer_does_not_survive(tmp_path, settings, db_summary: dict) -> None:
    """T22：InMemorySaver 只适合测试；"重启"后同一 thread 读不到状态。"""
    first = AppRuntime.create(settings, use_memory_checkpointer=True)
    try:
        turn = first.invoke_turn(MAIN_QUESTION)
        thread_id = turn["thread_id"]
    finally:
        first.close()

    second = AppRuntime.create(settings, use_memory_checkpointer=True)
    try:
        with pytest.raises(ThreadNotFoundError):
            second.read_thread(thread_id)
    finally:
        second.close()
