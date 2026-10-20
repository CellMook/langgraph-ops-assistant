"""单元 13 实验：指标计算、退款归属与差额分解。

保存路径：教程/实验/13_metrics_check.py

运行（在 ops-assistant/ 目录下）：
    uv run python ../教程/实验/13_metrics_check.py
    uv run python ../教程/实验/13_metrics_check.py --current 2026-09 --previous 2026-08
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

#: 教程/实验/13_metrics_check.py → parents[0]=实验, [1]=教程, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_DIR = REPO_ROOT / "ops-assistant"

#: 手写 SQL：故意不用 queries.py，用来独立复核。
GROSS_SQL = """
SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents,
       COUNT(*) AS order_count
FROM orders
WHERE order_date >= ? AND order_date < ?
"""

REFUND_SQL = """
SELECT COALESCE(SUM(amount_cents), 0) AS refund_cents,
       COUNT(*) AS approved_refund_count
FROM refunds
WHERE status = 'approved'
  AND refund_date >= ? AND refund_date < ?
"""

CROSS_MONTH_SQL = """
SELECT refunds.refund_id AS refund_id,
       refunds.amount_cents AS amount_cents,
       refunds.status AS status,
       refunds.refund_date AS refund_date,
       orders.order_id AS order_id,
       orders.order_date AS order_date,
       orders.region_id AS region_id
FROM refunds
JOIN orders ON orders.order_id = refunds.order_id
WHERE substr(orders.order_date, 1, 7) = ?
  AND substr(refunds.refund_date, 1, 7) = ?
ORDER BY refunds.refund_date, refunds.refund_id
"""


def _resolve_db(explicit: Path | None) -> Path:
    """决定要打开哪个数据库文件。"""
    if explicit is not None:
        return explicit
    return PROJECT_DIR / "data" / "ops.db"


def _month_bounds(month: str) -> tuple[str, str]:
    """返回半开区间 [本月 1 号, 次月 1 号)。"""
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        return f"{month}-01", f"{year + 1:04d}-01-01"
    return f"{month}-01", f"{year:04d}-{month_number + 1:02d}-01"


def cents_to_yuan_text(cents: int) -> str:
    """整数分 → 元，保留 2 位小数；全程整数运算，不经过 float。"""
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    return f"{sign}{value // 100:,}.{value % 100:02d} 元"


def cents_to_wan_text(cents: int) -> str:
    """整数分 → 万元，保留 2 位小数（四舍五入用 +5000 再整除实现）。"""
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    hundredths = (value + 5_000) // 10_000
    return f"{sign}{hundredths // 100:,}.{hundredths % 100:02d} 万元"


def _independent_numbers(
    conn: sqlite3.Connection, month: str
) -> tuple[int, int, int, int]:
    """手写 SQL 独立算 (毛额, 退款额, 净额, 订单数)，单位分。"""
    start, end = _month_bounds(month)
    gross_row = conn.execute(GROSS_SQL, (start, end)).fetchone()
    refund_row = conn.execute(REFUND_SQL, (start, end)).fetchone()
    gross = int(gross_row["gross_cents"])
    refund = int(refund_row["refund_cents"])
    return gross, refund, gross - refund, int(gross_row["order_count"])


def _show_independent(conn: sqlite3.Connection, months: list[str]) -> None:
    """[1] 手写 SQL 的独立计算结果。"""
    print("[1] 手写 SQL 独立计算（不 import queries.py）：")
    for month in months:
        gross, refund, net, orders = _independent_numbers(conn, month)
        print(
            f"    {month}: 毛额 {cents_to_yuan_text(gross)}"
            f" / 退款 {cents_to_yuan_text(refund)}"
            f" / 净额 {cents_to_yuan_text(net)}（订单 {orders} 笔）"
        )


def _cross_check(
    conn: sqlite3.Connection, months: list[str]
) -> list[str]:
    """[2] 独立结果 vs 查询层；返回不一致的月份。"""
    from ops_assistant.data.queries import get_month_sales

    print("[2] 与查询层逐项比对：")
    mismatched: list[str] = []
    for month in months:
        gross, refund, net, orders = _independent_numbers(conn, month)
        sales = get_month_sales(conn, month)
        pairs = (
            ("gross_cents", gross, sales.gross_cents),
            ("refund_cents", refund, sales.refund_cents),
            ("net_cents", net, sales.net_cents),
            ("order_count", orders, sales.order_count),
        )
        bad = [(name, a, b) for name, a, b in pairs if a != b]
        if bad:
            mismatched.append(month)
            for name, mine, theirs in bad:
                print(f"    {month} {name}: 独立 {mine} ≠ 查询层 {theirs}")
        else:
            print(f"    {month} 四项全部一致 ✔")
    return mismatched


def _show_cross_month_refunds(
    conn: sqlite3.Connection, order_month: str, refund_month: str
) -> None:
    """[3] 订单在某月、退款在另一个月的记录。"""
    rows = conn.execute(CROSS_MONTH_SQL, (order_month, refund_month)).fetchall()
    print(f"[3] 订单 {order_month} / 退款 {refund_month} 的跨月退款 {len(rows)} 笔：")
    for row in rows:
        print(
            f"    {row['refund_id']:<16} {row['status']:<8}"
            f" 订单日 {row['order_date']}  退款日 {row['refund_date']}"
            f"  {cents_to_yuan_text(int(row['amount_cents']))}"
        )
    print("    注意：这些金额计入退款月，不改动订单月。")


def _show_comparison(
    conn: sqlite3.Connection, current: str, previous: str
) -> None:
    """[4] 差额、变化率、地区分解与合计校验。"""
    from ops_assistant.domain.metrics import compare_months

    result = compare_months(conn, current, previous)

    print(f"[4] compare_months(current={current!r}, previous={previous!r}, metric='net_sales')")
    print(f"    本期净额 {cents_to_yuan_text(result.current.net_cents)}"
          f" / 上期净额 {cents_to_yuan_text(result.previous.net_cents)}")
    print(f"    差额 {cents_to_yuan_text(result.delta_net_cents)}")
    if result.base_zero:
        print("    变化率：无定义（基期为 0）；base_zero=True")
    else:
        print(f"    变化率：{result.pct_change:.2%}；base_zero=False")
    print(f"    本期订单 {result.current.order_count} 笔"
          f" / 上期订单 {result.previous.order_count} 笔")

    print("    地区差额（按 |差额| 降序）：")
    for item in result.regions:
        share = "—" if item.share_of_delta is None else f"{item.share_of_delta:.1%}"
        print(
            f"      {item.region_id:<8} {item.region_name:<3}"
            f" 本期 {cents_to_yuan_text(item.current_net_cents):>16}"
            f"  上期 {cents_to_yuan_text(item.previous_net_cents):>16}"
            f"  差额 {cents_to_yuan_text(item.delta_cents):>16}  贡献 {share}"
        )

    ok = result.region_delta_sum_cents == result.delta_net_cents
    print(
        f"    合计校验：Σ地区差额 {cents_to_yuan_text(result.region_delta_sum_cents)}"
        f" = 总差额 {cents_to_yuan_text(result.delta_net_cents)} → {ok}"
    )
    print(f"    维度 {result.dimension!r} / 指标 {result.metric!r}"
          f" / 不支持的维度 {result.unsupported_dimension!r}")
    print("    边界说明（notes）：")
    for note in result.notes:
        print(f"      - {note}")
    if not ok:
        raise SystemExit("合计校验失败：地区差额之和与总差额不一致。")


def _show_units() -> None:
    """[5] 整数分换算与浮点陷阱。"""
    print("[5] 单位换算（整数分）与浮点陷阱：")
    for cents in (1234567, 128000, -123456, 0):
        print(f"    {cents:>10} 分 → {cents_to_yuan_text(cents):>14}"
              f" → {cents_to_wan_text(cents):>12}")
    print(f"    知识库例子：1234567 分 → {cents_to_yuan_text(1234567)}"
          f" → {cents_to_wan_text(1234567)}")
    print("    浮点（预期，未在本环境执行）：")
    print("      0.1 + 0.2 = 0.30000000000000004")
    print("      sum([0.1] * 10) == 1.0 → False")
    print("      round(2.675, 2) → 2.67（偶数舍入）")


def main(argv: list[str] | None = None) -> int:
    """命令行入口：返回进程退出码（0 成功，1 环境问题）。"""
    parser = argparse.ArgumentParser(description="单元 13 实验：指标计算与差额分解")
    parser.add_argument("--db", type=Path, default=None,
                        help="数据库路径，默认 ops-assistant/data/ops.db")
    parser.add_argument("--current", default="2026-09", help="本期月份，默认 2026-09")
    parser.add_argument("--previous", default="2026-08", help="上期月份，默认 2026-08")
    args = parser.parse_args(argv)

    db_path = _resolve_db(args.db)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        print("请先在 ops-assistant/ 目录下运行：uv run python scripts/bootstrap.py",
              file=sys.stderr)
        return 1

    from ops_assistant.data.db import connect

    conn = connect(db_path)
    try:
        months = [args.previous, args.current]
        _show_independent(conn, months)
        mismatched = _cross_check(conn, months)
        _show_cross_month_refunds(conn, args.previous, args.current)
        _show_comparison(conn, args.current, args.previous)
    finally:
        conn.close()

    _show_units()

    if mismatched:
        print(f"独立复核失败：{mismatched} 与查询层不一致。")
        return 1
    print("全部检查通过（预期，未在本环境执行）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
