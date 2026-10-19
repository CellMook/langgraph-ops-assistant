"""单元 08 实验：条件路由、修复回环、预算刹车与失败出口。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/08_routing.py
不需要模型；样例数据与项目口径一致（单位分）。
"""

from __future__ import annotations

import json
import operator
import re
import sys
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

SAMPLE_MONTHS = {
    "2026-09": {"net_cents": 1286500},
    "2026-08": {"net_cents": 1190000},
    "2026-07": {"net_cents": 1102000},
}
DATA_MIN, DATA_MAX = "2025-01", "2026-09"


class OpsState(TypedDict, total=False):
    question: str
    spec: dict
    route: str
    data_result: dict
    calculation: dict
    status: str
    clarification: str | None
    errors: Annotated[list[str], operator.add]
    trace: Annotated[list[dict], operator.add]
    budget: dict


def initial_budget(max_steps: int = 6) -> dict:
    return {"steps": 0, "max_steps": max_steps, "tool_calls": 0,
            "max_tool_calls": 8, "repairs": 0, "max_repairs": 1}


def bump(state: OpsState) -> dict:
    """步数 +1；返回新 dict，不修改传入的 budget。"""
    budget = dict(state.get("budget") or {})
    budget["steps"] = budget.get("steps", 0) + 1
    return budget


# ---------- 纯函数：解析与路由（可脱离图单测） ----------

def parse_spec(question: str) -> dict:
    """极简规则解析：月份 + 是否问退款口径 + 是否要求不支持的渠道维度。"""
    spec: dict = {"raw": question, "metric": "net_sales", "dimension": "region",
                  "needs_clarification": False, "unsupported": False,
                  "asks_refund_definition": "退款" in question and "口径" in question}
    if "渠道" in question:
        spec["dimension"] = "channel"
        spec["unsupported"] = True
        return spec
    month = None
    for token, value in (("九月", "2026-09"), ("八月", "2026-08"), ("七月", "2026-07")):
        if token in question:
            month = value
            break
    if month is None:
        match = re.search(r"(20\d{2})-(0[1-9]|1[0-2])", question)
        month = match.group(0) if match else None
    if month is None:
        spec["needs_clarification"] = True
        spec["clarification"] = (
            f"请说明要分析的月份。可用数据范围：{DATA_MIN} ～ {DATA_MAX}；"
            "例如“2026 年九月”。"
        )
        return spec
    if not (DATA_MIN <= month <= DATA_MAX):
        spec["needs_clarification"] = True
        spec["clarification"] = (
            f"月份 {month} 超出数据范围 {DATA_MIN} ～ {DATA_MAX}，请确认年份。"
        )
        return spec
    year, mon = month.split("-")
    previous = f"{year}-{int(mon) - 1:02d}" if int(mon) > 1 else f"{int(year) - 1}-12"
    spec["current_month"] = month
    spec["previous_month"] = previous if previous >= DATA_MIN else None
    return spec


def route_after_understand(state: OpsState) -> str:
    """纯函数：预算 → 不支持 → 澄清 → 知识 → 取数。"""
    budget = state.get("budget") or {}
    if budget.get("steps", 0) > budget.get("max_steps", 0):
        return "fail"
    spec = state.get("spec") or {}
    if spec.get("unsupported"):
        return "fail"
    if spec.get("needs_clarification"):
        return "clarify"
    if spec.get("asks_refund_definition"):
        return "knowledge"
    return "data"


def route_after_data(state: OpsState) -> str:
    """纯函数：预算 → 修复次数 → 节点声明的下一跳（白名单校验）。"""
    budget = state.get("budget") or {}
    if budget.get("steps", 0) > budget.get("max_steps", 0):
        return "fail"
    if budget.get("repairs", 0) > budget.get("max_repairs", 1):
        return "fail"
    route = state.get("route", "fail")
    return route if route in {"compute", "repair", "fail"} else "fail"


# ---------- 节点 ----------

def node_understand(state: OpsState) -> dict:
    budget = bump(state)
    spec = parse_spec(state.get("question", ""))
    route = route_after_understand({**state, "spec": spec, "budget": budget})
    errors: list[str] = []
    if spec.get("unsupported"):
        errors.append(
            f"UNSUPPORTED_DIMENSION: 当前数据不支持 {spec.get('dimension')} 维度，"
            "可用维度：region / product / month"
        )
    return {
        "spec": spec,
        "route": route,
        "budget": budget,
        "status": "running",
        "errors": errors,
        "trace": [{"node": "understand", "action": "decided_route", "route": route}],
    }


def node_clarify(state: OpsState) -> dict:
    return {
        "status": "clarification",
        "clarification": (state.get("spec") or {}).get("clarification"),
        "trace": [{"node": "clarify", "action": "asked_for_missing_info"}],
    }


def node_knowledge(state: OpsState) -> dict:
    return {
        "route": "data",
        "trace": [{"node": "knowledge", "action": "loaded_policy_context",
                   "sources": ["01_指标口径#0", "02_退款与归属#0"]}],
    }


def node_data(state: OpsState) -> dict:
    budget = bump(state)
    spec = state.get("spec") or {}
    month = spec.get("current_month")
    # 模拟一次可重试的数据库错误：第一次访问走 repair，修复后第二次成功
    if spec.get("simulate_operational_error") and budget.get("repairs", 0) == 0:
        return {
            "route": "repair",
            "budget": budget,
            "errors": ["SQLITE_OPERATIONAL: database is locked（模拟，可重试）"],
            "trace": [{"node": "data", "action": "retryable_error"}],
        }
    if month not in SAMPLE_MONTHS:
        return {
            "route": "fail",
            "budget": budget,
            "errors": [f"NO_DATA: 没有 {month} 的数据"],
            "trace": [{"node": "data", "action": "no_data", "month": month}],
        }
    previous = spec.get("previous_month")
    if previous not in SAMPLE_MONTHS:
        return {
            "route": "fail",
            "budget": budget,
            "errors": [f"NO_DATA: 没有 {previous} 的数据，无法比较"],
            "trace": [{"node": "data", "action": "no_previous", "month": previous}],
        }
    return {
        "route": "compute",
        "budget": budget,
        "data_result": {
            "months": {previous: SAMPLE_MONTHS[previous], month: SAMPLE_MONTHS[month]},
            "unit": "cents",
        },
        "trace": [{"node": "data", "action": "loaded_months", "months": [previous, month]}],
    }


def node_repair(state: OpsState) -> dict:
    budget = bump(state)
    budget["repairs"] = budget.get("repairs", 0) + 1
    return {
        "route": "data",
        "budget": budget,
        "trace": [{"node": "repair", "action": "retried_once",
                   "repairs": budget["repairs"]}],
    }


def node_compute(state: OpsState) -> dict:
    budget = bump(state)
    months = state["data_result"]["months"]
    spec = state["spec"]
    current = months[spec["current_month"]]["net_cents"]
    previous = months[spec["previous_month"]]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    return {
        "budget": budget,
        "calculation": {"current_cents": current, "previous_cents": previous,
                        "delta_cents": delta, "pct_change": pct},
        "status": "answered",
        "trace": [{"node": "compute", "action": "calculated", "delta_cents": delta}],
    }


def node_fail(state: OpsState) -> dict:
    budget = state.get("budget") or {}
    exceeded = budget.get("steps", 0) > budget.get("max_steps", 0) or \
        budget.get("repairs", 0) > budget.get("max_repairs", 1)
    return {
        "status": "budget_exceeded" if exceeded else "failed",
        "trace": [{"node": "fail", "action": "budget_exceeded" if exceeded else "failed",
                   "steps": budget.get("steps"), "max_steps": budget.get("max_steps")}],
    }


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("understand", node_understand)
    builder.add_node("clarify", node_clarify)
    builder.add_node("knowledge", node_knowledge)
    builder.add_node("data", node_data)
    builder.add_node("repair", node_repair)
    builder.add_node("compute", node_compute)
    builder.add_node("fail", node_fail)

    builder.add_edge(START, "understand")
    builder.add_conditional_edges(
        "understand",
        route_after_understand,
        {"clarify": "clarify", "knowledge": "knowledge", "data": "data", "fail": "fail"},
    )
    builder.add_conditional_edges(
        "data",
        route_after_data,
        {"compute": "compute", "repair": "repair", "fail": "fail"},
    )
    builder.add_edge("knowledge", "data")
    builder.add_edge("repair", "data")
    builder.add_edge("clarify", END)
    builder.add_edge("compute", END)
    builder.add_edge("fail", END)
    return builder.compile()


def run_case(graph, name: str, question: str, *, max_steps: int = 6) -> dict:
    print(f"\n--- {name} ---")
    print("问题：", question)
    initial = {
        "question": question,
        "status": "running",
        "errors": [],
        "trace": [],
        "budget": initial_budget(max_steps),
    }
    try:
        final = graph.invoke(initial)
    except Exception as exc:
        print(f"图执行异常：{type(exc).__name__}: {exc}")
        return {"status": "exception", "error_type": type(exc).__name__}
    print("status =", final.get("status"))
    if final.get("clarification"):
        print("clarification =", final["clarification"])
    if final.get("calculation"):
        print("calculation =", json.dumps(final["calculation"], ensure_ascii=False))
    if final.get("errors"):
        print("errors =", final["errors"])
    print("budget =", json.dumps(final["budget"], ensure_ascii=False))
    print("trace =")
    for entry in final.get("trace", []):
        print("  ", json.dumps(entry, ensure_ascii=False))
    return final


def demo_route_unit_tests() -> None:
    print("\n== 路由函数单测（不需要图） ==")
    base = {"budget": {"steps": 1, "max_steps": 5}}
    cases = [
        ("正常取数", {**base, "spec": {}}, "data"),
        ("缺月份", {**base, "spec": {"needs_clarification": True}}, "clarify"),
        ("渠道不支持", {**base, "spec": {"unsupported": True}}, "fail"),
        ("问退款口径", {**base, "spec": {"asks_refund_definition": True}}, "knowledge"),
        ("预算耗尽", {"budget": {"steps": 9, "max_steps": 5}, "spec": {}}, "fail"),
    ]
    for label, state, expected in cases:
        actual = route_after_understand(state)
        flag = "OK" if actual == expected else "FAIL"
        print(f"  [{flag}] {label}: 期望 {expected}, 实际 {actual}")


def main() -> int:
    print("== 单元 08 实验：条件路由、循环、预算与失败出口 ==")
    graph = build_graph()

    demo_route_unit_tests()

    run_case(graph, "正常：九月 vs 八月", "2026 年九月销售额比八月变化多少？")
    run_case(graph, "澄清：没给月份", "销售额变化多少？")
    run_case(graph, "失败：不支持的渠道维度", "2026 年九月按渠道销售额")
    run_case(graph, "预算：max_steps=0", "2026 年九月销售额比八月变化多少？", max_steps=0)

    print("\n--- 修复回环：一次可重试错误 ---")
    print("说明：为触发 repair，需要 spec 里 simulate_operational_error=True；")
    print("      本实验用 '坏数据' 关键字在第 5.4 节的版本里演示，这里直接改初始状态。")
    initial = {
        "question": "2026 年九月销售额比八月变化多少？",
        "status": "running",
        "errors": [],
        "trace": [],
        "budget": initial_budget(6),
    }
    # 手工把 simulate_operational_error 塞进 spec 不方便（spec 由 understand 生成），
    # 因此这里走“直接调用 data 节点”的方式验证 repair 语义：
    state_after_understand = node_understand(initial)
    spec = dict(state_after_understand["spec"])
    spec["simulate_operational_error"] = True
    state_after_understand["spec"] = spec
    first_data = node_data(state_after_understand)
    print("第一次 data → route =", first_data["route"], "errors =", first_data.get("errors"))
    repaired = node_repair({**state_after_understand, **first_data})
    second_data = node_data({**state_after_understand, **first_data, **repaired})
    print("repair 后 data → route =", second_data["route"], "months =",
          sorted(second_data["data_result"]["months"]))

    print("\n--- recursion_limit（框架保险，预期抛异常） ---")
    try:
        graph.invoke(initial, config={"recursion_limit": 2})
        print("未抛异常（说明该版本按其他方式处理，请记录实际行为）")
    except Exception as exc:
        print(f"异常类型：{type(exc).__name__}: {exc}")

    print("\n[结论] 分支靠纯函数路由；循环靠 budget + recursion_limit 双保险；失败统一走 fail 出口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
