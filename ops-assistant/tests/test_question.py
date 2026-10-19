"""T07–T09：问题解析与澄清（工程规格 6.1）。

覆盖点：
- T07 主问题解析：2026-09 vs 2026-08、按地区、净销售额、询问退款口径；
- T08 澄清触发：缺年份、无时间、非法月份、超出数据范围、未知地区、空问题；
- T09 维度/指标识别与相对时间（今年/上个月），以及“那华东地区呢”的追问形态。

诚实声明：未在编写环境中执行。
"""

from __future__ import annotations

from datetime import date

import pytest

from ops_assistant.domain.question import QuestionSpec, parse_question

TODAY = date(2026, 10, 19)

KNOWN_REGIONS = [
    {"region_id": "R_EAST", "region_name": "华东"},
    {"region_id": "R_NORTH", "region_name": "华北"},
    {"region_id": "R_SOUTH", "region_name": "华南"},
    {"region_id": "R_SW", "region_name": "西南"},
    {"region_id": "R_NE", "region_name": "东北"},
]

DATA_RANGE = {"min_month": "2025-01", "max_month": "2026-09"}


def parse(text: str) -> QuestionSpec:
    """按统一上下文解析，减少每个用例的重复参数。"""
    return parse_question(
        text,
        today=TODAY,
        known_regions=KNOWN_REGIONS,
        **DATA_RANGE,
    )


# ---------------------------------------------------------------------------
# T07 主问题
# ---------------------------------------------------------------------------

MAIN_QUESTION = (
    "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
)


def test_t07_main_question_is_parsed_without_clarification() -> None:
    """T07：主问题必须一次解析成功，不能要求澄清。"""
    spec = parse(MAIN_QUESTION)
    assert spec.needs_clarification is False
    assert spec.clarification is None


def test_t07_main_question_months() -> None:
    """T07：本期 2026-09，对比期 2026-08（缺年份用前一个显式年份补全）。"""
    spec = parse(MAIN_QUESTION)
    assert spec.current_month == "2026-09"
    assert spec.previous_month == "2026-08"
    assert spec.wants_comparison is True


def test_t07_main_question_dimension_metric_and_flags() -> None:
    """T07：维度=region，指标=net_sales，且识别出“销售额是否扣退款”的口径问题。"""
    spec = parse(MAIN_QUESTION)
    assert spec.dimension == "region"
    assert spec.metric == "net_sales"
    assert spec.asks_refund_definition is True
    assert spec.wants_chart is False
    assert spec.regions == ()


def test_t07_main_question_keeps_raw_text() -> None:
    """T07：raw 保留原始问题，便于溯源与日志。"""
    spec = parse(MAIN_QUESTION)
    assert spec.raw == MAIN_QUESTION


# ---------------------------------------------------------------------------
# T08 澄清
# ---------------------------------------------------------------------------


def test_t08_missing_year_requires_clarification() -> None:
    """T08：只有“九月”没有年份时必须问清是哪一年。"""
    spec = parse("九月销售额是多少？")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert "年份" in spec.clarification
    assert spec.current_month is None


def test_t08_no_time_and_no_region_requires_clarification() -> None:
    """T08：既没有时间也没有地区时，要求补充时间范围。"""
    spec = parse("销售额怎么样？")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert "时间范围" in spec.clarification


def test_t08_empty_question_requires_clarification() -> None:
    """T08：空问题给出示例式澄清，而不是崩溃。"""
    spec = parse("   ")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert spec.current_month is None


def test_t08_invalid_month_requires_clarification() -> None:
    """T08：13 月不合法，必须澄清而不是静默按 1 月处理。"""
    spec = parse("2026 年 13 月销售额比 12 月变化多少？")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert "月份不合法" in spec.clarification


def test_t08_unknown_region_requires_clarification() -> None:
    """T08：不存在的地区要列出可用地区，不能编一个地区出来。"""
    spec = parse("2026 年 9 月华中地区销售额是多少？")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert "未找到地区" in spec.clarification
    # 澄清话术应列出真实可用地区名
    assert "华东" in spec.clarification


def test_t08_out_of_range_month_requires_clarification() -> None:
    """T08：早于数据起始月的期间要说明数据范围。"""
    spec = parse("2024 年 9 月销售额比 8 月变化多少？")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert "早于数据起始月" in spec.clarification


def test_t08_future_month_requires_clarification() -> None:
    """T08：晚于数据截止月的期间要说明数据范围。"""
    spec = parse("2027 年 3 月销售额比 2 月变化多少？")
    assert spec.needs_clarification is True
    assert spec.clarification is not None
    assert "晚于数据截止月" in spec.clarification


# ---------------------------------------------------------------------------
# T09 维度、指标、相对时间与追问
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("2026 年 9 月按商品销售额是多少？", "product"),
        ("2026 年 9 月按渠道销售额是多少？", "channel"),
        ("2026 年 9 月按月看趋势", "month"),
        ("2026 年 9 月按地区分解销售额", "region"),
        ("2026 年 9 月销售额是多少？", "unknown"),
    ],
)
def test_t09_dimension_detection(question: str, expected: str) -> None:
    """T09：维度识别覆盖商品/渠道/月度/地区/未知。"""
    spec = parse(question)
    assert spec.dimension == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("2026 年 9 月毛销售额是多少？", "gross_sales"),
        ("2026 年 9 月退款额是多少？", "refunds"),
        ("2026 年 9 月销售额是多少？", "net_sales"),
    ],
)
def test_t09_metric_detection(question: str, expected: str) -> None:
    """T09：指标识别区分毛销售额、退款额与净销售额。"""
    spec = parse(question)
    assert spec.metric == expected


def test_t09_relative_month_uses_today() -> None:
    """T09：today=2026-10-19 时，“上个月”应为 2026-09。"""
    spec = parse("上个月销售额是多少？")
    assert spec.needs_clarification is False
    assert spec.current_month == "2026-09"


def test_t09_this_year_month_uses_today() -> None:
    """T09：“今年九月”按 today 的年份解析为 2026-09。"""
    spec = parse("今年九月销售额是多少？")
    assert spec.needs_clarification is False
    assert spec.current_month == "2026-09"


def test_t09_follow_up_region_only() -> None:
    """T09：追问“那华东地区呢？”只带地区，不带期间（期间由工作流沿用上一轮）。"""
    spec = parse("那华东地区呢？")
    assert spec.regions == ("R_EAST",)
    assert spec.current_month is None
    assert spec.previous_month is None
    assert spec.needs_clarification is False


def test_t09_region_id_is_recognized() -> None:
    """T09：地区编码与中文名都能识别。"""
    spec = parse("2026 年 9 月 R_NE 销售额是多少？")
    assert "R_NE" in spec.regions


def test_t09_chart_request_is_detected() -> None:
    """T09：提到“画个图”时 wants_chart=True。"""
    spec = parse("2026 年 9 月销售额比 8 月变化多少？画个图。")
    assert spec.wants_chart is True


def test_t09_single_month_with_环比_fills_previous_month() -> None:
    """T09：“9 月环比”类表达会把对比期补成 2026-08，而不是只给单月数字。"""
    spec = parse("2026 年 9 月销售额环比变化多少？")
    assert spec.needs_clarification is False
    assert spec.current_month == "2026-09"
    assert spec.previous_month == "2026-08"
    assert spec.wants_comparison is True


def test_t09_refund_definition_question_without_time_is_allowed() -> None:
    """T09：纯口径问题（“退款怎么算”）不需要时间范围，不应强行澄清时间。"""
    spec = parse("退款怎么算？净销售额包含退款吗？")
    assert spec.needs_clarification is False
    assert spec.asks_refund_definition is True
