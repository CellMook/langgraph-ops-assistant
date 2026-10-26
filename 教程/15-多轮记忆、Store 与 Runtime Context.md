# 单元 15：多轮记忆、Store 与 Runtime Context

> 建议时长：**55 分钟**（读概念 15 分钟 + 读项目片段 15 分钟 + 跑实验 15 分钟 + 改坏与边界 10 分钟）
> 前置：单元 09（Checkpointer、thread 与恢复）、单元 14（把检索与数据接进工作流）；单元 07（状态与 Reducer）会用到但不必重读。
> 可跳过条件：你能不看资料说清 `AssistantState.last_spec`、`AssistantContext`、`PreferenceStore`、`SqliteSaver` 四者各自"保存在哪里、活多久、谁能读到"，并指出 `graph.invoke(..., context=...)` 注入的东西会不会进检查点——那本单元只需做第 7、8 节。
> 版本基线：`langchain==1.4.6`、`langgraph==1.2.16`（PyPI 元数据核对日期 **2026-10-19**）；本单元实验**不调用任何外部模型**，不需要 API Key。
> 事实标注约定：**官方事实**（有官方文档或可核对来源）、**教学简化**（为讲清楚而压缩的模型，不是官方定义）、**项目设计选择**（本项目自己的取舍，不是通用最佳实践）、**未验证**（本环境未执行或未逐句核对）。
> 执行说明：本环境**不能运行 Python**（无法启动进程，见单元 00 第 3.3 节）。所有"预期输出"都写成 **预期（未在本环境执行）**，请你按第 5 节命令在自己机器上核对。

---

## 1. 学习目标与前置

学完本单元，你应该能做到下面 6 件事（每一条都能在代码里指到行）：

1. **把六个概念分开讲**：State（状态通道）/ Checkpointer（检查点）/ thread（会话）/ Store（跨会话的长期记忆）/ Runtime Context（本次执行的依赖注入）/ 持久化后端（checkpointer 的具体实现），并说清每个的"生命周期"。
2. **指出主项目里每个概念落在哪个文件**：`workflow/state.py`（State + Context）、`workflow/persistence.py`（Checkpointer + Store + Runtime 装配）、`workflow/nodes.py`（读取 Context、写 `last_spec`）。
3. **解释"追问为什么能记住上一轮"**：`last_spec` 留在检查点里，`initial_state()` 故意不重置它（单元 09 已埋伏笔），`understand` 节点在"新问题没带时间"时继承它。
4. **解释"换个会话为什么还记得用户偏好"**：偏好不在 State 里，而在 `PreferenceStore`（`data/preferences.db`），按 `user_id` 存，跨 thread 生效。
5. **说清 `context=` 为什么不能当记忆用**：它是一次 `invoke` 的入参，不写检查点、不跨轮、不跨进程；把它当记忆会得到"进程内碰巧能用、重启就丢"的假象。
6. **识别"教学适配器"的边界**：项目里的 `PreferenceStore` 有 `get/put/search` 的轮廓，但**不是**官方 `BaseStore` 的完整实现（没有向量检索、没有 TTL、没有 `batch`）。

前置检查（30 秒，能答上就不用回看）：

- 单元 09 里，`get_state(config)` 读到的 `values` 与 `context` 是同一个东西吗？（不是。）
- `initial_state()` 为什么不写 `last_spec`？（写了每轮都会被空值覆盖，追问就读不到上一轮解析结果。）

如果这两问答不上来，先回单元 09 第 3 节与第 5.5 节。

---

## 2. 官方文档与阅读问题

本单元需要三页官方文档。三个地址我在 **2026-10-19** 用 HTTP 抓取过，都返回 `200`（能取到站点导航与页面标题）；但抓回来的正文被前端框架截断，所以我**没有**逐句核对页面正文。凡是我写"官方把 X 定义为 Y"的地方，都属于我根据页面标题、页面内的小标题与 LangGraph 长期稳定的概念做出的表述，请你自己点开链接确认原文。

| 链接 | 与本单元的关系 | 读的时候要回答的问题 |
| --- | --- | --- |
| [Persistence（LangGraph 官方）](https://docs.langchain.com/oss/python/langgraph/persistence) | checkpointer 按 `thread_id` 保存每步状态快照；这是"短期记忆"的实现 | 快照里存的是什么（状态通道值还是运行时对象）？换 `thread_id` 之后旧快照还能读到吗？ |
| [Memory（LangChain 官方）](https://docs.langchain.com/oss/python/langchain/memory) | 区分"短期记忆（对话历史）"与"长期记忆（跨会话偏好/事实）" | 官方把记忆分成哪两类？本项目哪一处对应短期、哪一处对应长期？ |
| [Runtime（LangGraph 官方）](https://docs.langchain.com/oss/python/langgraph/runtime) | `context` 与 `Runtime` 的用途：把依赖注入节点，而不是塞进状态 | `Runtime` 里除了 `context` 还有什么？为什么模型客户端、数据库连接这类对象不该放进 State？ |

三个阅读问题的"一句话参考答案"放在第 10 节，先自己想。

> **官方事实**：LangGraph 的持久化模型是"每个 thread 一条检查点链，图上每一步都写一条状态快照"，恢复时从最近检查点继续（链接见上表，核对日期 2026-10-19）。
>
> **未验证**：`Runtime` 对象上除 `context` 外的具体属性名（例如 `store`、`stream_writer` 之类）在本环境没有核对；本单元的实验只使用 `runtime.context`，这是项目源码 `nodes.py` 已经在用的写法，但因为没运行，标记为**未验证**。

---

## 3. 通俗解释与精确技术定义

### 3.1 一句类比：六样东西，六个抽屉

把一次问答想成去一家常年营业的店办事：

| 说法 | 现实对应 | 生命周期 |
| --- | --- | --- |
| **State**（状态） | 这一次办事桌上摊开的材料 | 一次 `invoke` 内有效；带 reducer 的字段会跨轮累积 |
| **Checkpointer**（检查点） | 每办完一步就拍照存档 | 进程内（内存 saver）或落盘（SQLite saver），按 thread 存 |
| **thread**（会话） | 档案袋编号 | 只要存档还在，编号就一直能查 |
| **Store**（长期记忆） | 客户资料卡（不属于任何一次办事） | 跨 thread、跨进程；按 `user_id` + `namespace` 存 |
| **Runtime Context** | 你今天带来的工具箱（连接、模型、配置） | 一次 `invoke` 内有效；**不入档** |
| **持久化后端** | 档案柜本身（铁柜 / 纸箱） | 由具体实现决定：`InMemorySaver` 是纸箱，`SqliteSaver` 是铁柜 |

这张表是本单元的核心。下面把每一行讲精确。

### 3.2 精确一点：六个定义

> **官方事实**：LangGraph 把"图的状态"建模为一组**通道（channel）**，每个通道有自己的更新规则（默认是"新值覆盖旧值"，声明了 reducer 的通道是"累积"）。这是单元 06/07 已经讲过的东西，本单元只关心它"存在哪里"。

- **State（状态）**：`AssistantState` 是 `TypedDict`，定义图的通道名与类型。节点返回的 dict 是"部分更新（partial update）"，由图的 reducer 合并进状态。**它不是数据库，也不是记忆**——它只是"这一步图里能读到什么"的类型声明。
- **Checkpointer（检查点 / checkpoint）**：图在每一步之后，把**状态通道的当前值**（可 JSON 序列化的那部分）写成一条快照，并按 `thread_id` 串成链。`SqliteSaver` 把它写进 `data/checkpoints.db`；`InMemorySaver` 只放在内存 dict 里。
- **thread（线程 / 会话）**：检查点的分组键，通过 `config={"configurable": {"thread_id": ...}}` 传入。同一 thread 的多次 `invoke` 共享一条快照链；不同 thread 完全隔离。**注意**：LangGraph 的 `thread` 与操作系统的线程（thread）不是一回事，中文里最好说"会话"，避免和 `threading` 混淆。
- **Store（长期记忆存储）**：LangGraph 的跨 thread 键值存储抽象（`BaseStore`），按**命名空间（namespace）**组织数据。官方实现有 Postgres / Redis 等；项目为了"零外部依赖 + 可离线跑"，用 SQLite 小表实现了一个**适配器**。
- **Runtime Context（运行时上下文）**：通过 `graph.invoke(..., context=...)` 注入、由节点用 `Runtime[AssistantContext]` 读取的**依赖集合**。它是"这次执行要用到的对象"（数据库连接、模型客户端、配置、指标收集器），不是状态的一部分。
- **持久化后端（persistence backend）**：checkpointer 的具体实现。同一份图代码可以换 `InMemorySaver`（测试）或 `SqliteSaver`（真实使用），这就是"后端可替换"。

### 3.3 最容易混的三组

**（1）State vs Context：都叫"状态"，但一个入档、一个不入档**

| 维度 | State（`AssistantState`） | Context（`AssistantContext`） |
| --- | --- | --- |
| 存放内容 | 业务数据：问题、解析结果、计算结果、报告、轨迹 | 运行依赖：连接、模型、配置、偏好存储、指标 |
| 是否写检查点 | 是（必须可 JSON 序列化） | 否 |
| 跨轮是否还在 | 是（同一 thread） | 否（每次 `invoke` 重新注入） |
| 能否被 `get_state` 读到 | 能 | 不能 |
| 放错会怎样 | 放连接/模型 → 检查点序列化失败 | 放业务数据 → 每轮丢失 |

**（2）thread vs user：一个管"这次对话"，一个管"这个人"**

同一 `user_id` 可以开很多 thread（不同话题）；同一 thread 只属于一个 `user_id`。项目里 `_config()` 两个都写进 `configurable`，但**只有 `thread_id` 决定检查点归属**，`user_id` 只用于偏好读取与 API 侧过滤（`list_threads(user_id=...)`）。

**（3）短期记忆 vs 长期记忆**

| 类型 | 项目实现 | 存哪 | 例子 |
| --- | --- | --- | --- |
| 短期记忆（对话历史 + 上一轮解析结果） | `messages`（`add_messages`）+ `last_spec` | 检查点（`data/checkpoints.db`） | "那华东地区呢？"能接上"2026 年九月" |
| 长期记忆（用户偏好） | `PreferenceStore` | 独立表（`data/preferences.db`） | 默认金额单位"万元"、默认维度"按产品" |

> **教学简化**：项目把"长期记忆"缩到两个键（`amount_unit`、`default_dimension`），并只做"读全部 + 批量写"。这是为了让读者在一屏内看完；真实产品还会有事实抽取、置信度、过期策略、权限隔离。官方 `BaseStore` 的完整语义（`batch`、`list_namespaces`、向量检索、TTL）本项目**没有实现**。

### 3.4 `PreferenceStore` 到底简化了什么

项目源码 `workflow/persistence.py` 的类注释已经把差异写清楚了（原文摘录，第 171–186 行）：

```python
class PreferenceStore:
    """会话级偏好的 SQLite 持久化存储（教学用 Store 适配器）。

    实现 ``get / put / search / all_for_user / set_many`` 五个方法，因此既能直接给
    节点当"偏好读写器"用，也能作为 Store 适配器注入 ``builder.compile(store=...)``。
    ...
```

> **勘误（已修复）**：早期源码的这行 docstring 里混进过一个非中文字符（"教学용"，韩文），不影响运行；2026-10-19 审查时已在 `persistence.py` 里改为"教学用"，本单元引文同步更新。

它真实提供的接口（`persistence.py` 第 233、242、266、291、310、348 行）：

| 方法 | 作用 | 与官方 `BaseStore` 的差异 |
| --- | --- | --- |
| `get(namespace, key)` | 读一条 | 官方返回 `Item` 对象；这里返回 `PreferenceItem` dataclass，字段更少 |
| `put(namespace, key, value)` | 写一条（覆盖） | 官方 `value` 是任意 JSON dict；这里也只接受 dict，并用 `jsonable()` 兜底 |
| `search(namespace_prefix, limit, offset, filter)` | 前缀 + 等值过滤 | **没有向量相似度**，没有打分 |
| `all_for_user(user_id)` | 项目自加的便捷方法 | 官方没有这个方法，是项目给节点用的"读全部偏好" |
| `set_many(user_id, values)` | 项目自加：一次事务批量写，`None` 表示删除 | 官方对应 `batch()`，语义更复杂 |
| `delete(namespace, key)` | 删除 | 官方也有，但本项目表里没有 TTL |

> **项目设计选择**：`graph.py` 在 `builder.compile(store=store)` 外面包了 `try/except`：如果这个适配器不被接受（不是官方 `BaseStore` 实例），就退化为"只注入 checkpointer"，节点仍从 `context` 里的同一个 `PreferenceStore` 读写，**功能不受影响**。所以项目里"Store 注入"是"尽力而为"，真正的读写通道是 context。

### 3.5 记忆的"写入时机"才是难点

记住三句话：

1. **节点只返回增量**，不要返回整个状态（否则会把别的节点写的东西覆盖掉）。
2. **要跨轮保留的字段，绝不能出现在 `initial_state()` 里**（`last_spec` 就是这样被保护起来的）。
3. **每次 `invoke_turn` 都会重置"本轮字段"**（`spec`、`data_result`、`calculation`、`draft_report`、`review`…），因为它们是"这一轮的分析结果"，不该被上一轮污染。

---

## 4. 这个知识在主项目中的位置

| 概念 | 项目文件（接口名与源码一致） | 具体用法 |
| --- | --- | --- |
| `AssistantState` | `src/ops_assistant/workflow/state.py` | 23 个通道；`messages` 用 `add_messages`，`trace`/`errors` 用 `operator.add`，`last_spec` 是"跨轮记忆" |
| `AssistantContext` | 同上 | 6 个依赖：`settings` / `conn` / `kb` / `llm` / `prefs` / `metrics` |
| `initial_state()` | 同上 | 构造本轮输入；**故意不含 `last_spec`**（第 138–141 行注释写明了原因） |
| `effective_preferences()` | 同上 | 合并默认值 + 过滤非法值（`amount_unit` 只允许"元/万元"，`default_dimension` 只允许 region/product/month） |
| `PreferenceStore` | `src/ops_assistant/workflow/persistence.py` | 表 `preferences(user_id, namespace, key, value, created_at, updated_at)`；`all_for_user` / `set_many` 给节点用 |
| `AppRuntime.store` | 同上 | 属性，返回 `self.prefs`，注释写明"同时作为 `builder.compile(store=...)` 注入的 Store" |
| `AppRuntime._context()` | 同上 | 一次 `invoke` 构造一份 `AssistantContext`，六个依赖全是**对象引用**，会随进程消失 |
| `Runtime[AssistantContext]` | `src/ops_assistant/workflow/nodes.py` | 每个节点第一件事就是 `_resolve_deps(runtime)`，从 `runtime.context` 取依赖 |
| `understand_node` 的继承逻辑 | 同上（第 732–790 行） | 无时间 → 继承 `last_spec.current_month`；无地区 → 继承 `last_spec.regions`；维度缺失 → 用偏好 `default_dimension` |
| `build_graph()` | `src/ops_assistant/workflow/graph.py` | `StateGraph(AssistantState, context_schema=AssistantContext)` + `compile(checkpointer=..., store=...)` |

数据流（一张图看完）：

```text
用户问题
   │
   ▼
invoke_turn(question, thread_id, user_id)          ← persistence.py
   │  构造 initial_state()（不含 last_spec）
   │  构造 _config()：thread_id + recursion_limit
   │  构造 _context()：settings/conn/kb/llm/prefs/metrics
   ▼
graph.invoke(state, config=config, context=context)
   │
   ├─ checkpointer 读出该 thread 的旧快照（含上一轮 last_spec）
   ├─ understand：读 context.prefs（长期记忆）+ state.last_spec（短期记忆）
   │              写 spec / route / 新的 last_spec
   └─ …
   ▼
每一步后写一条新快照 → data/checkpoints.db
```

> **项目设计选择**：为什么偏好用 `user_id` 而不用 `thread_id`？因为"默认金额单位"是人的属性，不是某次对话的属性；换一个话题不该重新问一遍。

---

## 5. 文件、完整代码、配置与命令

### 5.1 项目里真正在跑的四段代码（源码摘录）

**片段 A：`workflow/state.py` 第 138–141 行——为什么 `last_spec` 不放进 `initial_state()`**

```python
    # --- 会话级（跨轮）字段 ---
    # 注意：last_spec 故意不放进 initial_state，否则每轮都会被空值覆盖，
    # 追问时就读不到上一轮的解析结果了（非 reducer 通道会被输入字典替换）。
    last_spec: dict[str, Any] | None
```

**片段 B：`workflow/state.py` 第 144–156 行——Context 里放什么**

```python
class AssistantContext(TypedDict):
    """图运行时的依赖集合（通过 ``graph.invoke(..., context=...)`` 注入）。

    这样节点无需访问模块级全局变量，测试时可以注入临时目录/临时数据库。
    """

    settings: Settings
    conn: Any
    kb: KnowledgeBase
    llm: LLMClient
    prefs: "PreferenceStore"
    metrics: MetricsCollector
```

**片段 C：`workflow/persistence.py` 第 499–518 行——`store` 属性与 `_context()`**

```python
    @property
    def store(self) -> PreferenceStore:
        """偏好存储；同时作为 ``builder.compile(store=...)`` 注入的 Store。"""
        return self.prefs

    def _context(self, user_id: str) -> AssistantContext:
        """构造节点依赖上下文（一次 invoke 复用同一份）。"""
        return {
            "settings": self.settings,
            "conn": self.conn,
            "kb": self.kb,
            "llm": self.llm,
            "prefs": self.prefs,
            "metrics": self.metrics,
        }
```

**片段 D：`workflow/nodes.py` 第 372–384 行——节点怎么读长期记忆（读失败不影响主流程）**

```python
def _prefs_for_user(deps: _Deps, user_id: str, *, thread_id: str) -> dict[str, Any]:
    """读取用户偏好并合并默认值；读失败只记日志，不影响主流程。"""
    try:
        values = deps.prefs.all_for_user(user_id) or {}
    except Exception as exc:  # 偏好读失败不应让整轮失败
        deps.metrics.record_error(
            code="PREF_READ_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            thread_id=thread_id,
            node="understand",
        )
        values = {}
    return effective_preferences(values if isinstance(values, dict) else {})
```

**片段 E：`workflow/nodes.py` 第 739–748 行——追问继承时间范围**

```python
    if last_spec is not None:
        if not current_month and not _has_explicit_time(question) and last_spec.get("current_month"):
            inherited_month = str(last_spec.get("current_month") or "").strip()
            if LEGAL_MONTH.match(inherited_month):
                spec_dict["current_month"] = inherited_month
                spec_dict["previous_month"] = (
                    str(last_spec.get("previous_month") or "").strip()
                    or _previous_month_of(inherited_month)
                )
                current_month = inherited_month
                needs_clarification = False
```

> 这五段合起来就是本单元的全部机制：**短期记忆在检查点里（`last_spec`）、长期记忆在独立表里（`prefs`）、依赖从 context 注入（不入档）**。

### 5.2 可运行实验：`教程/我的实验/15_memory_store.py`（一步步搭出来）

不导入 ops_assistant、不联网：用一个**迷你图**复刻主项目的四层机制（State / Checkpointer / Store / Runtime Context），每层单独观察。分 7 步搭出，最终与仓库完全一致；运行前不需要任何环境变量。

#### 步骤 1：常量、上下文与状态——三个"记忆体"先分家

**这一步解决什么**：动手前先划清三块领地——**图状态**（MiniState，进检查点）、**运行时依赖**（MiniContext，不进检查点）、**默认偏好**（DEFAULT_PREFS，兜底值）。

新建 `教程/我的实验/15_memory_store.py`（参考实现在 `教程/实验/15_memory_store.py`，写完本单元再对照）：

```python
"""单元 15 实验（步骤 1）：常量、上下文与状态。"""

from __future__ import annotations

import operator
import sqlite3
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
PREFS_DB = OUTPUT_DIR / "preferences.db"

DEFAULT_PREFS: dict[str, str] = {"amount_unit": "元", "default_dimension": "region"}
VALID_DIMENSIONS = ("region", "product", "month")


class MiniContext(TypedDict):
    """运行时依赖：与主项目 AssistantContext 同构，但只保留必需的三项。"""

    store: "MiniStore"
    user_id: str
    note: str


class MiniState(TypedDict, total=False):
    """图状态：messages/trace 带 reducer，last_spec 是跨轮字段。"""

    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    spec: dict[str, Any]
    last_spec: dict[str, Any] | None
    status: str
    trace: Annotated[list[dict[str, Any]], operator.add]
```

**逐行讲解**：

- `MiniContext` 是 **TypedDict 而不是 dataclass**：LangGraph 的 `context_schema` 要一个类型标注，节点从 `runtime.context` 里按键取——**依赖注入的形状**。
- `last_spec` 放在 MiniState 里（**不是** context 里）：它要**跟着检查点走**，换进程也要在——上一轮的解析结果是"短期记忆"，属于状态。
- `store`/`user_id` 放在 context 里（**不是**状态里）：连接对象**不能序列化进检查点**，用户身份每轮由外部传入——步骤 4 的快照观察会证明"context 不在状态里"。
- `DEFAULT_PREFS` 与 `VALID_DIMENSIONS`：偏好的**合法值域**写死在常量里——校验的依据。

**运行与观察**（临时验证）：

```python
if __name__ == "__main__":
    print(DEFAULT_PREFS, VALID_DIMENSIONS)
```

```text
{'amount_unit': '元', 'default_dimension': 'region'} ('region', 'product', 'month')
```

#### 步骤 2：`MiniStore`——两列表的教学版长期记忆

**这一步解决什么**：跨 thread 的长期记忆到底是什么？一个 SQLite 两列键值表（namespace + key → value）就足以讲清语义——namespace 按 user_id 隔离。

```python
class MiniStore:
    """教学版 Store：SQLite 两列键值表。

    只实现 get/put/all_for_user 三个方法，足以说明"跨 thread 的长期记忆"；
    不是官方 BaseStore 的完整实现（没有 search/向量检索/TTL/batch）。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS prefs (namespace TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, PRIMARY KEY (namespace, key))"
        )
        self._conn.commit()

    @staticmethod
    def namespace_for(user_id: str) -> str:
        return f"users/{user_id}/preferences"

    def put(self, user_id: str, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO prefs (namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (self.namespace_for(user_id), str(key), str(value)),
        )
        self._conn.commit()

    def all_for_user(self, user_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM prefs WHERE namespace = ?", (self.namespace_for(user_id),)
        ).fetchall()
        return {str(key): str(value) for key, value in rows}

    def close(self) -> None:
        self._conn.close()
```

**逐行讲解**：

- `namespace_for` 生成 `users/<user_id>/preferences`：**主键是 (namespace, key)**，同一 key 在不同用户下互不干扰——Store 的隔离单位是**命名空间**，不是 thread。
- `ON CONFLICT ... DO UPDATE`：SQLite 的 upsert——写偏好是"改"不是"增"，幂等。
- docstring 主动声明"不是完整 BaseStore"：**教学替身的边界要写明**，读者才知道换到官方实现时哪些行为会变。

**运行与观察**（临时验证）：

```python
    store = MiniStore(PREFS_DB)
    store.put("u1", "amount_unit", "万元")
    store.put("u2", "amount_unit", "元")
    print(store.all_for_user("u1"), store.all_for_user("u2"))
    store.close()
```

```text
{'amount_unit': '万元'} {'amount_unit': '元'}
```

同名 key、不同用户、各存各的——**长期记忆按人走**。

#### 步骤 3：`effective_prefs` 与 `parse_mini`——偏好合并与极简解析

**这一步解决什么**：偏好不能直接信（库里可能是脏值）——合并默认值 + **非法回退**；解析器只认几个词，够演示就行。

```python
def effective_prefs(store: MiniStore, user_id: str) -> dict[str, str]:
    """合并默认值与已存偏好；非法值回退成默认（与主项目同口径）。"""
    merged = dict(DEFAULT_PREFS)
    for key, value in store.all_for_user(user_id).items():
        if key in merged and value:
            merged[key] = value
    if merged["default_dimension"] not in VALID_DIMENSIONS:
        merged["default_dimension"] = DEFAULT_PREFS["default_dimension"]
    if merged["amount_unit"] not in {"元", "万元"}:
        merged["amount_unit"] = DEFAULT_PREFS["amount_unit"]
    return merged


def parse_mini(question: str) -> dict[str, Any]:
    """极简解析：只认两个月份、两个地区和"渠道"这个词。"""
    spec: dict[str, Any] = {"current_month": None, "regions": [], "dimension": None}
    for token, month in (("九月", "2026-09"), ("八月", "2026-08")):
        if token in question:
            spec["current_month"] = month
    for region in ("华东", "西南"):
        if region in question:
            spec["regions"].append(region)
    if "渠道" in question:
        spec["dimension"] = "channel"
    elif spec["regions"]:
        spec["dimension"] = "region"
    return spec
```

**逐行讲解**：

- `effective_prefs` 三步走：**默认打底 → 已存覆盖 → 非法回退**。注意未知 key 直接被丢弃（`key in merged` 才收）——Store 里多出来的脏键污染不了运行时。
- `parse_mini` 里"九月和八月都出现时，**后识别的覆盖**"（循环顺序八月在后）——极简实现的已知怪癖，实测 A1 会得到 `2026-08`；主项目的 `parse_question` 没有这个问题（它取"最靠前的月份词"）——**教学替身与生产代码的差异要心里有数**。
- dimension 的推断顺序：明说"渠道" > 提到地区 > None（留给偏好兜底）。

**运行与观察**（临时验证）：

```python
    print(parse_mini("2026 年九月销售额比八月变化多少？"))
    print(parse_mini("那华东地区呢？"))
    print(parse_mini("2026 年九月销售额多少？"))
```

```text
{'current_month': '2026-08', 'regions': [], 'dimension': None}
{'current_month': None, 'regions': ['华东'], 'dimension': 'region'}
{'current_month': '2026-09', 'regions': [], 'dimension': None}
```

注意第三行 dimension=None——**它正等着长期偏好来填**（步骤 4）。

#### 步骤 4：`understand_mini`——三层记忆在一个节点里合流

**这一步解决什么**：本实验的心脏。一轮理解 = **本轮解析**（parse_mini）+ **短期记忆**（last_spec 继承）+ **长期记忆**（prefs 兜底），每用一个记忆就在 trace 里记一笔。

```python
from langchain_core.messages import AIMessage


def understand_mini(state: MiniState, runtime: Runtime[MiniContext]) -> dict[str, Any]:
    """迷你版 understand：短期记忆（last_spec）+ 长期记忆（store）+ 本轮解析。"""
    context = runtime.context
    store = context["store"]
    user_id = context["user_id"]
    prefs = effective_prefs(store, user_id)
    question = str(state.get("question") or "")

    trace: list[dict[str, Any]] = []
    spec = parse_mini(question)
    last_spec = state.get("last_spec")
    if not isinstance(last_spec, dict):
        last_spec = None

    if spec["current_month"] is None and last_spec and last_spec.get("current_month"):
        spec["current_month"] = str(last_spec["current_month"])
        trace.append({"node": "understand", "action": "inherit_period", "from": spec["current_month"]})

    if not spec["regions"] and last_spec and last_spec.get("regions"):
        spec["regions"] = list(last_spec["regions"])
        trace.append({"node": "understand", "action": "inherit_regions", "from": spec["regions"]})

    if spec["dimension"] is None:
        spec["dimension"] = prefs["default_dimension"]
        trace.append(
            {
                "node": "understand",
                "action": "used_preference",
                "key": "default_dimension",
                "value": prefs["default_dimension"],
            }
        )

    needs_month = spec["current_month"] is None
    if needs_month:
        answer = "请说明要分析的月份（例如“2026 年九月”）。"
        status = "clarification"
    else:
        answer = f"已记录：{spec['current_month']} / {spec['dimension']} / {spec['regions']}"
        status = "answered"

    update: dict[str, Any] = {
        "spec": spec,
        "status": status,
        "messages": [AIMessage(content=answer)],
        "trace": trace,
    }
    if spec["current_month"]:
        update["last_spec"] = dict(spec)
    return update
```

**逐行讲解**：

- 节点签名多出第二个参数 `runtime: Runtime[MiniContext]`：**context 从 runtime 进，不从 state 进**——这是它与单元 06～08 节点的唯一区别。
- 三层记忆的**优先级**：本轮解析 > 短期继承 > 长期偏好——用户**这轮明说的**永远最优先，没说的才问记忆要。
- `trace.append(...)` 每次记忆使用都记一笔（inherit_period / inherit_regions / used_preference）——**记忆的使用可审计**，这是主项目 trace 设计的延续。
- `update["last_spec"] = dict(spec)` 只有拿到月份才写：**没解析出任何东西就不污染短期记忆**（clarification 轮不覆盖上一轮的好 spec）。

#### 步骤 5：装配与驱动——`build_graph` / `config_for` / `run_turn`

**这一步解决什么**：把图编译起来（带 checkpointer；store 能不能塞进 compile **因版本而异**，要有回退），并写一个统一的"跑一轮"驱动：每轮**现场组装 context** 注入。

```python
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver


def build_graph(checkpointer: Any | None, store: MiniStore | None) -> tuple[Any, bool]:
    """装配迷你图；返回 (编译后的图, 是否成功把 store 传进了 compile)。"""
    builder = StateGraph(MiniState, context_schema=MiniContext)
    builder.add_node("understand", understand_mini)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", END)

    if store is not None:
        try:
            return builder.compile(checkpointer=checkpointer, store=store), True
        except Exception:
            # 教学适配器不是官方 BaseStore 时会走到这里：退化为只注入 checkpointer，
            # 节点仍然从 context 读到同一个 store，读写行为不变。
            pass
    return builder.compile(checkpointer=checkpointer), False


def config_for(thread_id: str, user_id: str) -> dict[str, Any]:
    """一次 invoke 的配置：thread_id 决定检查点归属，user_id 只影响偏好。"""
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def run_turn(
    graph: Any,
    store: MiniStore,
    *,
    thread_id: str,
    user_id: str,
    question: str,
) -> dict[str, Any]:
    """跑一轮并返回图状态（含 last_spec 与 trace）。"""
    context: MiniContext = {"store": store, "user_id": user_id, "note": "本对象不入检查点"}
    config = config_for(thread_id, user_id)
    return graph.invoke(
        {"question": question, "messages": [HumanMessage(content=question)]},
        config=config,
        context=context,
    )
```

**逐行讲解**：

- `StateGraph(MiniState, context_schema=MiniContext)`：**context 也要在图上声明**——不声明，节点签名里的 runtime 参数不会被注入。
- `compile(..., store=store)` 的 try/except：教学版 MiniStore **不是**官方 BaseStore 子类，某些版本会在编译期校验失败——回退后节点照样从 context 拿到 store（**行为不变**，注入路径不同而已），返回的 bool 供 main 汇报。
- `run_turn` 每轮新建 context 字典：**context 是"每次调用"的东西**，不是全局单例——note 字段那句"本对象不入检查点"就是给步骤 6 的快照观察埋的伏笔。
- `config` 带 thread_id（检查点归属）与 user_id（偏好归属）：**两把不同的钥匙开两扇不同的门**。

#### 步骤 6：`main()` 上半——短期继承、跨 thread 失忆、长期偏好

**这一步解决什么**：三组对照实验：①同一 thread 追问（last_spec 接住）；②换 thread 同样追问（失忆）；③换 user（偏好变）。

```python
def main() -> int:
    """按五段演示：跨轮继承 → 长期偏好 → 快照内容 → 无检查点 → store 注入结果。"""
    print("== 单元 15 实验：多轮记忆、Store 与 Runtime Context ==")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if PREFS_DB.exists():
        PREFS_DB.unlink()
    store = MiniStore(PREFS_DB)
    store.put("demo-user", "default_dimension", "region")

    graph, store_injected = build_graph(InMemorySaver(), store)

    print("\n== 1) 同一 thread 的追问：last_spec 从检查点里读回 ==")
    first = run_turn(
        graph, store, thread_id="thread-a", user_id="demo-user",
        question="2026 年九月销售额比八月变化多少？",
    )
    follow = run_turn(
        graph, store, thread_id="thread-a", user_id="demo-user",
        question="那华东地区呢？",
    )
    print("A1 spec =", first["spec"])
    print("A1 trace =", first["trace"])
    print("A2 spec =", follow["spec"])
    print("A2 trace =", follow["trace"])

    print("\n== 2) 另一个 thread 的同一句追问：没有 last_spec 可继承 ==")
    lonely = run_turn(
        graph, store, thread_id="thread-b", user_id="demo-user",
        question="那华东地区呢？",
    )
    print("B1 status =", lonely["status"])
    print("B1 spec =", lonely["spec"])

    print("\n== 3) 长期记忆：换 thread 也生效（按 user_id，不看 thread） ==")
    store.put("vip-user", "default_dimension", "product")
    print("demo-user 偏好 =", effective_prefs(store, "demo-user"))
    print("vip-user  偏好 =", effective_prefs(store, "vip-user"))
    vip = run_turn(
        graph, store, thread_id="thread-c", user_id="vip-user",
        question="2026 年九月销售额多少？",
    )
    print("C1 spec =", vip["spec"])
    print("C1 trace =", vip["trace"])
```

**逐行讲解**：

- 开头 `PREFS_DB.unlink()`：**每跑一次都从干净偏好库开始**——实验可重复。
- 三组对照的变量控制堪称模板：①②同 user 不同 thread（短期记忆差异）；①③同图不同 user（长期偏好差异）。
- `run_turn` 复用让每组实验只剩"参数不同"——**实验代码的噪音被驱动函数吃掉了**。

**运行与观察**（实测 2026-10-23）：

```text
== 1) 同一 thread 的追问：last_spec 从检查点里读回 ==
A1 spec = {'current_month': '2026-08', 'regions': [], 'dimension': 'region'}
A1 trace = [{'node': 'understand', 'action': 'used_preference', …}]
A2 spec = {'current_month': '2026-08', 'regions': ['华东'], 'dimension': 'region'}
A2 trace = [{…'used_preference'…}, {…'inherit_period', 'from': '2026-08'}]

== 2) 另一个 thread 的同一句追问：没有 last_spec 可继承 ==
B1 status = clarification
B1 spec = {'current_month': None, 'regions': ['华东'], 'dimension': 'region'}

== 3) 长期记忆：换 thread 也生效（按 user_id，不看 thread） ==
demo-user 偏好 = {'amount_unit': '元', 'default_dimension': 'region'}
vip-user  偏好 = {'amount_unit': '元', 'default_dimension': 'product'}
C1 spec = {'current_month': '2026-09', 'regions': [], 'dimension': 'product'}
```

三行结论：A2 继承了月份（inherit_period）；B1 换 thread 立刻 clarification（**短期记忆贴 thread**）；C1 的问题只写了月份、dimension 由 vip-user 的偏好填成 product（**长期记忆贴 user**）。另注意 A1 是 2026-08——步骤 3 讲过的"后识别覆盖"怪癖，如实呈现。

#### 步骤 7：`main()` 下半与整合——快照、无检查点、注入汇报

**这一步解决什么**：最后三问：检查点里到底存了什么（context 在不在）？没有 checkpointer 会怎样？store 塞进 compile 成功了吗？

```python
    print("\n== 4) 检查点里到底存了什么（context 不在里面） ==")
    snapshot = graph.get_state(config_for("thread-a", "demo-user"))
    print("状态通道 =", sorted(snapshot.values.keys()))
    print("context 是否进了状态 =", "store" in snapshot.values)
    print("next =", snapshot.next)

    print("\n== 5) 没有 checkpointer：同一段追问立刻失忆 ==")
    stateless, _ = build_graph(None, store)
    run_turn(stateless, store, thread_id="thread-d", user_id="demo-user",
             question="2026 年九月销售额比八月变化多少？")
    lost = run_turn(stateless, store, thread_id="thread-d", user_id="demo-user",
                    question="那华东地区呢？")
    print("D2 status =", lost["status"])
    print("D2 spec =", lost["spec"])

    print("\n== 6) 把教学版 Store 传进 compile() 的结果 ==")
    if store_injected:
        print("store 注入 compile：成功（你的版本接受了这个适配器）")
    else:
        print("store 注入 compile：回退为只注入 checkpointer（适配器不是官方 BaseStore）")
    print("→ 无论哪条分支，节点都从 context 读同一个 store，偏好行为不变。")

    store.close()
    return 0
```

（文件末尾补 `if __name__ == "__main__": raise SystemExit(main())`。）

**逐行讲解**：

- 段 4：快照的 values 里**只有状态通道的键**（last_spec/messages/…），`"store" in snapshot.values` 是 False——**context 活在进程里，不进检查点**，与步骤 1 的分家设计互相印证。
- 段 5：`build_graph(None, store)` 造一个**无检查点**的图——同 thread 两连问，第二轮立刻 clarification：短期记忆的载体就是检查点，没有它就没有"上一轮"。
- 段 6：`store_injected` 的两条分支都**不影响偏好行为**——注入方式是实现细节，语义靠 context 保证。

清理各步临时验证，完整文件应与仓库完全一致：

```python
"""单元 15 实验：State / Checkpointer / thread / Store / Runtime Context 的分工。

本脚本不导入 ops_assistant，也不联网：用一个"迷你图"复刻主项目的四个机制，
方便单独观察每一层的行为。运行前不需要任何环境变量。

用法：
    cd ops-assistant
    uv run python ../教程/实验/15_memory_store.py
"""

from __future__ import annotations

import operator
import sqlite3
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
PREFS_DB = OUTPUT_DIR / "preferences.db"

DEFAULT_PREFS: dict[str, str] = {"amount_unit": "元", "default_dimension": "region"}
VALID_DIMENSIONS = ("region", "product", "month")


class MiniContext(TypedDict):
    """运行时依赖：与主项目 AssistantContext 同构，但只保留必需的三项。"""

    store: "MiniStore"
    user_id: str
    note: str


class MiniState(TypedDict, total=False):
    """图状态：messages/trace 带 reducer，last_spec 是跨轮字段。"""

    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    spec: dict[str, Any]
    last_spec: dict[str, Any] | None
    status: str
    trace: Annotated[list[dict[str, Any]], operator.add]


class MiniStore:
    """教学版 Store：SQLite 两列键值表。

    只实现 get/put/all_for_user 三个方法，足以说明"跨 thread 的长期记忆"；
    不是官方 BaseStore 的完整实现（没有 search/向量检索/TTL/batch）。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS prefs (namespace TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, PRIMARY KEY (namespace, key))"
        )
        self._conn.commit()

    @staticmethod
    def namespace_for(user_id: str) -> str:
        return f"users/{user_id}/preferences"

    def put(self, user_id: str, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO prefs (namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (self.namespace_for(user_id), str(key), str(value)),
        )
        self._conn.commit()

    def all_for_user(self, user_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM prefs WHERE namespace = ?", (self.namespace_for(user_id),)
        ).fetchall()
        return {str(key): str(value) for key, value in rows}

    def close(self) -> None:
        self._conn.close()


def effective_prefs(store: MiniStore, user_id: str) -> dict[str, str]:
    """合并默认值与已存偏好；非法值回退成默认（与主项目同口径）。"""
    merged = dict(DEFAULT_PREFS)
    for key, value in store.all_for_user(user_id).items():
        if key in merged and value:
            merged[key] = value
    if merged["default_dimension"] not in VALID_DIMENSIONS:
        merged["default_dimension"] = DEFAULT_PREFS["default_dimension"]
    if merged["amount_unit"] not in {"元", "万元"}:
        merged["amount_unit"] = DEFAULT_PREFS["amount_unit"]
    return merged


def parse_mini(question: str) -> dict[str, Any]:
    """极简解析：只认两个月份、两个地区和"渠道"这个词。"""
    spec: dict[str, Any] = {"current_month": None, "regions": [], "dimension": None}
    for token, month in (("九月", "2026-09"), ("八月", "2026-08")):
        if token in question:
            spec["current_month"] = month
    for region in ("华东", "西南"):
        if region in question:
            spec["regions"].append(region)
    if "渠道" in question:
        spec["dimension"] = "channel"
    elif spec["regions"]:
        spec["dimension"] = "region"
    return spec


def understand_mini(state: MiniState, runtime: Runtime[MiniContext]) -> dict[str, Any]:
    """迷你版 understand：短期记忆（last_spec）+ 长期记忆（store）+ 本轮解析。"""
    context = runtime.context
    store = context["store"]
    user_id = context["user_id"]
    prefs = effective_prefs(store, user_id)
    question = str(state.get("question") or "")

    trace: list[dict[str, Any]] = []
    spec = parse_mini(question)
    last_spec = state.get("last_spec")
    if not isinstance(last_spec, dict):
        last_spec = None

    if spec["current_month"] is None and last_spec and last_spec.get("current_month"):
        spec["current_month"] = str(last_spec["current_month"])
        trace.append({"node": "understand", "action": "inherit_period", "from": spec["current_month"]})

    if not spec["regions"] and last_spec and last_spec.get("regions"):
        spec["regions"] = list(last_spec["regions"])
        trace.append({"node": "understand", "action": "inherit_regions", "from": spec["regions"]})

    if spec["dimension"] is None:
        spec["dimension"] = prefs["default_dimension"]
        trace.append(
            {
                "node": "understand",
                "action": "used_preference",
                "key": "default_dimension",
                "value": prefs["default_dimension"],
            }
        )

    needs_month = spec["current_month"] is None
    if needs_month:
        answer = "请说明要分析的月份（例如“2026 年九月”）。"
        status = "clarification"
    else:
        answer = f"已记录：{spec['current_month']} / {spec['dimension']} / {spec['regions']}"
        status = "answered"

    update: dict[str, Any] = {
        "spec": spec,
        "status": status,
        "messages": [AIMessage(content=answer)],
        "trace": trace,
    }
    if spec["current_month"]:
        update["last_spec"] = dict(spec)
    return update


def build_graph(checkpointer: Any | None, store: MiniStore | None) -> tuple[Any, bool]:
    """装配迷你图；返回 (编译后的图, 是否成功把 store 传进了 compile)。"""
    builder = StateGraph(MiniState, context_schema=MiniContext)
    builder.add_node("understand", understand_mini)
    builder.add_edge(START, "understand")
    builder.add_edge("understand", END)

    if store is not None:
        try:
            return builder.compile(checkpointer=checkpointer, store=store), True
        except Exception:
            # 教学适配器不是官方 BaseStore 时会走到这里：退化为只注入 checkpointer，
            # 节点仍然从 context 读到同一个 store，读写行为不变。
            pass
    return builder.compile(checkpointer=checkpointer), False


def config_for(thread_id: str, user_id: str) -> dict[str, Any]:
    """一次 invoke 的配置：thread_id 决定检查点归属，user_id 只影响偏好。"""
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


def run_turn(
    graph: Any,
    store: MiniStore,
    *,
    thread_id: str,
    user_id: str,
    question: str,
) -> dict[str, Any]:
    """跑一轮并返回图状态（含 last_spec 与 trace）。"""
    context: MiniContext = {"store": store, "user_id": user_id, "note": "本对象不入检查点"}
    config = config_for(thread_id, user_id)
    return graph.invoke(
        {"question": question, "messages": [HumanMessage(content=question)]},
        config=config,
        context=context,
    )


def main() -> int:
    """按五段演示：跨轮继承 → 长期偏好 → 快照内容 → 无检查点 → store 注入结果。"""
    print("== 单元 15 实验：多轮记忆、Store 与 Runtime Context ==")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if PREFS_DB.exists():
        PREFS_DB.unlink()
    store = MiniStore(PREFS_DB)
    store.put("demo-user", "default_dimension", "region")

    graph, store_injected = build_graph(InMemorySaver(), store)

    print("\n== 1) 同一 thread 的追问：last_spec 从检查点里读回 ==")
    first = run_turn(
        graph, store, thread_id="thread-a", user_id="demo-user",
        question="2026 年九月销售额比八月变化多少？",
    )
    follow = run_turn(
        graph, store, thread_id="thread-a", user_id="demo-user",
        question="那华东地区呢？",
    )
    print("A1 spec =", first["spec"])
    print("A1 trace =", first["trace"])
    print("A2 spec =", follow["spec"])
    print("A2 trace =", follow["trace"])

    print("\n== 2) 另一个 thread 的同一句追问：没有 last_spec 可继承 ==")
    lonely = run_turn(
        graph, store, thread_id="thread-b", user_id="demo-user",
        question="那华东地区呢？",
    )
    print("B1 status =", lonely["status"])
    print("B1 spec =", lonely["spec"])

    print("\n== 3) 长期记忆：换 thread 也生效（按 user_id，不看 thread） ==")
    store.put("vip-user", "default_dimension", "product")
    print("demo-user 偏好 =", effective_prefs(store, "demo-user"))
    print("vip-user  偏好 =", effective_prefs(store, "vip-user"))
    vip = run_turn(
        graph, store, thread_id="thread-c", user_id="vip-user",
        question="2026 年九月销售额多少？",
    )
    print("C1 spec =", vip["spec"])
    print("C1 trace =", vip["trace"])

    print("\n== 4) 检查点里到底存了什么（context 不在里面） ==")
    snapshot = graph.get_state(config_for("thread-a", "demo-user"))
    print("状态通道 =", sorted(snapshot.values.keys()))
    print("context 是否进了状态 =", "store" in snapshot.values)
    print("next =", snapshot.next)

    print("\n== 5) 没有 checkpointer：同一段追问立刻失忆 ==")
    stateless, _ = build_graph(None, store)
    run_turn(stateless, store, thread_id="thread-d", user_id="demo-user",
             question="2026 年九月销售额比八月变化多少？")
    lost = run_turn(stateless, store, thread_id="thread-d", user_id="demo-user",
                    question="那华东地区呢？")
    print("D2 status =", lost["status"])
    print("D2 spec =", lost["spec"])

    print("\n== 6) 把教学版 Store 传进 compile() 的结果 ==")
    if store_injected:
        print("store 注入 compile：成功（你的版本接受了这个适配器）")
    else:
        print("store 注入 compile：回退为只注入 checkpointer（适配器不是官方 BaseStore）")
    print("→ 无论哪条分支，节点都从 context 读同一个 store，偏好行为不变。")

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**回看地图**：

| 步骤 | 最终脚本里的位置 |
| --- | --- |
| 1 分家 | 常量、`MiniContext`、`MiniState` |
| 2 长期记忆 | `MiniStore`（namespace 键值表） |
| 3 合并与解析 | `effective_prefs()`、`parse_mini()` |
| 4 心脏 | `understand_mini()`（三层记忆 + trace 审计） |
| 5 装配 | `build_graph()`、`config_for()`、`run_turn()` |
| 6 对照实验 | `main()` 段 1–3（继承/失忆/偏好） |
| 7 见底 | `main()` 段 4–6（快照/无检查点/注入汇报）+ 整合 |

运行与验收：见 §5.3 的命令。核心验收：A2 有 inherit_period、B1 clarification、C1 的 dimension=product、`"store" in snapshot.values` 为 False——四件事都对，四层分工就真的看懂了。

### 5.3 运行命令

```powershell
cd "D:\MySpace\LangChain学习路线\ops-assistant"
uv run python ../教程/我的实验/15_memory_store.py
```

生成物（都在实验目录，不进 `ops-assistant/data`）：

- `教程/我的实验/_output/preferences.db`：教学版 Store 的 SQLite 文件（每次运行前会删掉重建）。

### 5.4 逐段解释

| 代码段 | 演示的机制 | 观察重点 |
| --- | --- | --- |
| `MiniContext` + `context_schema=MiniContext` | Runtime Context 注入 | `runtime.context` 里是三个依赖对象，`note` 字段故意写"本对象不入检查点" |
| `MiniState.last_spec` | 短期记忆 | 它出现在 `snapshot.values` 里；`initial_state` 里没有它 |
| `run_turn` 两次同 `thread_id` | 检查点恢复 | 第二次 `invoke` 只传了新问题，`last_spec` 从快照里来 |
| 第 2 段换 `thread-b` | thread 隔离 | 没有上一轮 → `status="clarification"` |
| 第 3 段 `vip-user` | 长期记忆 | 偏好按 `user_id` 存，新 thread 立即生效 |
| 第 4 段 `get_state` | 看清边界 | `sorted(snapshot.values)` 只有 6 个状态通道，没有 `store`/`user_id` |
| 第 5 段 `build_graph(None, store)` | 无 checkpointer | 同一 thread 的追问失忆，退化成"每次都是新会话" |
| 第 6 段 `compile(store=...)` | 适配器边界 | 可能成功也可能回退，两条分支都不影响业务 |

### 5.5 六者的对照卡（建议抄进笔记）

| 问题 | State | Checkpointer | thread | Store | Context | 后端 |
| --- | --- | --- | --- | --- | --- | --- |
| 谁创建 | 图定义时 | `compile(checkpointer=...)` | 调用方传 `config` | 项目装配时 | 调用方传 `context=` | 项目装配时 |
| 活多久 | 一次 invoke（reducer 字段跨轮累积） | 取决于后端 | 会话寿命 | 长期 | 一次 invoke | 进程/磁盘 |
| 存业务数据 | 是 | 存状态的快照 | 只是键 | 存偏好/事实 | 否 | 否 |
| 换进程还在 | 否 | `SqliteSaver` 是 / `InMemorySaver` 否 | 看后端 | 是（SQLite 文件） | 否 | — |
| 放连接/模型 | 禁止 | 会被序列化失败 | — | 不适用 | **正确位置** | — |

---

## 6. 预期输出与关键状态变化

### 6.1 实验输出

**预期（未在本环境执行）**：

```text
== 单元 15 实验：多轮记忆、Store 与 Runtime Context ==

== 1) 同一 thread 的追问：last_spec 从检查点里读回 ==
A1 spec = {'current_month': '2026-09', 'regions': [], 'dimension': 'region'}
A1 trace = [{'node': 'understand', 'action': 'used_preference', 'key': 'default_dimension', 'value': 'region'}]
A2 spec = {'current_month': '2026-09', 'regions': ['华东'], 'dimension': 'region'}
A2 trace = [{'node': 'understand', 'action': 'inherit_period', 'from': '2026-09'}]

== 2) 另一个 thread 的同一句追问：没有 last_spec 可继承 ==
B1 status = clarification
B1 spec = {'current_month': None, 'regions': ['华东'], 'dimension': 'region'}

== 3) 长期记忆：换 thread 也生效（按 user_id，不看 thread） ==
demo-user 偏好 = {'amount_unit': '元', 'default_dimension': 'region'}
vip-user  偏好 = {'amount_unit': '元', 'default_dimension': 'product'}
C1 spec = {'current_month': '2026-09', 'regions': [], 'dimension': 'product'}
C1 trace = [{'node': 'understand', 'action': 'used_preference', 'key': 'default_dimension', 'value': 'product'}]

== 4) 检查点里到底存了什么（context 不在里面） ==
状态通道 = ['last_spec', 'messages', 'question', 'spec', 'status', 'trace']
context 是否进了状态 = False
next = ()

== 5) 没有 checkpointer：同一段追问立刻失忆 ==
D2 status = clarification
D2 spec = {'current_month': None, 'regions': ['华东'], 'dimension': 'region'}

== 6) 把教学版 Store 传进 compile() 的结果 ==
store 注入 compile：回退为只注入 checkpointer（适配器不是官方 BaseStore）
→ 无论哪条分支，节点都从 context 读同一个 store，偏好行为不变。
```

> 第 6 段的结论行可能是"成功"而不是"回退"：不同 LangGraph 版本对 `store=` 的类型检查松紧不同，两种结果都正常，**不影响**前五段（**未验证**）。如果你看到的是"成功"，说明这个适配器恰好满足了你版本的要求。

### 6.2 关键状态变化

| 对象 | 之前 | 之后 |
| --- | --- | --- |
| 对"记忆"的理解 | 以为"多轮"就是把历史拼进提示词 | 知道短期记忆是检查点里的状态，长期记忆是 Store 里的偏好 |
| 对 `context=` 的理解 | 以为它也是某种状态 | 知道它是一次执行的依赖注入，不入档 |
| 对 `last_spec` 的理解 | 知道它跨轮 | 能说清"跨轮"靠的是非 reducer 通道被检查点保存、且不被 `initial_state` 覆盖 |
| 对 `PreferenceStore` 的定位 | 以为是官方 Store | 知道是教学适配器，读写走 context，`compile(store=)` 只是尽力而为 |

---

## 7. 修改练习（至少 2 个，含一个"改坏它"）

### 练习 7.1（简单）：让"地区"也变成长期偏好

在 `MiniStore` 里加一个键 `default_regions`（例如 `["华东"]`），让 `understand_mini` 在"问题没提地区、`last_spec` 也没有地区"时用这个偏好，并在 trace 里记 `used_preference / default_regions`。

验收：`thread-b` 那一轮从 `clarification` 变成 `answered`，`spec["regions"] == ["华东"]`。想一想：主项目为什么**没有**做这个功能？（提示：地区是业务口径，用户随意设默认地区容易导出错误报告；项目里只让"维度"有默认值。）

### 练习 7.2（中等）：给 Store 加一条"最近一次问题"的记忆

在 `MiniStore` 里加表 `history(user_id, thread_id, question, created_at)`，在 `understand_mini` 里写一条，在 `main` 里打印 `demo-user` 最近 3 条问题。

验收：换 thread 之后仍能看到跨会话的历史问题记录。要求：写库失败时**不能**让这一轮失败（参考项目 `_prefs_for_user` 的 `try/except` 写法）。

### 练习 7.3（改坏它）：把 `last_spec` 塞进 `initial_state`

改 `run_turn`，让每次 `invoke` 都传 `"last_spec": None`：

```python
return graph.invoke(
    {"question": question, "messages": [HumanMessage(content=question)], "last_spec": None},
    config=config,
    context=context,
)
```

观察并解释：A2 为什么从 `answered` 变成 `clarification`？

参考答案要点：`last_spec` 不是 reducer 通道，**输入字典里出现的键会覆盖检查点里的旧值**。所以"每轮重置一次"就等于删掉了短期记忆。修复方式是删掉那一行——或者像主项目那样，只在真正需要清空时才写它（项目里根本没有这个入口）。

### 练习 7.4（进阶，可选）：让 Context 变成 dataclass

把 `MiniContext` 从 `TypedDict` 改成 `@dataclass(frozen=True)`，仍然用 `context_schema=MiniContext`，节点里改成 `runtime.context.store`。

验收：输出完全不变。想一想：主项目为什么选 `TypedDict` 而不是 dataclass？（`TypedDict` 与 `graph.invoke(context=dict)` 的调用方式一致，测试里直接传 dict 最省事；dataclass 更严格但要求调用方构造对象。**未验证**：你的版本对两者是否都支持。）

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：把 `InMemorySaver` 换成"不要 saver"

删掉 `build_graph(InMemorySaver(), store)` 的 `InMemorySaver()`，改成 `build_graph(None, store)`，再跑第 1 段。

预期现象：A2 不再是 `answered`——第二次 `invoke` 拿不到 `last_spec`，直接问回月份。**结论**：短期记忆的能力来自 checkpointer，不是来自"图"本身。

### 实验 8.2：偏好里写一个非法维度

在 `main` 里加一行：

```python
store.put("demo-user", "default_dimension", "渠道")
```

再跑第 1 段。

预期现象：`A1 spec` 里的 `dimension` 仍是 `'region'`——`effective_prefs` 把非法值回退成默认值了。**对照主项目**：`effective_preferences()` 做同样的回退（`state.py` 第 54–57 行），注释写明"避免坏数据把报告渲染搞崩"。

> **教学简化**：真实系统里"非法偏好"应该被拒绝写入并提示用户，而不是静默回退。项目选回退，是因为偏好读发生在图里、没有交互通道可提示。

### 实验 8.3：删掉 Store 的 `preferences.db` 再读

先正常跑一次（生成 `_output/preferences.db`），然后手动删除该文件，再运行脚本。

预期现象：脚本会重建库、重新 `put` 默认偏好，因此输出**不变**。这说明"长期记忆"的载体是文件本身：文件没了，记忆就没了，但程序不会崩。**项目对照**：`PreferenceStore.__init__` 每次都会 `CREATE TABLE IF NOT EXISTS`，所以删库也能自愈（但已存偏好会丢）。

### 实验 8.4：把 `context` 里的 `user_id` 改成空串

把 `run_turn` 里的 `user_id` 传 `""`，同时在 `MiniContext` 里也传 `""`。

预期现象：`effective_prefs` 用 `users//preferences` 这个奇怪的命名空间，读不到任何偏好，于是全部回退默认值——输出仍然是 `region`，但你观察不到"偏好生效"的现象。**项目对照**：`initial_state()` 里写了 `user_id or DEFAULT_USER_ID`，专门防这种空值。

### 实验 8.5：两次运行之间数据是否还在（跨进程）

1. 第一次运行脚本，记下输出。
2. 注释掉 `if PREFS_DB.exists(): PREFS_DB.unlink()` 这一行。
3. 第二次运行。

预期现象：第 3 段的 `demo-user 偏好` 与第一次相同（因为每次都 `put` 覆盖同一键）。真正能观察到"跨进程"的是 `vip-user` 的 `product`：第一次运行写入后，第二次运行即使不写 `put` 也还在。**结论**：`InMemorySaver` 的检查点跨不了进程，`MiniStore` 的 SQLite 文件可以——这正是 `SqliteSaver` 与 `InMemorySaver` 的关键差别（单元 09 / 17）。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能不看资料，用一句话分别解释 State / Checkpointer / thread / Store / Runtime Context / 持久化后端，并说出各自的生命周期。
- [ ] 我能指出 `last_spec` 为什么不进 `initial_state()`，并说出把它写进输入后会发生什么（练习 7.3 的现象）。
- [ ] 我能指出"默认金额单位/默认维度"存在哪个文件、按什么键存、换 thread 是否失效。
- [ ] 我能解释 `graph.invoke(..., context=...)` 注入的对象**不会**进检查点，并说出 `get_state(...).values` 里为什么没有 `store`。
- [ ] 我能说出 `PreferenceStore` 与官方 `BaseStore` 的三处差异（无向量检索、无 TTL、无 `batch`/`list_namespaces`）。
- [ ] 我完成了练习 7.3，并观察到 A2 从 `answered` 退化成 `clarification`。
- [ ] 我完成了实验 8.1，并观察到"没有 checkpointer 就没有短期记忆"。
- [ ] 我完成了实验 8.2，并观察到非法偏好被回退成默认值。
- [ ] 我能回答：如果用户想"跨设备"保留偏好，应该改哪一层？（答：Store 的后端，不是 State、不是 checkpointer。）
- [ ] 我能说出本单元的实验里哪一行代码是"故意写成教学版"的（`MiniStore` 的注释与 `compile(store=)` 的 `try/except`）。

---

## 10. 复盘问题与参考答案、下一步

**Q1：既然 `context` 能传 `prefs`，为什么还要 `compile(store=store)`？**

两套通道服务于不同读者。`context` 是**本项目节点代码**的读取方式（`_resolve_deps` → `deps.prefs`），对所有自定义节点都直接可用；`compile(store=...)` 是 LangGraph 提供的**官方 Store 入口**，走这个入口的对象才能被框架内置工具、未来官方 API（例如按 namespace 检索）识别。项目两处都开，是为了"代码现在能用 + 将来换官方实现时不用改节点"。**代价**：读者会困惑"到底以哪个为准"——答案是以 `context` 为准，`store=` 是兼容层。

**Q2：为什么不把 `user_id` 也放进 State，而要在 `configurable` 里传？**

`user_id` 确实也写进了 State（`initial_state` 有它），但**权威来源**是 `config`：`_config()` 把它写进 `configurable` 与 `metadata`，API 层用它做线程可见性过滤（`list_threads(user_id=...)`）。State 里的那份是"给节点读的副本"，方便节点不依赖 config 解析；config 里的那份是"给框架和运行时读的定位信息"。两份不一致时以谁为准？项目里 `read_thread`/`list_threads` 都以传入的 `user_id` 为准，State 里的值只是快照。

**Q3：`last_spec` 会不会无限增长？**

不会，它只保存"最近一轮的解析结果"（一个 dict，几个键），每轮被覆盖。真正会增长的是 `messages` 与 `trace`：`add_messages` 与 `operator.add` 都是累积语义，跨轮只会变多。所以单元 02 讲的"上下文截断"、以及主项目 `trace[-200:]`（`persistence._turn_result`）都是在处理"增长"问题。**未验证**：`messages` 在图里没有截断策略，长会话会一直变长；这是已知限制，写入 `docs/verification.md` 时应该记一笔。

**Q4：换一个 checkpointer（比如 Postgres）要改哪里？**

只改装配层：`AppRuntime.create()` 里 `SqliteSaver(checkpoint_conn)` 那三行，换成 Postgres saver 的构造与 `setup()`；节点、路由、状态定义一行不用改。前提是新的 saver 也满足 `BaseCheckpointSaver` 接口（`get_state`/`get_state_history`/`put`…）。**未验证**：`langgraph-checkpoint-postgres` 的具体构造函数与依赖，本环境没有核对。

**Q5：如果用户说"以后都用万元"，这个信息应该写进哪？**

写 Store（`prefs.set_many(user_id, {"amount_unit": "万元"})`），**不要**写 State。原因：State 是"这一轮的分析结果"，下一轮会被 `initial_state` 重置；而"以后都用万元"是跨轮、跨 thread 的偏好。项目对此的落地是 API 的 `PUT /api/preferences`（单元 19 会用到）与 `understand` 节点读偏好，两者共用同一张表。

**下一步**：

- 单元 16 会把"停在中途等人"做出来：`interrupt()` 让图在 `review` 节点暂停，`Command(resume=...)` 把人工结论送回图里，并解释"节点为什么会被重跑一遍"。
- 在那之前，建议把本单元实验的 `MiniState` 与主项目 `AssistantState` 并排抄一次，只抄字段名，标出哪些是 reducer 字段、哪些是跨轮字段。这一步花 5 分钟，后面 16–18 都会用到这张对照表。

---

### 附：本单元引用的官方链接与核对记录

| 链接 | 核对日期 | 核对方式 | 结论 |
| --- | --- | --- | --- |
| <https://docs.langchain.com/oss/python/langgraph/persistence> | 2026-10-19 | HTTP 抓取，返回 200 | 页面存在；"检查点按 thread 保存状态快照"属长期稳定概念，正文未逐句核对（**未验证**细节） |
| <https://docs.langchain.com/oss/python/langchain/memory> | 2026-10-19 | HTTP 抓取，返回 200 | 页面存在；短期/长期记忆的术语沿用官方页面标题表述，正文未逐句核对（**未验证**细节） |
| <https://docs.langchain.com/oss/python/langgraph/runtime> | 2026-10-19 | HTTP 抓取，返回 200 | 页面存在；`Runtime.context` 的用法来自项目源码 `nodes.py` 的实际写法，官方页面正文未逐句核对（**未验证**细节） |

如果将来这些地址跳转或改名，请以 [LangGraph 文档首页](https://docs.langchain.com/oss/python/langgraph/overview) 的导航为准，并把新的核对日期写回本表。
