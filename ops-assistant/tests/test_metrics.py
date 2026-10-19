"""T10–T12：指标比较与差额分解（工程规格 6.1）。

覆盖点：
- T10 主对比：2026-09 vs 2026-08 的净额差额、变化率、地区分解合计校验；
- T11 地区过滤：只分析指定地区时，本期/上期汇总必须按过滤后重算；
- T12 边界：零基数不抛异常、渠道维度不崩且明确不支持、非法指标报错。

断言里的期望金额不写死魔法数字：优先与 ``ground_truth`` 夹具（独立实现）交叉验证，
这样"实现"与"标准答案"必须同时错才会漏测。

诚实声明：未在编写环境中执行。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from ops_assistant.data.queries import get_region_breakdown
from ops_assistant.domain.metrics import (
    ComparisonResult,
    RegionDelta,
    compare_months,
)

CURRENT = "2026-09"
PREVIOUS = "2026-08"


# ---------------------------------------------------------------------------
# T10 主对比
# ---------------------------------------------------------------------------


def test_t10_comparison_matches_ground_truth_delta(conn, ground_truth: dict[str, Any]) -> None:
    """T10：总差额与独立标准答案脚本的结果完全一致（整数分）。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    expected = ground_truth["comparison_2026_09_vs_2026_08"]
    assert result.delta_net_cents == expected["delta_net_cents"]


def test_t10_comparison_current_previous_match_ground_truth(conn, ground_truth: dict[str, Any]) -> None:
    """T10：本期/上期的整月汇总与标准答案逐字段一致。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    expected = ground_truth["comparison_2026_09_vs_2026_08"]
    assert dataclasses.asdict(result.current) == expected["current"]
    assert dataclasses.asdict(result.previous) == expected["previous"]


def test_t10_delta_equals_current_minus_previous(conn) -> None:
    """T10：delta_net_cents 必须等于本期净额减上期净额。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    assert result.delta_net_cents == result.current.net_cents - result.previous.net_cents


def test_t10_pct_change_uses_absolute_previous(conn) -> None:
    """T10：变化率 = 差额 / |上期净额|，上期非 0 时不是 None。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    assert result.base_zero is False
    assert result.pct_change is not None
    expected = result.delta_net_cents / abs(result.previous.net_cents)
    assert result.pct_change == pytest.approx(expected, rel=1e-12)


def test_t10_region_deltas_sum_to_total(conn) -> None:
    """T10：地区差额之和必须等于总差额（口径闭环，规格 4.7）。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    assert result.region_delta_sum_cents == result.delta_net_cents


def test_t10_region_deltas_cover_all_five_regions(conn) -> None:
    """T10：地区分解覆盖全部 5 个地区（含本月无记录但上月有记录的地区）。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    assert len(result.regions) == 5
    assert {item.region_id for item in result.regions} == {
        "R_EAST",
        "R_NORTH",
        "R_SOUTH",
        "R_SW",
        "R_NE",
    }


def test_t10_each_region_delta_is_current_minus_previous(conn) -> None:
    """T10：每个地区的差额 = 本期净额 - 上期净额。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    for item in result.regions:
        assert item.delta_cents == item.current_net_cents - item.previous_net_cents


def test_t10_share_of_delta_sums_to_one(conn) -> None:
    """T10：总差额非 0 时，各地区贡献占比之和应为 1。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    assert result.delta_net_cents != 0
    shares = [item.share_of_delta for item in result.regions]
    assert all(share is not None for share in shares)
    assert sum(share for share in shares if share is not None) == pytest.approx(1.0, abs=1e-9)


def test_t10_regions_sorted_by_absolute_delta(conn) -> None:
    """T10：地区按差额绝对值降序排列，便于报告突出主要贡献者。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    keys = [(-abs(item.delta_cents), item.region_id) for item in result.regions]
    assert keys == sorted(keys)


def test_t10_result_is_dataclass_types(conn) -> None:
    """T10：返回类型是对外承诺的 dataclass，而不是裸字典。"""
    result = compare_months(conn, CURRENT, PREVIOUS)
    assert isinstance(result, ComparisonResult)
    assert all(isinstance(item, RegionDelta) for item in result.regions)


# ---------------------------------------------------------------------------
# T11 地区过滤
# ---------------------------------------------------------------------------


def test_t11_region_filter_recomputes_month_totals(conn) -> None:
    """T11：只分析华东时，本期/上期汇总必须是华东的值，而不是全国的值。"""
    result = compare_months(conn, CURRENT, PREVIOUS, region_ids=("R_EAST",))
    regions = {item.region_id: item for item in get_region_breakdown(conn, CURRENT)}
    assert result.current.net_cents == regions["R_EAST"].net_cents


def test_t11_region_filter_keeps_only_selected_region(conn) -> None:
    """T11：过滤后地区分解只含被选中的地区。"""
    result = compare_months(conn, CURRENT, PREVIOUS, region_ids=("R_EAST",))
    assert len(result.regions) == 1
    assert result.regions[0].region_id == "R_EAST"


def test_t11_region_filter_keeps_delta_consistency(conn) -> None:
    """T11：过滤后依然满足“地区差额之和 == 总差额”。"""
    result = compare_months(conn, CURRENT, PREVIOUS, region_ids=("R_EAST", "R_SOUTH"))
    assert result.region_delta_sum_cents == result.delta_net_cents


def test_t11_region_filter_delta_not_equal_to_national_delta(conn) -> None:
    """T11：单地区差额不应等于全国差额（防止忘记重算的典型 bug）。"""
    national = compare_months(conn, CURRENT, PREVIOUS)
    east_only = compare_months(conn, CURRENT, PREVIOUS, region_ids=("R_EAST",))
    assert east_only.delta_net_cents != national.delta_net_cents


# ---------------------------------------------------------------------------
# T12 边界与不支持维度
# ---------------------------------------------------------------------------


def test_t12_zero_base_returns_none_pct(conn) -> None:
    """T12：基期为 0 时变化率无定义，返回 None 并标记 base_zero。

    东北在 2026-08 无订单、无退款，是现成的零基数场景。
    """
    result = compare_months(conn, CURRENT, PREVIOUS, region_ids=("R_NE",))
    assert result.previous.net_cents == 0
    assert result.base_zero is True
    assert result.pct_change is None


def test_t12_zero_base_adds_note(conn) -> None:
    """T12：零基数必须在 notes 里明确说明，避免读者误读变化率。"""
    result = compare_months(conn, CURRENT, PREVIOUS, region_ids=("R_NE",))
    assert any("基期" in note for note in result.notes)


def test_t12_channel_dimension_is_explicitly_unsupported(conn) -> None:
    """T12：按渠道不做差额分解，但必须返回明确标记而不是抛异常。"""
    result = compare_months(conn, CURRENT, PREVIOUS, dimension="channel")
    assert result.unsupported_dimension == "channel"
    assert result.regions == ()
    assert any("只支持按地区" in note for note in result.notes)
    # 总数仍然要算出来，供报告使用
    assert result.delta_net_cents == result.current.net_cents - result.previous.net_cents


def test_t12_unknown_dimension_defaults_to_region(conn) -> None:
    """T12：维度未知时按地区处理（规格 5.3），而不是拒绝回答。"""
    result = compare_months(conn, CURRENT, PREVIOUS, dimension="unknown")
    assert result.unsupported_dimension is None
    assert len(result.regions) == 5


def test_t12_invalid_metric_raises_value_error(conn) -> None:
    """T12：非法指标必须显式报错，避免用错口径静默出数。"""
    with pytest.raises(ValueError):
        compare_months(conn, CURRENT, PREVIOUS, metric="profit")


def test_t12_gross_metric_uses_gross_values(conn) -> None:
    """T12：metric=gross_sales 时用毛额做差额，与净额结果不同。"""
    net_result = compare_months(conn, CURRENT, PREVIOUS)
    gross_result = compare_months(conn, CURRENT, PREVIOUS, metric="gross_sales")
    assert gross_result.current.gross_cents == net_result.current.gross_cents
    assert gross_result.delta_net_cents == (
        gross_result.current.gross_cents - gross_result.previous.gross_cents
    )


def test_t12_refunds_metric_uses_refund_values(conn) -> None:
    """T12：metric=refunds 时差额基于退款额。"""
    result = compare_months(conn, CURRENT, PREVIOUS, metric="refunds")
    assert result.delta_net_cents == (
        result.current.refund_cents - result.previous.refund_cents
    )
