"""ops-assistant：经营数据分析与知识库智能助手（演示项目）。
本包按职责分为 config / data / domain / knowledge / agent / workflow / report / api / ui 子包。
导入本包不做任何 I/O、不读取 .env、不连接数据库，副作用仅限版本号常量。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
