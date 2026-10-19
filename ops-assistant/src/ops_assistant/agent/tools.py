"""LangChain 工具集：把数据查询、指标比较与知识检索暴露给模型。
所有工具用 @tool 定义、返回统一 JSON（ok/data/unit/sources 或 ok:false/error/hint）。
参数错误统一返回 ok:false，不向模型抛异常；工具内部只调用 queries/metrics，不拼接 SQL 执行。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from langchain_core.tools import tool

from ops_assistant.config import Settings
from ops_assistant.data.queries import (
    MonthSales,
    RegionSales,
    get_month_sales,
    get_region_breakdown,
    list_available_months,
)
from ops_assistant.domain.metrics import (
    ComparisonResult,
    RegionDelta,
    compare_months,
)
from ops_assistant.knowledge.qa import SCORE_THRESHOLD
from ops_assistant.knowledge.retrieve import KnowledgeBase

logger = logging.getLogger(__name__)

# 合法月份格式：YYYY-MM，月份 01-12。
_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

# 这些 SQL 仅作为 sources 的“口径说明”随工具结果返回，实际查询全部走 queries.py。
_SQL_LIST_MONTHS = (
    "SELECT month FROM ("
    "SELECT DISTINCT substr(order_date, 1, 7) AS month FROM orders "
    "UNION SELECT DISTINCT substr(refund_date, 1, 7) AS month FROM refunds"
    ") ORDER BY month"
)
_SQL_MONTH_SALES = (
    "SELECT "
    "(SELECT COALESCE(SUM(quantity * unit_price_cents), 0) FROM orders "
    " WHERE substr(order_date, 1, 7) = :month) AS gross_cents, "
    "(SELECT COALESCE(SUM(amount_cents), 0) FROM refunds "
    " WHERE status = 'approved' AND substr(refund_date, 1, 7) = :month) AS refund_cents, "
    "(SELECT COUNT(*) FROM orders WHERE substr(order_date, 1, 7) = :month) AS order_count, "
    "(SELECT COUNT(*) FROM refunds WHERE status = 'approved' "
    " AND substr(refund_date, 1, 7) = :month) AS approved_refund_count, "
    "(SELECT COUNT(*) FROM refunds WHERE status = 'pending' "
    " AND substr(refund_date, 1, 7) = :month) AS pending_refund_count"
)
_SQL_REGION_BREAKDOWN = (
    "SELECT r.region_id, r.region_name, "
    "(SELECT COALESCE(SUM(o.quantity * o.unit_price_cents), 0) FROM orders o "
    " WHERE o.region_id = r.region_id AND substr(o.order_date, 1, 7) = :month) AS gross_cents, "
    "(SELECT COALESCE(SUM(f.amount_cents), 0) FROM refunds f "
    " JOIN orders o2 ON o2.order_id = f.order_id "
    " WHERE f.status = 'approved' AND substr(f.refund_date, 1, 7) = :month "
    " AND o2.region_id = r.region_id) AS refund_cents, "
    "(SELECT COUNT(*) FROM orders o3 WHERE o3.region_id = r.region_id "
    " AND substr(o3.order_date, 1, 7) = :month) AS order_count "
    "FROM regions r ORDER BY r.region_id"
)


def _dumps(payload: dict[str, Any]) -> str:
    """把结果字典序列化为紧凑 JSON 字符串（保留中文）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _ok(
    data: dict[str, Any],
    *,
    unit: str,
    sources: list[dict[str, Any]] | None = None,
) -> str:
    """构造成功返回：包含 ok、data、unit、sources 四个键的 JSON。"""
    return _dumps({"ok": True, "data": data, "unit": unit, "sources": sources or []})


def _err(code: str, message: str, hint: str | None = None) -> str:
    """构造失败返回：包含 ok=false 与 error 两个键的 JSON，可选 hint。"""
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message},
    }
    if hint:
        payload["hint"] = hint
    return _dumps(payload)


def _sql_source(sql: str, params: dict[str, Any]) -> dict[str, Any]:
    """构造 SQL 来源描述（只做展示，不执行）。"""
    return {"type": "sql", "sql": sql, "params": params}


def _month_dict(sales: MonthSales) -> dict[str, Any]:
    """把 MonthSales 转成 JSON 友好的 dict（金额单位：分）。"""
    return {
        "month": sales.month,
        "gross_cents": sales.gross_cents,
        "refund_cents": sales.refund_cents,
        "net_cents": sales.net_cents,
        "order_count": sales.order_count,
        "approved_refund_count": sales.approved_refund_count,
        "pending_refund_count": sales.pending_refund_count,
        "has_orders": sales.has_orders,
        "has_any_refund_records": sales.has_any_refund_records,
    }


def _boundary_flags(sales: MonthSales) -> list[str]:
    """标记月份边界状态，供报告区分“无记录/真零/负净额/待处理退款”。"""
    flags: list[str] = []
    if not sales.has_orders:
        flags.append("NO_RECORDS")
    if sales.has_orders and sales.net_cents == 0:
        flags.append("TRUE_ZERO")
    if sales.net_cents < 0:
        flags.append("NEGATIVE_NET")
    if sales.pending_refund_count > 0:
        flags.append("PENDING_REFUNDS")
    return flags


def _region_row(region: RegionSales) -> dict[str, Any]:
    """把 RegionSales 转成 JSON 友好的 dict。"""
    return {
        "region_id": region.region_id,
        "region_name": region.region_name,
        "gross_cents": region.gross_cents,
        "refund_cents": region.refund_cents,
        "net_cents": region.net_cents,
        "order_count": region.order_count,
        "has_records": region.order_count > 0,
    }


def _region_delta_row(delta: RegionDelta) -> dict[str, Any]:
    """把 RegionDelta 转成 JSON 友好的 dict。"""
    return {
        "region_id": delta.region_id,
        "region_name": delta.region_name,
        "current_net_cents": delta.current_net_cents,
        "previous_net_cents": delta.previous_net_cents,
        "delta_cents": delta.delta_cents,
        "share_of_delta": delta.share_of_delta,
    }


def make_tools(conn: sqlite3.Connection, kb: KnowledgeBase, settings: Settings) -> list[Any]:
    """创建绑定数据库与知识库的 LangChain 工具列表。

    参数：
        conn: 已打开的 SQLite 连接（由调用方管理生命周期）。
        kb: 知识库实例。
        settings: 项目配置（仅用于错误提示与运行环境说明）。

    返回：
        list[Any]：6 个 StructuredTool，顺序与规格 5.5 一致。
    """
    months_hint_cache: dict[str, str] = {}

    def _hint() -> str:
        """知识库/数据库不可用时的通用提示。"""
        return (
            "请先初始化环境：`uv run python scripts/bootstrap.py`"
            f"（app_env={settings.app_env}，db={settings.db_path}）。"
        )

    def _months_hint() -> str:
        """可用月份提示（带进程内缓存，避免每次失败都重查）。"""
        cached = months_hint_cache.get("text")
        if cached is not None:
            return cached
        try:
            months = list_available_months(conn)
        except Exception:
            hint = "无法读取可用月份；请先运行 `uv run python scripts/bootstrap.py` 初始化数据库。"
        else:
            if not months:
                hint = "数据库中还没有订单月份；请先运行 bootstrap.py 生成数据。"
            else:
                hint = f"可用月份：{months[0]} ~ {months[-1]}（共 {len(months)} 个）"
        months_hint_cache["text"] = hint
        return hint

    def _check_month(month: Any) -> tuple[str | None, str | None]:
        """校验月份格式；返回 (规范化月份, 失败 JSON)。"""
        text = str(month or "").strip()
        if not _MONTH_PATTERN.fullmatch(text):
            return None, _err(
                "INVALID_MONTH",
                "月份格式必须是 YYYY-MM（例如 2026-09），且月份必须在 01-12 之间。",
                _months_hint(),
            )
        return text, None

    def _coerce_int(value: Any, field_name: str) -> tuple[int | None, str | None]:
        """把工具入参宽松转换为整数分；失败时返回可读错误 JSON。"""
        if isinstance(value, bool):
            return None, _err(
                "INVALID_NUMBER",
                f"{field_name} 必须是整数（单位：分），不能是布尔值。",
            )
        if isinstance(value, int):
            return value, None
        if isinstance(value, float) and value.is_integer():
            return int(value), None
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
            return int(value.strip()), None
        return None, _err(
            "INVALID_NUMBER",
            f"{field_name} 必须是整数（单位：分），例如 123456。",
        )

    @tool
    def list_available_periods() -> str:
        """列出数据库中有订单或退款记录的月份，用于确定可查询的时间范围。

        参数：无。

        返回 JSON：
        {"ok": true, "data": {"months": ["2025-02", "2025-03"], "count": 2,
          "min_month": "2025-02", "max_month": "2025-03"},
         "unit": "months", "sources": [{"type": "sql",
                                        "sql": "SELECT month FROM (orders UNION refunds)",
                                        "params": {}}]}
        失败时返回 {"ok": false, "error": {"code": "QUERY_FAILED", "message": "查询可用月份失败"},
                    "hint": "请先运行 bootstrap.py"}。
        """
        try:
            months = list_available_months(conn)
        except Exception as exc:
            logger.warning("list_available_periods 查询失败：%s", type(exc).__name__)
            return _err("QUERY_FAILED", f"查询可用月份失败：{type(exc).__name__}", _hint())

        data = {
            "months": list(months),
            "count": len(months),
            "min_month": months[0] if months else None,
            "max_month": months[-1] if months else None,
        }
        return _ok(data, unit="months", sources=[_sql_source(_SQL_LIST_MONTHS, {})])

    @tool
    def query_month_sales(month: str) -> str:
        """查询指定月份（YYYY-MM）的销售汇总，金额单位为“分”（cents）。

        毛销售额 = 当月订单 quantity * unit_price_cents 之和；
        退款额只统计 status='approved' 且 refund_date 落在该月的退款；
        净销售额 = 毛销售额 - 退款额（可能为负数）。

        参数：
            month: 月份，格式必须是 YYYY-MM，例如 "2026-09"。

        返回 JSON：
        {"ok": true, "data": {"month": "2026-09", "gross_cents": 8597, "refund_cents": 1299,
          "net_cents": 7298, "order_count": 2, "approved_refund_count": 1,
          "pending_refund_count": 0, "has_orders": true,
          "has_any_refund_records": true, "boundary": ["PENDING_REFUNDS"]},
         "unit": "cents", "sources": [{"type": "sql", "params": {"month": "2026-09"}}]}。
        月份非法时返回 {"ok": false, "error": {"code": "INVALID_MONTH",
          "message": "月份格式必须是 YYYY-MM"}, "hint": "可用月份：2025-02 ~ 2026-09"}，不抛异常。
        """
        normalized, error = _check_month(month)
        if error is not None:
            return error
        assert normalized is not None

        try:
            sales = get_month_sales(conn, normalized)
        except ValueError as exc:
            return _err("INVALID_MONTH", str(exc), _months_hint())
        except Exception as exc:
            logger.warning("query_month_sales 查询失败：%s", type(exc).__name__)
            return _err("QUERY_FAILED", f"查询月度销售失败：{type(exc).__name__}", _hint())

        data = _month_dict(sales)
        data["boundary"] = _boundary_flags(sales)
        return _ok(
            data,
            unit="cents",
            sources=[_sql_source(_SQL_MONTH_SALES, {"month": normalized})],
        )

    @tool
    def query_region_breakdown(month: str) -> str:
        """查询指定月份（YYYY-MM）按地区的销售拆分，金额单位为“分”（cents）。

        返回每个地区的毛销售额、退款额（仅 approved）、净销售额与订单数；
        没有订单的地区通过 has_records=false 标识，不代表净额为 0。

        参数：
            month: 月份，格式必须是 YYYY-MM，例如 "2026-09"。

        返回 JSON：
        {"ok": true, "data": {"month": "2026-09",
          "regions": [{"region_id": "R_EAST", "region_name": "华东",
                       "gross_cents": 8597, "refund_cents": 1299, "net_cents": 7298,
                       "order_count": 2, "has_records": true}],
          "totals": {"gross_cents": 8597, "refund_cents": 1299, "net_cents": 7298,
                     "order_count": 2}},
         "unit": "cents", "sources": [{"type": "sql", "params": {"month": "2026-09"}}]}。
        """
        normalized, error = _check_month(month)
        if error is not None:
            return error
        assert normalized is not None

        try:
            regions = get_region_breakdown(conn, normalized)
        except ValueError as exc:
            return _err("INVALID_MONTH", str(exc), _months_hint())
        except Exception as exc:
            logger.warning("query_region_breakdown 查询失败：%s", type(exc).__name__)
            return _err("QUERY_FAILED", f"查询地区拆分失败：{type(exc).__name__}", _hint())

        rows = [_region_row(region) for region in regions]
        data = {
            "month": normalized,
            "regions": rows,
            "totals": {
                "gross_cents": sum(row["gross_cents"] for row in rows),
                "refund_cents": sum(row["refund_cents"] for row in rows),
                "net_cents": sum(row["net_cents"] for row in rows),
                "order_count": sum(row["order_count"] for row in rows),
            },
        }
        return _ok(
            data,
            unit="cents",
            sources=[_sql_source(_SQL_REGION_BREAKDOWN, {"month": normalized})],
        )

    @tool
    def compare_two_months(current_month: str, previous_month: str) -> str:
        """比较两个月份（YYYY-MM）的净销售额，并按地区分解差额，金额单位为“分”。

        差额 delta = 本期净销售额 - 上期净销售额；
        变化率 = delta / abs(上期净销售额)，上期为 0 时返回 null 且 base_zero=true；
        地区差额之和必须等于总差额，返回 check 字段给出校验结果。

        参数：
            current_month: 本期月份，格式 YYYY-MM，例如 "2026-09"。
            previous_month: 上期月份，格式 YYYY-MM，例如 "2026-08"。

        返回 JSON：
        {"ok": true, "data": {"current": {"month": "2026-09", "net_cents": 7298},
          "previous": {"month": "2026-08", "net_cents": 6890},
          "current_month": "2026-09", "previous_month": "2026-08",
          "delta_net_cents": 408, "pct_change": 0.0592, "base_zero": false,
          "regions": [{"region_id": "R_EAST", "region_name": "华东",
                       "delta_cents": 408, "share_of_delta": 1.0}],
          "region_delta_sum_cents": 408,
          "check": {"region_delta_sum_matches_total": true, "difference_cents": 0,
                    "reported_sum_matches_regions": true},
          "dimension": "region", "metric": "net_sales",
          "notes": ["变化率按上期净额绝对值计算"], "unsupported_dimension": null},
         "unit": "cents", "sources": [{"type": "sql", "params": {"month": "2026-09"}},
                                      {"type": "sql", "params": {"month": "2026-08"}}]}。
        """
        normalized_current, error = _check_month(current_month)
        if error is not None:
            return error
        normalized_previous, error = _check_month(previous_month)
        if error is not None:
            return error
        assert normalized_current is not None and normalized_previous is not None

        try:
            comparison: ComparisonResult = compare_months(
                conn,
                normalized_current,
                normalized_previous,
            )
        except ValueError as exc:
            return _err("INVALID_MONTH", str(exc), _months_hint())
        except Exception as exc:
            logger.warning("compare_two_months 计算失败：%s", type(exc).__name__)
            return _err("COMPARE_FAILED", f"比较两个月份失败：{type(exc).__name__}", _hint())

        actual_region_sum = sum(delta.delta_cents for delta in comparison.regions)
        difference = actual_region_sum - comparison.delta_net_cents
        reported_sum_difference = comparison.region_delta_sum_cents - actual_region_sum
        data = {
            "current": _month_dict(comparison.current),
            "previous": _month_dict(comparison.previous),
            "current_month": normalized_current,
            "previous_month": normalized_previous,
            "delta_net_cents": comparison.delta_net_cents,
            "pct_change": comparison.pct_change,
            "base_zero": comparison.base_zero,
            "regions": [_region_delta_row(delta) for delta in comparison.regions],
            "region_delta_sum_cents": comparison.region_delta_sum_cents,
            "actual_region_delta_sum_cents": actual_region_sum,
            "dimension": comparison.dimension,
            "metric": comparison.metric,
            "notes": list(comparison.notes),
            "unsupported_dimension": comparison.unsupported_dimension,
            "check": {
                "region_delta_sum_matches_total": difference == 0,
                "difference_cents": difference,
                "reported_sum_matches_regions": reported_sum_difference == 0,
                "reported_sum_difference_cents": reported_sum_difference,
            },
        }
        return _ok(
            data,
            unit="cents",
            sources=[
                _sql_source(_SQL_MONTH_SALES, {"month": normalized_current}),
                _sql_source(_SQL_MONTH_SALES, {"month": normalized_previous}),
            ],
        )

    @tool
    def calculate_change(current_cents: int, previous_cents: int) -> str:
        """计算两个金额（整数“分”）之间的差额与变化率，不查询数据库。

        参数：
            current_cents: 本期金额，整数分，例如 123456。
            previous_cents: 上期金额，整数分，例如 100000。

        返回 JSON：
        {"ok": true, "data": {"current_cents": 123456, "previous_cents": 100000,
          "delta_cents": 23456, "pct_change": 0.2346, "base_zero": false,
          "formula": "delta = current - previous; pct_change = delta / abs(previous)"},
         "unit": "cents", "sources": [{"type": "calculation",
          "formula": "(current - previous) / abs(previous)",
          "params": {"current_cents": 123456, "previous_cents": 100000}}]}。
        上期为 0 时 pct_change 为 null 且 base_zero=true（变化率无定义）。
        """
        current, error = _coerce_int(current_cents, "current_cents")
        if error is not None:
            return error
        previous, error = _coerce_int(previous_cents, "previous_cents")
        if error is not None:
            return error
        assert current is not None and previous is not None

        delta = current - previous
        base_zero = previous == 0
        pct_change = None if base_zero else round(delta / abs(previous), 4)
        data = {
            "current_cents": current,
            "previous_cents": previous,
            "delta_cents": delta,
            "pct_change": pct_change,
            "base_zero": base_zero,
            "formula": "delta = current - previous; pct_change = delta / abs(previous)",
        }
        return _ok(
            data,
            unit="cents",
            sources=[
                {
                    "type": "calculation",
                    "formula": "(current - previous) / abs(previous)",
                    "params": {
                        "current_cents": current,
                        "previous_cents": previous,
                    },
                }
            ],
        )

    @tool
    def search_knowledge(query: str, k: int = 4) -> str:
        """检索本地知识库（指标口径、退款归属、地区说明、数据字典等）。

        返回混合检索（关键词 + 哈希向量）命中的知识块；当最高分低于 0.15 时
        insufficient=true，表示资料不足，不要据此编造答案。

        参数：
            query: 检索问题，例如“销售额是否扣除退款？”。
            k: 返回条数，1-10 的整数，默认 4。

        返回 JSON：
        {"ok": true, "data": {"query": "销售额是否扣除退款", "count": 2, "top_score": 0.51,
          "insufficient": false,
          "hits": [{"chunk_id": "01_指标口径#0", "doc_id": "01_指标口径",
                    "title": "指标口径说明（演示口径）", "score": 0.51, "method": "hybrid",
                    "snippet": "净销售额 = 毛销售额 - 退款额"}]},
         "unit": "score", "sources": [{"type": "knowledge", "chunk_id": "01_指标口径#0",
                                       "doc_id": "01_指标口径"}]}。
        """
        text = str(query or "").strip()
        if not text:
            return _err(
                "EMPTY_QUERY",
                "检索问题不能为空。",
                "示例：销售额是否扣除了退款？",
            )
        try:
            limit = int(k)
        except (TypeError, ValueError):
            return _err("INVALID_K", "k 必须是 1-10 的整数。")
        if limit < 1 or limit > 10:
            return _err("INVALID_K", "k 必须是 1-10 的整数。")

        try:
            hits = kb.search(text, k=limit, mode="hybrid")
        except Exception as exc:
            logger.warning("search_knowledge 检索失败：%s", type(exc).__name__)
            return _err(
                "SEARCH_FAILED",
                f"知识检索失败：{type(exc).__name__}",
                "可运行 `uv run python scripts/bootstrap.py` 重建知识索引。",
            )

        top_score = max((hit.score for hit in hits), default=0.0)
        rows = [
            {
                "chunk_id": hit.chunk.chunk_id,
                "doc_id": hit.chunk.doc_id,
                "title": hit.chunk.title,
                "score": round(hit.score, 4),
                "method": hit.method,
                "snippet": hit.citation()["snippet"],
            }
            for hit in hits
        ]
        data = {
            "query": text,
            "count": len(rows),
            "top_score": round(top_score, 4),
            "insufficient": not rows or top_score < SCORE_THRESHOLD,
            "hits": rows,
        }
        sources = [
            {
                "type": "knowledge",
                "doc_id": hit.chunk.doc_id,
                "title": hit.chunk.title,
                "chunk_id": hit.chunk.chunk_id,
            }
            for hit in hits
        ]
        return _ok(data, unit="score", sources=sources)

    return [
        list_available_periods,
        query_month_sales,
        query_region_breakdown,
        compare_two_months,
        calculate_change,
        search_knowledge,
    ]
