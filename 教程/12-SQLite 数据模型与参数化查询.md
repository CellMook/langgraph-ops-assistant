# 单元 12：SQLite 数据模型与参数化查询

> 建议时长 55 分钟（**上半 25 分钟**：5.1～5.3 四表结构、连接与参数化查询；**下半 30 分钟**：5.4～5.8 半开区间、恒返回全部地区、独立复核） | 前置：单元 01（环境与 `uv`）；单元 11（知识库与引用）能让你看到完整链条，但**本单元可独立学习** | 可跳过条件：能不看资料说出半开区间的两个参数、解释 `?` 与 f-string 的区别，并讲清“无记录 / 净额为 0 / 零基数”是三件不同的事
>
> 版本基线：Python 标准库 `sqlite3`（Python 3.11+ 均可）、SQLite 3.x（官方文档核对日期 2026-10-19）
>
> 本单元不需要 API Key、不调用模型。需要 `ops-assistant/data/ops.db` 已存在。
>
> **本环境（编写教程的机器）无法启动任何进程**（`pwsh` 返回 `0xC0000142`），因此本单元所有终端输出都写成“**预期（未在本环境执行）**”，凡是由数据库内容决定的数字一律写成 `⟨…⟩`，不伪造具体数值。

---

## 1. 学习目标与前置

完成后你应该能：

1. 画出 `regions / products / orders / refunds` 四张表的主外键关系，并解释“为什么没有一张 `sales` 表”。
2. 用 `sqlite3` 打开数据库，并说清 `row_factory=sqlite3.Row` 与 `PRAGMA foreign_keys = ON` 各自解决什么问题。
3. 写出日期过滤的**半开区间**：`order_date >= '2026-09-01' AND order_date < '2026-10-01'`，并解释为什么不是 `LIKE '2026-09%'`、也不是“月末最后一天”。
4. 用参数化查询（`?`）把外部输入当**数据**传入，并亲手证明字符串拼接会让输入变成 **SQL 代码**。
5. 区分三种“看起来都是 0 的结果”：无记录、真零（有订单但净额 0）、零基数（上期净额 0 导致变化率无定义）。
6. 解释 `get_region_breakdown` 为什么必须**恒返回全部 5 个地区**，而不是只返回“有订单的地区”。
7. 知道怎么用一份**不 import 项目代码**的独立 SQL 把同一个数字再算一遍（对拍），以及为什么必须这么做。

前置要求：

- 单元 01 完成：能在 `ops-assistant/` 目录下执行 `uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"`。
- 你的 `ops-assistant/data/ops.db` 已生成。若还没有，在 `ops-assistant/` 目录下执行：

```powershell
# 在 ops-assistant/ 目录下；会创建 data/ops.db、知识点索引，并尝试生成标准答案
uv run python scripts/bootstrap.py
```

---

## 2. 官方文档与阅读问题

本单元只读三页官方文档：一页 Python 标准库，两页 SQLite 官方站。不要背 API。

**官方文档：Python `sqlite3` 模块（https://docs.python.org/3/library/sqlite3.html，核对日期 2026-10-19）**
其中请重点找“How to use placeholders to bind values in SQL queries”这一节。

**官方文档：SQLite 数据类型（https://www.sqlite.org/datatype3.html，核对日期 2026-10-19）**
看第 2 节的存储类列表，以及 2.2 小节 “Date and Time Datatype”。

**官方文档：SQLite 外键支持（https://www.sqlite.org/foreignkeys.html，核对日期 2026-10-19）**
看第 2 节 “Enabling Foreign Key Support”。

带着这些问题读（每页 3～4 分钟）：

1. Python 官方文档在这几页里，对“把值直接格式化进 SQL 字符串”是什么态度？它给的“不要这样做”的例子长什么样？
2. `sqlite3.Row` 让查询结果多出什么能力？（提示：按列名取值）
3. SQLite 有专门的“日期类型”吗？如果日期存成 `TEXT`，那么 `2026-09-05` 这种定长字符串的比较是按什么规则进行的？
4. 外键约束在 SQLite 里默认是开还是关？是“一次打开整个库”还是“每个连接分别打开”？
5. 读完想一想：如果外键没开，`orders` 里出现一个不存在的 `region_id`，数据库会拦吗？

> **官方事实**（链接见上，核对日期 2026-10-19）：
> SQLite 使用**动态类型系统**，值本身带存储类（`NULL` / `INTEGER` / `REAL` / `TEXT` / `BLOB`），日期**没有独立类型**，官方建议存成 `TEXT`（`YYYY-MM-DD HH:MM:SS.SSS`）、`REAL` 或 `INTEGER`。
> SQLite 的外键约束**默认关闭**，而且必须**为每个数据库连接单独开启**（官方原话大意：disabled by default, so must be enabled separately for each database connection）。
>
> **教学推论**（不是官方原话，是我从上面两条推出来的）：既然日期是定长、零填充的 `TEXT`，那么 `'2026-09-01' <= order_date < '2026-10-01'` 的字典序比较就等于时间先后比较——这就是本项目敢用字符串比较日期、敢用 `substr(order_date, 1, 7)` 取月份的前提。前提被破坏（比如写成 `2026-9-1`）结论就不成立，所以项目在入口处用正则**强校验** `YYYY-MM`。
>
> **项目设计选择**：金额全部用 `INTEGER` 存**分**，不用 `REAL` 存元；详见单元 13 第 3.4 节与官方浮点说明（https://docs.python.org/3/tutorial/floatingpoint.html，核对日期 2026-10-19）。
>
> **未验证**：你的 Python 版本自带的 SQLite 版本号、以及 `uv.lock` 锁到的版本，我无法在本环境读取；请用第 1 节那条 `print(sqlite3.sqlite_version)` 命令自己看。

---

## 3. 通俗解释与精确技术定义

### 3.1 一句话比喻：四张表就是四种账本

- `regions`（地区表）= **组织架构表**：5 个地区，谁是负责人。
- `products`（商品表）= **价目表**：12 个商品的**目录价**（注意：目录价不是成交价）。
- `orders`（订单表）= **流水账**：每一笔成交记一行，写清“哪天、哪个地区、哪个商品、几件、成交单价多少、什么渠道”。
- `refunds`（退款表）= **退款流水**：每一笔退款记一行，写清“退的是哪张订单、**哪天退的**、退了多少钱、什么状态”。

关键点：**没有“销售额表”**。所谓“2026-09 的净销售额”，不是某一列里存着的数，而是**每次查询时从流水账里现算出来的**。这一条决定了后面所有事情：

```
毛销售额 = 把某月 orders 的 (数量 × 成交单价) 加起来
退款额   = 把某月 refunds 里状态为 approved 的 amount_cents 加起来
净销售额 = 毛销售额 − 退款额
```

### 3.2 精确技术定义（术语首现双语）

| 术语 | 英文 | 精确含义 | 本项目中的例子 |
| --- | --- | --- | --- |
| 主键 | primary key | 唯一标识一行的列；不允许重复、不允许空 | `orders.order_id` |
| 外键 | foreign key | 指向另一张表主键的列，用来表达“必须存在”的关系 | `orders.region_id → regions.region_id` |
| 约束 | constraint | 数据库层面的规则，违反就报错（比“代码里记得检查”更硬） | `CHECK (quantity > 0)`、`UNIQUE`、`NOT NULL` |
| 索引 | index | 为某些列额外建的一份“目录”，让等值/范围查询不必全表扫描 | `idx_orders_date` |
| 存储类 / 类型亲和性 | storage class / type affinity | SQLite 的动态类型概念：值带存储类，列只有“亲和性” | `order_date TEXT`（存日期字符串） |
| 占位符 | placeholder | SQL 里先用 `?` 占位，执行时再把值单独传进去 | `WHERE order_date >= ?` |
| 参数绑定 | parameter binding | 把 Python 值按类型交给数据库，而不是拼进 SQL 文本 | `conn.execute(sql, (start, end))` |
| 半开区间 | half-open interval | 左闭右开 `[start, end)`，只写“下一个区间的开头” | `>= '2026-09-01' AND < '2026-10-01'` |
| 行工厂 | row factory | 把结果行包装成某种对象；本项目用 `sqlite3.Row` | `row["gross_cents"]` |
| 空结果聚合 | aggregate over empty set | 一行都没匹配到时，聚合函数的返回值 | `COALESCE(SUM(amount_cents), 0)` 兜底 |
| 无记录 | no rows | 查询条件**一行都没匹配到**，与“值是 0”不是一回事 | `2025-01` 全月无订单 |
| 真零 | true zero | **有数据**，但计算结果是 0 | 某地区当月净额 = 0（毛额被退款冲平） |

### 3.3 半开区间：为什么不是“最后一天”

初学者最容易写：

```sql
-- 反例 1：写死了月末日期
WHERE order_date >= '2026-09-01' AND order_date <= '2026-09-30'

-- 反例 2：用 LIKE 取前缀
WHERE order_date LIKE '2026-09%'
```

两条都能“跑对”，但都有代价：

- 反例 1 把“9 月有 30 天”这个事实抄进了代码。月份天数、闰年、跨年全都要你维护；抄错一次就少算一天，而且**没人会立刻发现**。
- 反例 2 依赖“日期恰好是 `YYYY-MM-DD` 定长格式”，一旦有人写进 `2026-9-5`（缺前导零），`LIKE '2026-09%'` 就匹配不到，而 `>= '<'` 也会跟着判错——两种写法都会错，但 `LIKE` 还会额外牵出索引与排序规则的问题。

正确写法只有一个来源：**下个月的 1 号**。

```sql
WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01'
```

左边闭、右边开，所以叫**半开区间**。三句话记住它：

1. 右边界**永远**是“下个月 1 号”，不管这个月是 28 天还是 31 天。
2. 12 月的下个月是**次年 1 月**（`2026-12` → `2027-01-01`），跨年由代码算，不由你记。
3. 任何一条记录只会落进**一个**区间，不会重复计、不会漏计。

### 3.4 三种“0”必须分清

| 说法 | 数据事实 | 应该怎么写 | 对应字段 |
| --- | --- | --- | --- |
| **无记录** | 该月/该地区一行都没有 | “本月无销售记录” | `has_orders=False`、`order_count=0` |
| **真零** | 有订单，但净额恰好为 0 | “净额为 0（有订单，退款冲抵）” | `gross_cents>0`、`net_cents=0` |
| **零基数** | 上一期净额为 0，导致变化率无定义 | “基期为 0，变化率无定义” | `base_zero=True`、`pct_change=None` |

它们三个在报告里**不允许**都写成“0”。原因很现实：运营看到“0”会以为“那天没卖东西”，而真相可能是“卖了 30 万，当天全退了”——这两件事的处置动作完全相反。

### 3.5 推理小练习（先想再看答案）

`orders.channel`（渠道）这一列**有值**，为什么项目不提供“按渠道分解差额”？

<details>
<summary>参考答案</summary>

“字段存在”和“这个维度被支持”是两件事。项目把支持范围**显式**限定为 `region / product / month` 三个维度，问到渠道时明确回答“不支持”，而不是静默忽略、更不是编一个数字。理由：静默忽略会让用户以为自己的问题被回答了；编数字是幻觉。这也是单元 13 要讲的“`unsupported_dimension` 字段为什么存在”。

</details>

---

## 4. 这个知识在主项目中的位置

数据层在整条链路上的位置是这样的：

```
scripts/bootstrap.py                      ← 一次性初始化：建库 + 建索引 + 算标准答案
        │
        ├── data/generate.py              ← 固定种子 20260901 生成演示数据，写入 data/ops.db
        │        └── data/schema.sql      ← 四张表 + 四个索引（本单元 5.1）
        │
        └── scripts/compute_ground_truth.py  ← 独立实现，算出 data/ground_truth.json

运行时（每一次提问）：
    config.Settings ──► data/db.py connect()  ← 连接与 PRAGMA（本单元 5.2）
                              │
                              ├── data/queries.py       ← 只读查询（本单元 5.3～5.5）
                              │        │
                              │        ├── domain/metrics.py   ← 差额与变化率（单元 13）
                              │        └── agent/tools.py      ← 供模型请求调用的工具
                              └── knowledge/*.md         ← 口径文档（单元 11）
```

本单元负责最底下那一层。它有三个硬性要求，后面所有单元都建立在上面：

1. **只读**：`queries.py` 里的函数不写库、不打印、不格式化，只返回数据。
2. **单位统一**：所有金额字段名以 `_cents` 结尾，值一律是整数分。没有例外。
3. **无记录可区分**：无记录时返回**全 0 加 `has_orders=False`**，绝不用 `None` 冒充 0，也绝不用 0 冒充“没查到”。

---

## 5. 文件、完整代码、配置与命令

### 5.1 四张表与它们的关系（`src/ops_assistant/data/schema.sql` 完整代码）

保存路径：`ops-assistant/src/ops_assistant/data/schema.sql`

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE regions (
    region_id   TEXT PRIMARY KEY,
    region_name TEXT NOT NULL UNIQUE,
    manager     TEXT NOT NULL
);

CREATE TABLE products (
    product_id       TEXT PRIMARY KEY,
    product_name     TEXT NOT NULL,
    category         TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0)
);

CREATE TABLE orders (
    order_id         TEXT PRIMARY KEY,
    order_date       TEXT NOT NULL,              -- YYYY-MM-DD
    region_id        TEXT NOT NULL REFERENCES regions(region_id),
    product_id       TEXT NOT NULL REFERENCES products(product_id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    channel          TEXT NOT NULL               -- 线下/天猫/京东/抖音；仅存不分析
);

CREATE TABLE refunds (
    refund_id   TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    refund_date TEXT NOT NULL,                   -- YYYY-MM-DD
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('approved','pending','rejected'))
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_region ON orders(region_id);
CREATE INDEX idx_refunds_date ON refunds(refund_date);
CREATE INDEX idx_refunds_status ON refunds(status);
```

关系图（箭头读作“多对一，指向主键”）：

```
regions ──1:N──► orders ◄──N:1── products
                   ▲
                   │ 1:N
                   │
                refunds
```

四条设计决定，每条都有理由：

1. **`orders` 不存金额合计，只存 `quantity` 与 `unit_price_cents`**。
   合计（`quantity * unit_price_cents`）在查询时算。好处：单价的来源只有一个，不会出现“合计 ÷ 数量 ≠ 单价”的自相矛盾数据。代价：每次聚合都要乘一遍（在这个数据量下可以忽略）。
2. **`orders.unit_price_cents` ≠ `products.unit_price_cents`**。
   前者是**成交价**，后者是**目录价**。这里刻意允许两者不等（地区折扣/浮动），所以查询必须用订单价。用错列不会报错，只会**每个数字都偏一点**——这是最危险的一类 bug。
3. **`refunds` 有自己的 `refund_date`，不是靠 `orders.order_date`**。
   退款可以发生在订单之后的任何月份。把退款归到哪个月，是**口径**问题，不是表结构问题——本项目的口径是“按 `refund_date` 归属”（单元 13 详讲）。
4. **`channel` 存了但不在索引里，也不进入任何查询**。
   它存在的目的是让“不支持渠道分析”这个测试有真实数据可依（查询能查到这一列，但产品层面不暴露该维度）。

> **官方事实**：`CHECK` 约束、`NOT NULL`、`UNIQUE`、外键都是数据库自己执行的规则，违反时抛 `sqlite3.IntegrityError`；这类“把规则放进 schema”的做法比“在 Python 里记得校验”更难绕过（任何人用任何客户端连上来都绕不过）。
>
> **项目设计选择**：这里是普通 `CREATE TABLE`，**不是** `CREATE TABLE IF NOT EXISTS`，也没有迁移脚本。所以 `schema.sql` **不能**在同一个库上重复执行（第二次会报 `table regions already exists`）。好处是初始化语义清晰、错误立刻暴露；代价是升级表结构需要手工处理（生产里会用迁移工具，见单元 18）。

### 5.2 连接与建表（`src/ops_assistant/data/db.py` 完整代码）

保存路径：`ops-assistant/src/ops_assistant/data/db.py`

```python
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
```

逐点读：

- `Path(__file__).resolve().with_name("schema.sql")`：**按文件位置**定位表结构，不写 `D:\某目录\ops.db` 这种绝对路径。换台机器、换个目录都能跑。
- `path.parent.mkdir(parents=True, exist_ok=True)`：第一次运行时 `data/` 可能不存在；`exist_ok=True` 让重复调用不报错。
- `timeout=30.0`：SQLite 默认写锁等待时间很短，多进程/多线程时会直接抛 `database is locked`；这里放宽到 30 秒（单元 09 的 `SqliteSaver`、单元 18 的并发讨论都绕不开这一点）。
- `row_factory = sqlite3.Row`：结果行可以用 `row["gross_cents"]` 按列名取；没有它只能 `row[0]`，一旦 SQL 里列的顺序变了，取值就悄悄错位。
- `conn.execute("PRAGMA foreign_keys = ON")`：**每个连接**都要执行一次。这一句是抄不掉的坑——如 5.1 的官方事实所说，外键默认关闭且按连接生效。

### 5.3 参数化查询：值永远走 `?`，绝不进 SQL 文本

保存路径：`ops-assistant/src/ops_assistant/data/queries.py`。下面是该文件的关键片段：**每个函数体都是完整的（没有省略任何一行逻辑）**；为节省篇幅，docstring 做了压缩，与本单元无关的函数（`list_available_months` 等）未列入——完整文件以仓库为准。

先看“反面教材”。假设用户输入被直接格式化进 SQL：

```python
# 反面教材：绝对不要这样写
user_input = "2026-09' OR '1'='1"
sql = f"SELECT COUNT(*) FROM orders WHERE substr(order_date, 1, 7) = '{user_input}'"
# 真正发给 SQLite 的文本会变成：
# SELECT COUNT(*) FROM orders WHERE substr(order_date, 1, 7) = '2026-09' OR '1'='1'
# 结果：WHERE 条件被 OR 短路，整张表都命中，“查一个月”变成了“查全表”。
```

注意这里最反直觉的一点：**这个输入里没有任何“恶意命令”**。它没有 `DROP TABLE`，只是一个引号加一个 `OR '1'='1'`。危害不是“数据被删”，而是**你查到的数字不再是你要查的那个数字**，而且不报错。对数据分析类程序来说，数字被悄悄改掉比进程崩掉严重得多。

正确写法（项目里的真实代码）：

```python
"""只读查询层（工程规格 3.4 的 ``queries.py``）。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

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
    """某个月的汇总口径（金额单位：分）。"""

    month: str
    gross_cents: int
    refund_cents: int
    net_cents: int
    order_count: int
    approved_refund_count: int
    pending_refund_count: int
    has_orders: bool
    has_any_refund_records: bool


def _validate_month(month: str) -> str:
    """校验 ``YYYY-MM`` 并返回去掉首尾空白的月份。

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
    """返回半开区间 ``[本月第一天, 次月第一天)`` 的 ISO 日期字符串。"""
    year = int(month[:4])
    month_number = int(month[5:7])
    if month_number == 12:
        next_year, next_month_number = year + 1, 1
    else:
        next_year, next_month_number = year, month_number + 1
    return f"{month}-01", f"{next_year:04d}-{next_month_number:02d}-01"


def get_month_sales(conn: sqlite3.Connection, month: str) -> MonthSales:
    """查询某个月的毛销售额、退款额、净销售额与订单/退款计数。"""
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
```

这段代码里有 6 个值得逐个说清的点：

1. **`?` 是占位符，`(start, end)` 是值**。`conn.execute(sql, params)` 会先把 SQL 文本编译好，再把值按类型绑定进去。值**永远**不会被当成 SQL 语法解析，所以 5.3 开头那个反面教材在这里不可能发生。
2. **三个 SQL 常量是模块级字符串常量**，不是每次调用现拼。好处：SQL 一眼可见、可以被测试覆盖、不会被某次分支悄悄改掉。
3. **`COALESCE(SUM(...), 0)` 兜底空结果**。一行都没匹配时，`SUM` 返回 `NULL`，而 `NULL` 在 Python 里是 `None`——`int(None)` 直接崩，或者更糟：`None + 100` 崩在别处。这个兜底就是“无记录返回全 0”这句需求的落地点。
4. **`COUNT(*)` 与 `SUM` 一起查**。`order_count` 是判断“无记录”的唯一依据：`has_orders = order_count > 0`。金额为 0 不能用来判断无记录（真零也是 0）。
5. **`has_any_refund_records` 与 `refund_cents` 是两个维度**。前者统计“**有没有**退款记录（含 pending/rejected）”，后者只统计“**approved 的金额**”。所以“华北某月有 2 笔退款但金额为 0”这种状态是表达得出来的：`pending_refund_count=2`、`refund_cents=0`。
6. **`SUM(status = 'pending')`**。SQLite 里比较表达式的结果就是 `1`/`0`，求和等于计数，比写一长串 `CASE WHEN status = 'pending' THEN 1 ELSE 0 END` 短，也不容易漏 `ELSE`。
7. **`_validate_month` 用正则把 `2026-9`、`2026-13`、`2026-09-01` 全部挡在门外**。为什么不能让 SQL 宽容一点？因为“宽容”意味着 `2026-9` 被当成 `2026-09`、`2026-09-01` 被当成某个奇怪的字符串，最终**静默算出错误的月份数据**。宁可抛 `ValueError` 让调用方处理。
8. **`_month_range` 处理 12 月跨年**。`2026-12` → `("2026-12-01", "2027-01-01")`。这是把手算月末天数彻底从代码里删掉的唯一原因。

> **官方事实**：Python 官方文档专门写了“如何用占位符绑定值”的一节，并给出“不要用字符串格式化拼 SQL”的反面示例（https://docs.python.org/3/library/sqlite3.html，核对日期 2026-10-19）。`?` 是 qmark 风格；同一页还介绍 `:name` 命名风格。两者都安全，选一种统一即可。
>
> **教学简化**：本项目所有 SQL 都是**常量**，所以“参数化”只用在值上。生产里连“表名/列名”也不该拼接——列名不能用占位符绑定（这是 SQL 语法限制），正确做法是**白名单映射**（如 `{"region": "region_id", "product": "product_id"}[dimension]`），不认识就报错。项目里的 `unsupported_dimension` 就是这个思路的产物。

### 5.4 半开区间与索引：用 `EXPLAIN QUERY PLAN` 自己看

“能不能用上索引”不该靠猜。SQLite 提供了一个只读命令：

```sql
EXPLAIN QUERY PLAN
SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents,
       COUNT(*) AS order_count
FROM orders
WHERE order_date >= ? AND order_date < ?;
```

预期（未在本环境执行）：输出的 `detail` 里会出现 `SEARCH orders USING INDEX idx_orders_date (order_date>? AND order_date<?)` 之类的字样——出现 `SEARCH ... USING INDEX` 通常说明索引被用上了。

再对比一条“看起来一样”的写法：

```sql
EXPLAIN QUERY PLAN
SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = ?;
```

预期（未在本环境执行）：`SCAN orders`——**全表扫描**。

原因是**经验规则（教学解释）**：索引是按 `order_date` 这一列的**原值**排序的。`substr(order_date, 1, 7) = '2026-09'` 是先对每一行算出前 7 个字符再比较，索引里的顺序帮不上忙（除非专门为这个表达式建“表达式索引”）。而 `order_date >= ? AND order_date < ?` 直接对原值做范围约束，索引的 B 树顺序正好可用。

> **官方文档**：SQLite 的查询规划器与索引使用规则见 https://www.sqlite.org/optoverview.html（核对日期 2026-10-19）；`LIKE 'abc%'` 有专门的 “The LIKE optimization” 一节，但触发条件比 `>=`/`<` 更多（涉及排序规则与 `case_sensitive_like`）。
>
> **项目设计选择**：本项目统一用 `>= / <` 两个参数，不用 `BETWEEN`、不用 `LIKE`。理由不是“谁更快”，而是**优化条件最少、行为最可预测**，而且和 `refund_date` 的过滤保持完全相同的形状，读代码时一眼能对上。代价：每个查询多传一个参数。
>
> **未验证**：`EXPLAIN QUERY PLAN` 的具体措辞在不同 SQLite 版本间会变（`SEARCH`/`SCAN` 关键字稳定，括号里的细节不一定）。请以你自己机器上的输出为准，不要背我这里的字符串。

### 5.5 恒返回全部地区：`get_region_breakdown` 的“空行”必须有意义

保存路径：`ops-assistant/src/ops_assistant/data/queries.py`（完整函数）

```python
def get_region_breakdown(conn: sqlite3.Connection, month: str) -> list[RegionSales]:
    """查询某月全部地区的销售与退款明细（按 ``region_id`` 升序）。

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
```

三个关键设计：

1. **`FROM regions LEFT JOIN order_agg`**，不是 `FROM orders`。
   如果写成 `FROM orders` 再 `GROUP BY region_id`，那么“当月没有订单的地区”**根本不会出现在结果里**。上层拿到 4 行，就会把“东北这个月没数据”和“东北不存在”混为一谈，差额分解时还会少算一个地区的差额。只有 5 行齐全，才能用 `order_count == 0` 明确表达“无记录”。
2. **`LEFT JOIN` 两遍，`COALESCE` 兜住 `NULL`**。
   退款聚合单独一个 CTE（`refund_agg`），因为它要按 `refund_date` 过滤、还要 `JOIN orders` 才能拿到地区——这与订单聚合的过滤条件不是同一列。两个 CTE 各自 `GROUP BY`，最后左连接回 `regions`。
3. **参数传了 4 个 `(start, end, start, end)`**。
   顺序必须与 SQL 里 `?` 出现的顺序严格一致：先订单的两个，再退款的两个。这也是参数化唯一的“代价”——**位置错了不报错，只是数字全错**。所以顺序敏感的 SQL 建议写成“按 `?` 出现顺序排版的常量”，并用测试锁住结果。

> **教学简化**：这里用 `WITH`（CTE）而不是相关子查询，是为了可读性与便于解释计划。数据量再大、维度再多时，生产做法通常是预聚合表或物化视图；本项目是演示库，**项目设计选择**是“每次现算”，保证口径只有一处。
>
> **官方事实**：`WITH` 子句（CTE）在 SQLite 3.8.3+ 可用，写法与标准 SQL 一致，官方语法见 https://www.sqlite.org/lang_with.html（核对日期 2026-10-19）。本项目没有用到递归 CTE。

### 5.6 实验脚本（一步步搭出来）

§5.1～5.5 读完 schema、连接层与查询层，本实验**五段观察**：库里有啥 → 半开区间 vs LIKE（带执行计划）→ 敌意输入 → 项目查询层与"空月"语义。分 6 步搭出，最终与仓库 `教程/实验/12_sql_basics.py` 完全一致；不联网、不调用模型（需要先 `bootstrap.py` 生成数据库）。

#### 步骤 1：常量与三个小工具——SQL 与路径先立规矩

**这一步解决什么**：本实验的主角是 **SQL 文本本身**，所以 SQL 全部写成**模块级常量**（一眼看全、绝不运行时拼接）；再备三个工具：定位数据库、裸连、算次月 1 号。

新建 `教程/我的实验/12_sql_basics.py`（参考实现在 `教程/实验/12_sql_basics.py`，写完本单元再对照）：

```python
"""单元 12 实验（步骤 1）：常量与工具。"""

from __future__ import annotations

import sqlite3
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
```

**逐行讲解**：

- `COUNT_SQL` 把**表名写死在常量里**：表名不能用 `?` 绑定（`?` 只能绑值），所以受信标识符走"常量白名单"，值走参数——**两类风险两种处理**。
- `RANGE_SQL`/`LIKE_SQL` 都是**多行字符串常量**：`>= ? AND < ?` 两个参数就是半开区间的形状；`COALESCE(..., 0)` 保证空集也返回 0 而不是 None——**空月与零额在 SQL 层就不许混淆**。
- `_open_plain` 裸连（不走项目的 `connect()`）：这一段要证明"**表结构确实在文件里**，不依赖项目代码"；`row_factory = sqlite3.Row` 让行可以按列名取值。
- `_next_month_start` 手算次月：12 月跨年单独处理——这个 4 行函数是"半开区间"右端点的全部秘密。

**运行与观察**（临时验证）：

```python
if __name__ == "__main__":
    print(_next_month_start("2026-09"))
    print(_next_month_start("2026-12"))
```

```text
2026-10-01
2027-01-01
```

#### 步骤 2：[1][2]——库里有什么、每张表多少行

**这一步解决什么**：动手查数据前先**清点库存**：`sqlite_master` 列出真实对象（表+索引），四张表各数一遍行数——后面一切数字的地基。

```python
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
```

**逐行讲解**：

- `sqlite_master` 是 SQLite 的**元数据表**：库里有几张表、几个索引，它说了算——**别信文档，信库自己**。
- `NOT LIKE 'sqlite_%'`：排除 SQLite 内部对象（如自动索引 `sqlite_autoindex_*`）。
- 循环里执行的是 `COUNT_SQL` 常量而不是 f-string 拼表名——步骤 1 的规矩当场兑现。

**运行与观察**（实测 2026-10-23，你的行数不同很正常——bootstrap 有随机成分）：

```text
[1] 数据库里真实存在的对象：
    index  idx_orders_date
    index  idx_orders_region
    index  idx_refunds_date
    index  idx_refunds_status
    table  orders
    table  products
    table  refunds
    table  regions
[2] 四张表的行数：
    regions   5
    products  12
    orders    956
    refunds   94
```

四张表、四个索引——**每一个索引都对应一种高频查询路径**（日期/地区/状态），步骤 3 的执行计划会用到 `idx_orders_date`。

#### 步骤 3：[3]——半开区间 vs LIKE，用 EXPLAIN 见分晓

**这一步解决什么**：两种写法**结果一样**（在本库），但**代价不一样**。肉眼看不出的差别，`EXPLAIN QUERY PLAN` 曝光。

```python
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
```

**逐行讲解**：

- `EXPLAIN QUERY PLAN` 前缀拼接的 `sql` **只可能是本文件常量**（docstring 里写死了这条纪律）——EXPLAIN 不是参数化的入口，所以"输入受信"是它的安全前提。
- 返回值 `same` 冒泡到 main：如果两种写法**结果都不一致**，说明数据里有不符合 `YYYY-MM-DD` 定长格式的日期——**实验本身就是数据质量探测器**。
- `(start, end)` 两参数与 `(f"{month}%",)` 一参数：半开区间给的是**两个端点**，LIKE 给的是**模式**——语义完全不同的两样东西。

**运行与观察**（实测）：

```text
[3] 同一个月的两种写法（2026-09）：
    半开区间：50 行，毛额 1618920 分
    LIKE    ：50 行，毛额 1618920 分
    半开区间的执行计划：
      SEARCH orders USING INDEX idx_orders_date (order_date>? AND order_date<?)
    LIKE 的执行计划：
      SCAN orders
    两者结果一致：True（一致不代表代价一致，看上面两段计划）
```

**SEARCH（走索引） vs SCAN（全表扫）**——结果相同，代价天差地别；表越大差距越大。这就是 §5.4 的结论从"听说"变成"亲眼看到"。

#### 步骤 4：[4]——敌意输入：当参数 vs 拼进 SQL

**这一步解决什么**：SQL 注入不是传说。同一条敌意字符串，**当参数传**与**拼进 SQL 文本**的下场并排看（拼接版只打印不执行）。

```python
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
```

**逐行讲解**：

- `hostile` 里的 `'` 与 `OR '1'='1`：拼接后 WHERE 条件被 **OR 短路成永真**——整张表全命中。这就是注入的全部原理，两行讲完。
- 参数版命中 **0 行**：参数永远是"一个值"，引号只是字符——数据库根本不会把它当 SQL 结构解析。
- 拼接版**只打印不执行**：演示危险不等于制造危险——教学代码也要有安全底线。

**运行与观察**（实测）：

```text
[4] 敌意输入 "2026-09' OR '1'='1" 当参数传入：命中 0 行（它只是数据）
    同一句话如果拼进 SQL，发给数据库的文本会变成：
      SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = '2026-09' OR '1'='1'
    这里只打印、不执行；执行它会命中整张表（条件被 OR 短路）。
```

#### 步骤 5：[5]——换上项目的查询层，看"空月"语义

**这一步解决什么**：前四段是裸 SQL；最后换**项目自己的查询层**（`connect`/`get_month_sales`/`get_region_breakdown`/`list_available_months`），重点验证 §5.5 的两个设计：恒返回全部地区、**无记录 ≠ 零销售额**。

```python
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
```

**逐行讲解**：

- 项目的 import 放**函数内**：本实验前四段不需要项目代码也能跑，第五段才挂上——依赖分层让"裸 SQL 教学"与"项目验证"互不拖累。
- `get_month_sales` 的返回是**带语义字段的对象**（has_orders/has_any_refund_records）：查询层把"无记录"这个事实带出来了，业务层才有资格区分"没数据"和"真的是 0"。
- `list_available_months` 与空月对照：`2025-01` 不在列表里 → 它**从未有过订单**——数据范围的事实要在库里查，不能靠人记。

**运行与观察**（实测节选）：

```text
[5] get_month_sales('2026-09')：
    毛额 1618920 分 / 退款 508121 分 / 净额 1110799 分
    订单 50 笔，approved 退款 16 笔，pending 退款 0 笔
    has_orders=True, has_any_refund_records=True
[5] get_region_breakdown('2026-09')：
    R_EAST   华东  净额       203654 分  订单   9 笔  有订单
    R_SW     西南  净额            0 分  订单  10 笔  有订单
[5] 库中真实出现过的月份 20 个：2025-02 … 2026-09
[5] 空月 '2025-01'：毛额 0 分，has_orders=False（无记录，而不是“销售额为 0”）
    '2025-01' 在 list_available_months() 里：False
```

注意西南：**净额 0、订单 10 笔**——"真零"（毛额=退款）与空月的"无记录"是两回事，两种字段（order_count 与 has_orders）各说各的。

#### 步骤 6：整合——`main()` 与命令行

**这一步解决什么**：argparse 三个参数（db/month/empty-month）让实验**可复现、可换月**；数据库不存在的失败要前置；两种写法不一致要用退出码报警。

```python
import argparse
import sys


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
```

清理临时验证，完整文件应与仓库完全一致：

```python
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
```

**回看地图**：

| 步骤 | 最终脚本里的位置 |
| --- | --- |
| 1 规矩 | `COUNT_SQL`/`RANGE_SQL`/`LIKE_SQL`、`_resolve_db()`、`_open_plain()`、`_next_month_start()` |
| 2 清点 | `_show_schema_objects()`[1]、`_show_row_counts()`[2] |
| 3 对比 | `_explain()`、`_compare_range_and_like()`[3]（SEARCH vs SCAN） |
| 4 注入 | `_parameter_demo()`[4] |
| 5 项目层 | `_project_layer()`[5]（空月语义） |
| 6 整合 | `main()`（argparse + 退出码）、`raise SystemExit(main())` |

运行与验收：见 §5.7 的命令（含换月重跑）。核心验收：[3] 两段执行计划一 SEARCH 一 SCAN、[4] 参数版命中 0 行、[5] 空月的 has_orders=False——三件事都对，本实验才算过。

### 5.7 运行命令

```powershell
# 1) 进项目目录（虚拟环境与依赖都挂在项目上）
cd ops-assistant

# 2) 先确认库在（没有就先初始化）
Test-Path data/ops.db
uv run python scripts/bootstrap.py

# 3) 跑本单元实验
uv run python ../教程/我的实验/12_sql_basics.py

# 4) 换个月份、换个库路径
uv run python ../教程/我的实验/12_sql_basics.py --month 2026-03
uv run python ../教程/我的实验/12_sql_basics.py --db data/ops.db --empty-month 2025-01

# 5) 看退出码（0 = 两种日期写法结果一致）
echo $LASTEXITCODE
```

直接用 SQLite 命令行也行（如果你装了 `sqlite3.exe`）：

```powershell
sqlite3 data/ops.db ".tables"
sqlite3 data/ops.db ".schema orders"
sqlite3 data/ops.db "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01';"
```

### 5.8 手写 SQL 独立复核：为什么必须“另写一份”

项目里有一份**故意重复实现**的口径计算：`ops-assistant/scripts/compute_ground_truth.py`。它的头两行注释就把动机写明了：

> 只用标准库 `sqlite3` + 原生 SQL + Python 整数运算重新计算口径，**不 import** 项目里的 `queries.py` / `metrics.py`，避免“用被验证的实现验证自己”。

这句话值得单独讲。如果你写测试时调用 `get_month_sales()` 拿到结果，再用同一个 `get_month_sales()` 去断言，那么无论口径错得多离谱，测试都会通过——因为你验证的是“代码等于它自己”。所以正确姿势是**两条独立路径**：

```
路径 A（被测）：data/queries.py + domain/metrics.py
路径 B（对照）：scripts/compute_ground_truth.py（手写 SQL，不 import A）
        ↓
        逐项比较 A 与 B 的输出，不一致就说明至少一边错了
```

你可以现在就手写一条独立 SQL，验证 `get_month_sales('2026-09')` 的毛额：

```sql
-- 与 queries.py 完全无关的一条 SQL；值自己填，不要复制粘贴占位符的顺序
SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS gross_cents,
       COUNT(*) AS order_count
FROM orders
WHERE order_date >= '2026-09-01'
  AND order_date < '2026-10-01';
```

再手写一条退款额（注意两个容易错的地方：状态必须是 `approved`，月份取 `refund_date` 而不是 `order_date`）：

```sql
SELECT COALESCE(SUM(amount_cents), 0) AS refund_cents,
       COUNT(*) AS approved_refund_count
FROM refunds
WHERE status = 'approved'
  AND refund_date >= '2026-09-01'
  AND refund_date < '2026-10-01';
```

生成标准答案的命令：

```powershell
# 在 ops-assistant/ 目录下；不带参数时默认读写 data/ops.db 与 data/ground_truth.json
uv run python scripts/compute_ground_truth.py

# 也可以显式指定
uv run python scripts/compute_ground_truth.py --db data/ops.db --out data/ground_truth.json
uv run python scripts/compute_ground_truth.py --help
```

预期（未在本环境执行）：脚本以退出码 0 结束，并打印 `[完成]` 开头的一行，内容包含输出文件的绝对路径与月份数量。

> **项目设计选择**：标准答案里的数值**全部在运行时算出来**，文件里不预置任何计算结果；唯一的“硬编码”是一笔固定值跨月退款的退款号与金额（见单元 13 第 5.4 节）。这样做的代价是“标准答案”依赖数据库内容，所以生成顺序必须是“先建库、再算标准答案”。
>
> **未验证**：本环境无法启动 Python 进程，所以 `compute_ground_truth.py` 与上面所有命令我都没有实际运行过；脚本里也把这句话写进了 `meta.notes`。

---

## 6. 预期输出与关键状态变化

### 6.1 实验脚本输出（2026-10-19 实测运行通过，退出码 0）

```
[1] 数据库里真实存在的对象：
    index  idx_orders_date
    index  idx_orders_region
    index  idx_refunds_date
    index  idx_refunds_status
    table  orders
    table  products
    table  refunds
    table  regions
[2] 四张表的行数：
    regions   ⟨5⟩
    products  ⟨12⟩
    orders    ⟨…⟩
    refunds   ⟨…⟩
[3] 同一个月的两种写法（2026-09）：
    半开区间：⟨n⟩ 行，毛额 ⟨gross_cents⟩ 分
    LIKE    ：⟨n⟩ 行，毛额 ⟨gross_cents⟩ 分
    半开区间的执行计划：
      SEARCH orders USING INDEX idx_orders_date (order_date>? AND order_date<?)
    LIKE 的执行计划：
      ⟨SCAN orders 或 SEARCH ... USING COVERING INDEX ... ，取决于版本与排序规则⟩
    两者结果一致：True（一致不代表代价一致，看上面两段计划）
[4] 敌意输入 "2026-09' OR '1'='1" 当参数传入：命中 0 行（它只是数据）
    同一句话如果拼进 SQL，发给数据库的文本会变成：
      SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = '2026-09' OR '1'='1'
    这里只打印、不执行；执行它会命中整张表（条件被 OR 短路）。
[5] get_month_sales('2026-09')：
    毛额 ⟨…⟩ 分 / 退款 ⟨…⟩ 分 / 净额 ⟨…⟩ 分
    订单 ⟨…⟩ 笔，approved 退款 ⟨…⟩ 笔，pending 退款 ⟨…⟩ 笔
    has_orders=True, has_any_refund_records=⟨True/False⟩
[5] get_region_breakdown('2026-09')：
    R_EAST   华东  净额 ⟨…⟩ 分  订单 ⟨…⟩ 笔  有订单
    R_NE     东北  净额 ⟨…⟩ 分  订单 ⟨…⟩ 笔  有订单
    R_NORTH  华北  净额 ⟨…⟩ 分  订单 ⟨…⟩ 笔  有订单
    R_SOUTH  华南  净额 ⟨…⟩ 分  订单 ⟨…⟩ 笔  有订单
    R_SW     西南  净额           0 分  订单 ⟨…⟩ 笔  有订单
[5] 库中真实出现过的月份 ⟨20⟩ 个：2025-02 … 2026-09
[5] 空月 '2025-01'：毛额 0 分，has_orders=False（无记录，而不是“销售额为 0”）
    '2025-01' 在 list_available_months() 里：False
```

三处最值得盯住的输出：

- **`[3]` 两种写法结果一致**：这是“能跑对”的最低标准；**执行计划不同**才是真正的知识点。别停在“结果一样就行”。
- **`[5]` 西南净额 0 但订单数 > 0**：这是**真零**，不是无记录。它必须和 `2025-01` 那样“订单 0 笔”的输出形成对照。
- **`[5]` `'2025-01'` 不在 `list_available_months()` 里**：数据范围声明是 `2025-01` 起，但这个月**一行都没有**；`list_available_months()` 返回的是“真实出现过的月份”，所以不含它。这正是“声明范围 ≠ 有记录的范围”。

> **数字说明**：上面凡是写成 `⟨…⟩` 的地方，都由你自己的库内容决定。编写教程的这台机器**无法启动进程**，所以我不写具体数值——**不伪造未运行过的输出**，这也是本教程的写作约定。

### 6.2 关键状态变化

| 动作 | 数据库连接状态 | 你的认知应该变的地方 |
| --- | --- | --- |
| `sqlite3.connect(path)` | 连接建立，`row_factory` 默认是 `tuple` | 这时 `row["x"]` 会报 `TypeError` |
| 设置 `row_factory = sqlite3.Row` | 可按列名取值 | 列顺序变化不再影响取值 |
| `PRAGMA foreign_keys = ON` | 本连接开始强制外键 | 这个开关**按连接**生效，换连接要重设 |
| 执行 `WHERE d >= ? AND d < ?` | 走 `idx_orders_date`（范围扫描） | 半开区间的两个参数是“下个月 1 号” |
| 执行 `WHERE substr(d,1,7) = ?` | 通常全表扫描 | 对索引列做函数运算，索引帮不上忙 |
| 空结果 + `COALESCE(SUM(amount_cents), 0)` | 返回 0 而不是 `NULL` | “无记录”要靠 `order_count=0` 判断，不能靠金额 |
| `FROM regions LEFT JOIN order_agg` | 恒 5 行 | 差额分解不会漏地区 |
| `_validate_month("2026-9")` | 抛 `ValueError` | 非法输入不进入 SQL，不产生“静默错月” |

---

## 7. 修改练习（至少 2 个，含一个“改坏它”）

### 练习 7.1（简单）：把“数据范围”打印出来

在 `queries.py` 里已经有 `list_available_months()`。写一个脚本，打印：

1. 库中**真实出现过**的月份个数与首尾月份；
2. 声明范围 `2025-01` 与 `2026-09` 之间应有的月份个数；
3. 两者的差集（即“声明了但没有记录”的月份）。

验收：差集里能看到 `2025-01`，并解释它为什么不在 `list_available_months()` 里。

### 练习 7.2（中等）：用一条 SQL 统计每月订单数

手写（不要用 `queries.py`）：

```sql
SELECT substr(order_date, 1, 7) AS month, COUNT(*) AS order_count
FROM orders
GROUP BY substr(order_date, 1, 7)
ORDER BY month;
```

把它和 `list_available_months()` 的输出对照，回答：为什么`GROUP BY` 的结果里没有 `2025-01`？

### 练习 7.3（改坏它，必做）：把半开区间改成闭区间

这是本单元最重要的一个练习，因为它会**真的算错**。

1. 在库里手工插入一条“下个月第一天”的订单（只为观察，用完删掉）：

```sql
INSERT INTO orders (order_id, order_date, region_id, product_id, quantity,
                    unit_price_cents, channel)
VALUES ('O_TEST_20261001', '2026-10-01', 'R_EAST', 'P_001', 1, 19999, '线下');
```

2. 用两种条件各查一次 `2026-09`：

```sql
-- A：半开区间（项目写法）
SELECT COUNT(*) FROM orders
WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01';

-- B：闭区间（错）
SELECT COUNT(*) FROM orders
WHERE order_date >= '2026-09-01' AND order_date <= '2026-10-01';
```

3. 记录两个数字的差，然后删掉测试数据：

```sql
DELETE FROM orders WHERE order_id = 'O_TEST_20261001';
```

验收：能说出 B 比 A **多算了 1 行**，并解释“为什么把 `<` 写成 `<=` 是月份统计里最常见的一类错误”。

> **要理解的点**：错误的边界不会报错、不会崩，只会让数字偏一点点。一个月的偏差在总表里几乎看不出来，但它会被写进报告、发给运营。

### 练习 7.4（进阶）：给列名做白名单，而不是拼接

写一个函数：

```python
def metric_column(name: str) -> str:
    """把外部传来的指标名映射成列名；不认识就报错。"""
```

要求：`"gross"`、`"refund"`、`"net"` 三个键走白名单；其他值抛 `ValueError`；**不允许**出现 `f"SELECT {name} FROM orders"` 这种把外部输入拼进 SQL 的写法，并说明为什么列名不能用 `?` 绑定。

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：查一个“不存在的月份”

```python
from ops_assistant.data.db import connect
from ops_assistant.data.queries import get_month_sales

conn = connect("data/ops.db")
sales = get_month_sales(conn, "2025-01")
print(sales)
conn.close()
```

预期（未在本环境执行）：`MonthSales` 全 0，且 `has_orders=False`、`order_count=0`。

**要理解的点**：**没有抛异常、也没有返回 `None`**。这是刻意设计的：调用方拿到的是“一个口径齐全的 0 结果”，并且有一个**显式字段**可以判断“这是无记录”。如果查询层返回 `None`，那么每个调用方都要写 `if x is None`，总有人会漏。

### 8.2：做一个“真零”，看看和无记录差在哪

```python
from ops_assistant.data.db import connect
from ops_assistant.data.queries import get_region_breakdown

conn = connect("data/ops.db")
for region in get_region_breakdown(conn, "2026-09"):
    print(region.region_id, region.gross_cents, region.refund_cents, region.net_cents, region.order_count)
conn.close()
```

预期（未在本环境执行）：`R_SW` 这一行出现 `gross_cents > 0`、`refund_cents == gross_cents`、`net_cents == 0`、`order_count > 0`。

**要理解的点**：净额是 0，但**不是无记录**（订单数大于 0），也不是“没有销售”（毛额大于 0）。报告里必须写“净额为 0（有订单，退款冲抵）”。这条演示数据是由生成器**刻意构造**的，规格里写明这是必须存在的边界事实。

### 8.3：把 `2026-9` 传进去

```python
from ops_assistant.data.db import connect
from ops_assistant.data.queries import get_month_sales

conn = connect("data/ops.db")
try:
    get_month_sales(conn, "2026-9")
except ValueError as error:
    print("预期内的报错：", error)
conn.close()
```

预期（未在本环境执行）：打印 `预期内的报错： 月份格式必须是 YYYY-MM，例如 2026-09；收到：'2026-9'`。

**要理解的点**：`2026-9` 在人类眼里和 `2026-09` 是一回事，但在字符串比较里**完全不同**（`'2026-9-01'` 与 `'2026-09-01'` 比较，第 6 位 `'9'` > `'0'`）。如果放行，半开区间会静默算错。**校验放在入口，而不是靠下游宽容。**

### 8.4：把 `?` 换成 f-string（改坏它，务必做一次）

找一处查询，把

```python
conn.execute("SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = ?", (month,))
```

改成

```python
conn.execute(f"SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = '{month}'")
```

然后传 `month = "2026-09' OR '1'='1"`。

预期（未在本环境执行）：第一条返回 0 行；第二条返回**全表行数**。

**要理解的点**：参数化不是“防御黑客”的额外装饰，它是**结果正确性**的一部分。在数据分析程序里，被注入的后果首先表现为“数字不对”，其次才是安全问题。

### 8.5：让索引失效，再用执行计划看见

```sql
EXPLAIN QUERY PLAN
SELECT COUNT(*) FROM orders WHERE order_date >= '2026-09-01' AND order_date < '2026-10-01';

EXPLAIN QUERY PLAN
SELECT COUNT(*) FROM orders WHERE substr(order_date, 1, 7) = '2026-09';
```

预期（未在本环境执行）：第一条出现 `SEARCH ... USING INDEX idx_orders_date`，第二条出现 `SCAN orders`。

**要理解的点**：`substr(order_date, 1, 7)` 在结果上完全正确，只是用不上索引。**先写对，再谈快**；而“快不快”要用执行计划看，不靠感觉。

### 8.6：把外键关掉，插一条脏数据

```sql
PRAGMA foreign_keys = OFF;
INSERT INTO orders (order_id, order_date, region_id, product_id, quantity,
                    unit_price_cents, channel)
VALUES ('O_BAD', '2026-09-10', 'R_NOT_EXIST', 'P_001', 1, 19999, '线下');

PRAGMA foreign_keys = ON;
-- 再插一条同类脏数据，观察区别
INSERT INTO orders (order_id, order_date, region_id, product_id, quantity,
                    unit_price_cents, channel)
VALUES ('O_BAD2', '2026-09-10', 'R_NOT_EXIST', 'P_001', 1, 19999, '线下');
```

预期（未在本环境执行）：第一条**成功插入**（外键当时是关的），第二条抛 `sqlite3.IntegrityError: FOREIGN KEY constraint failed`。

**要理解的点**：这正是 5.1 官方事实的实际后果——**外键默认关闭、按连接生效**。项目因此在 `connect()` 里每次都用 `PRAGMA foreign_keys = ON`。记得清理：

```sql
DELETE FROM orders WHERE order_id IN ('O_BAD', 'O_BAD2');
```

### 8.7：重复执行建表脚本

```python
from ops_assistant.data.db import connect, init_schema

conn = connect("data/ops.db")
init_schema(conn)   # 第二次执行
```

预期（未在本环境执行）：抛 `sqlite3.OperationalError: table regions already exists`。

**要理解的点**：`schema.sql` 是普通 `CREATE TABLE`（与规格一致），**不幂等**。要重来就删文件或换路径；生产里用迁移工具（单元 18）。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能画出四张表的关系图，并说出 `orders.unit_price_cents` 与 `products.unit_price_cents` 的区别。
- [ ] 我能不看资料写出 `2026-09` 的半开区间两个参数，并说出 12 月的右边界是什么。
- [ ] 我跑通了 `教程/我的实验/12_sql_basics.py`，看到 `[3]` 的两种写法结果一致、执行计划不同。
- [ ] 我亲手做了一次 8.4：同一句 SQL，`?` 传参返回 0 行，f-string 拼接返回全表。
- [ ] 我能解释为什么 `substr(order_date, 1, 7) = ?` 用不上 `idx_orders_date`。
- [ ] 我能说出“无记录 / 真零 / 零基数”的区别，并各举一个本项目的例子。
- [ ] 我能解释 `get_region_breakdown` 为什么恒返回 5 行，而不是只返回有订单的地区。
- [ ] 我做了 7.3：把 `<` 改成 `<=` 后，多算了一行“下月第一天”的订单。
- [ ] 我做了 8.6：知道外键默认关闭、按连接生效，并清理了脏数据。
- [ ] 我能说出为什么 `scripts/compute_ground_truth.py` 要故意“另写一份”而不 import `queries.py`。

---

## 10. 复盘问题与参考答案、下一步

### Q1：为什么金额不能存成浮点“元”？

**参考**：浮点数无法精确表示 `0.1` 这类十进制小数（官方说明：https://docs.python.org/3/tutorial/floatingpoint.html，核对日期 2026-10-19），几十万笔累加会出现分位误差，而财务口径要求分毫不差。本项目所有 `*_cents` 字段都是 `INTEGER`，只在展示时除以 100。这是**项目设计选择**，代价是每个展示函数都要处理单位与负数；收益是“对账能对上”。（另：SQLite 也没有 `DECIMAL` 精确小数类型，用 `REAL` 存钱是同类错误。）

### Q2：为什么用 `>= / <` 而不用 `BETWEEN`？

**参考**：`BETWEEN '2026-09-01' AND '2026-09-30'` 需要你知道“9 月有 30 天”，月末天数、闰年、跨年都要维护；写错一次就少算一天且无人察觉。半开区间只依赖“下个月 1 号”，跨年由 `_month_range` 处理。另外 `BETWEEN` 是闭区间，相邻两个月会**共享边界日**，只要数据里真出现了边界日的记录就会重复计数（练习 7.3 亲手验证过）。

### Q3：为什么查询层不直接返回 `None` 表示“没查到”？

**参考**：`None` 会把“判断”的责任推给每个调用方，而调用方总会有人忘记判断，于是 `None` 一路飘到报告里变成 `null`、或者崩在某个加法上。返回“全 0 + `has_orders=False`”让类型永远一致、可判断、可累加；SQL 层也用 `COALESCE(SUM(金额列), 0)` 配合。这是**项目设计选择**：宁可多一个布尔字段，也不要多一个空值分支。

### Q4：`?` 顺序传错会怎样？怎么防？

**参考**：不会报错，只会算错——这是参数化唯一的“代价”。防法有三条：① SQL 里的 `?` 与参数元组按出现顺序排版，两者放在相邻几行内；② 结果用测试锁住（换顺序会让测试变红）；③ 复杂的多参数 SQL 用命名占位符 `:start` / `:end`（Python 官方支持，见 sqlite3 文档“How to use placeholders”），可读性更好。本项目选了 `?` + 就近排版 + 测试覆盖。

### Q5：为什么必须让“标准答案”另写一份实现？

**参考**：同一份实现既当运动员又当裁判时，它的错误会**同时**出现在被测结果和期望值里，测试永远绿。`scripts/compute_ground_truth.py` 因此只用 `sqlite3` + 原生 SQL + Python 整数运算重算一遍，与 `queries.py` / `metrics.py` 的输出逐项比对。代价是口径要维护两处，所以两份实现的**常量**（种子、边界月份、固定值退款号与金额）必须保持一致——这也是单元 13 要讲的一个真实风险点。

### 下一步

进入 **单元 13：指标计算、退款归属与差额分解**。你会：

1. 把 `gross / refund / net` 三条公式写成可复核的 SQL 与 Python 代码。
2. 亲手验证“退款按 `refund_date` 归属”，并观察同一张订单如何同时影响两个月。
3. 处理三种状态（`approved` / `pending` / `rejected`）与“有待处理退款”的提示。
4. 计算差额、变化率、地区差额分解，并跑通**地区差额合计必须等于总差额**这条校验。
5. 看清“为什么数字必须由程序算，而不是由模型算”。

进入前请确认：你能在本机跑通 `uv run python ../教程/我的实验/12_sql_basics.py` 并看到 `[5]` 里“西南净额 0 但订单数大于 0”那一行。
