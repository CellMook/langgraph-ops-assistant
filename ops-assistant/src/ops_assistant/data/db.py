"""SQLite 连接与建表工具（工程规格 3.4 的 ``db.py``）。

职责：

1. ``connect``：统一打开连接，设置 ``row_factory=sqlite3.Row`` 与 ``PRAGMA foreign_keys = ON``。
2. ``init_schema``：执行同目录的 ``schema.sql`` 建表。
3. 本模块不写业务 SQL、不打印日志，也不在 import 阶段碰任何磁盘文件。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: ``schema.sql`` 的默认位置（与本模块同目录），避免硬编码绝对路径。
SCHEMA_PATH: Path = Path(__file__).resolve().with_name("schema.sql")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开数据库连接，并设置行工厂与外键约束。

    参数：
        db_path：数据库文件路径；传入字符串 ``":memory:"`` 时创建内存数据库（测试用）。

    返回：
        sqlite3.Connection：``row_factory`` 为 ``sqlite3.Row``（可按列名取值）、
        且 ``PRAGMA foreign_keys`` 已开启的连接。调用方负责 ``close()``。

    说明：
        - 父目录不存在时会自动创建，方便首次生成 ``data/ops.db``。
        - 打开连接本身不会建表；建表请继续调用 :func:`init_schema`。
    """
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    """读取并执行 ``schema.sql``，创建全部表与索引。

    参数：
        conn：由 :func:`connect` 打开的连接。
        schema_path：表结构文件路径；为 ``None`` 时使用同目录的 ``schema.sql``。

    返回：
        ``None``（执行后会 ``commit``）。

    说明：
        ``schema.sql`` 使用普通 ``CREATE TABLE``（与规格 3.1 保持一致），
        因此**不能**在同一个数据库上重复执行；重复初始化时请先删除旧文件，
        或者换一个新的 ``db_path``。``PRAGMA foreign_keys = ON`` 也写进了脚本，
        所以即使调用方用裸 ``sqlite3.connect`` 打开库再建表，外键同样生效。
    """
    target = Path(schema_path) if schema_path is not None else SCHEMA_PATH
    sql = target.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
