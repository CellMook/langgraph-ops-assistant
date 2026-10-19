"""独立标准答案生成脚本（工程规格 3.3）。

职责：

1. 只用标准库 ``sqlite3`` + 原生 SQL + Python 整数运算重新计算口径，
   **不 import** 项目里的 ``queries.py`` / ``metrics.py``，避免“用被验证的实现验证自己”。
2. 输出 ``data/ground_truth.json``（UTF-8、``ensure_ascii=False``、缩进 2）。
3. 所有金额都是整数分；``pct_change`` / ``share_of_delta`` 输出不取整的原始浮点，
   便于和 ``domain/metrics.py`` 逐位比对，另附 ``*_display`` 键给出保留位数后的展示值。

诚实声明：
    本脚本在本次编写环境中**没有执行过**（该环境无法启动 Python 进程）。
    文件中不预置任何计算结果，所有数值都由实际运行时的数据库内容决定；
    唯一的“硬编码”是固定值跨月退款的退款号（``RF_XM_PINNED``）与期望金额（128000 分），
    用于断言生成器确实构造了这笔固定值退款。``meta.generated_at_utc`` 也是运行时真实时间。

运行方式（在 ``ops-assistant`` 目录）：

    uv run python scripts/compute_ground_truth.py --db data/ops.db --out data/ground_truth.json

前置条件：``data/ops.db`` 已由 ``scripts/bootstrap.py`` 或
``python -m ops_assistant.data.generate`` 生成。
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 固定种子（与 ``data/generate.py`` 的 SEED 一致，仅写入 meta 供追溯）。
SEED: int = 20260901

#: 声明数据范围。
DATA_FIRST_MONTH: str = "2025-01"
DATA_LAST_MONTH: str = "2026-09"

#: 标准答案固定对比的两个月。
CURRENT_MONTH: str = "2026-09"
PREVIOUS_MONTH: str = "2026-08"

#: 固定值跨月退款（与 ``generate.py`` 的常量保持一致；此处重复定义是为了不依赖生成逻辑）。
PINNED_REFUND_ID: str = "RF_XM_PINNED"
PINNED_REFUND_AMOUNT_CENTS: int = 128_000

#: 需要逐一写进 ``edge_cases`` 的固定边界「地区 / 月份」。
EMPTY_MONTH: str = "2025-01"
EMPTY_REGION_MONTHS: tuple[tuple[str, str], ...] = (
    ("R_NE", "2025-09"),
    ("R_NE", "2026-08"),
)
PRODUCT_ZERO_ID: str = "P_012"
PRODUCT_ZERO_MONTH: str = "2026-05"
TRUE_ZERO_REGION_ID: str = "R_SW"
TRUE_ZERO_MONTH: str = "2026-09"
PENDING_ONLY_REGION_ID: str = "R_NORTH"
PENDING_ONLY_MONTH: str = "2026-07"
NEGATIVE_MONTH: str = "2026-03"
NEGATIVE_REGION_ID: str = "R_SOUTH"
CROSS_MONTH_ORDER_MONTH: str = "2026-08"
CROSS_MONTH_REFUND_MONTH: str = "2026-09"

#: 默认路径：``ops-assistant/data/ops.db`` 与 ``ops-assistant/data/ground_truth.json``。
PROJECT_DIR: Path = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH: Path = PROJECT_DIR / "data" / "ops.db"
DEFAULT_OUT_PATH: Path = PROJECT_DIR / "data" / "ground_truth.json"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """以只读意图打开数据库（``row_factory`` 为 ``sqlite3.Row``，外键开启）。

    参数：
        db_path：数据库文件路径。

    返回：
        sqlite3.Connection。
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _month_start(month: str) -> str:
    """返回某月第一天的 ISO 字符串。"""
    return f"{month}-01"


def _next_month_start(month: str) -> str:
    """返回次月第一天的 ISO 字符串，用于半开区间过滤。"""
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        return f"{year + 1:04d}-01-01"
    return f"{year:04d}-{month_number + 1:02d}-01"


def _iter_months(first_month: str, last_month: str) -> list[str]:
    """生成闭区间内所有 ``YYYY-MM``（升序）。"""
    year, month_number = int(first_month[:4]), int(first_month[5:7])
    end_year, end_month_number = int(last_month[:4]), int(last_month[5:7])
    months: list[str] = []
    while (year, month_number) <= (end_year, end_month_number):
        months.append(f"{year:04d}-{month_number:02d}")
        month_number += 1
        if month_number > 12:
            month_number = 1
            year += 1
    return months


# ---------------------------------------------------------------------------
# 口径计算（与 queries.py 相互独立）
# ---------------------------------------------------------------------------


def _month_sales(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    """计算某月的汇总口径（毛销售额 / 退款 / 净额 / 计数）。

    口径：
        - 毛销售额 = ``SUM(quantity * unit_price_cents)``，按 ``order_date`` 归月；
        - 退款额 = ``status = 'approved'`` 且 ``refund_date`` 在该月的金额之和；
        - 净销售额 = 毛销售额 - 退款额（允许负数）。

    参数：
        conn：数据库连接。
        month：``YYYY-MM``。

    返回：
        字典：``month`` / ``gross_cents`` / ``refund_cents`` / ``net_cents`` /
        ``order_count`` / ``approved_refund_count`` / ``pending_refund_count`` /
        ``has_orders`` / ``has_any_refund_records``（键名与 ``MonthSales`` 完全一致）。
    """
    start = _month_start(month)
    end = _next_month_start(month)
    order_row = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents,
               COUNT(*) AS order_count
        FROM orders
        WHERE order_date >= ? AND order_date < ?
        """,
        (start, end),
    ).fetchone()
    refund_row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS refund_cents,
               COUNT(*) AS approved_refund_count
        FROM refunds
        WHERE status = 'approved'
          AND refund_date >= ? AND refund_date < ?
        """,
        (start, end),
    ).fetchone()
    refund_count_row = conn.execute(
        """
        SELECT COUNT(*) AS refund_record_count,
               COALESCE(SUM(status = 'pending'), 0) AS pending_refund_count
        FROM refunds
        WHERE refund_date >= ? AND refund_date < ?
        """,
        (start, end),
    ).fetchone()
    gross_cents = int(order_row["gross_cents"])
    refund_cents = int(refund_row["refund_cents"])
    order_count = int(order_row["order_count"])
    return {
        "month": month,
        "gross_cents": gross_cents,
        "refund_cents": refund_cents,
        "net_cents": gross_cents - refund_cents,
        "order_count": order_count,
        "approved_refund_count": int(refund_row["approved_refund_count"]),
        "pending_refund_count": int(refund_count_row["pending_refund_count"]),
        "has_orders": order_count > 0,
        "has_any_refund_records": int(refund_count_row["refund_record_count"]) > 0,
    }


def _region_sales(
    conn: sqlite3.Connection, month: str, region_id: str
) -> dict[str, Any]:
    """计算某地区某月的口径（金额单位分）。

    参数：
        conn：数据库连接。
        month：``YYYY-MM``。
        region_id：地区 ID，例如 ``R_EAST``。

    返回：
        字典：``gross_cents`` / ``refund_cents`` / ``net_cents`` / ``order_count``。
    """
    start = _month_start(month)
    end = _next_month_start(month)
    order_row = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents,
               COUNT(*) AS order_count
        FROM orders
        WHERE region_id = ? AND order_date >= ? AND order_date < ?
        """,
        (region_id, start, end),
    ).fetchone()
    refund_row = conn.execute(
        """
        SELECT COALESCE(SUM(refunds.amount_cents), 0) AS refund_cents
        FROM refunds
        JOIN orders ON orders.order_id = refunds.order_id
        WHERE refunds.status = 'approved'
          AND orders.region_id = ?
          AND refunds.refund_date >= ? AND refunds.refund_date < ?
        """,
        (region_id, start, end),
    ).fetchone()
    gross_cents = int(order_row["gross_cents"])
    refund_cents = int(refund_row["refund_cents"])
    return {
        "gross_cents": gross_cents,
        "refund_cents": refund_cents,
        "net_cents": gross_cents - refund_cents,
        "order_count": int(order_row["order_count"]),
    }


def _region_breakdown(conn: sqlite3.Connection, month: str) -> dict[str, dict[str, Any]]:
    """计算某月全部地区的口径（按 ``region_id`` 升序）。

    返回：
        ``{region_id: 条目}``；每个条目的键名与 ``RegionSales`` 完全一致
        （``region_id`` / ``region_name`` / ``gross_cents`` / ``refund_cents`` /
        ``net_cents`` / ``order_count``）。
    """
    rows = conn.execute(
        "SELECT region_id, region_name FROM regions ORDER BY region_id"
    ).fetchall()
    breakdown: dict[str, dict[str, Any]] = {}
    for row in rows:
        region_id = str(row["region_id"])
        breakdown[region_id] = {
            "region_id": region_id,
            "region_name": str(row["region_name"]),
            **_region_sales(conn, month, region_id),
        }
    return breakdown


def _comparison(
    conn: sqlite3.Connection, current_month: str, previous_month: str
) -> dict[str, Any]:
    """计算两个月之间的差额、变化率与地区差额分解。

    口径：
        - ``delta_net_cents = current.net - previous.net``；
        - ``pct_change = delta / abs(previous.net)``（原始浮点，未取整，便于与
          ``domain/metrics.py`` 逐位比对），分母为 0 时 ``None`` 且 ``base_zero=True``；
        - ``pct_change_display`` 是它保留 4 位小数后的展示值；
        - 地区差额 = 该地区 ``current.net - previous.net``；
        - ``share_of_delta = 地区差额 / 总差额``（原始浮点），总差额为 0 时 ``None``；
        - ``share_of_delta_display`` 是它保留 6 位小数后的展示值。

    参数：
        conn：数据库连接。
        current_month：本期月份。
        previous_month：上期月份。

    返回：
        字典：含 ``current`` / ``previous``（与 ``MonthSales`` 同键）、``delta_net_cents`` /
        ``pct_change`` / ``pct_change_display`` / ``base_zero`` / ``regions``（每项与
        ``RegionDelta`` 同键，另加 ``share_of_delta_display``）/
        ``region_delta_sum_cents`` / ``region_delta_sum_matches`` 等键。
    """
    current = _month_sales(conn, current_month)
    previous = _month_sales(conn, previous_month)
    delta_net_cents = int(current["net_cents"]) - int(previous["net_cents"])
    previous_net_cents = int(previous["net_cents"])
    base_zero = previous_net_cents == 0
    pct_change: float | None = (
        None if base_zero else delta_net_cents / abs(previous_net_cents)
    )
    pct_change_display: float | None = (
        None if pct_change is None else round(pct_change, 4)
    )

    current_regions = _region_breakdown(conn, current_month)
    previous_regions = _region_breakdown(conn, previous_month)
    regions: list[dict[str, Any]] = []
    region_delta_sum_cents = 0
    for region_id in sorted(current_regions):
        current_net = int(current_regions[region_id]["net_cents"])
        previous_entry = previous_regions.get(region_id, {})
        previous_net = int(previous_entry.get("net_cents", 0))
        delta_cents = current_net - previous_net
        region_delta_sum_cents += delta_cents
        share_of_delta: float | None = (
            None if delta_net_cents == 0 else delta_cents / delta_net_cents
        )
        share_of_delta_display: float | None = (
            None if share_of_delta is None else round(share_of_delta, 6)
        )
        regions.append(
            {
                "region_id": region_id,
                "region_name": str(current_regions[region_id]["region_name"]),
                "current_net_cents": current_net,
                "previous_net_cents": previous_net,
                "delta_cents": delta_cents,
                "share_of_delta": share_of_delta,
                "share_of_delta_display": share_of_delta_display,
            }
        )

    return {
        "current_month": current_month,
        "previous_month": previous_month,
        "current": current,
        "previous": previous,
        "current_net_cents": int(current["net_cents"]),
        "previous_net_cents": previous_net_cents,
        "delta_net_cents": delta_net_cents,
        "pct_change": pct_change,
        "pct_change_display": pct_change_display,
        "base_zero": base_zero,
        "regions": regions,
        "region_delta_sum_cents": region_delta_sum_cents,
        "region_delta_sum_matches": region_delta_sum_cents == delta_net_cents,
    }


# ---------------------------------------------------------------------------
# 边界事实
# ---------------------------------------------------------------------------


def _edge_cases(conn: sqlite3.Connection) -> dict[str, Any]:
    """逐项计算规格 3.2 要求的 7 类边界事实（第 8 类 channel 放在 ``meta`` 里）。

    参数：
        conn：数据库连接。

    返回：
        ``edge_cases`` 字典：前 7 个键是 7 类边界事实（空月、地区缺月、西南真零、
        华北纯 pending、跨月退款、负净额、商品零记录）；最后 2 个键是规格 3.3 示例
        出现的同义布尔开关 ``"2025-01_is_empty"`` 与 ``"R_SW_2026_09_net_zero"``。
    """
    # 1) 空月：2025-01 无订单、无退款记录（无记录 ≠ 销售额 0）
    empty_month_sales = _month_sales(conn, EMPTY_MONTH)
    empty_month = {
        "month": EMPTY_MONTH,
        "order_count": int(empty_month_sales["order_count"]),
        "has_orders": bool(empty_month_sales["has_orders"]),
        "has_any_refund_records": bool(empty_month_sales["has_any_refund_records"]),
        "is_empty": int(empty_month_sales["order_count"]) == 0,
    }

    # 2) 地区缺月：东北在 2025-09、2026-08 无订单
    region_month_items: list[dict[str, Any]] = []
    for region_id, month in EMPTY_REGION_MONTHS:
        region_month_items.append(
            {
                "region_id": region_id,
                "month": month,
                "order_count": int(_region_sales(conn, month, region_id)["order_count"]),
            }
        )
    region_months_without_orders = {
        "pairs": region_month_items,
        "all_empty": all(item["order_count"] == 0 for item in region_month_items),
    }

    # 3) 西南 2026-09 真零：有订单，但已批准退款恰好冲抵毛销售额
    true_zero = _region_sales(conn, TRUE_ZERO_MONTH, TRUE_ZERO_REGION_ID)
    southwest_net_zero = {
        "region_id": TRUE_ZERO_REGION_ID,
        "month": TRUE_ZERO_MONTH,
        "gross_cents": int(true_zero["gross_cents"]),
        "refund_cents": int(true_zero["refund_cents"]),
        "net_cents": int(true_zero["net_cents"]),
        "order_count": int(true_zero["order_count"]),
        "is_true_zero": int(true_zero["net_cents"]) == 0
        and int(true_zero["order_count"]) > 0,
    }

    # 4) 华北 2026-07 只有 pending 退款：退款记录存在，但金额不计入净销售额
    pending_rows = conn.execute(
        """
        SELECT COALESCE(SUM(status = 'pending'), 0) AS pending_count,
               COALESCE(SUM(status = 'approved'), 0) AS approved_count,
               COALESCE(SUM(status = 'rejected'), 0) AS rejected_count,
               COUNT(*) AS record_count
        FROM refunds
        JOIN orders ON orders.order_id = refunds.order_id
        WHERE orders.region_id = ?
          AND refunds.refund_date >= ? AND refunds.refund_date < ?
        """,
        (
            PENDING_ONLY_REGION_ID,
            _month_start(PENDING_ONLY_MONTH),
            _next_month_start(PENDING_ONLY_MONTH),
        ),
    ).fetchone()
    pending_only = {
        "region_id": PENDING_ONLY_REGION_ID,
        "month": PENDING_ONLY_MONTH,
        "pending_refund_count": int(pending_rows["pending_count"]),
        "approved_refund_count": int(pending_rows["approved_count"]),
        "rejected_refund_count": int(pending_rows["rejected_count"]),
        "refund_record_count": int(pending_rows["record_count"]),
        "approved_refund_cents": int(
            _region_sales(conn, PENDING_ONLY_MONTH, PENDING_ONLY_REGION_ID)["refund_cents"]
        ),
        "is_pending_only": int(pending_rows["pending_count"]) > 0
        and int(pending_rows["approved_count"]) == 0,
    }

    # 5) 跨月退款：订单在 2026-08、退款在 2026-09 且 approved
    cross_rows = conn.execute(
        """
        SELECT refunds.refund_id AS refund_id,
               refunds.order_id AS order_id,
               refunds.refund_date AS refund_date,
               refunds.amount_cents AS amount_cents,
               orders.region_id AS region_id
        FROM refunds
        JOIN orders ON orders.order_id = refunds.order_id
        WHERE refunds.status = 'approved'
          AND orders.order_date >= ? AND orders.order_date < ?
          AND refunds.refund_date >= ? AND refunds.refund_date < ?
        ORDER BY refunds.refund_date, refunds.refund_id
        """,
        (
            _month_start(CROSS_MONTH_ORDER_MONTH),
            _next_month_start(CROSS_MONTH_ORDER_MONTH),
            _month_start(CROSS_MONTH_REFUND_MONTH),
            _next_month_start(CROSS_MONTH_REFUND_MONTH),
        ),
    ).fetchall()
    cross_items = [
        {
            "refund_id": str(row["refund_id"]),
            "order_id": str(row["order_id"]),
            "region_id": str(row["region_id"]),
            "refund_date": str(row["refund_date"]),
            "amount_cents": int(row["amount_cents"]),
        }
        for row in cross_rows
    ]
    pinned = next(
        (item for item in cross_items if item["refund_id"] == PINNED_REFUND_ID), None
    )
    cross_month = {
        "order_month": CROSS_MONTH_ORDER_MONTH,
        "refund_month": CROSS_MONTH_REFUND_MONTH,
        "count": len(cross_items),
        "total_amount_cents": sum(item["amount_cents"] for item in cross_items),
        "items": cross_items,
        "pinned": pinned,
        "meets_requirement": len(cross_items) >= 3
        and pinned is not None
        and pinned["amount_cents"] == PINNED_REFUND_AMOUNT_CENTS,
    }

    # 6) 2026-03 至少一个地区退款大于毛销售（净额为负）
    negative_month_regions = _region_breakdown(conn, NEGATIVE_MONTH)
    negative_items = [
        {
            "region_id": region_id,
            "gross_cents": int(values["gross_cents"]),
            "refund_cents": int(values["refund_cents"]),
            "net_cents": int(values["net_cents"]),
            "order_count": int(values["order_count"]),
        }
        for region_id, values in negative_month_regions.items()
        if int(values["net_cents"]) < 0
    ]
    negative_region_ids = [item["region_id"] for item in negative_items]
    negative_net = {
        "month": NEGATIVE_MONTH,
        "regions": negative_items,
        "pinned_region_id": NEGATIVE_REGION_ID,
        "pinned_region_is_negative": NEGATIVE_REGION_ID in negative_region_ids,
        "primary_region_id": (
            NEGATIVE_REGION_ID
            if NEGATIVE_REGION_ID in negative_region_ids
            else (negative_region_ids[0] if negative_region_ids else None)
        ),
        "has_negative": len(negative_items) > 0,
    }

    # 7) 至少一个商品在某月无订单（商品维度零记录）
    observed_months_row = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT substr(order_date, 1, 7) AS month FROM orders)"
    ).fetchone()
    products_row = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()
    zero_pair_rows = conn.execute(
        """
        WITH observed_months AS (
            SELECT DISTINCT substr(order_date, 1, 7) AS month FROM orders
        )
        SELECT products.product_id AS product_id,
               observed_months.month AS month
        FROM products
        CROSS JOIN observed_months
        WHERE NOT EXISTS (
            SELECT 1
            FROM orders
            WHERE orders.product_id = products.product_id
              AND substr(orders.order_date, 1, 7) = observed_months.month
        )
        ORDER BY observed_months.month, products.product_id
        """
    ).fetchall()
    zero_pairs = [
        {"product_id": str(row["product_id"]), "month": str(row["month"])}
        for row in zero_pair_rows
    ]
    known_pair = next(
        (
            pair
            for pair in zero_pairs
            if pair["product_id"] == PRODUCT_ZERO_ID and pair["month"] == PRODUCT_ZERO_MONTH
        ),
        zero_pairs[0] if zero_pairs else None,
    )
    product_month_without_orders = {
        "months_checked": int(observed_months_row["n"]),
        "products_checked": int(products_row["n"]),
        "total_zero_pairs": len(zero_pairs),
        "known_pair": known_pair,
        "sample_pairs": zero_pairs[:10],
        "has_zero_pair": len(zero_pairs) > 0,
    }

    return {
        # 7 类边界事实（每类一个键，内含可直接断言的数值）
        "empty_month_2025_01": empty_month,
        "region_months_without_orders": region_months_without_orders,
        "southwest_2026_09_net_zero": southwest_net_zero,
        "north_2026_07_pending_only": pending_only,
        "cross_month_refunds_2026_08_to_2026_09": cross_month,
        "negative_net_2026_03": negative_net,
        "product_month_without_orders": product_month_without_orders,
        # 规格 3.3 示例里出现的两个同义布尔开关，保留以便测试直接断言
        "2025-01_is_empty": bool(empty_month["is_empty"]),
        "R_SW_2026_09_net_zero": bool(southwest_net_zero["is_true_zero"]),
    }


# ---------------------------------------------------------------------------
# 组装与写出
# ---------------------------------------------------------------------------


def compute_ground_truth(db_path: Path) -> dict[str, Any]:
    """读取数据库并计算完整标准答案（不写文件）。

    参数：
        db_path：已生成的数据库文件路径。

    返回：
        与工程规格 3.3 一致的字典：``meta`` / ``months`` / ``regions`` /
        ``comparison_2026_09_vs_2026_08`` / ``edge_cases``。

    异常：
        FileNotFoundError：数据库文件不存在。
        RuntimeError：关键口径自检失败（例如地区差额之和 != 总差额、本期无数据）。
    """
    database = Path(db_path)
    if not database.is_file():
        raise FileNotFoundError(
            f"数据库文件不存在：{database}；请先运行 scripts/bootstrap.py "
            "或 python -m ops_assistant.data.generate"
        )

    conn = _connect(database)
    try:
        months = _iter_months(DATA_FIRST_MONTH, DATA_LAST_MONTH)
        month_entries = {month: _month_sales(conn, month) for month in months}
        if not month_entries[CURRENT_MONTH]["has_orders"]:
            raise RuntimeError(f"{CURRENT_MONTH} 没有任何订单，标准答案无法生成")
        if not month_entries[PREVIOUS_MONTH]["has_orders"]:
            raise RuntimeError(f"{PREVIOUS_MONTH} 没有任何订单，标准答案无法生成")

        comparison = _comparison(conn, CURRENT_MONTH, PREVIOUS_MONTH)
        if not comparison["region_delta_sum_matches"]:
            raise RuntimeError(
                "地区差额之和与总差额不相等："
                f"{comparison['region_delta_sum_cents']} != {comparison['delta_net_cents']}"
            )

        observed_month_rows = conn.execute(
            """
            SELECT month FROM (
                SELECT DISTINCT substr(order_date, 1, 7) AS month FROM orders
                UNION
                SELECT DISTINCT substr(refund_date, 1, 7) AS month FROM refunds
            )
            WHERE month IS NOT NULL
            ORDER BY month
            """
        ).fetchall()
        observed_months = [str(row["month"]) for row in observed_month_rows]
        channel_rows = conn.execute(
            "SELECT DISTINCT channel FROM orders ORDER BY channel"
        ).fetchall()
        channel_values = [str(row["channel"]) for row in channel_rows]

        meta = {
            "seed": SEED,
            "months": months,
            "generated_by": "compute_ground_truth.py",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "data_range": {"first": DATA_FIRST_MONTH, "last": DATA_LAST_MONTH},
            "observed_months": observed_months,
            "db_file": database.name,
            "channel_values": channel_values,
            "comparison_pinned": {
                "current_month": CURRENT_MONTH,
                "previous_month": PREVIOUS_MONTH,
                "pinned_refund_id": PINNED_REFUND_ID,
                "pinned_refund_amount_cents": PINNED_REFUND_AMOUNT_CENTS,
            },
            "notes": [
                "所有金额单位为分（int），展示时由 report/format.py 转为元或万元。",
                "meta.months 是声明的数据范围（2025-01 至 2026-09）；"
                "meta.observed_months 是库中实际出现过的月份，2025-01 为空月所以不在其中。",
                "months[*] 的字段与 queries.MonthSales 完全同名，便于逐项比对。",
                "regions[month][region_id] 的字段与 queries.RegionSales 完全同名，便于逐项比对。",
                "comparison 里的 pct_change / share_of_delta 是不取整的原始浮点，"
                "与 domain/metrics.py 的计算结果可直接相等比较；"
                "pct_change_display / share_of_delta_display 才是保留 4 / 6 位小数的展示值。",
                "channel 字段在 orders 表里存在，但分析工具不支持渠道维度（规格第 4 节第 11 条）。",
                "edge_cases 前 7 个键对应规格 3.2 的 7 类边界；"
                "另有 2 个布尔开关沿用规格 3.3 示例命名，便于直接断言。",
                "本文件由独立实现计算：只使用 sqlite3 + 原生 SQL，不 import queries.py / metrics.py。",
                "本环境无法启动 Python 进程，因此文件中的数值未经本次运行验证。",
            ],
        }

        payload: dict[str, Any] = {
            "meta": meta,
            "months": month_entries,
            "regions": {CURRENT_MONTH: _region_breakdown(conn, CURRENT_MONTH)},
            "comparison_2026_09_vs_2026_08": comparison,
            "edge_cases": _edge_cases(conn),
        }
        return payload
    finally:
        conn.close()


def write_ground_truth(
    db_path: Path = DEFAULT_DB_PATH, out_path: Path = DEFAULT_OUT_PATH
) -> Path:
    """计算标准答案并写入 JSON 文件（先写临时文件再 ``replace``，避免半成品）。

    参数：
        db_path：已生成的数据库文件路径。
        out_path：输出 JSON 路径。

    返回：
        实际写出的文件路径。
    """
    payload = compute_ground_truth(db_path)
    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_path = output.with_name(output.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(output)
    return output


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    参数：
        argv：命令行参数（``None`` 表示读取 ``sys.argv``）。

    返回：
        进程退出码：0 成功，1 失败。
    """
    parser = argparse.ArgumentParser(
        description="独立计算 ops-assistant 标准答案并写出 ground_truth.json"
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH, help="数据库文件路径（默认 data/ops.db）"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help="输出 JSON 路径（默认 data/ground_truth.json）",
    )
    args = parser.parse_args(argv)

    try:
        written = write_ground_truth(args.db, args.out)
    except FileNotFoundError as error:
        print(f"[失败] {error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"[失败] 自检未通过：{error}", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print(f"[失败] 读取数据库出错：{error}", file=sys.stderr)
        return 1

    print(f"[完成] 标准答案已写入：{written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
