"""指标比较：月份环比、差额分解与边界标记。

本模块只做纯计算，不打印、不写库、不调用模型。所有金额都是整数分，
``pct_change`` / ``share_of_delta`` 只用于展示，计算过程不产生浮点误差。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from ops_assistant.data.queries import (
    MonthSales,
    RegionSales,
    get_month_sales,
    get_region_breakdown,
)

#: 允许的指标：净销售额、毛销售额、退款额。
_METRICS = ("net_sales", "gross_sales", "refunds")


@dataclass(frozen=True)
class RegionDelta:
    """一个地区在两个月之间的差额。金额单位：分。"""

    region_id: str
    region_name: str
    current_net_cents: int
    previous_net_cents: int
    delta_cents: int
    share_of_delta: float | None


@dataclass(frozen=True)
class ComparisonResult:
    """两个月比较的完整结果，供报告层与测试使用。"""

    current: MonthSales
    previous: MonthSales
    delta_net_cents: int
    pct_change: float | None
    base_zero: bool
    regions: tuple[RegionDelta, ...]
    region_delta_sum_cents: int
    dimension: str
    metric: str
    notes: tuple[str, ...]
    unsupported_dimension: str | None


def _metric_value(sales: MonthSales, metric: str) -> int:
    """按指标取月份汇总值。"""

    if metric == "gross_sales":
        return sales.gross_cents
    if metric == "refunds":
        return sales.refund_cents
    return sales.net_cents


def _region_value(region: RegionSales, metric: str) -> int:
    """按指标取地区汇总值。"""

    if metric == "gross_sales":
        return region.gross_cents
    if metric == "refunds":
        return region.refund_cents
    return region.net_cents


def compare_months(
    conn: sqlite3.Connection,
    current_month: str,
    previous_month: str,
    *,
    dimension: str = "region",
    region_ids: tuple[str, ...] | None = None,
    metric: str = "net_sales",
) -> ComparisonResult:
    """比较两个月的指标，并按地区分解差额。

    参数
    ----
    conn:
        已打开的 SQLite 连接。
    current_month / previous_month:
        ``YYYY-MM``；格式错误由查询层抛 ``ValueError``。
    dimension:
        目前真正支持差额分解的是 ``region``；``unknown`` 按 ``region`` 处理；
        ``channel`` 或其他值会返回 ``unsupported_dimension``，不抛异常。
    region_ids:
        非空时只分析这些地区，并把本月/上月的汇总按过滤后的地区重算。
    metric:
        ``net_sales``（默认）/ ``gross_sales`` / ``refunds``。

    返回
    ----
    ComparisonResult：包含差额、变化率、地区分解、边界说明。
    """

    if metric not in _METRICS:
        raise ValueError(f"不支持的指标：{metric!r}；可选：{', '.join(_METRICS)}")

    normalized_dimension = "region" if dimension == "unknown" else dimension
    unsupported: str | None = None
    notes: list[str] = []

    if normalized_dimension not in ("region",):
        unsupported = normalized_dimension
        notes.append(
            "当前差额分解只支持按地区；"
            f"维度 {normalized_dimension!r} 可以查询明细，但不做差额分解。"
        )

    current = get_month_sales(conn, current_month)
    previous = get_month_sales(conn, previous_month)
    current_regions = get_region_breakdown(conn, current_month)
    previous_regions = get_region_breakdown(conn, previous_month)

    # 过滤地区后，整月汇总必须重算，保证“地区差额之和 == 总差额”。
    if region_ids:
        current = _recompute_month(current, current_regions, region_ids)
        previous = _recompute_month(previous, previous_regions, region_ids)
        current_regions = [item for item in current_regions if item.region_id in region_ids]
        previous_regions = [item for item in previous_regions if item.region_id in region_ids]

    current_value = _metric_value(current, metric)
    previous_value = _metric_value(previous, metric)
    delta = current_value - previous_value
    base_zero = previous_value == 0
    pct_change = None if base_zero else delta / abs(previous_value)

    region_deltas: list[RegionDelta] = []
    if unsupported is None:
        previous_by_id = {item.region_id: item for item in previous_regions}
        for item in current_regions:
            previous_item = previous_by_id.pop(item.region_id, None)
            previous_net = _region_value(previous_item, metric) if previous_item else 0
            current_net = _region_value(item, metric)
            region_deltas.append(
                RegionDelta(
                    region_id=item.region_id,
                    region_name=item.region_name,
                    current_net_cents=current_net,
                    previous_net_cents=previous_net,
                    delta_cents=current_net - previous_net,
                    share_of_delta=None if delta == 0 else (current_net - previous_net) / delta,
                )
            )
        # 只在上月出现、本月完全没有记录的地区也要展示（例如东北缺月）。
        for region_id, item in previous_by_id.items():
            previous_net = _region_value(item, metric)
            region_deltas.append(
                RegionDelta(
                    region_id=region_id,
                    region_name=item.region_name,
                    current_net_cents=0,
                    previous_net_cents=previous_net,
                    delta_cents=-previous_net,
                    share_of_delta=None if delta == 0 else (-previous_net) / delta,
                )
            )

    region_deltas.sort(key=lambda item: (-abs(item.delta_cents), item.region_id))
    region_sum = sum(item.delta_cents for item in region_deltas)

    if not current.has_orders:
        notes.append(f"{current.month} 没有任何订单记录，属于“无记录”，不能当作销售额为 0。")
    if not previous.has_orders:
        notes.append(f"{previous.month} 没有任何订单记录；作为基期时无法计算有意义的环比。")
    if current.gross_cents > 0 and current.net_cents == 0:
        notes.append(f"{current.month} 有订单，但退款恰好冲抵毛销售额，净销售额为 0（真零）。")
    if previous.gross_cents > 0 and previous.net_cents == 0:
        notes.append(f"{previous.month} 净销售额为 0（有订单且被退款冲抵），变化率以 0 为基期无定义。")
    if current.net_cents < 0:
        notes.append(f"{current.month} 净销售额为负：已批准退款大于毛销售额。")
    if current.pending_refund_count > 0:
        notes.append(
            f"{current.month} 有 {current.pending_refund_count} 笔待处理退款，未计入退款额；"
            "若后续批准，净销售额会下降。"
        )
    if base_zero and not any("基期" in note for note in notes):
        notes.append("基期指标为 0，变化率无定义。")
    if unsupported is None and delta != 0 and region_sum != delta:
        notes.append(
            f"内部校验异常：地区差额合计 {region_sum} 分与总差额 {delta} 分不一致。"
        )

    return ComparisonResult(
        current=current,
        previous=previous,
        delta_net_cents=delta,
        pct_change=pct_change,
        base_zero=base_zero,
        regions=tuple(region_deltas),
        region_delta_sum_cents=region_sum,
        dimension=normalized_dimension,
        metric=metric,
        notes=tuple(notes),
        unsupported_dimension=unsupported,
    )


def _recompute_month(
    month_sales: MonthSales,
    regions: list[RegionSales],
    region_ids: tuple[str, ...],
) -> MonthSales:
    """按地区过滤重算月份汇总。"""

    kept = [item for item in regions if item.region_id in region_ids]
    gross = sum(item.gross_cents for item in kept)
    refund = sum(item.refund_cents for item in kept)
    orders = sum(item.order_count for item in kept)
    return replace(
        month_sales,
        gross_cents=gross,
        refund_cents=refund,
        net_cents=gross - refund,
        order_count=orders,
        has_orders=orders > 0,
    )
