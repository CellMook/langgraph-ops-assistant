"""数据层包：表结构、连接工具、固定种子生成器与只读查询函数。

职责划分（对应工程规格第 3 节）：

- ``schema.sql``：与规格 3.1 完全一致的表结构（含 CHECK、外键与索引）。
- ``db.py``：``connect`` / ``init_schema`` / ``SCHEMA_PATH``。
- ``generate.py``：``generate_database``，用固定种子生成可复现数据，并显式构造 8 类边界事实。
- ``queries.py``：``MonthSales`` / ``RegionSales`` 数据类与只读查询函数。

约定：

- 导入本包的任意子模块都不会创建或修改数据库文件；写库只能通过显式调用
  ``generate_database``（或调用 ``init_schema``）发生。
- 金额一律用 ``int`` 表示“分”，禁止浮点存钱；查询层不打印、不格式化输出。
"""
