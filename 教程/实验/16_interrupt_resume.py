"""单元 16 实验：interrupt 与恢复（approve / edit / reject）。

迷你图：draft → review（interrupt）→ export（只有批准/编辑才走）。
不导入 ops_assistant、不联网、不需要模型。

用法：
    cd ops-assistant
    uv run python ../教程/实验/16_interrupt_resume.py
"""

from __future__ import annotations

import json
import operator
import re
import shutil
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
REPORT_PATH = OUTPUT_DIR / "report-demo.md"

APPROVAL_OPTIONS = ("approve", "reject", "edit")
ALIASES = {
    "approve": "approve",
    "approved": "approve",
    "ok": "approve",
    "yes": "approve",
    "通过": "approve",
    "批准": "approve",
    "reject": "reject",
    "rejected": "reject",
    "no": "reject",
    "拒绝": "reject",
    "驳回": "reject",
    "edit": "edit",
    "modify": "edit",
    "修改": "edit",
    "编辑": "edit",
}

# 模块级列表：用来观察"节点被重复执行"（真实项目里应改成 trace 字段）
REPLAYS: list[str] = []


class St(TypedDict, total=False):
    """迷你状态：草稿、决策、最终文本、状态、错误。"""

    topic: str
    draft: str
    decision: str
    comment: str
    final: str
    status: str
    errors: Annotated[list[str], operator.add]


def edited_or_draft(edited: str, draft: str) -> tuple[str, list[str]]:
    """正文不可改；只允许追加不含数字的备注。"""
    text = str(edited or "").strip()
    if not text.startswith(draft):
        raise ValueError("正文只读，只能追加人工备注")
    note = text[len(draft) :]
    if re.search(r"[\d一二三四五六七八九十百千万亿零〇两%％]|https?://|\[来源", note):
        raise ValueError("备注不能新增数字、百分比、URL 或引用")
    return text, []


def parse_resume(value: Any) -> tuple[str, str, str]:
    """把恢复值归一成 (decision, comment, edited_report)；无法识别返回空串。"""
    if isinstance(value, str):
        return ALIASES.get(value.strip().lower(), ""), "", ""
    if not isinstance(value, dict):
        return "", "", ""
    decision = str(value.get("decision") or value.get("action") or "").strip().lower()
    comment = str(value.get("comment") or value.get("feedback") or "").strip()
    edited = value.get("edited_report")
    if edited is None:
        edited = value.get("report_markdown")
    return ALIASES.get(decision, ""), comment, str(edited or "").strip()


def draft_node(state: St) -> dict[str, Any]:
    """渲染一份固定草稿（真实项目里由 report/format.py 渲染）。"""
    REPLAYS.append("draft")
    topic = str(state.get("topic") or "（未指定）")
    draft = (
        f"# 经营分析报告：{topic}\n"
        "## 结论\n"
        "本期净销售额 1,234.00 元，比上期减少 12.5%。\n"
        "## 明细\n"
        "差额 -176.00 元，其中退款 200.00 元。"
    )
    return {"draft": draft, "status": "awaiting_approval"}


def review_node(state: St) -> dict[str, Any]:
    """审批节点：interrupt 之前只做纯读/纯算，恢复后只写状态。"""
    REPLAYS.append("review:before_interrupt")
    payload = {
        "type": "report_approval",
        "topic": str(state.get("topic") or ""),
        "options": list(APPROVAL_OPTIONS),
        "summary": "本期 1,234.00 元 / 上期 1,410.00 元 / 差额 -176.00 元",
        "report_markdown": str(state.get("draft") or ""),
        "instructions": "approve 导出；edit 提供修改稿后导出（正文只读，仅追加无数字备注）；reject 不导出。",
    }
    value = interrupt(payload)
    REPLAYS.append("review:after_interrupt")

    decision, comment, edited = parse_resume(value)
    errors: list[str] = []
    if decision not in APPROVAL_OPTIONS:
        errors.append(
            f"INVALID_DECISION: 无法识别“{decision or '（空）'}”，按拒绝处理以免误导出文件。"
        )
        decision = "reject"

    if decision == "reject":
        return {
            "decision": "reject",
            "comment": comment,
            "final": "",
            "status": "rejected",
            "errors": errors,
        }

    draft = str(state.get("draft") or "")
    final, notes = edited_or_draft(edited, draft) if decision == "edit" else (draft, [])
    errors.extend(notes)
    return {
        "decision": decision,
        "comment": comment,
        "final": final,
        "status": "approved",
        "errors": errors,
    }


def export_node(state: St) -> dict[str, Any]:
    """唯一的落盘节点；真实项目用"临时文件 + os.replace"原子写。"""
    REPLAYS.append("export")
    text = str(state.get("final") or state.get("draft") or "")
    REPORT_PATH.write_text(text + "\n", encoding="utf-8")
    return {"status": "approved", "final": text}


def route_after_review(state: St) -> str:
    """批准或编辑才导出，其它一律结束。"""
    return (
        "export" if str(state.get("decision") or "") in {"approve", "edit"} else "end"
    )


def build_graph(checkpointer: Any) -> Any:
    """装配迷你图：draft → review → (export | END)。"""
    builder = StateGraph(St)
    builder.add_node("draft", draft_node)
    builder.add_node("review", review_node)
    builder.add_node("export", export_node)
    builder.add_edge(START, "draft")
    builder.add_edge("draft", "review")
    builder.add_conditional_edges(
        "review", route_after_review, {"export": "export", "end": END}
    )
    builder.add_edge("export", END)
    return builder.compile(checkpointer=checkpointer)


def extract_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """从状态快照里取待处理中断（与项目 _extract_interrupt 同构的简化版）。"""
    candidates: list[Any] = []
    raw = getattr(snapshot, "interrupts", None)
    if raw:
        candidates.extend(list(raw))
    if not candidates:
        for task in getattr(snapshot, "tasks", ()) or ():
            for item in getattr(task, "interrupts", ()) or ():
                candidates.append(item)
    if not candidates:
        return None
    first = candidates[0]
    if isinstance(first, dict):
        return {"id": str(first.get("id") or ""), "payload": first.get("value")}
    return {
        "id": str(getattr(first, "id", "") or ""),
        "payload": getattr(first, "value", None),
    }


def resume_mini(graph: Any, config: dict[str, Any], value: Any) -> dict[str, Any]:
    """模拟项目 resume_turn：先确认有 pending interrupt，再 Command(resume=...)。"""
    snapshot = graph.get_state(config)
    pending = extract_interrupt(snapshot)
    values = dict(snapshot.values or {})
    if pending is None:
        if values.get("status") == "approved" and values.get("final"):
            return values
        raise ValueError("该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。")
    decision, _, edited = parse_resume(value)
    if decision == "edit":
        edited_or_draft(edited, str(values.get("draft") or ""))
    graph.invoke(Command(resume=value), config=config)
    return dict(graph.get_state(config).values)


def config_for(thread_id: str) -> dict[str, Any]:
    """thread_id 决定检查点归属；没有它 interrupt 无处可存。"""
    return {"configurable": {"thread_id": thread_id}}


def main() -> int:
    """依次演示：暂停 → approve → reject → edit（追加/拒绝）→ 非法值 → 幂等 → 报错。"""
    print("== 单元 16 实验：interrupt 与恢复 ==")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    graph = build_graph(InMemorySaver())

    print("\n== 1) 第一次 invoke：图停在 review 的 interrupt 上 ==")
    cfg1 = config_for("t1")
    graph.invoke({"topic": "九月华东净销售额"}, config=cfg1)
    snap = graph.get_state(cfg1)
    pending = extract_interrupt(snap)
    print("status =", snap.values["status"])
    print("next =", snap.next)
    print("interrupt.payload.type =", pending["payload"]["type"])
    print("interrupt.payload.options =", pending["payload"]["options"])
    print(
        "payload 可 JSON 序列化 =",
        bool(json.dumps(pending["payload"], ensure_ascii=False)),
    )
    print("REPLAYS =", REPLAYS)

    print("\n== 2) resume approve：继续跑 export ==")
    after = resume_mini(graph, cfg1, "approve")
    print("status =", after["status"])
    print("final 非空 =", bool(after["final"]))
    print("REPLAYS =", REPLAYS)
    print("review 节点被执行的次数 =", REPLAYS.count("review:before_interrupt"))

    print("\n== 3) resume reject：不进 export，不写文件 ==")
    cfg2 = config_for("t2")
    graph.invoke({"topic": "八月西南退款"}, config=cfg2)
    rejected = resume_mini(graph, cfg2, {"decision": "reject", "comment": "口径不对"})
    print("status =", rejected["status"])
    print("final =", repr(rejected["final"]))
    print("最后执行的是 =", REPLAYS[-1])

    print("\n== 4) resume edit：追加无数字备注会被采纳 ==")
    cfg3 = config_for("t3")
    graph.invoke({"topic": "九月西南净销售额"}, config=cfg3)
    draft3 = str(graph.get_state(cfg3).values["draft"])
    edited_ok = draft3 + "\n（本报告已由运营负责人复核）"
    edited_out = resume_mini(
        graph, cfg3, {"decision": "edit", "edited_report": edited_ok}
    )
    print("status =", edited_out["status"])
    print("errors =", edited_out["errors"])
    print("采纳了修改稿 =", "运营负责人复核" in edited_out["final"])

    print("\n== 5) resume edit：改了正文会被拒绝 ==")
    cfg4 = config_for("t4")
    graph.invoke({"topic": "九月华南净销售额"}, config=cfg4)
    draft4 = str(graph.get_state(cfg4).values["draft"])
    bad = draft4.replace("1,234.00", "9,999.00")
    try:
        resume_mini(graph, cfg4, {"decision": "edit", "edited_report": bad})
    except ValueError as exc:
        print("ValueError:", exc)
    print("仍待审批 =", bool(extract_interrupt(graph.get_state(cfg4))))
    assert graph.get_state(cfg4).values["status"] == "awaiting_approval"

    print("\n== 6) 无法识别的结论：按拒绝处理 ==")
    cfg5 = config_for("t5")
    graph.invoke({"topic": "九月华北净销售额"}, config=cfg5)
    unknown = resume_mini(graph, cfg5, "同意")
    print("status =", unknown["status"])
    print("errors =", unknown["errors"])

    print("\n== 7) 重复 resume 已批准的会话：幂等返回同一结果 ==")
    again = resume_mini(graph, cfg1, "approve")
    print("status =", again["status"])
    print("final 与第一次相同 =", again["final"] == after["final"])

    print("\n== 8) resume 一个没有待审批的会话：报错而不是瞎跑 ==")
    try:
        resume_mini(graph, cfg2, "approve")
    except ValueError as exc:
        print("ValueError:", exc)

    print("\n== 9) 落盘结果：只有 approve/edit 的两条线程各写了一次 ==")
    print("_output 下的文件 =", sorted(path.name for path in OUTPUT_DIR.iterdir()))
    print(
        "文件里含人工复核尾注 =",
        "运营负责人复核" in REPORT_PATH.read_text(encoding="utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
