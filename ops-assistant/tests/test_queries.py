"""T03–T06：查询层口径（工程规格 6.1）。

覆盖点：
- T03 月份参数校验：非法格式抛 ValueError，边界月份可用；
- T04 金额恒等式：net = gross - refund，退款只算 approved 且按 refund_date 归属；
- T05 维度完整性：地区分解恒返回 5 个地区、商品维度恒返回 12 个商品；
- T06 边界语义：无记录月返回全 0 且 has_orders=False；pending 只计数不计额。

诚实声明：未在编写环境中执行。
"""

from __future__ import annotations

import pytest

from ops_assistant.data.queries import (
    MonthSales,
    get_month_sales,
    get_product_sales,
    get_region,
    get_region_breakdown,
    list_available_months,
    list_regions,
)

INVALID_MONTHS = ["2026-9", "2026-13", "2026-00", "202609", "2026/09", "", "abc"]


# ---------------------------------------------------------------------------
# T03 月份校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_month", INVALID_MONTHS)
def test_t03_get_month_sales_rejects_invalid_month(conn, bad_month: str) -> None:
    """T03：非法月份格式必须抛 ValueError，而不是悄悄返回 0。"""
    with pytest.raises(ValueError):
        get_month_sales(conn, bad_month)


@pytest.mark.parametrize("bad_month", INVALID_MONTHS)
def test_t03_region_breakdown_rejects_invalid_month(conn, bad_month: str) -> None:
    """T03：地区分解同样校验月份格式。"""
    with pytest.raises(ValueError):
        get_region_breakdown(conn, bad_month)


def test_t03_boundary_months_are_accepted(conn) -> None:
    """T03：数据首月与末月都能正常查询（不因边界抛异常）。"""
    first = get_month_sales(conn, "2025-01")
    last = get_month_sales(conn, "2026-09")
    assert first.month == "2025-01"
    assert last.month == "2026-09"


# ---------------------------------------------------------------------------
# T04 金额恒等式与退款归属
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("month", ["2026-08", "2026-09", "2026-03", "2025-12"])
def test_t04_net_equals_gross_minus_refund(conn, month: str) -> None:
    """T04：任意月份的 net_cents 必须等于 gross_cents - refund_cents。"""
    sales = get_month_sales(conn, month)
    assert sales.net_cents == sales.gross_cents - sales.refund_cents


def test_t04_refund_only_counts_approved_by_refund_date(conn) -> None:
    """T04：退款额只统计 approved，且按 refund_date 落在当月。

    做法：直接用原生 SQL 算一遍（与查询层实现独立），再比对。
    """
    month = "2026-09"
    expected = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds"
        " WHERE status = 'approved' AND refund_date >= ? AND refund_date < ?",
        (f"{month}-01", "2026-10-01"),
    ).fetchone()["total"]

    sales = get_month_sales(conn, month)
    assert sales.refund_cents == int(expected)


def test_t04_cross_month_refund_is_attributed_to_refund_month(conn) -> None:
    """T04：跨月退款（8 月下单、9 月退款）计入 9 月，不计入 8 月的 approved 退款。

    生成器固定构造了 RF_XM_PINNED；这里校验它的订单月与退款月不同，
    并验证两边的 approved 退款额分别包含/不包含它。
    """
    row = conn.execute(
        "SELECT o.order_date AS order_date, r.refund_date AS refund_date,"
        " r.amount_cents AS amount_cents, r.status AS status"
        " FROM refunds AS r JOIN orders AS o ON o.order_id = r.order_id"
        " WHERE r.refund_id = 'RF_XM_PINNED'"
    ).fetchone()
    assert row is not None, "生成器应构造固定值跨月退款 RF_XM_PINNED"
    assert row["status"] == "approved"
    assert row["order_date"][:7] == "2026-08"
    assert row["refund_date"][:7] == "2026-09"

    august = get_month_sales(conn, "2026-08")
    september = get_month_sales(conn, "2026-09")
    # 8 月的退款额来自退款日期在 8 月的记录；9 月的来自 9 月
    assert september.refund_cents >= int(row["amount_cents"])
    august_expected = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds"
        " WHERE status = 'approved' AND refund_date >= '2026-08-01'"
        " AND refund_date < '2026-09-01'"
    ).fetchone()["total"]
    assert august.refund_cents == int(august_expected)


def test_t04_pending_refund_counts_but_does_not_deduct(conn) -> None:
    """T04/T06：pending 退款只增加 pending_refund_count，不减少净销售额。"""
    sales = get_month_sales(conn, "2026-07")
    assert sales.pending_refund_count >= 1
    approved_only = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds"
        " WHERE status = 'approved' AND refund_date >= '2026-07-01'"
        " AND refund_date < '2026-08-01'"
    ).fetchone()["total"]
    assert sales.refund_cents == int(approved_only)


# ---------------------------------------------------------------------------
# T05 维度完整性
# ---------------------------------------------------------------------------


def test_t05_region_breakdown_returns_all_regions(conn) -> None:
    """T05：地区分解恒返回 5 个地区，即使某地区当月无订单。"""
    regions = get_region_breakdown(conn, "2026-09")
    assert len(regions) == 5
    assert {item.region_id for item in regions} == {
        "R_EAST",
        "R_NORTH",
        "R_SOUTH",
        "R_SW",
        "R_NE",
    }


def test_t05_region_breakdown_sums_match_month_total(conn) -> None:
    """T05：各地区毛额/退款/净额之和必须等于整月汇总（口径闭环）。"""
    month = "2026-09"
    month_sales = get_month_sales(conn, month)
    regions = get_region_breakdown(conn, month)
    assert sum(item.gross_cents for item in regions) == month_sales.gross_cents
    assert sum(item.refund_cents for item in regions) == month_sales.refund_cents
    assert sum(item.net_cents for item in regions) == month_sales.net_cents


def test_t05_product_dimension_returns_all_products(conn) -> None:
    """T05：商品维度恒返回 12 个商品（某商品当月无记录时金额为 0）。"""
    products = get_product_sales(conn, "2026-05")
    assert len(products) == 12


def test_t05_region_without_orders_still_returned(conn) -> None:
    """T05：东北 2026-08 无订单，但仍在分解结果里，金额为 0。"""
    regions = {item.region_id: item for item in get_region_breakdown(conn, "2026-08")}
    assert "R_NE" in regions
    assert regions["R_NE"].gross_cents == 0
    assert regions["R_NE"].order_count == 0


# ---------------------------------------------------------------------------
# T06 边界语义
# ---------------------------------------------------------------------------


def test_t06_empty_month_returns_zeros_with_flag(conn) -> None:
    """T06：2025-01 是设计上的空月：全 0 且 has_orders=False。"""
    sales = get_month_sales(conn, "2025-01")
    assert isinstance(sales, MonthSales)
    assert sales.gross_cents == 0
    assert sales.refund_cents == 0
    assert sales.net_cents == 0
    assert sales.order_count == 0
    assert sales.has_orders is False


def test_t06_true_zero_month_has_orders_but_zero_net(conn) -> None:
    """T06：「真零」与「无记录」必须可区分：西南 2026-09 有订单但净额为 0。"""
    regions = {item.region_id: item for item in get_region_breakdown(conn, "2026-09")}
    southwest = regions["R_SW"]
    assert southwest.order_count > 0, "真零场景应有订单记录"
    assert southwest.gross_cents > 0
    assert southwest.net_cents == 0
    assert southwest.gross_cents == southwest.refund_cents


def test_t06_negative_net_exists_in_2026_03(conn) -> None:
    """T06：2026-03 至少一个地区净额为负（退款额大于毛额的教学场景）。"""
    regions = get_region_breakdown(conn, "2026-03")
    assert any(item.net_cents < 0 for item in regions)


def test_t06_missing_region_month_is_zero_not_error(conn) -> None:
    """T06：东北 2025-09 无订单，查询不报错且金额为 0。"""
    regions = {item.region_id: item for item in get_region_breakdown(conn, "2025-09")}
    assert regions["R_NE"].order_count == 0
    assert regions["R_NE"].net_cents == 0


def test_t06_list_available_months_excludes_empty_month(conn) -> None:
    """T06：可用月份来自真实记录，因此不含空月 2025-01。"""
    months = list_available_months(conn)
    assert "2025-01" not in months
    assert "2026-09" in months
    assert months == sorted(months)


def test_t06_get_region_by_name_and_id(conn) -> None:
    """T06：地区既可用中文名也可用编码查询。"""
    by_name = get_region(conn, "华东")
    by_id = get_region(conn, "R_EAST")
    assert by_name is not None and by_id is not None
    assert by_name["region_id"] == by_id["region_id"] == "R_EAST"
    assert get_region(conn, "不存在的地区") is None


def test_t06_list_regions_returns_five(conn) -> None:
    """T06：地区字典恒为 5 条。"""
    regions = list_regions(conn)
    assert len(regions) == 5
    assert all("region_id" in item and "region_name" in item for item in regions)
