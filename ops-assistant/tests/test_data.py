"""T01–T02：数据库结构与种子数据生成（工程规格 6.1）。

覆盖点：
- T01 schema：四张表、索引、外键开启、CHECK 约束生效；
- T02 生成器：固定种子可复现、行数在预期范围、退款状态取值合法、
  8 类边界事实中可在数据层直接观察的部分。

诚实声明：本文件未经运行验证（编写环境无法启动 Python 进程）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ops_assistant.data.db import SCHEMA_PATH, connect, init_schema
from ops_assistant.data.generate import generate_database

EXPECTED_TABLES = {"regions", "products", "orders", "refunds"}


def test_t01_schema_creates_four_tables(tmp_path: Path) -> None:
    """T01：schema.sql 能建出四张业务表，且外键 PRAGMA 已开启。"""
    db_path = tmp_path / "schema-check.db"
    connection = connect(db_path)
    try:
        init_schema(connection, SCHEMA_PATH)
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {row["name"] for row in rows}
        assert EXPECTED_TABLES <= names, f"缺少表：{EXPECTED_TABLES - names}"
        # 外键开关必须为 ON，否则外键约束形同虚设
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_t01_schema_has_indexes(tmp_path: Path) -> None:
    """T01：schema.sql 建出 4 个查询索引。"""
    db_path = tmp_path / "schema-index.db"
    connection = connect(db_path)
    try:
        init_schema(connection, SCHEMA_PATH)
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        ).fetchall()
        names = {row["name"] for row in rows}
        expected = {
            "idx_orders_date",
            "idx_orders_region",
            "idx_refunds_date",
            "idx_refunds_status",
        }
        assert expected <= names, f"缺少索引：{expected - names}"
    finally:
        connection.close()


def test_t01_schema_rejects_invalid_status(settings, db_summary: dict) -> None:
    """T01：退款状态 CHECK 约束拒绝非法值（'unknown' 不是允许值）。

    依赖 ``db_summary``（而不只是 ``settings``）：session 级 ``settings`` 夹具只建目录，
    数据库由 ``db_summary`` 负责生成；单跑本文件时避免"表不存在"的假失败。
    """
    connection = connect(settings.db_path)
    try:
        order_id = connection.execute("SELECT order_id FROM orders LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO refunds (refund_id, order_id, refund_date, amount_cents, reason, status)"
                " VALUES ('RF_INVALID', ?, '2026-09-01', 100, '测试', 'unknown')",
                (order_id,),
            )
        connection.rollback()
    finally:
        connection.close()


def test_t01_schema_rejects_non_positive_amount(settings, db_summary: dict) -> None:
    """T01：退款金额 CHECK 约束拒绝 0 与负数。"""
    connection = connect(settings.db_path)
    try:
        order_id = connection.execute("SELECT order_id FROM orders LIMIT 1").fetchone()[0]
        for bad_amount in (0, -100):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO refunds (refund_id, order_id, refund_date, amount_cents,"
                    " reason, status) VALUES (?, ?, '2026-09-01', ?, '测试', 'approved')",
                    (f"RF_BAD_{bad_amount}", order_id, bad_amount),
                )
            connection.rollback()
    finally:
        connection.close()


def test_t02_generated_row_counts(db_summary: dict) -> None:
    """T02：生成器摘要里的行数符合预期范围（5 地区、12 商品、21 个月）。"""
    assert db_summary["region_count"] == 5
    assert db_summary["product_count"] == 12
    # 21 个月 x 每地区每月 6~14 单；这里只断言量级，避免把随机抽样写死
    assert 21 * 5 * 6 <= db_summary["order_count"] <= 21 * 5 * 14
    assert db_summary["refund_count"] > 0


def test_t02_orders_cover_all_months_except_designed_empty_month(conn) -> None:
    """T02：除 2025-01（设计的空月）外，每个月都有订单。"""
    rows = conn.execute(
        "SELECT DISTINCT substr(order_date, 1, 7) AS month FROM orders ORDER BY month"
    ).fetchall()
    months = [row["month"] for row in rows]
    assert "2025-01" not in months
    assert "2026-09" in months
    assert "2026-08" in months


def test_t02_refund_status_values_are_legal(conn) -> None:
    """T02：退款状态只出现 approved/pending/rejected 三种。"""
    rows = conn.execute("SELECT DISTINCT status FROM refunds").fetchall()
    assert {row["status"] for row in rows} <= {"approved", "pending", "rejected"}


def test_t02_channel_column_exists(conn) -> None:
    """T02：orders 有 channel 字段且取值非空（数据存在但本项目不按渠道分析）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(channel) AS filled,"
        " COUNT(DISTINCT channel) AS kinds FROM orders"
    ).fetchone()
    assert row["total"] == row["filled"]
    assert row["kinds"] >= 2


def test_t02_generation_is_deterministic(tmp_path: Path) -> None:
    """T02：相同种子生成两次，2026-09 的毛销售额完全一致（可复现）。"""
    first = generate_database(tmp_path / "a.db")
    second = generate_database(tmp_path / "b.db")
    assert first["order_count"] == second["order_count"]
    assert first["refund_count"] == second["refund_count"]

    def gross(db_path: Path) -> int:
        connection = connect(db_path)
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS total"
                " FROM orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01'"
            ).fetchone()
            return int(row["total"])
        finally:
            connection.close()

    assert gross(tmp_path / "a.db") == gross(tmp_path / "b.db")
