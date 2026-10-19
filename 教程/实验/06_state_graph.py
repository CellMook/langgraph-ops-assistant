"""单元 06 实验：用 StateGraph 把“九月 vs 八月”拆成三个节点。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/06_state_graph.py
不需要模型：三个节点都是纯函数，数字来自脚本内置的样例数据（与项目口径一致）。
"""

from __future__ import annotations

import json
import sys
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

# 样例数据：与项目 seed 数据的口径一致（单位：分）
SAMPLE_MONTHS = {
    "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500, "order_count": 57},
    "2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000, "order_count": 61},
}


class OpsState(TypedDict, total=False):
    """本实验的状态：字段命名刻意与项目 AssistantState 对齐。"""

    question: str
    spec: dict
    data_result: dict
    calculation: dict
    status: str
    errors: list[str]
    trace: list[dict]


def node_parse(state: OpsState) -> dict:
    """解析问题：本实验用固定结果，真实项目由纯规则 parse_question 完成。"""
    spec = {
        "raw": state.get("question", ""),
        "current_month": "2026-09",
        "previous_month": "2026-08",
        "metric": "net_sales",
        "dimension": "region",
        "needs_clarification": False,
    }
    trace = [{"node": "parse", "action": "parsed_spec", "current": "2026-09", "previous": "2026-08"}]
    return {"spec": spec, "trace": trace, "status": "running"}


def node_fetch(state: OpsState) -> dict:
    """取数：真实项目调用 queries.get_month_sales(conn, month)。"""
    spec = state["spec"]
    months = [spec["previous_month"], spec["current_month"]]
    data = {month: SAMPLE_MONTHS[month] for month in months}
    trace = [{"node": "fetch", "action": "loaded_months", "months": months}]
    return {"data_result": {"months": data, "unit": "cents"}, "trace": trace}


def node_compute(state: OpsState) -> dict:
    """计算：真实项目由 domain/metrics.py 的纯函数完成。"""
    months = state["data_result"]["months"]
    current = months["2026-09"]["net_cents"]
    previous = months["2026-08"]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    calculation = {
        "current_month": "2026-09",
        "previous_month": "2026-08",
        "current_cents": current,
        "previous_cents": previous,
        "delta_cents": delta,
        "pct_change": pct,
        "base_zero": previous == 0,
        "formula": "delta = net(current) - net(previous)",
    }
    trace = [{"node": "compute", "action": "calculated", "delta_cents": delta, "pct_change": pct}]
    return {"calculation": calculation, "trace": trace, "status": "answered"}


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("parse", node_parse)
    builder.add_node("fetch", node_fetch)
    builder.add_node("compute", node_compute)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", "fetch")
    builder.add_edge("fetch", "compute")
    builder.add_edge("compute", END)
    return builder.compile()


def main() -> int:
    print("== 单元 06 实验：StateGraph 三节点直线图 ==")
    graph = build_graph()
    initial = {
        "question": "2026 年九月净销售额比八月变化多少？",
        "status": "running",
        "errors": [],
    }

    print("入口状态：", json.dumps(initial, ensure_ascii=False))
    print("\n--- stream(updates)：每个节点跑完各产出一条 ---")
    for update in graph.stream(initial, stream_mode="updates"):
        for node_name, node_output in update.items():
            print(f"[{node_name}] 返回键：{sorted(node_output)}")
            print("    ", json.dumps(node_output, ensure_ascii=False))

    print("\n--- invoke：返回最终完整状态 ---")
    final = graph.invoke(initial)
    print("最终状态键：", sorted(final))
    print("status =", final["status"])
    print("spec =", json.dumps(final["spec"], ensure_ascii=False))
    print("calculation =", json.dumps(final["calculation"], ensure_ascii=False))

    print("\n--- 校验：三个节点的 trace 是否都在？ ---")
    nodes_in_trace = [item["node"] for item in final["trace"]]
    print("trace 里保留的节点 =", nodes_in_trace)
    print("注意：这里每个节点都返回了整条 trace 列表，最后一个节点的返回值会覆盖前面的。")
    print("     这正是单元 07 要解决的“追加而不是覆盖”问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
