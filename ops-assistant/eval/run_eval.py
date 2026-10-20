"""确定性评估脚本：跑固定用例集，记录"实际发生了什么"。

用途与边界（务必先读）：

* 本脚本跑的是**确定性内核 + LangGraph 编排**（无模型、无网络）。
  它记录每轮的 status、节点轨迹、耗时、来源数量与错误信息，
  便于回归与演示；它**不评估模型行为**，也不声称"正确率"。
* 有模型时模型行为需要另外记录（工具序列、最终答案、token），
  本脚本只把 ``usage`` 原样写出，不做任何自动判分。
* 结果写到 ``eval/results/deterministic-<UTC 时间戳>.json``；
  ``eval/results/`` 已在 .gitignore 里，避免把一次性输出当成交付物。

运行方式（在 ``ops-assistant`` 目录，且已跑过 bootstrap）：

    uv run python eval/run_eval.py
    uv run python eval/run_eval.py --cases eval/cases.jsonl --verbose

诚实声明：
    本脚本在编写环境中**没有执行过**（环境无法启动 Python 进程）。
    因此仓库里不存在任何"实测结果"文件；学习者机器上第一次运行才会产生。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ops_assistant.config import Settings, load_settings  # noqa: E402
from ops_assistant.observability import new_request_id  # noqa: E402
from ops_assistant.workflow.persistence import AppRuntime  # noqa: E402

DEFAULT_CASES = Path(__file__).resolve().with_name("cases.jsonl")
DEFAULT_OUT_DIR = Path(__file__).resolve().with_name("results")

#: 追问类用例的前置问题：先跑一轮主问题，才有 last_spec 可继承。
PRELUDE_QUESTION = (
    "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
)


def load_cases(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL 用例；空行与 ``#`` 注释行会被跳过。"""
    cases: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
        if "id" not in payload or "question" not in payload:
            raise ValueError(f"{path.name} 第 {line_number} 行缺少 id/question 字段")
        cases.append(payload)
    return cases


def _settings_for(settings: Settings, case: dict[str, Any]) -> Settings:
    """按用例的 ``settings_override`` 派生设置（例如把 max_steps 调到很小）。"""
    override = case.get("settings_override")
    if not isinstance(override, dict) or not override:
        return settings
    return dataclasses.replace(settings, **override)


def _nodes_of(turn: dict[str, Any]) -> list[str]:
    """提取去重后的节点顺序（相邻重复合并）。"""
    order: list[str] = []
    for item in turn.get("trace") or []:
        node = str(item.get("node") or "")
        if node and (not order or order[-1] != node):
            order.append(node)
    return order


def _run_case(runtime: AppRuntime, case: dict[str, Any]) -> dict[str, Any]:
    """执行单条用例并返回记录（不做断言，失败也记录实际结果）。

    状态比较规则（避免"用例语义混用"导致假性不匹配）：
        - ``expect_status_at`` 缺省为 ``"question"``：``expect_status`` 描述
          **提问那一轮**结束时的状态（例如停在 ``awaiting_approval``），
          审批 flow 只是收尾动作，不参与比较；
        - ``expect_status_at="final"``：``expect_status`` 描述 **flow 执行完**
          的终态（例如 E13 的 ``approved``、E14 的 ``rejected``）。
    """
    started = time.perf_counter()
    record: dict[str, Any] = {
        "id": case.get("id"),
        "category": case.get("category"),
        "question": case.get("question"),
        "expected_status": case.get("expect_status"),
        "expect_status_at": case.get("expect_status_at", "question"),
        "flow": case.get("flow", "none"),
        "notes": case.get("note", ""),
    }
    thread_id: str | None = None
    turn: dict[str, Any] = {}
    try:
        if case.get("requires_thread"):
            prelude = runtime.invoke_turn(PRELUDE_QUESTION)
            thread_id = prelude["thread_id"]
            if prelude.get("interrupt") is not None:
                runtime.resume_turn(thread_id, "reject")

        turn = runtime.invoke_turn(case["question"], thread_id=thread_id)
        thread_id = turn["thread_id"]
        status_at_question = turn.get("status")

        flow = str(case.get("flow") or "none")
        flow_applied = False
        if flow == "approve" and turn.get("interrupt") is not None:
            turn = runtime.resume_turn(thread_id, "approve")
            flow_applied = True
        elif flow == "reject" and turn.get("interrupt") is not None:
            turn = runtime.resume_turn(thread_id, "reject")
            flow_applied = True
        elif flow == "edit" and turn.get("interrupt") is not None:
            edited = str(turn.get("report") or "") + "\n\n> 人工确认：措辞已核对，数字未改动。\n"
            turn = runtime.resume_turn(thread_id, "edit", edited_report=edited)
            flow_applied = True

        compare_at_final = flow_applied and record["expect_status_at"] == "final"
        compared_status = turn.get("status") if compare_at_final else status_at_question
        nodes = _nodes_of(turn)
        expected_nodes = [str(item) for item in (case.get("expect_trace_contains") or [])]
        record.update(
            {
                "thread_id": thread_id,
                "status_at_question": status_at_question,
                "final_status": turn.get("status"),
                "flow_applied": flow_applied,
                "actual_status": compared_status,
                "status_match": compared_status == case.get("expect_status"),
                "missing_expected_nodes": [
                    node for node in expected_nodes if node not in nodes
                ],
                "trace_nodes": nodes,
                "clarification": turn.get("clarification"),
                "report_chars": len(str(turn.get("report") or "")),
                "sources": len(turn.get("sources") or []),
                "tables": len(turn.get("tables") or []),
                "has_chart": isinstance(turn.get("chart"), dict),
                "export_path": turn.get("export_path"),
                "interrupt_present": turn.get("interrupt") is not None,
                "usage": turn.get("usage"),
                "metrics": turn.get("metrics"),
                "errors": turn.get("errors") or [],
                "ok": True,
            }
        )
    except Exception as exc:  # 记录异常本身也是有价值的评估结果
        record.update(
            {
                "thread_id": thread_id,
                "actual_status": "EXCEPTION",
                "status_match": False,
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        )
    record["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    return record


def run(
    cases: list[dict[str, Any]],
    settings: Settings,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """按用例集顺序执行，返回可序列化的评估报告。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        key = json.dumps(
            case.get("settings_override") or {}, sort_keys=True, ensure_ascii=False
        )
        grouped.setdefault(key, []).append(case)

    records: list[dict[str, Any]] = []
    for key, bucket in grouped.items():
        case_settings = settings
        if key != "{}":
            case_settings = _settings_for(settings, bucket[0])
        runtime = AppRuntime.create(case_settings)
        try:
            for case in bucket:
                record = _run_case(runtime, case)
                records.append(record)
                if verbose:
                    print(
                        f"[{record.get('id')}] {record.get('actual_status')} "
                        f"({record.get('elapsed_ms')} ms) "
                        f"expected={record.get('expected_status')} "
                        f"match={record.get('status_match')}"
                    )
        finally:
            runtime.close()

    matched = sum(1 for item in records if item.get("status_match"))
    return {
        "meta": {
            "generated_by": "eval/run_eval.py",
            "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "request_id": new_request_id(),
            "case_file": str(DEFAULT_CASES.name),
            "model_enabled": bool(getattr(settings, "llm_enabled", False)),
            "model": getattr(settings, "model", None),
            "settings": {
                "max_steps": settings.max_steps,
                "max_tool_calls": settings.max_tool_calls,
            },
            "scope": (
                "只评估确定性路径：规则解析、SQL 取数、指标计算、工作流状态机与审批语义。"
                "status_match 只比较工作流状态，不代表最终答案质量。"
            ),
            "model_behavior_evaluated": False,
            "model_behavior_note": (
                "本次未评估模型行为（工具序列、最终答案措辞、token 用量）。"
                "要评估模型需要配置 OPS_MODEL/OPS_API_KEY 后另行记录，"
                "且不能用本文件的 status_match 代替。"
            ),
            "not_executed_notice": (
                "本文件由脚本运行时生成。若你的机器上没有 eval/results/ 目录，"
                "说明还没有真正执行过评估——不要凭空写“通过率”。"
            ),
        },
        "summary": {
            "total": len(records),
            "status_matched": matched,
            "status_mismatched": len(records) - matched,
            "exception_count": sum(1 for item in records if not item.get("ok")),
            "note": "status_matched 仅统计工作流状态是否与用例预期一致，不是准确率。",
        },
        "cases": records,
    }


def main(argv: list[str] | None = None) -> int:
    """命令行入口：读用例 -> 跑确定性评估 -> 写 JSON -> 打印摘要。"""
    parser = argparse.ArgumentParser(description="ops-assistant 确定性评估（无模型）")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="用例 JSONL 路径")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="结果输出目录")
    parser.add_argument("--verbose", action="store_true", help="逐条打印进度")
    args = parser.parse_args(argv)

    if not args.cases.is_file():
        print(f"错误：找不到用例文件 {args.cases}", file=sys.stderr)
        return 2

    settings = load_settings()
    try:
        cases = load_cases(args.cases)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not cases:
        print("错误：用例集为空", file=sys.stderr)
        return 2

    report = run(cases, settings, verbose=args.verbose)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out_dir / f"deterministic-{stamp}.json"
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summary = report["summary"]
    print(
        "确定性评估完成："
        f"用例 {summary['total']} 条，状态匹配 {summary['status_matched']} 条，"
        f"异常 {summary['exception_count']} 条。"
    )
    print(f"结果文件：{out_path}")
    print("提醒：这只覆盖确定性路径；模型行为未评估，不要把它当作“准确率”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
