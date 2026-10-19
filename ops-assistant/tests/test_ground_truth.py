"""T13–T16：标准答案与查询层/指标层的一致性（工程规格 6.1）。

为什么单独一个文件：
    标准答案由 ``scripts/compute_ground_truth.py`` **独立实现**（只用 sqlite3 + 原生
    SQL，不 import 项目查询层），因此它可以当作交叉验证的"第二实现"：
    这里断言"生产实现 == 独立实现"，比用实现验证自己更有意义。

覆盖点：
- T13 月份表：21 个声明月份齐全，逐月与 ``get_month_sales`` 完全一致；
- T14 地区分解：2026-09 五个地区逐项一致；
- T15 对比结果：差额、变化率、地区分解与 ``compare_months`` 一致；
- T16 边界事实：空月、缺月、真零、pending、跨月、负额、商品零记录都被
  生成器真实构造出来（而不是只写在文档里）。

诚实声明：未在编写环境中执行；标准答案 JSON 由夹具现场生成。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ops_assistant.data.queries import get_month_sales, get_region_breakdown
from ops_assistant.domain.metrics import compare_months

CURRENT = "2026-09"
PREVIOUS = "2026-08"


# ---------------------------------------------------------------------------
# T13 月份表
# ---------------------------------------------------------------------------


def test_t13_meta_declares_21_months(ground_truth: dict[str, Any]) -> None:
    """T13：meta.months 是声明的数据范围，共 21 个月（2025-01 到 2026-09）。"""
    months = ground_truth["meta"]["months"]
    assert len(months) == 21
    assert months[0] == "2025-01"
    assert months[-1] == "2026-09"


def test_t13_months_table_matches_declared_range(ground_truth: dict[str, Any]) -> None:
    """T13：months 表的键集合与 meta.months 完全一致。"""
    assert set(ground_truth["months"]) == set(ground_truth["meta"]["months"])


def test_t13_every_month_matches_queries(conn, ground_truth: dict[str, Any]) -> None:
    """T13：21 个月的 9 个字段都与 get_month_sales 一致（逐月交叉验证）。"""
    for month, expected in ground_truth["months"].items():
        actual = dataclasses.asdict(get_month_sales(conn, month))
        assert actual == expected, f"{month} 与标准答案不一致"


def test_t13_empty_month_is_zero_not_missing(ground_truth: dict[str, Any]) -> None:
    """T13：空月 2025-01 仍然出现在月份表里，值为全 0 且 has_orders=False。"""
    entry = ground_truth["months"]["2025-01"]
    assert entry["has_orders"] is False
    assert entry["order_count"] == 0
    assert entry["gross_cents"] == 0
    assert entry["refund_cents"] == 0
    assert entry["net_cents"] == 0


def test_t13_observed_months_exclude_empty_month(ground_truth: dict[str, Any]) -> None:
    """T13：真实出现过的月份不含 2025-01（区分“声明范围”与“实际记录”）。"""
    observed = ground_truth["meta"]["observed_months"]
    assert "2025-01" not in observed
    assert CURRENT in observed


# ---------------------------------------------------------------------------
# T14 地区分解
# ---------------------------------------------------------------------------


def test_t14_region_breakdown_matches_queries(conn, ground_truth: dict[str, Any]) -> None:
    """T14：2026-09 五个地区的 6 个字段都与 get_region_breakdown 一致。"""
    expected = ground_truth["regions"][CURRENT]
    actual = {item.region_id: dataclasses.asdict(item) for item in get_region_breakdown(conn, CURRENT)}
    assert set(actual) == set(expected)
    for region_id, values in expected.items():
        assert actual[region_id] == values, f"{region_id} 与标准答案不一致"


def test_t14_region_breakdown_has_five_regions(ground_truth: dict[str, Any]) -> None:
    """T14：地区分解覆盖 5 个地区。"""
    assert len(ground_truth["regions"][CURRENT]) == 5


# ---------------------------------------------------------------------------
# T15 对比结果
# ---------------------------------------------------------------------------


def test_t15_comparison_summary_matches_metrics(conn, ground_truth: dict[str, Any]) -> None:
    """T15：差额、变化率、基期零标记与 compare_months 一致。"""
    expected = ground_truth["comparison_2026_09_vs_2026_08"]
    actual = compare_months(conn, CURRENT, PREVIOUS)

    assert actual.delta_net_cents == expected["delta_net_cents"]
    assert actual.pct_change == expected["pct_change"]
    assert actual.base_zero == expected["base_zero"]


def test_t15_comparison_regions_match_metrics(conn, ground_truth: dict[str, Any]) -> None:
    """T15：每个地区的差额与贡献占比都与标准答案一致。"""
    expected = ground_truth["comparison_2026_09_vs_2026_08"]["regions"]
    expected_by_id = {item["region_id"]: item for item in expected}
    actual = compare_months(conn, CURRENT, PREVIOUS)

    assert {item.region_id for item in actual.regions} == set(expected_by_id)
    for item in actual.regions:
        values = dataclasses.asdict(item)
        target = expected_by_id[item.region_id]
        for key, value in values.items():
            assert target[key] == value, f"{item.region_id}.{key} 不一致"


def test_t15_region_sum_check_is_true(ground_truth: dict[str, Any]) -> None:
    """T15：标准答案自检行必须为真——地区差额合计 == 总差额。"""
    comparison = ground_truth["comparison_2026_09_vs_2026_08"]
    assert comparison["region_delta_sum_matches"] is True
    assert comparison["region_delta_sum_cents"] == comparison["delta_net_cents"]


# ---------------------------------------------------------------------------
# T16 边界事实
# ---------------------------------------------------------------------------


def test_t16_empty_month_edge_case(ground_truth: dict[str, Any]) -> None:
    """T16：空月事实成立，且有布尔开关便于断言。"""
    edge = ground_truth["edge_cases"]
    assert edge["empty_month_2025_01"]["is_empty"] is True
    assert edge["empty_month_2025_01"]["order_count"] == 0
    assert edge["2025-01_is_empty"] is True


def test_t16_region_months_without_orders(ground_truth: dict[str, Any]) -> None:
    """T16：东北在 2025-09 与 2026-08 都没有订单。"""
    edge = ground_truth["edge_cases"]["region_months_without_orders"]
    assert edge["all_empty"] is True
    pairs = {(item["region_id"], item["month"]) for item in edge["pairs"]}
    assert ("R_NE", "2025-09") in pairs
    assert ("R_NE", "2026-08") in pairs


def test_t16_true_zero_is_distinguishable(ground_truth: dict[str, Any]) -> None:
    """T16：西南 2026-09 是“真零”（有订单、净额为 0），不是“无记录”。"""
    edge = ground_truth["edge_cases"]["southwest_2026_09_net_zero"]
    assert edge["is_true_zero"] is True
    assert edge["order_count"] > 0
    assert edge["net_cents"] == 0
    assert edge["gross_cents"] == edge["refund_cents"]
    assert ground_truth["edge_cases"]["R_SW_2026_09_net_zero"] is True


def test_t16_pending_only_region_month(ground_truth: dict[str, Any]) -> None:
    """T16：华北 2026-07 只有 pending 退款：有记录、不扣减、approved 计数为 0。"""
    edge = ground_truth["edge_cases"]["north_2026_07_pending_only"]
    assert edge["is_pending_only"] is True
    assert edge["pending_refund_count"] > 0
    assert edge["approved_refund_count"] == 0
    assert edge["approved_refund_cents"] == 0


def test_t16_cross_month_refunds(ground_truth: dict[str, Any]) -> None:
    """T16：至少 3 笔跨月 approved 退款（8 月下单、9 月退款），含固定值那笔。"""
    edge = ground_truth["edge_cases"]["cross_month_refunds_2026_08_to_2026_09"]
    assert edge["count"] >= 3
    assert edge["meets_requirement"] is True
    assert edge["pinned"] is not None
    assert edge["pinned"]["refund_id"] == "RF_XM_PINNED"
    assert edge["pinned"]["amount_cents"] == ground_truth["meta"]["comparison_pinned"][
        "pinned_refund_amount_cents"
    ]


def test_t16_negative_net_exists(ground_truth: dict[str, Any]) -> None:
    """T16：2026-03 存在净额为负的地区（退款额大于毛销售额）。"""
    edge = ground_truth["edge_cases"]["negative_net_2026_03"]
    assert edge["has_negative"] is True
    assert edge["pinned_region_is_negative"] is True
    for item in edge["regions"]:
        assert item["net_cents"] < 0
        assert item["refund_cents"] > item["gross_cents"]


def test_t16_product_month_without_orders(ground_truth: dict[str, Any]) -> None:
    """T16：存在“某商品某月零记录”的组合（商品维度可返回 0 而不是缺失）。"""
    edge = ground_truth["edge_cases"]["product_month_without_orders"]
    assert edge["has_zero_pair"] is True
    known = edge["known_pair"]
    assert known is not None
    assert known["product_id"] == "P_012"
    assert known["month"] == "2026-05"
