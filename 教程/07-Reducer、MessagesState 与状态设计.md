# 单元 07：Reducer、MessagesState 与状态设计

> 建议时长 55 分钟（**上半 25 分钟**：5.1～5.4 reducer 语义；**下半 30 分钟**：5.5～5.7 消息合并与状态设计清单） | 前置：单元 06 | 可跳过条件：能解释“默认覆盖 vs 追加”，并说出 `add_messages` 与 `operator.add` 的两点差异
>
> 版本基线：`langgraph 1.2.16`、`langchain-core 1.6.10`（核对日期 2026-10-19）
>
> 本单元完全确定性，不需要 API Key。所有输出为“预期（未在本环境执行）”。

---

## 1. 学习目标与前置

完成后你应该能：

1. 定义 reducer：**同一个状态字段被多次更新时，如何合并**。
2. 说出 LangGraph 的默认行为是“覆盖（last write wins）”，并用 `Annotated[list, operator.add]` 改成“追加”。
3. 用 `add_messages` 管理消息历史，并说出它与 `operator.add` 的两点差异（按 id 更新、支持删除）。
4. 用 `MessagesState` 作为最小状态，再对比项目的 `AssistantState` 多了什么。
5. 用一张检查清单判断“某字段该不该进 State、该用什么 reducer、该不该可序列化”。

前置：单元 06 完成，亲手看到过 `trace` 被覆盖的现象。

---

## 2. 官方文档与阅读问题

**官方文档：Graph API（https://docs.langchain.com/oss/python/langgraph/graph-api，核对日期 2026-10-19）**（reducer、`Annotated`、`MessagesState` 都在此页）

**官方文档：Persistence（https://docs.langchain.com/oss/python/langgraph/persistence，核对日期 2026-10-19）**（状态如何被保存，与 reducer 的关系）

带着这些问题读：

1. 官方怎么定义 reducer？默认 reducer 是什么行为？
2. 用 `Annotated[list, operator.add]` 声明追加，官方示例长什么样？
3. `MessagesState` 的定义包含哪些字段？`messages` 用的 reducer 是什么？
4. `add_messages` 除了追加，还做了什么？（提示：按 id 合并/替换、支持 RemoveMessage）
5. 官方有没有提醒“状态字段应该是可序列化的”？

> **官方事实**：状态字段可用 `Annotated[类型, reducer函数]` 声明合并方式；未声明时按“覆盖”处理。`MessagesState` 是预置的最小状态，其 `messages` 字段使用 `add_messages`。
>
> **未验证**：`RemoveMessage` 与“按 id 替换”的具体行为细节在不同版本间有细微差异；以你 `langgraph` 版本的官方示例与实际输出为准。

---

## 3. 通俗解释与精确技术定义

### 3.1 什么是 reducer

Reducer 就是**合并函数**：`(旧值, 新值) -> 合并后的值`。

| 字段 | 默认（无 reducer） | 声明 reducer |
| --- | --- | --- |
| `status: str` | 新值覆盖旧值 | 通常不需要 |
| `trace: Annotated[list[dict], operator.add]` | — | 旧列表 + 新列表 |
| `messages: Annotated[list[AnyMessage], add_messages]` | — | 追加 + 按 id 更新 + 支持删除 |

一句话：**默认覆盖，声明后按你给的规则合并。**

### 3.2 为什么默认是覆盖

因为大多数字段是“当前值”：`status`、`route`、`spec`、`final_report`。它们只应该有一个最新值，如果默认追加会变成列表，反而难用。

需要“累积”的只有少数：消息、轨迹、错误。所以 LangGraph 让“追加”成为**显式选择**，逼你想清楚。

### 3.3 `add_messages` 与 `operator.add` 的差异

| 场景 | `operator.add` | `add_messages` |
| --- | --- | --- |
| 追加新消息 | 追加 | 追加 |
| 同 id 消息再次出现 | 两条都在（重复） | **替换**旧的那条 |
| 删除某条 | 做不到 | `RemoveMessage(id=...)` |
| 输入是 dict/元组 | 原样进列表 | 转成消息对象 |

项目用 `add_messages`，因为对话里可能出现“重写上一条”（例如用户编辑问题、系统重新生成），按 id 替换比“追加两条”更符合语义。

> **官方事实**：`add_messages` 是 LangGraph 预置的消息 reducer；`MessagesState` 直接使用它。
>
> **教学简化**：真实实现还处理命名空间、消息 id 生成、序列化；初学只需记住上表三行。

### 3.4 `MessagesState` 是什么

```python
from langgraph.graph import MessagesState
# 等价于：
# class MessagesState(TypedDict):
#     messages: Annotated[list[AnyMessage], add_messages]
```

它只做一件事：让“最小 Agent 状态”开箱可用。项目不用它，因为项目需要 23 个字段；但项目里 `messages` 的声明与它完全一致。

### 3.5 状态设计的四个问题

每加一个字段，先回答：

1. **它是输入、中间结果、还是输出？** 输入（question）、中间（spec/data_result）、输出（final_report/export_path）都要显式。
2. **它会被多次写吗？** 会 → 需要 reducer（追加/合并）；不会 → 默认覆盖。
3. **它可序列化吗？** 连接、句柄、锁不能进 State；放 `AssistantContext`。
4. **它属于这个 thread 还是跨 thread？** 跨 thread 的偏好在 `PreferenceStore`，不进 State。

这四问就是本单元的“设计检查清单”。

---

## 4. 这个知识在主项目中的位置

| 项目位置 | 设计 | 理由 |
| --- | --- | --- |
| `AssistantState.messages` | `Annotated[list[AnyMessage], add_messages]` | 多轮对话 + 报告作为 `AIMessage` 追加 |
| `AssistantState.trace` | `Annotated[list[dict], operator.add]` | 每个节点都追加一条审计记录，不能被覆盖 |
| `AssistantState.errors` | `Annotated[list[str], operator.add]` | 错误可能来自多处，要累积 |
| `AssistantState.spec` / `route` / `status` | 普通字段（覆盖） | 只关心最新值 |
| `AssistantState.budget` | 普通 `dict`（覆盖） | 每次更新整个预算快照，读起来简单 |
| `PreferenceStore` | 不在 State 里 | 跨 thread、可查询、有命名空间 |
| `AssistantContext` | 不在 State 里 | 连接/设置/LLM 客户端是运行期依赖，不可序列化 |

**项目里最容易被忽略的一点**：`trace` 用 `operator.add` 后，节点必须返回**只含本条**的列表（`[entry]`），而不是把 `state["trace"]` 整个带回来。否则会指数级重复（每节点把历史再加一遍）。

---

## 5. 文件、完整代码、配置与命令

### 5.1 最小对比：默认覆盖 vs 追加

```python
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    overwrite: list[str]
    appended: Annotated[list[str], operator.add]


def node_a(state: State) -> dict:
    return {"overwrite": ["a"], "appended": ["a"]}


def node_b(state: State) -> dict:
    return {"overwrite": ["b"], "appended": ["b"]}


builder = StateGraph(State)
builder.add_node("a", node_a)
builder.add_node("b", node_b)
builder.add_edge(START, "a")
builder.add_edge("a", "b")
builder.add_edge("b", END)
graph = builder.compile()

print(graph.invoke({"overwrite": [], "appended": []}))
```

预期（未在本环境执行）：

```text
{'overwrite': ['b'], 'appended': ['a', 'b']}
```

这一行输出就是本单元的全部核心：**没声明 reducer 的字段只剩最后一次写入；声明了 `operator.add` 的字段累积。**

### 5.2 `add_messages`：按 id 替换与删除

```python
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class ChatState(TypedDict, total=False):
    messages: Annotated[list, add_messages]


def node_user(state: ChatState) -> dict:
    return {"messages": [HumanMessage(content="2026 年九月销售额比八月变化多少？", id="m1")]}


def node_fix(state: ChatState) -> dict:
    # 同一个 id：替换而不是追加
    return {"messages": [HumanMessage(content="2026 年九月净销售额比八月变化多少？", id="m1")]}


def node_answer(state: ChatState) -> dict:
    return {"messages": [AIMessage(content="差额 96500 分，变化率 8.11%。", id="m2")]}


builder = StateGraph(ChatState)
builder.add_node("user", node_user)
builder.add_node("fix", node_fix)
builder.add_node("answer", node_answer)
builder.add_edge(START, "user")
builder.add_edge("user", "fix")
builder.add_edge("fix", "answer")
builder.add_edge("answer", END)
graph = builder.compile()

result = graph.invoke({"messages": []})
for message in result["messages"]:
    print(type(message).__name__, message.id, message.content)
print("消息条数：", len(result["messages"]))

# 删除：RemoveMessage 也交给 add_messages 处理
delete_builder = StateGraph(ChatState)
delete_builder.add_node("remove", lambda state: {"messages": [RemoveMessage(id="m2")]})
delete_builder.add_edge(START, "remove")
delete_builder.add_edge("remove", END)
delete_graph = delete_builder.compile()

after = delete_graph.invoke({"messages": result["messages"]})
print("删除 m2 后：")
for message in after["messages"]:
    print(type(message).__name__, message.id, message.content)
print("删除后消息条数：", len(after["messages"]))
```

预期（未在本环境执行）：

```text
HumanMessage m1 2026 年九月净销售额比八月变化多少？
AIMessage m2 差额 96500 分，变化率 8.11%。
消息条数： 2
删除 m2 后：
HumanMessage m1 2026 年九月净销售额比八月变化多少？
删除后消息条数： 1
```

注意：`node_user` 与 `node_fix` 用了同一个 `m1`，所以最终只剩 `fix` 写的那条。**如果用 `operator.add`，这里会有 3 条且两条重复。**

删除示例（可选验证）：

```python
def node_drop(state: ChatState) -> dict:
    return {"messages": [RemoveMessage(id="m2")]}
```

### 5.3 实验：修掉单元 06 的 `trace` 覆盖（一步步搭出来）

单元 06 结尾留下的问题——三个节点只留最后一条 trace——本实验用**一行注解**修掉它，再用两组对照实验把"覆盖 vs 合并"的机制彻底看清。分 6 步搭出，最终与仓库 `教程/实验/07_reducers.py` 完全一致；全程无密钥。

#### 步骤 1：状态加注解——一行修掉覆盖 bug

**这一步解决什么**：单元 06 的 `trace` 之所以被覆盖，是因为**默认 reducer 是"整个键覆盖"**。修法不是改节点函数，而是**在状态定义上声明合并方式**：`Annotated[类型, reducer函数]`。

新建 `教程/实验/07_reducers.py`：

```python
"""单元 07 实验（步骤 1）：给状态声明合并方式。"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

SAMPLE_MONTHS = {
    "2026-09": {"net_cents": 1286500},
    "2026-08": {"net_cents": 1190000},
}


class OpsState(TypedDict, total=False):
    """对照状态：trace 用 operator.add，messages 用 add_messages，status 默认覆盖。"""

    question: str
    spec: dict
    calculation: dict
    status: str
    trace: Annotated[list[dict], operator.add]
    messages: Annotated[list, add_messages]
```

**逐行讲解**：

- `trace: Annotated[list[dict], operator.add]`：`Annotated[类型, 元数据]` 本是类型标注语法，LangGraph 拿第二个位置当 **reducer**——节点返回 `[新条目]` 时，框架执行 `旧值 + 新值`（列表拼接）而不是覆盖。**与单元 06 的差异就这一行**。
- `messages: Annotated[list, add_messages]`：消息专用 reducer——比 `operator.add` 聪明（按 id 去重/替换/删除），步骤 4、5 专门实验它。
- `status` 故意**不加注解**：阶段标记就该"后者覆盖前者"（running → answered）——三种语义并存一个状态里，各自声明。
- `operator` 与 `Annotated` 是标准库；`add_messages` 来自 langgraph。

**运行与观察**（临时验证——注解确实被框架读到）：

```python
if __name__ == "__main__":
    print(OpsState.__annotations__["trace"])
```

```text
(list[dict], <built-in function add>)
```

注解只是"写在类型上的元数据"，**是 StateGraph 编译时读它、执行时用它的**。

#### 步骤 2：两节点 + 图——验证修复

**这一步解决什么**：节点函数**照旧返回一整条新 trace 列表**（不用改！），跑一遍看它是否被**追加**而不是覆盖。

```python
def node_parse(state: OpsState) -> dict:
    spec = {"current_month": "2026-09", "previous_month": "2026-08", "metric": "net_sales"}
    return {
        "spec": spec,
        "status": "running",
        "trace": [{"node": "parse", "action": "parsed_spec"}],
        "messages": [HumanMessage(content=state.get("question", ""), id="q1")],
    }


def node_compute(state: OpsState) -> dict:
    current = SAMPLE_MONTHS["2026-09"]["net_cents"]
    previous = SAMPLE_MONTHS["2026-08"]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    return {
        "calculation": {
            "current_cents": current,
            "previous_cents": previous,
            "delta_cents": delta,
            "pct_change": pct,
        },
        "status": "answered",
        "trace": [{"node": "compute", "action": "calculated", "delta_cents": delta}],
        "messages": [AIMessage(content=f"差额 {delta} 分，变化率 {pct:.2%}。", id="a1")],
    }


from langgraph.graph import END, START, StateGraph


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("parse", node_parse)
    builder.add_node("compute", node_compute)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", "compute")
    builder.add_edge("compute", END)
    return builder.compile()
```

**逐行讲解**：

- 两个节点的 trace 仍然各是 `[单条]` 新列表——**reducer 的意义正是让节点不用关心合并**，只管报告自己这一段。
- 消息都带 `id="q1"`/`id="a1"`：`add_messages` 的魔法靠 id 起作用，先养成"每条消息都有 id"的习惯。

**运行与观察**（临时验证）：

```python
    final = build_graph().invoke(
        {"question": "九月比八月变化多少？", "status": "running", "trace": [], "messages": []}
    )
    print([item["node"] for item in final["trace"]])
    print(final["status"])
    print([type(m).__name__ for m in final["messages"]])
```

```text
['parse', 'compute']
answered
['HumanMessage', 'AIMessage']
```

**单元 06 的 bug 修好了**：trace 两条都在；status 正常覆盖成 answered；messages 也两条齐全。

#### 步骤 3：对照组——同一个键，覆盖 vs 追加

**这一步解决什么**："修好了"还不够，要**隔离变量**看清机制：同一个图、同两个节点，一个键不加注解、一个键加 `operator.add`，结果并排看。

```python
import json


def demo_operator_add_vs_plain() -> None:
    print("\n== 第三段：operator.add vs 默认覆盖 ==")

    class PlainState(TypedDict, total=False):
        plain: list[str]
        added: Annotated[list[str], operator.add]

    def node_a(state: PlainState) -> dict:
        return {"plain": ["a"], "added": ["a"]}

    def node_b(state: PlainState) -> dict:
        return {"plain": ["b"], "added": ["b"]}

    builder = StateGraph(PlainState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    result = builder.compile().invoke({"plain": [], "added": []})
    print(json.dumps(result, ensure_ascii=False))
```

**逐行讲解**：

- `PlainState` 里 `plain` 与 `added` **唯一区别是一个 Annotated**——教科书式控制变量。
- 状态类可以**在函数里就地定义**（demo 专用小图不必升到模块级）。

**运行与观察**（临时验证 `demo_operator_add_vs_plain()`）：

```text
== 第三段：operator.add vs 默认覆盖 ==
{"plain": ["b"], "added": ["a", "b"]}
```

`plain` 只剩 `["b"]`（b 覆盖 a），`added` 是 `["a", "b"]`（追加）——一句话讲清全部机制。

#### 步骤 4：`add_messages` 魔法之一——同 id 替换

**这一步解决什么**：`operator.add` 只会无脑拼接；**消息场景需要"改写历史"**——用户编辑了刚才的提问，正确语义是**替换**那条旧消息，而不是追加一条新的。`add_messages` 按 id 实现。

```python
from langchain_core.messages import RemoveMessage


def demo_messages() -> None:
    print("\n== 第二段：add_messages 的“同 id 替换” ==")

    class ChatState(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    def node_first(state: ChatState) -> dict:
        return {"messages": [HumanMessage(content="原始问题", id="m1")]}

    def node_edit(state: ChatState) -> dict:
        return {"messages": [HumanMessage(content="编辑后的问题", id="m1")]}

    builder = StateGraph(ChatState)
    builder.add_node("first", node_first)
    builder.add_node("edit", node_edit)
    builder.add_edge(START, "first")
    builder.add_edge("first", "edit")
    builder.add_edge("edit", END)
    result = builder.compile().invoke({"messages": []})
    print("同 id 写入后条数：", len(result["messages"]))
    print("内容：", [message.content for message in result["messages"]])
```

**逐行讲解**：

- 两个节点写**同一个 id="m1"** 的 HumanMessage、内容不同——`add_messages` 的裁决是**保留一条、内容取后写**。
- 这就是项目里"编辑仅正文末尾"那条契约的底层机制：编辑 = 同 id 再写一次。

**运行与观察**（临时验证，在 `demo_messages` 末尾先 return）：

```text
== 第二段：add_messages 的“同 id 替换” ==
同 id 写入后条数： 1
内容： ['编辑后的问题']
```

一条，不是两条——替换发生了。

#### 步骤 5：`add_messages` 魔法之二——RemoveMessage 删除

**这一步解决什么**：上下文窗口是稀缺资源，**修剪历史**（删旧消息）是长会话的日常。`RemoveMessage(id=...)` 是 `add_messages` 约定的"删除指令"。

在 `demo_messages()` 里**接续**补上删除实验（替换实验之后）：

```python
    def node_drop(state: ChatState) -> dict:
        return {"messages": [RemoveMessage(id="m1")]}

    builder2 = StateGraph(ChatState)
    builder2.add_node("first", node_first)
    builder2.add_node("drop", node_drop)
    builder2.add_edge(START, "first")
    builder2.add_edge("first", "drop")
    builder2.add_edge("drop", END)
    dropped = builder2.compile().invoke({"messages": [HumanMessage(content="历史消息", id="old")]})
    print("RemoveMessage 之后条数：", len(dropped["messages"]))
    print("内容：", [message.content for message in dropped["messages"]])
```

**逐行讲解**：

- `RemoveMessage(id="m1")`：不是一条新消息，而是**发给 reducer 的指令**——把 id 为 m1 的消息从列表里拿掉。
- builder2 的入口**预置了一条 id="old" 的历史消息**，first 又写入 m1，drop 只删 m1——验证删除是**按 id 精确命中**，不是清空。

**运行与观察**（完整跑 `demo_messages()`）：

```text
== 第二段：add_messages 的“同 id 替换” ==
同 id 写入后条数： 1
内容： ['编辑后的问题']
RemoveMessage 之后条数： 1
内容： ['历史消息']
```

id="old" 的历史消息**幸存**，m1 被精准移除。

#### 步骤 6：整合——`main()` 三段汇报

**这一步解决什么**：把三个实验按"修复验证 → 替换/删除 → 覆盖对照"的顺序串起来，末尾一句话总结三条规律。

```python
import sys


def main() -> int:
    print("== 单元 07 实验：reducer 与消息状态 ==")
    graph = build_graph()
    final = graph.invoke(
        {
            "question": "2026 年九月净销售额比八月变化多少？",
            "status": "running",
            "trace": [],
            "messages": [],
        }
    )

    print("\n== 第一段：trace 用 operator.add 后不再被覆盖 ==")
    print("trace 条数：", len(final["trace"]))
    for entry in final["trace"]:
        print("  ", json.dumps(entry, ensure_ascii=False))
    print("status（默认覆盖）：", final["status"])
    print("messages 条数：", len(final["messages"]))
    for message in final["messages"]:
        print(f"  {type(message).__name__}(id={message.id}) {message.content}")

    demo_messages()
    demo_operator_add_vs_plain()
    print("\n[结论] 默认覆盖，Annotated 声明合并；消息用 add_messages，轨迹用 operator.add。")
    return 0
```

清理临时验证，完整文件应与仓库完全一致：

```python
"""单元 07 实验：默认覆盖 vs 追加 reducer vs add_messages。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/07_reducers.py
不需要模型；数字来自样例数据（与项目口径一致，单位分）。
"""

from __future__ import annotations

import json
import operator
import sys
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

SAMPLE_MONTHS = {
    "2026-09": {"net_cents": 1286500},
    "2026-08": {"net_cents": 1190000},
}


class OpsState(TypedDict, total=False):
    """对照状态：trace 用 operator.add，messages 用 add_messages，status 默认覆盖。"""

    question: str
    spec: dict
    calculation: dict
    status: str
    trace: Annotated[list[dict], operator.add]
    messages: Annotated[list, add_messages]


def node_parse(state: OpsState) -> dict:
    spec = {"current_month": "2026-09", "previous_month": "2026-08", "metric": "net_sales"}
    return {
        "spec": spec,
        "status": "running",
        "trace": [{"node": "parse", "action": "parsed_spec"}],
        "messages": [HumanMessage(content=state.get("question", ""), id="q1")],
    }


def node_compute(state: OpsState) -> dict:
    current = SAMPLE_MONTHS["2026-09"]["net_cents"]
    previous = SAMPLE_MONTHS["2026-08"]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    return {
        "calculation": {
            "current_cents": current,
            "previous_cents": previous,
            "delta_cents": delta,
            "pct_change": pct,
        },
        "status": "answered",
        "trace": [{"node": "compute", "action": "calculated", "delta_cents": delta}],
        "messages": [AIMessage(content=f"差额 {delta} 分，变化率 {pct:.2%}。", id="a1")],
    }


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("parse", node_parse)
    builder.add_node("compute", node_compute)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", "compute")
    builder.add_edge("compute", END)
    return builder.compile()


def demo_messages() -> None:
    print("\n== 第二段：add_messages 的“同 id 替换” ==")

    class ChatState(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    def node_first(state: ChatState) -> dict:
        return {"messages": [HumanMessage(content="原始问题", id="m1")]}

    def node_edit(state: ChatState) -> dict:
        return {"messages": [HumanMessage(content="编辑后的问题", id="m1")]}

    def node_drop(state: ChatState) -> dict:
        return {"messages": [RemoveMessage(id="m1")]}

    builder = StateGraph(ChatState)
    builder.add_node("first", node_first)
    builder.add_node("edit", node_edit)
    builder.add_edge(START, "first")
    builder.add_edge("first", "edit")
    builder.add_edge("edit", END)
    result = builder.compile().invoke({"messages": []})
    print("同 id 写入后条数：", len(result["messages"]))
    print("内容：", [message.content for message in result["messages"]])

    builder2 = StateGraph(ChatState)
    builder2.add_node("first", node_first)
    builder2.add_node("drop", node_drop)
    builder2.add_edge(START, "first")
    builder2.add_edge("first", "drop")
    builder2.add_edge("drop", END)
    dropped = builder2.compile().invoke({"messages": [HumanMessage(content="历史消息", id="old")]})
    print("RemoveMessage 之后条数：", len(dropped["messages"]))
    print("内容：", [message.content for message in dropped["messages"]])


def demo_operator_add_vs_plain() -> None:
    print("\n== 第三段：operator.add vs 默认覆盖 ==")

    class PlainState(TypedDict, total=False):
        plain: list[str]
        added: Annotated[list[str], operator.add]

    def node_a(state: PlainState) -> dict:
        return {"plain": ["a"], "added": ["a"]}

    def node_b(state: PlainState) -> dict:
        return {"plain": ["b"], "added": ["b"]}

    builder = StateGraph(PlainState)
    builder.add_node("a", node_a)
    builder.add_node("b", node_b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    result = builder.compile().invoke({"plain": [], "added": []})
    print(json.dumps(result, ensure_ascii=False))


def main() -> int:
    print("== 单元 07 实验：reducer 与消息状态 ==")
    graph = build_graph()
    final = graph.invoke(
        {
            "question": "2026 年九月净销售额比八月变化多少？",
            "status": "running",
            "trace": [],
            "messages": [],
        }
    )

    print("\n== 第一段：trace 用 operator.add 后不再被覆盖 ==")
    print("trace 条数：", len(final["trace"]))
    for entry in final["trace"]:
        print("  ", json.dumps(entry, ensure_ascii=False))
    print("status（默认覆盖）：", final["status"])
    print("messages 条数：", len(final["messages"]))
    for message in final["messages"]:
        print(f"  {type(message).__name__}(id={message.id}) {message.content}")

    demo_messages()
    demo_operator_add_vs_plain()
    print("\n[结论] 默认覆盖，Annotated 声明合并；消息用 add_messages，轨迹用 operator.add。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**回看地图**：

| 步骤 | 最终脚本里的位置 |
| --- | --- |
| 1 注解 | `SAMPLE_MONTHS`、`OpsState`（trace/messages 两行 Annotated） |
| 2 修复验证 | `node_parse()`、`node_compute()`、`build_graph()` |
| 3 对照 | `demo_operator_add_vs_plain()`（plain vs added） |
| 4 替换 | `demo_messages()` 上半（同 id 替换） |
| 5 删除 | `demo_messages()` 下半（RemoveMessage） |
| 6 整合 | `main()` 三段汇报、docstring、`sys.exit(main())` |

运行与验收：

```powershell
cd ops-assistant
uv run python ../教程/实验/07_reducers.py
```

退出码 0。对照单元 06 的输出：trace 从 `['compute']` 变成 `['parse', 'compute']`——**一行注解的差别**。

### 5.4 逐段解释

| 段 | 演示 | 关键观察 |
| --- | --- | --- |
| 第一段 | `trace` 追加 + `status` 覆盖 | trace 2 条，status 只有最后值 `answered` |
| 第二段 | `add_messages` 同 id 替换 / `RemoveMessage` 删除 | 条数不变、内容被替换；删除后少一条 |
| 第三段 | `operator.add` vs 普通 list | `plain` 只剩 `['b']`，`added` 是 `['a','b']` |

### 5.5 与项目状态字段的逐条对照

| 字段 | 类型 | reducer | 为什么 |
| --- | --- | --- | --- |
| `messages` | `Annotated[list[AnyMessage], add_messages]` | 追加 + 按 id 更新 | 多轮对话、报告追加、可编辑 |
| `trace` | `Annotated[list[dict], operator.add]` | 追加 | 每节点一条审计，不能丢 |
| `errors` | `Annotated[list[str], operator.add]` | 追加 | 多处错误累积 |
| `sources` | `list[dict]` | 覆盖 | 每轮由 `data`/`knowledge` 重新产出 |
| `tables` / `chart` | 覆盖 | 覆盖 | 每轮重算，旧值无意义 |
| `budget` | 覆盖 | 覆盖 | 每步更新整个快照 |
| `spec` | 覆盖 | 覆盖 | 只关心本轮解析结果 |
| `last_spec` | 覆盖 | 覆盖 | 本轮结束时写回，供下一轮追问 |

> **注意**：`sources` 用覆盖而不是追加，是因为它的语义是“本轮报告引用到的来源”；如果追加，上一轮的来源会污染本轮的引用校验。**项目设计选择**：能覆盖的都用覆盖，只有真正“事件流”才追加。

### 5.6 状态设计检查清单（可直接贴到 PR 描述里）

1. 字段名与项目术语一致（`spec` 不是 `question_spec`，`data_result` 不是 `query_result`）。
2. 每个字段写一行注释说明“谁会写、谁会读”。
3. 会被多次写的字段声明 reducer；其余用覆盖。
4. 需要累积的，节点返回**只含本条**的列表。
5. 字段值必须可 JSON 序列化（测试里跑一次 `json.dumps(state, default=str)` 兜底）。
6. 不可序列化的依赖放 `Context`，不放 State。
7. 跨 thread 的数据（偏好）放 `PreferenceStore`，不放 State。
8. 新增字段必须同时更新 `initial_state()` 与单测的键集合断言。

---

## 6. 预期输出与关键状态变化

### 6.1 实验输出（2026-10-19 实测运行通过，退出码 0）

```text
== 单元 07 实验：reducer 与消息状态 ==

== 第一段：trace 用 operator.add 后不再被覆盖 ==
trace 条数： 2
   {"node": "parse", "action": "parsed_spec"}
   {"node": "compute", "action": "calculated", "delta_cents": 96500}
status（默认覆盖）： answered
messages 条数： 2
  HumanMessage(id=q1) 2026 年九月净销售额比八月变化多少？
  AIMessage(id=a1) 差额 96500 分，变化率 8.11%。

== 第二段：add_messages 的“同 id 替换” ==
同 id 写入后条数： 1
内容： ['编辑后的问题']
RemoveMessage 之后条数： 1
内容： ['历史消息']

== 第三段：operator.add vs 默认覆盖 ==
{"plain": ["b"], "added": ["a", "b"]}

[结论] 默认覆盖，Annotated 声明合并；消息用 add_messages，轨迹用 operator.add。
```

### 6.2 第二段的“1 条”要看懂

- 第一次写入 `HumanMessage(id="m1")` → 1 条。
- 第二次写入同 id → **替换**，仍是 1 条，内容是“编辑后的问题”。
- 删除演示里，初始状态带 `HumanMessage(id="old")`，`RemoveMessage(id="m1")` 只删 `m1`（不存在），所以**仍是 1 条**：这就是预期输出里 `['历史消息']` 的来源。

如果你把 `RemoveMessage(id="m1")` 改成 `RemoveMessage(id="old")`，预期就变成 0 条——请自己验证一次，确认你真的理解了。

### 6.3 关键状态变化

| 对象 | 之前 | 之后 |
| --- | --- | --- |
| 对“状态字段”的认识 | 所有字段一视同仁 | 分清“当前值”与“事件流” |
| `trace` | 被覆盖，只剩最后一条 | 用 `operator.add` 累积 |
| 消息 | 手拼列表、可能重复 | `add_messages` 按 id 更新、可删除 |
| 节点写法 | 返回整个列表 | 返回 `[本条]` |
| 状态设计 | 想到就加字段 | 按四问 + 检查清单过滤 |

---

## 7. 修改练习（至少 2 个，含一个“改坏它”）

### 练习 7.1（简单）：给 `errors` 加追加 reducer

给 `OpsState` 加 `errors: Annotated[list[str], operator.add]`，在 `node_parse` 与 `node_compute` 各返回一条错误（例如 `["parse: demo warning"]`）。

验收：`final["errors"]` 有 2 条，且顺序是 parse 在前。

### 练习 7.2（中等）：实现“节点只返回本条 trace”并断言

把 `node_parse` 改成：

```python
return {"trace": [{"node": "parse", "action": "parsed_spec"}]}
```

再写一个断言函数：

```python
def assert_trace_not_duplicated(final: dict) -> None:
    actions = [entry["action"] for entry in final["trace"]]
    assert len(actions) == len(set(actions)), f"trace 出现重复：{actions}"
```

验收：断言通过；再把某个节点改成返回 `state["trace"] + [新条目]`，断言应当失败（亲手看到重复）。

### 练习 7.3（改坏它）：把 `add_messages` 换成 `operator.add`

```python
messages: Annotated[list, operator.add]
```

重跑第二段（同 id 替换）。

请回答：

1. 条数变成几？内容是什么？
2. 为什么“同一个 id 出现两条”在真实对话里是灾难？（提示：UI 会显示两条；模型会看到重复上下文；按 id 更新/撤销失效）
3. 如果把 `RemoveMessage` 也一起用 `operator.add`，会发生什么？（提示：`RemoveMessage` 会被当成一条普通消息塞进列表）

### 练习 7.4（进阶）：给 `sources` 做“按 type 合并”

项目里 `sources` 用覆盖。假设你想改成“追加并按 `(type, sql, params)` 去重”，写一个自定义 reducer `merge_sources(old, new) -> list[dict]`，并用 `Annotated[list[dict], merge_sources]` 声明。

验收：同一来源追加两次后列表长度不变；不同来源追加后长度 +1。

> 提示：reducer 必须是纯函数，不能改传入的列表（先 `list(old)` 再操作）。

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：节点返回整条 trace（重复累积）

把 `node_parse` 的返回改成 `{"trace": state.get("trace", []) + [{"node": "parse"}]}`。

预期（未在本环境执行）：3 个节点后 trace 条数不是 3，而是更多（每个节点都把前面历史带上了）。

**要理解的点**：`operator.add` 的语义是“旧 + 新”，你返回的内容会被再加一次旧值。**节点只返回增量**。

### 实验 8.2：reducer 改传入的列表

```python
def merge_sources_bad(old, new):
    old.extend(new)   # 就地修改
    return old
```

预期（未在本环境执行）：可能出现“同一来源重复出现”或历史状态被污染（取决于是否共享引用）。

**要理解的点**：reducer 必须是纯函数。项目里所有 reducer 都写成“返回新列表”。

### 实验 8.3：给字符串字段声明 `operator.add`

```python
answer: Annotated[str, operator.add]
```

两个节点各返回 `"A"`、`"B"`。

预期（未在本环境执行）：结果是 `"AB"`（字符串相加）。

**要理解的点**：`operator.add` 不止用于列表。但字符串拼接在报告场景几乎总是错（你要的是“覆盖成本轮答案”），所以别滥用。

### 实验 8.4：消息 id 撞车

两个不同节点都返回 `id="x"` 的消息。

预期（未在本环境执行）：后写的替换先写的，最终只有一条。

**要理解的点**：id 是消息的身份。项目里 `HumanMessage` 用调用方传入的 id 或自动生成；`AIMessage` 用报告轮次 id，避免与用户消息撞车。

### 实验 8.5：State 里放不可序列化对象

```python
conn: sqlite3.Connection
```

接入 `InMemorySaver` 后跑两轮。

预期（未在本环境执行）：第一轮可能正常，写 checkpoint 时失败（序列化错误）。

**要理解的点**：reducer 解决“怎么合并”，不解决“能不能保存”。可序列化是独立约束（单元 09）。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能一句话定义 reducer，并说出默认行为是覆盖。
- [ ] 我运行了 `07_reducers.py`，看到 `trace` 2 条、`status` 只有最后值、`plain=['b']` / `added=['a','b']`。
- [ ] 我能说出 `add_messages` 与 `operator.add` 的两点差异（按 id 替换、支持删除）。
- [ ] 我完成了练习 7.2，并见过“返回整条 trace 导致重复”的现象。
- [ ] 我能背出状态设计检查清单里至少 5 条。
- [ ] 我能解释项目为什么 `sources` 用覆盖、`trace`/`errors` 用追加。
- [ ] 我知道 `MessagesState` 只有一个 `messages` 字段，且项目 `AssistantState` 与它兼容。
- [ ] 我知道不可序列化的依赖要放 `Context`，不能进 `State`。

---

## 10. 复盘问题与参考答案、下一步

### Q1：为什么默认 reducer 是覆盖，而不是追加？

**参考**：因为大多数状态字段是“当前值”语义（`status`、`route`、`spec`）。如果默认追加，这些字段会变成列表，每次读取都要取 `[-1]`，且旧的错误值会一直留在状态里。把“追加”设为显式选择，是一次**刻意的设计摩擦**：逼你为每个字段想清楚语义。

### Q2：`trace` 用 `operator.add`，节点返回什么？

**参考**：只返回**本次新增的那一条**（列表里一个元素）。框架会执行 `旧 trace + [新条目]`。如果返回整条历史，会重复累积，越跑越长。

### Q3：为什么不把偏好（金额单位、默认维度）放进 State？

**参考**：偏好的生命周期是“跨 thread、跨会话”，且要能查询/覆盖；State 的生命周期是“一个 thread 的一次运行”。放一起会导致：换 thread 偏好丢失、checkpoint 里塞满非必要数据、无法做“查询所有用户偏好”。项目用 `PreferenceStore` 单独存，State 里只存本轮用到的偏好快照（如有需要）。

### Q4：`messages` 会不会无限增长？

**参考**：会。所以项目里有两道限制：上下文窗口策略（单元 02）+ checkpoint 只保留必要历史。真实生产常见做法是“滑动窗口 + 摘要”，并在报告生成后把中间过程消息移出（`RemoveMessage`）。**未验证**：`create_agent` 是否自动裁剪历史，以你的版本行为为准。

### Q5：如果两个 reducer 冲突（例如 `errors` 既追加又去重）怎么办？

**参考**：自己写一个自定义 reducer（纯函数），明确语义：`merge_errors(old, new)` 去重且保序。reducer 是普通函数，可单测。项目里 `errors` 允许重复（不同节点报同一错误反而说明问题多处），所以用简单的 `operator.add`。

### 下一步

进入 **单元 08：条件路由、循环、预算与失败退出**。你会：

1. 写**纯函数路由**：`route_after_understand(state) -> str`，并单测它。
2. 用 `add_conditional_edges` 实现 `understand` 的四条分支（clarify / knowledge / data / fail）。
3. 用 `budget` + `recursion_limit` 给循环上刹车，并实现 `fail` 节点。
4. 把项目的边表与代码一一对上（单元 08 末尾有一张对照表）。

进入前请确认：`uv run python ../教程/实验/07_reducers.py` 能跑完，且你能解释输出里“同 id 写入后条数：1”的原因。
