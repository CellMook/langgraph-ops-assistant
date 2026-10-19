"""只读查询层（工程规格 3.4 的 ``queries.py``）。

职责：

1. 定义 ``MonthSales`` / ``RegionSales`` 两个不可变数据类，字段与规格 3.4 逐字一致。
2. 提供 ``get_month_sales`` / ``get_region_breakdown`` / ``list_available_months`` /
   ``list_regions`` / ``get_region`` / ``get_product_sales`` 六个函数。
3. 只做“取数 + 口径计算”，不打印、不格式化、不写库；返回值里的金额一律是整数分。

口径（对应工程规格第 4 节）：

- 毛销售额 ``gross_cents`` = 该月 ``orders`` 的 ``quantity * unit_price_cents`` 之和（用订单成交价，不用目录价）。
- 退款额 ``refund_cents`` = ``status = 'approved'`` 且 ``refund_date`` 落在该月的 ``amount_cents`` 之和；
  ``pending`` / ``rejected`` 不计入，且按“退款发生月”归属，不按原订单月。
- 净销售额 ``net_cents = gross_cents - refund_cents``，允许为负。
- 无记录时返回全 0 且 ``has_orders=False``，不得用 ``None`` 冒充 0。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

#: 合法月份：四位年份 + 01～12 两位月份。
MONTH_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")

_ORDER_AGG_SQL = """
SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents,
       COUNT(*) AS order_count
FROM orders
WHERE order_date >= ? AND order_date < ?
"""

_REFUND_AGG_SQL = """
SELECT COALESCE(SUM(amount_cents), 0) AS refund_cents,
       COUNT(*) AS approved_refund_count
FROM refunds
WHERE status = 'approved'
  AND refund_date >= ? AND refund_date < ?
"""

_REFUND_COUNT_SQL = """
SELECT COUNT(*) AS refund_record_count,
       COALESCE(SUM(status = 'pending'), 0) AS pending_refund_count
FROM refunds
WHERE refund_date >= ? AND refund_date < ?
"""


@dataclass(frozen=True)
class MonthSales:
    """某个月的汇总口径（金额单位：分）。

    字段：
        month：``YYYY-MM``。
        gross_cents：毛销售额（订单数量 × 成交单价之和）。
        refund_cents：退款额，仅 ``approved``，按 ``refund_date`` 归属到本月。
        net_cents：净销售额 = ``gross_cents - refund_cents``，允许负数。
        order_count：本月订单笔数。
        approved_refund_count：本月通过（approved）退款记录数。
        pending_refund_count：本月待处理（pending）退款记录数（金额不计入）。
        has_orders：本月是否有订单；``False`` 表示“无记录”，而不是“销售额为 0”。
        has_any_refund_records：本月是否有任何退款记录（含 pending/rejected）。
    """

    month: str
    gross_cents: int
    refund_cents: int
    net_cents: int
    order_count: int
    approved_refund_count: int
    pending_refund_count: int
    has_orders: bool
    has_any_refund_records: bool


@dataclass(frozen=True)
class RegionSales:
    """某地区在某月的汇总口径（金额单位：分）。

    字段：
        region_id：地区主键，例如 ``R_EAST``。
        region_name：地区中文名，例如 ``华东``。
        gross_cents：该地区本月毛销售额。
        refund_cents：该地区本月已批准退款额（按退款发生月归属）。
        net_cents：净销售额 = ``gross_cents - refund_cents``。
        order_count：该地区本月订单笔数；``0`` 表示该地区本月无记录。
    """

    region_id: str
    region_name: str
    gross_cents: int
    refund_cents: int
    net_cents: int
    order_count: int


def _validate_month(month: str) -> str:
    """校验 ``YYYY-MM`` 并返回去掉首尾空白的月份。

    参数：
        month：待校验月份字符串。

    返回：
        规范化后的月份字符串。

    异常：
        ValueError：格式不合法（含 13 月、缺前导零、非字符串等）时抛出。
    """
    if not isinstance(month, str):
        raise ValueError("月份格式必须是 YYYY-MM，例如 2026-09")
    normalized = month.strip()
    if not MONTH_PATTERN.match(normalized):
        raise ValueError(f"月份格式必须是 YYYY-MM，例如 2026-09；收到：{month!r}")
    return normalized


def _month_range(month: str) -> tuple[str, str]:
    """返回半开区间 ``[本月第一天, 次月第一天)`` 的 ISO 日期字符串。

    参数：
        month：已通过校验的 ``YYYY-MM``。

    返回：
        ``("2026-09-01", "2026-10-01")`` 形式的两个字符串，用于 ``>= / <`` 过滤，
        这样能命中 ``order_date`` / ``refund_date`` 上的索引。
    """
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        next_year, next_month_number = year + 1, 1
    else:
        next_year, next_month_number = year, month_number + 1
    return f"{month}-01", f"{next_year:04d}-{next_month_number:02d}-01"


def get_month_sales(conn: sqlite3.Connection, month: str) -> MonthSales:
    """查询某个月的毛销售额、退款额、净销售额与订单/退款计数。

    参数：
        conn：已打开的数据库连接（由 ``db.connect`` 创建即可）。
        month：``YYYY-MM``；格式非法时抛 ``ValueError``。

    返回：
        :class:`MonthSales`。没有任何记录时返回全 0 且 ``has_orders=False``。
    """
    normalized = _validate_month(month)
    start, end = _month_range(normalized)

    order_row = conn.execute(_ORDER_AGG_SQL, (start, end)).fetchone()
    refund_row = conn.execute(_REFUND_AGG_SQL, (start, end)).fetchone()
    refund_count_row = conn.execute(_REFUND_COUNT_SQL, (start, end)).fetchone()

    gross_cents = int(order_row["gross_cents"])
    order_count = int(order_row["order_count"])
    refund_cents = int(refund_row["refund_cents"])
    approved_refund_count = int(refund_row["approved_refund_count"])
    pending_refund_count = int(refund_count_row["pending_refund_count"])
    refund_record_count = int(refund_count_row["refund_record_count"])

    return MonthSales(
        month=normalized,
        gross_cents=gross_cents,
        refund_cents=refund_cents,
        net_cents=gross_cents - refund_cents,
        order_count=order_count,
        approved_refund_count=approved_refund_count,
        pending_refund_count=pending_refund_count,
        has_orders=order_count > 0,
        has_any_refund_records=refund_record_count > 0,
    )


def get_region_breakdown(conn: sqlite3.Connection, month: str) -> list[RegionSales]:
    """查询某月全部地区的销售与退款明细（按 ``region_id`` 升序）。

    参数：
        conn：已打开的数据库连接。
        month：``YYYY-MM``；格式非法时抛 ``ValueError``。

    返回：
        ``list[RegionSales]``，**包含当月没有订单的地区**（其字段为 0、``order_count=0``），
        这样上层可以区分“无记录”和“真零”。

    说明：
        退款按 ``refunds.refund_date`` 归属月份，地区归属取自退款对应订单的 ``region_id``。
    """
    normalized = _validate_month(month)
    start, end = _month_range(normalized)
    sql = """
    WITH order_agg AS (
        SELECT region_id,
               SUM(quantity * unit_price_cents) AS gross_cents,
               COUNT(*) AS order_count
        FROM orders
        WHERE order_date >= ? AND order_date < ?
        GROUP BY region_id
    ),
    refund_agg AS (
        SELECT orders.region_id AS region_id,
               SUM(refunds.amount_cents) AS refund_cents
        FROM refunds
        JOIN orders ON orders.order_id = refunds.order_id
        WHERE refunds.status = 'approved'
          AND refunds.refund_date >= ? AND refunds.refund_date < ?
        GROUP BY orders.region_id
    )
    SELECT regions.region_id AS region_id,
           regions.region_name AS region_name,
           COALESCE(order_agg.gross_cents, 0) AS gross_cents,
           COALESCE(refund_agg.refund_cents, 0) AS refund_cents,
           COALESCE(order_agg.order_count, 0) AS order_count
    FROM regions
    LEFT JOIN order_agg ON order_agg.region_id = regions.region_id
    LEFT JOIN refund_agg ON refund_agg.region_id = regions.region_id
    ORDER BY regions.region_id
    """
    rows = conn.execute(sql, (start, end, start, end)).fetchall()
    results: list[RegionSales] = []
    for row in rows:
        gross_cents = int(row["gross_cents"])
        refund_cents = int(row["refund_cents"])
        results.append(
            RegionSales(
                region_id=str(row["region_id"]),
                region_name=str(row["region_name"]),
                gross_cents=gross_cents,
                refund_cents=refund_cents,
                net_cents=gross_cents - refund_cents,
                order_count=int(row["order_count"]),
            )
        )
    return results


def list_available_months(conn: sqlite3.Connection) -> list[str]:
    """列出数据库里真实出现过的月份（``orders`` 与 ``refunds`` 的并集，升序）。

    参数：
        conn：已打开的数据库连接。

    返回：
        ``list[str]``，元素形如 ``"2026-09"``。

    说明：
        只返回“真实存在记录”的月份，因此 ``2025-01``（构造为空月）不会出现在结果里；
        声明数据范围是 ``2025-01`` 至 ``2026-09``。用户仍可以直接查询空月，
        ``get_month_sales`` 会返回 ``has_orders=False`` 的全 0 结果。
    """
    sql = """
    SELECT month FROM (
        SELECT DISTINCT substr(order_date, 1, 7) AS month FROM orders
        UNION
        SELECT DISTINCT substr(refund_date, 1, 7) AS month FROM refunds
    )
    WHERE month IS NOT NULL
    ORDER BY month
    """
    rows = conn.execute(sql).fetchall()
    return [str(row["month"]) for row in rows]


def list_regions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """列出全部地区。

    参数：
        conn：已打开的数据库连接。

    返回：
        字典列表，键为 ``region_id`` / ``region_name`` / ``manager``，按 ``region_id`` 升序。
    """
    rows = conn.execute(
        "SELECT region_id, region_name, manager FROM regions ORDER BY region_id"
    ).fetchall()
    return [
        {
            "region_id": str(row["region_id"]),
            "region_name": str(row["region_name"]),
            "manager": str(row["manager"]),
        }
        for row in rows
    ]


def get_region(conn: sqlite3.Connection, name_or_id: str) -> dict[str, Any] | None:
    """按地区中文名或地区 ID 查一个地区。

    参数：
        conn：已打开的数据库连接。
        name_or_id：``"华东"`` 或 ``"R_EAST"``，首尾空白会被忽略。

    返回：
        命中时返回 ``{"region_id", "region_name", "manager"}``；未命中返回 ``None``
        （由调用方决定是澄清还是报错，查询层不抛业务异常）。
    """
    if not isinstance(name_or_id, str):
        return None
    key = name_or_id.strip()
    if not key:
        return None
    row = conn.execute(
        """
        SELECT region_id, region_name, manager
        FROM regions
        WHERE region_id = ? OR region_name = ?
        ORDER BY CASE WHEN region_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (key, key, key),
    ).fetchone()
    if row is None:
        return None
    return {
        "region_id": str(row["region_id"]),
        "region_name": str(row["region_name"]),
        "manager": str(row["manager"]),
    }


def get_product_sales(conn: sqlite3.Connection, month: str) -> list[dict[str, Any]]:
    """查询某月的商品维度明细（扩展维度，按 ``product_id`` 升序）。

    参数：
        conn：已打开的数据库连接。
        month：``YYYY-MM``；格式非法时抛 ``ValueError``。

    返回：
        字典列表，包含**全部商品**（当月没有订单的商品返回 0 且 ``order_count=0``），
        键为 ``product_id`` / ``product_name`` / ``category`` /
        ``gross_cents`` / ``refund_cents`` / ``net_cents`` / ``order_count``。

    说明：
        ``channel`` 虽然在 ``orders`` 表里有值，但按规格第 4 节第 11 条，
        分析工具不支持渠道维度，本函数也不返回渠道拆分。
    """
    normalized = _validate_month(month)
    start, end = _month_range(normalized)
    sql = """
    WITH order_agg AS (
        SELECT product_id,
               SUM(quantity * unit_price_cents) AS gross_cents,
               COUNT(*) AS order_count
        FROM orders
        WHERE order_date >= ? AND order_date < ?
        GROUP BY product_id
    ),
    refund_agg AS (
        SELECT orders.product_id AS product_id,
               SUM(refunds.amount_cents) AS refund_cents
        FROM refunds
        JOIN orders ON orders.order_id = refunds.order_id
        WHERE refunds.status = 'approved'
          AND refunds.refund_date >= ? AND refunds.refund_date < ?
        GROUP BY orders.product_id
    )
    SELECT products.product_id AS product_id,
           products.product_name AS product_name,
           products.category AS category,
           COALESCE(order_agg.gross_cents, 0) AS gross_cents,
           COALESCE(refund_agg.refund_cents, 0) AS refund_cents,
           COALESCE(order_agg.order_count, 0) AS order_count
    FROM products
    LEFT JOIN order_agg ON order_agg.product_id = products.product_id
    LEFT JOIN refund_agg ON refund_agg.product_id = products.product_id
    ORDER BY products.product_id
    """
    rows = conn.execute(sql, (start, end, start, end)).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        gross_cents = int(row["gross_cents"])
        refund_cents = int(row["refund_cents"])
        results.append(
            {
                "product_id": str(row["product_id"]),
                "product_name": str(row["product_name"]),
                "category": str(row["category"]),
                "gross_cents": gross_cents,
                "refund_cents": refund_cents,
                "net_cents": gross_cents - refund_cents,
                "order_count": int(row["order_count"]),
            }
        )
    return results
