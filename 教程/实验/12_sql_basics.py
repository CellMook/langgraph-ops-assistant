"""单元 12 实验：看清四张表、半开区间与参数化查询。

保存路径：教程/实验/12_sql_basics.py

运行（在 ops-assistant/ 目录下）：
    uv run python ../教程/实验/12_sql_basics.py
    uv run python ../教程/实验/12_sql_basics.py --db data/ops.db --month 2026-09
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

#: 教程/实验/12_sql_basics.py → parents[0]=实验, [1]=教程, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIR = REPO_ROOT / "ops-assistant"

#: 表名与它的计数 SQL 成对写死；绝不用 f-string 拼表名（表名不能用 ? 绑定）。
COUNT_SQL: tuple[tuple[str, str], ...] = (
    ("regions", "SELECT COUNT(*) AS n FROM regions"),
    ("products", "SELECT COUNT(*) AS n FROM products"),
    ("orders", "SELECT COUNT(*) AS n FROM orders"),
    ("refunds", "SELECT COUNT(*) AS n FROM refunds"),
)

RANGE_SQL = """
SELECT COUNT(*) AS n,
       COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents
FROM orders
WHERE order_date >= ? AND order_date < ?
"""

LIKE_SQL = """
SELECT COUNT(*) AS n,
       COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents
FROM orders
WHERE order_date LIKE ?
"""


def _resolve_db(explicit: Path | None) -> Path:
    """决定要打开哪个数据库文件。"""
    if explicit is not None:
        return explicit
    return PROJECT_DIR / "data" / "ops.db"


def _open_plain(db_path: Path) -> sqlite3.Connection:
    """不用项目代码裸连一次，证明“表结构确实在文件里”。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _next_month_start(month: str) -> str:
    """自己算次月 1 号：12 月要跨年。"""
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        return f"{year + 1:04d}-01-01"
    return f"{year:04d}-{month_number + 1:02d}-01"


def _show_schema_objects(conn: sqlite3.Connection) -> None:
    """[1] 列出库里真实存在的表与索引。"""
    rows = conn.execute(
        """
        SELECT type, name FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    print("[1] 数据库里真实存在的对象：")
    for row in rows:
        print(f"    {row['type']:<6} {row['name']}")


def _show_row_counts(conn: sqlite3.Connection) -> None:
    """[2] 看四张表各有多少行。"""
    print("[2] 四张表的行数：")
    for table, sql in COUNT_SQL:
        count = conn.execute(sql).fetchone()["n"]
        print(f"    {table:<9} {count}")


def _explain(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> None:
    """打印执行计划；sql 必须是本文件里的常量，绝不能来自用户输入。"""
    for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall():
        print(f"      {row['detail']}")


def _compare_range_and_like(conn: sqlite3.Connection, month: str) -> bool:
    """[3] 两种写法结果是否一致，以及执行计划差在哪里。"""
    start = f"{month}-01"
    end = _next_month_start(month)
    range_row = conn.execute(RANGE_SQL, (start, end)).fetchone()
    like_row = conn.execute(LIKE_SQL, (f"{month}%",)).fetchone()

    print(f"[3] 同一个月的两种写法（{month}）：")
    print(f"    半开区间：{range_row['n']} 行，毛额 {range_row['gross_cents']} 分")
    print(f"    LIKE    ：{like_row['n']} 行，毛额 {like_row['gross_cents']} 分")
    print("    半开区间的执行计划：")
    _explain(conn, RANGE_SQL, (start, end))
    print("    LIKE 的执行计划：")
    _explain(conn, LIKE_SQL, (f"{month}%",))

    same = (range_row["n"] == like_row["n"]) and (
        range_row["gross_cents"] == like_row["gross_cents"]
    )
    print(f"    两者结果一致：{same}（一致不代表代价一致，看上面两段计划）")
    return same


def _parameter_demo(conn: sqlite3.Connection) -> None:
    """[4] 敌意输入：当参数 vs 拼进 SQL。"""
    hostile = "2026-09' OR '1'='1"

    safe_row = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = ?",
        (hostile,),
    ).fetchone()
    print(f"[4] 敌意输入 {hostile!r} 当参数传入：命中 {safe_row['n']} 行（它只是数据）")

    built = (
        "SELECT COUNT(*) AS n FROM orders "
        f"WHERE substr(order_date, 1, 7) = '{hostile}'"
    )
    print("    同一句话如果拼进 SQL，发给数据库的文本会变成：")
    print(f"      {built}")
    print("    这里只打印、不执行；执行它会命中整张表（条件被 OR 短路）。")


def _project_layer(db_path: Path, month: str, empty_month: str) -> None:
    """[5] 用项目自己的查询层，对比“有数据”与“无记录”。"""
    from ops_assistant.data.db import connect
    from ops_assistant.data.queries import (
        get_month_sales,
        get_region_breakdown,
        list_available_months,
    )

    conn = connect(db_path)
    try:
        sales = get_month_sales(conn, month)
        print(f"[5] get_month_sales({month!r})：")
        print(f"    毛额 {sales.gross_cents} 分 / 退款 {sales.refund_cents} 分"
              f" / 净额 {sales.net_cents} 分")
        print(f"    订单 {sales.order_count} 笔，approved 退款 {sales.approved_refund_count} 笔，"
              f"pending 退款 {sales.pending_refund_count} 笔")
        print(f"    has_orders={sales.has_orders}, "
              f"has_any_refund_records={sales.has_any_refund_records}")

        print(f"[5] get_region_breakdown({month!r})：")
        for region in get_region_breakdown(conn, month):
            flag = "无记录" if region.order_count == 0 else "有订单"
            print(f"    {region.region_id:<8} {region.region_name:<3} "
                  f"净额 {region.net_cents:>12} 分  订单 {region.order_count:>3} 笔  {flag}")

        available = list_available_months(conn)
        print(f"[5] 库中真实出现过的月份 {len(available)} 个：{available[0]} … {available[-1]}")

        empty = get_month_sales(conn, empty_month)
        print(f"[5] 空月 {empty_month!r}：毛额 {empty.gross_cents} 分，"
              f"has_orders={empty.has_orders}（无记录，而不是“销售额为 0”）")
        print(f"    {empty_month!r} 在 list_available_months() 里：{empty_month in available}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """命令行入口：返回进程退出码（0 成功，1 失败）。"""
    parser = argparse.ArgumentParser(description="单元 12 实验：四张表、半开区间与参数化查询")
    parser.add_argument("--db", type=Path, default=None, help="数据库路径，默认 ops-assistant/data/ops.db")
    parser.add_argument("--month", default="2026-09", help="要观察的月份，默认 2026-09")
    parser.add_argument("--empty-month", default="2025-01", help="已知无记录的月份，默认 2025-01")
    args = parser.parse_args(argv)

    db_path = _resolve_db(args.db)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        print("请先在 ops-assistant/ 目录下运行：uv run python scripts/bootstrap.py", file=sys.stderr)
        return 1

    conn = _open_plain(db_path)
    try:
        _show_schema_objects(conn)
        _show_row_counts(conn)
        consistent = _compare_range_and_like(conn, args.month)
        _parameter_demo(conn)
    finally:
        conn.close()

    _project_layer(db_path, args.month, args.empty_month)

    if not consistent:
        print("两种日期写法的结果不一致，说明数据里存在不符合 YYYY-MM-DD 定长格式的日期。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
