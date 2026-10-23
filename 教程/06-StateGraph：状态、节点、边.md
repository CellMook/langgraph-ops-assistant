# 单元 06：StateGraph：状态、节点、边

> 建议时长 60 分钟（**上半 25 分钟**：5.1～5.4 概念与第一个节点；**下半 35 分钟**：5.5～5.7 三节点直线图 + 与项目图对齐） | 前置：单元 05 | 可跳过条件：能不看资料写出“定义 State → 加节点 → 连边 → compile → invoke”，并说清节点返回的是“部分更新”
>
> 版本基线：`langgraph 1.2.16`（核对日期 2026-10-19）
>
> 本单元完全确定性，**不需要 API Key**。所有输出为“预期（未在本环境执行）”。

---

## 1. 学习目标与前置

完成后你应该能：

1. 用一句话定义 `StateGraph`：一张“节点读写共享状态”的有向图，编译后可以像函数一样调用。
2. 用 `TypedDict` 声明 State schema，并解释每个字段为什么是“通道（channel）”。
3. 写节点函数：**接收完整状态，只返回自己改动的字段**（部分更新 / partial update）。
4. 用 `START` / `END` 连一条直线图，`compile()` 后 `invoke()`，并用 `stream()` 看到每一步的更新。
5. 把项目的 10 节点图与本单元的三节点图对上：哪些是固定边、哪些是条件边（单元 08）。

前置：单元 05 完成，知道 Agent 内部就是“模型节点 + 工具节点 + 循环边”。

---

## 2. 官方文档与阅读问题

**官方文档：Graph API（https://docs.langchain.com/oss/python/langgraph/graph-api，核对日期 2026-10-19）**

**官方文档：LangGraph 概览（https://docs.langchain.com/oss/python/langgraph/overview，核对日期 2026-10-19）**

带着这些问题读：

1. 官方定义 State 的推荐方式是什么？（`TypedDict` / dataclass / Pydantic）
2. 节点函数的签名是什么？返回值是什么类型？
3. `add_node`、`add_edge`、`set_entry_point`（或 `START`）分别做什么？
4. `compile()` 返回什么？为什么必须先编译？
5. 官方有没有说“节点返回的 dict 会**合并**进状态”？默认合并规则是什么？

> **官方事实**：LangGraph 的 `StateGraph` 以状态（state）为中心：你声明状态的字段结构，节点读取状态并返回更新，框架负责把更新合并进状态并决定下一个节点。`START` 与 `END` 是表示图入口与出口的特殊节点。默认情况下每个状态字段的更新方式是“覆盖（overwrite）”；要改成“追加/合并”需要声明 reducer（单元 07）。

---

## 3. 通俗解释与精确技术定义

### 3.1 为什么需要图

`create_agent` 的循环是“模型自己决定下一步”。但财务报告流程不是这样：**步骤是确定的**——先解析问题，再查数据，再计算，再校验，再渲染，再审批，再导出。

把流程写成图的好处：

- 每一步是一个**普通函数**，可以单测；
- 步骤之间的数据传递显式（都在 State 里）；
- 哪里出错、走到哪一步，状态里有记录；
- 想加“审批”“重试”“失败出口”，只是加节点和边。

### 3.2 四个概念

| 概念 | 定义 | 本项目例子 |
| --- | --- | --- |
| 状态（State） | 一张共享的、带字段声明的数据结构 | `AssistantState` |
| 节点（Node） | 一个函数：`state -> dict`（只含要改的字段） | `understand`、`data`、`compute` |
| 边（Edge） | 节点之间的连接；固定边是“总是去下一个” | `data → compute` |
| 编译（Compile） | 把“声明”变成可执行对象，检查图是否合法 | `graph.compile()` |

### 3.3 节点返回的是“补丁”，不是“新状态”

这是初学者最容易写错的地方。节点**不应该**返回完整状态：

```python
# 错：返回完整状态（会把别的节点刚写的字段覆盖回旧值）
def compute_bad(state):
    return {**state, "calculation": {"delta_cents": 96500}}


# 对：只返回自己改的字段
def compute(state):
    return {"calculation": {"delta_cents": 96500}}
```

框架会把返回的 dict **按键合并**进当前状态。你没返回的键，保持原值。

> **官方事实**：节点可以返回部分更新（partial update）；默认 reducer 是“新值覆盖旧值”。
>
> **教学简化**：真实实现里状态是“通道（channel）”的集合，每个通道有自己的 reducer 与版本；初学只需记住“按字段合并，默认覆盖”。

### 3.4 编译做了什么

`compile()` 会检查：

- 有没有入口（`START` 能到达某个节点）；
- 节点名是否都存在；
- 边是否指向已声明的节点；
- 图能否到达 `END`。

编译后得到 `CompiledStateGraph`（可 `invoke` / `stream` / `ainvoke`）。**没有编译的图不能执行**——因为此时它只是一份声明。

### 3.5 `invoke` 与 `stream`

- `invoke(state)`：跑完整条图，返回**最终完整状态**。
- `stream(state, stream_mode="updates")`：每跑完一个节点就产出一条 `{节点名: 该节点的返回值}`，适合调试与前端实时展示。

项目里 UI（单元 19）会用 stream 显示“正在查数据…正在计算…”，靠的就是这个机制。

---

## 4. 这个知识在主项目中的位置

| 项目位置 | 内容 |
| --- | --- |
| `src/ops_assistant/workflow/state.py` | `AssistantState(TypedDict, total=False)`：23 个字段（messages / question / thread_id / user_id / spec / last_spec / route / knowledge_hits / policy_context / data_result / calculation / draft_report / sources / tables / chart / review / final_report / export_path / trace / errors / budget / status / clarification） |
| 同上 | `initial_state(question, thread_id, user_id, settings) -> dict`：把预算、空列表、`status="running"` 一次填好 |
| `src/ops_assistant/workflow/graph.py` | `build_graph(*, settings, checkpointer=None, store=None) -> CompiledStateGraph`：建 10 个节点、连固定边与条件边、`compile(checkpointer=...)` |
| `src/ops_assistant/workflow/nodes/*.py` | 每个节点一个模块；节点是纯函数（除 `data` 用 `conn`、`draft` 用 `llm`） |
| `src/ops_assistant/workflow/persistence.py` | `AppRuntime`：持有 graph、conn、settings，`invoke_turn` 调 `graph.invoke` |

**图长这样**（工程规格 5.7，共 10 个节点）：

```
START → understand
understand --clarify--> clarify → END
understand --knowledge--> knowledge → data → compute → draft → review
understand --data--> data
understand --fail--> fail → END
data --repair--> repair → data
data --fail--> fail
review --export--> export → END
review --reject--> END
```

本单元只做直线：`START → parse → fetch → compute → END`。条件边、循环、失败出口是单元 08。

---

## 5. 文件、完整代码、配置与命令

### 5.1 最小图（先跑通再扩展）

```python
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class TinyState(TypedDict, total=False):
    question: str
    answer: str


def node_parse(state: TinyState) -> dict:
    return {"question": state["question"].strip()}


def node_answer(state: TinyState) -> dict:
    return {"answer": f"收到：{state['question']}"}


builder = StateGraph(TinyState)
builder.add_node("parse", node_parse)
builder.add_node("answer", node_answer)
builder.add_edge(START, "parse")
builder.add_edge("parse", "answer")
builder.add_edge("answer", END)
graph = builder.compile()

print(graph.invoke({"question": "  九月比八月  "}))
```

预期（未在本环境执行）：

```text
{'question': '九月比八月', 'answer': '收到：九月比八月'}
```

五个要点：

- `StateGraph(TinyState)`：声明状态类型（**只用于声明**，运行时仍是 dict）。
- `add_node("名字", 函数)`：名字是图里的节点 id，字符串要唯一。
- `add_edge(START, "parse")`：入口。`START` 不是“一个真节点”，是特殊标记。
- `add_edge("answer", END)`：出口。
- `compile()` 之后才能 `invoke`。

### 5.2 节点返回部分更新（演示“不覆盖”）

```python
class TinyState(TypedDict, total=False):
    question: str
    spec: dict
    data_result: dict


def node_parse(state: TinyState) -> dict:
    return {"spec": {"current_month": "2026-09", "previous_month": "2026-08"}}


def node_fetch(state: TinyState) -> dict:
    # 注意：这里只改 data_result，spec 不会丢
    return {"data_result": {"net_cents": {"2026-09": 1286500, "2026-08": 1190000}}}
```

最终状态里 `spec` 与 `data_result` **同时存在**。如果你在 `node_fetch` 里写 `return {**state, "data_result": ...}`，看起来也能跑，但一旦有并发分支或 reducer，就会产生“把别人的更新覆盖回去”的隐蔽 bug。

### 5.3 实验：三节点业务直线图（一步步搭出来）

§5.1、5.2 已经跑过最小图和"部分更新"。本实验搭一张**真的像业务的直线图**：解析 → 取数 → 计算，每个节点都带 trace，最后**亲眼看见**一个刻意埋下的覆盖 bug——它是单元 07 的引子。分 6 步搭出，最终与仓库 `教程/实验/06_state_graph.py` 完全一致；全程无密钥（节点是纯函数）。

#### 步骤 1：状态契约与样例数据

**这一步解决什么**：图还没画，先把**状态长什么样**定下来——TypeScript 里叫 interface，LangGraph 里就是 TypedDict。字段名**刻意与项目 `AssistantState` 对齐**，学完能无缝迁移。

新建 `教程/实验/06_state_graph.py`：

```python
"""单元 06 实验（步骤 1）：状态契约与样例数据。"""

from __future__ import annotations

from typing import TypedDict

# 样例数据：与项目 seed 数据的口径一致（单位：分）
SAMPLE_MONTHS = {
    "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500, "order_count": 57},
    "2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000, "order_count": 61},
}


class OpsState(TypedDict, total=False):
    """本实验的状态：字段命名刻意与项目 AssistantState 对齐。"""

    question: str
    spec: dict
    data_result: dict
    calculation: dict
    status: str
    errors: list[str]
    trace: list[dict]
```

**逐行讲解**：

- `TypedDict, total=False`：**所有键都可缺省**——LangGraph 的节点是"部分更新"语义（§5.2），入口状态可以只有 `question`，每个节点各补各的键。如果用普通 TypedDict（total=True），类型检查器会把缺键视为错误。
- 七个字段就是一条业务流水线：问题（question）→ 解析结果（spec）→ 取数结果（data_result）→ 计算结果（calculation）；status/errors 是控制与错误通道，trace 是审计通道。
- `SAMPLE_MONTHS` 两行数据：单位分、与项目 seed 一致——**口径从教学第一行就对齐**。

**运行与观察**（临时验证）：

```python
if __name__ == "__main__":
    state: OpsState = {"question": "九月比八月变化多少？"}
    print(sorted(state))
```

```text
['question']
```

一个只有问题、什么都没有的状态——这就是图的**入口**。

#### 步骤 2：三个节点——纯函数，各返回一小片

**这一步解决什么**：把"解析 / 取数 / 计算"写成**三个纯函数**。每个函数只读它要读的键、只返回它负责的键——**绝不修改入参，绝不返回整个状态**。

```python
def node_parse(state: OpsState) -> dict:
    """解析问题：本实验用固定结果，真实项目由纯规则 parse_question 完成。"""
    spec = {
        "raw": state.get("question", ""),
        "current_month": "2026-09",
        "previous_month": "2026-08",
        "metric": "net_sales",
        "dimension": "region",
        "needs_clarification": False,
    }
    trace = [{"node": "parse", "action": "parsed_spec", "current": "2026-09", "previous": "2026-08"}]
    return {"spec": spec, "trace": trace, "status": "running"}


def node_fetch(state: OpsState) -> dict:
    """取数：真实项目调用 queries.get_month_sales(conn, month)。"""
    spec = state["spec"]
    months = [spec["previous_month"], spec["current_month"]]
    data = {month: SAMPLE_MONTHS[month] for month in months}
    trace = [{"node": "fetch", "action": "loaded_months", "months": months}]
    return {"data_result": {"months": data, "unit": "cents"}, "trace": trace}


def node_compute(state: OpsState) -> dict:
    """计算：真实项目由 domain/metrics.py 的纯函数完成。"""
    months = state["data_result"]["months"]
    current = months["2026-09"]["net_cents"]
    previous = months["2026-08"]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    calculation = {
        "current_month": "2026-09",
        "previous_month": "2026-08",
        "current_cents": current,
        "previous_cents": previous,
        "delta_cents": delta,
        "pct_change": pct,
        "base_zero": previous == 0,
        "formula": "delta = net(current) - net(previous)",
    }
    trace = [{"node": "compute", "action": "calculated", "delta_cents": delta, "pct_change": pct}]
    return {"calculation": calculation, "trace": trace, "status": "answered"}
```

**逐行讲解**：

- 依赖方向清晰可见：`node_fetch` 读 `spec`（parse 写的），`node_compute` 读 `data_result`（fetch 写的）——**下游只读上游产出**，这就是直线图的数据流。
- 每个节点的 trace 都是 `[...]` **一整条新列表**，而不是追加到旧列表——这不是笔误，是**故意埋的 bug**：默认 reducer 是"整体覆盖"，所以最后只剩 compute 的一条。步骤 5 会亲眼看见，单元 07 修它。
- `node_parse` 返回 `"status": "running"`、`node_compute` 返回 `"answered"`：状态机的阶段标记跟着数据走，下游（或人）一眼看出图走到了哪。
- `pct = None if previous == 0 else ...`：零基期纪律第三次出现——它会在 13 单元成为正式规则。

#### 步骤 3：`build_graph`——把函数接成图

**这一步解决什么**：节点是零件，图是**装配**。`StateGraph` + `add_node` + `add_edge` 就是全部语法；`compile()` 把 builder 变成可执行的图对象。

```python
from langgraph.graph import END, START, StateGraph


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("parse", node_parse)
    builder.add_node("fetch", node_fetch)
    builder.add_node("compute", node_compute)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", "fetch")
    builder.add_edge("fetch", "compute")
    builder.add_edge("compute", END)
    return builder.compile()
```

**逐行讲解**：

- `add_node(名字, 函数)`：名字是**边和 trace 里引用的 ID**，函数是行为——名字一旦定了就不要改（检查点、日志、调试都认名字）。
- `START → parse → fetch → compute → END`：直线图没有分支，`add_edge` 一条条连即可；条件分支要等单元 08 的 `add_conditional_edges`。
- `compile()` 之后得到的是**可调用对象**（有 `.invoke()` / `.stream()`）——builder 本身不能执行，"图纸"和"机器"是两个东西。

**运行与观察**（临时验证）：

```python
    graph = build_graph()
    print(type(graph).__name__)
```

```text
CompiledStateGraph
```

#### 步骤 4：`stream(updates)`——每个节点跑完各吐一条

**这一步解决什么**：直线图一闪就跑完了，**中间过程看不见**。`stream_mode="updates"` 让每个节点跑完立刻产出"我返回了哪些键"——这是调试图的第一件武器。

```python
import json

initial = {
    "question": "2026 年九月净销售额比八月变化多少？",
    "status": "running",
    "errors": [],
}
for update in graph.stream(initial, stream_mode="updates"):
    for node_name, node_output in update.items():
        print(f"[{node_name}] 返回键：{sorted(node_output)}")
        print("    ", json.dumps(node_output, ensure_ascii=False))
```

**逐行讲解**：

- `stream_mode="updates"`：每次产出 `{节点名: 该节点返回的 dict}`——**增量**，不是全量状态。对照着看三个节点的返回键：parse 给 `spec/trace/status`，fetch 给 `data_result/trace`，compute 给 `calculation/trace/status`。
- 入口状态里预置 `errors: []`：空列表先占位——后面单元的条件路由要读它。

**运行与观察**：

```text
[parse] 返回键：['spec', 'status', 'trace']
     {"spec": {"raw": "2026 年九月净销售额比八月变化多少？", "current_month": "2026-09", …}, "status": "running", "trace": [{…}]}
[fetch] 返回键：['data_result', 'trace']
     {"data_result": {"months": {"2026-08": {…}, "2026-09": {…}}, "unit": "cents"}, "trace": [{…}]}
[compute] 返回键：['calculation', 'status', 'trace']
     {"calculation": {"current_cents": 1286500, "previous_cents": 1190000, "delta_cents": 96500, "pct_change": 0.0811, …}, "status": "answered", "trace": [{…}]}
```

三个节点**各回各的键**——"部分更新"在真实业务里的样子。

#### 步骤 5：`invoke` 与 trace 覆盖——本实验的高潮

**这一步解决什么**：`invoke` 拿到**最终全量状态**；然后校验"三个节点的 trace 都在吗"——**不在**。覆盖 bug 现形。

```python
final = graph.invoke(initial)
print("最终状态键：", sorted(final))
print("status =", final["status"])
print("calculation =", json.dumps(final["calculation"], ensure_ascii=False))

nodes_in_trace = [item["node"] for item in final["trace"]]
print("trace 里保留的节点 =", nodes_in_trace)
print("注意：这里每个节点都返回了整条 trace 列表，最后一个节点的返回值会覆盖前面的。")
print("     这正是单元 07 要解决的“追加而不是覆盖”问题。")
```

**逐行讲解**：

- `final` 是**合并后**的状态：入口的 question、三个节点各自补的键都在——LangGraph 默认按"键级覆盖合并"（节点返回里有的键覆盖，没有的保留）。
- `nodes_in_trace` 只有 `['compute']`：parse 和 fetch 的 trace **被 compute 的一条覆盖了**。审计通道断在最后一环——这正是默认 reducer 的语义边界，单元 07 用 `Annotated[list, operator.add]` 修成"追加"。

**运行与观察**：

```text
最终状态键： ['calculation', 'data_result', 'errors', 'question', 'spec', 'status', 'trace']
status = answered
calculation = {"current_month": "2026-09", "previous_month": "2026-08", "current_cents": 1286500, "previous_cents": 1190000, "delta_cents": 96500, "pct_change": 0.0811, "base_zero": false, "formula": "delta = net(current) - net(previous)"}
trace 里保留的节点 = ['compute']
注意：这里每个节点都返回了整条 trace 列表，最后一个节点的返回值会覆盖前面的。
     这正是单元 07 要解决的“追加而不是覆盖”问题。
```

**数字是对的（96500 分、8.11%），审计是断的**——业务结论与可审计性是两回事，这个区分会贯穿全书。

#### 步骤 6：整合——完整文件

把步骤 4、5 的观察代码装进 `main()`，清理临时验证：

```python
import sys


def main() -> int:
    print("== 单元 06 实验：StateGraph 三节点直线图 ==")
    graph = build_graph()
    initial = {
        "question": "2026 年九月净销售额比八月变化多少？",
        "status": "running",
        "errors": [],
    }

    print("入口状态：", json.dumps(initial, ensure_ascii=False))
    print("\n--- stream(updates)：每个节点跑完各产出一条 ---")
    for update in graph.stream(initial, stream_mode="updates"):
        for node_name, node_output in update.items():
            print(f"[{node_name}] 返回键：{sorted(node_output)}")
            print("    ", json.dumps(node_output, ensure_ascii=False))

    print("\n--- invoke：返回最终完整状态 ---")
    final = graph.invoke(initial)
    print("最终状态键：", sorted(final))
    print("status =", final["status"])
    print("spec =", json.dumps(final["spec"], ensure_ascii=False))
    print("calculation =", json.dumps(final["calculation"], ensure_ascii=False))

    print("\n--- 校验：三个节点的 trace 是否都在？ ---")
    nodes_in_trace = [item["node"] for item in final["trace"]]
    print("trace 里保留的节点 =", nodes_in_trace)
    print("注意：这里每个节点都返回了整条 trace 列表，最后一个节点的返回值会覆盖前面的。")
    print("     这正是单元 07 要解决的“追加而不是覆盖”问题。")
    return 0
```

完整文件应与仓库完全一致：

```python
"""单元 06 实验：用 StateGraph 把“九月 vs 八月”拆成三个节点。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/06_state_graph.py
不需要模型：三个节点都是纯函数，数字来自脚本内置的样例数据（与项目口径一致）。
"""

from __future__ import annotations

import json
import sys
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

# 样例数据：与项目 seed 数据的口径一致（单位：分）
SAMPLE_MONTHS = {
    "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500, "order_count": 57},
    "2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000, "order_count": 61},
}


class OpsState(TypedDict, total=False):
    """本实验的状态：字段命名刻意与项目 AssistantState 对齐。"""

    question: str
    spec: dict
    data_result: dict
    calculation: dict
    status: str
    errors: list[str]
    trace: list[dict]


def node_parse(state: OpsState) -> dict:
    """解析问题：本实验用固定结果，真实项目由纯规则 parse_question 完成。"""
    spec = {
        "raw": state.get("question", ""),
        "current_month": "2026-09",
        "previous_month": "2026-08",
        "metric": "net_sales",
        "dimension": "region",
        "needs_clarification": False,
    }
    trace = [{"node": "parse", "action": "parsed_spec", "current": "2026-09", "previous": "2026-08"}]
    return {"spec": spec, "trace": trace, "status": "running"}


def node_fetch(state: OpsState) -> dict:
    """取数：真实项目调用 queries.get_month_sales(conn, month)。"""
    spec = state["spec"]
    months = [spec["previous_month"], spec["current_month"]]
    data = {month: SAMPLE_MONTHS[month] for month in months}
    trace = [{"node": "fetch", "action": "loaded_months", "months": months}]
    return {"data_result": {"months": data, "unit": "cents"}, "trace": trace}


def node_compute(state: OpsState) -> dict:
    """计算：真实项目由 domain/metrics.py 的纯函数完成。"""
    months = state["data_result"]["months"]
    current = months["2026-09"]["net_cents"]
    previous = months["2026-08"]["net_cents"]
    delta = current - previous
    pct = None if previous == 0 else round(delta / abs(previous), 4)
    calculation = {
        "current_month": "2026-09",
        "previous_month": "2026-08",
        "current_cents": current,
        "previous_cents": previous,
        "delta_cents": delta,
        "pct_change": pct,
        "base_zero": previous == 0,
        "formula": "delta = net(current) - net(previous)",
    }
    trace = [{"node": "compute", "action": "calculated", "delta_cents": delta, "pct_change": pct}]
    return {"calculation": calculation, "trace": trace, "status": "answered"}


def build_graph():
    builder = StateGraph(OpsState)
    builder.add_node("parse", node_parse)
    builder.add_node("fetch", node_fetch)
    builder.add_node("compute", node_compute)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", "fetch")
    builder.add_edge("fetch", "compute")
    builder.add_edge("compute", END)
    return builder.compile()


def main() -> int:
    print("== 单元 06 实验：StateGraph 三节点直线图 ==")
    graph = build_graph()
    initial = {
        "question": "2026 年九月净销售额比八月变化多少？",
        "status": "running",
        "errors": [],
    }

    print("入口状态：", json.dumps(initial, ensure_ascii=False))
    print("\n--- stream(updates)：每个节点跑完各产出一条 ---")
    for update in graph.stream(initial, stream_mode="updates"):
        for node_name, node_output in update.items():
            print(f"[{node_name}] 返回键：{sorted(node_output)}")
            print("    ", json.dumps(node_output, ensure_ascii=False))

    print("\n--- invoke：返回最终完整状态 ---")
    final = graph.invoke(initial)
    print("最终状态键：", sorted(final))
    print("status =", final["status"])
    print("spec =", json.dumps(final["spec"], ensure_ascii=False))
    print("calculation =", json.dumps(final["calculation"], ensure_ascii=False))

    print("\n--- 校验：三个节点的 trace 是否都在？ ---")
    nodes_in_trace = [item["node"] for item in final["trace"]]
    print("trace 里保留的节点 =", nodes_in_trace)
    print("注意：这里每个节点都返回了整条 trace 列表，最后一个节点的返回值会覆盖前面的。")
    print("     这正是单元 07 要解决的“追加而不是覆盖”问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**回看地图**：

| 步骤 | 最终脚本里的位置 |
| --- | --- |
| 1 契约 | `SAMPLE_MONTHS`、`OpsState`（TypedDict, total=False） |
| 2 节点 | `node_parse()`、`node_fetch()`、`node_compute()`（纯函数 + 埋覆盖伏笔） |
| 3 装配 | `build_graph()`（add_node/add_edge/compile） |
| 4 观察 | `main()` 里 `stream(stream_mode="updates")` 段 |
| 5 高潮 | `main()` 里 invoke + trace 校验段 |
| 6 整合 | docstring、`sys.exit(main())` |

运行与验收：

```powershell
cd ops-assistant
uv run python ../教程/实验/06_state_graph.py
```

退出码 0；你应当能回答：①每个节点返回了哪些键？②为什么 trace 只剩 compute 一条？③如果要让 trace 追加，该改状态的哪一行？（答不上第 3 问没关系，单元 07 第一节就讲。）

### 5.4 逐行解释关键点

| 代码 | 作用 | 常见错误 |
| --- | --- | --- |
| `StateGraph(OpsState)` | 声明状态类型 | 忘了 `total=False` 导致必须传全部字段 |
| `add_node("parse", node_parse)` | 注册节点 | 节点名重复、与 `START`/`END` 同名 |
| `node_parse(state) -> dict` | 节点读状态、返回补丁 | 返回 `None` 表示无状态更新；显式 dict 更便于审查 |
| `add_edge(START, "parse")` | 设入口 | 用字符串 `"__start__"` 手写（易错） |
| `compile()` | 校验并生成可执行图 | 忘记调用就 `invoke` |
| `stream(..., "updates")` | 逐步观察 | 误以为返回完整状态 |

### 5.5 与项目的字段对照

| 实验字段 | 项目字段（`AssistantState`） | 说明 |
| --- | --- | --- |
| `question` | `question` | 用户原始问题 |
| `spec` | `spec` + `last_spec` | 项目多存一份“上一轮 spec”用于追问补全 |
| `data_result` | `data_result` | 项目里还含 `has_orders`、地区明细 |
| `calculation` | `calculation` | 项目里含 `region_delta_sum_cents` 与校验结果 |
| `trace` | `trace` | 项目用 `Annotated[list, operator.add]` 追加（单元 07） |
| `errors` | `errors` | 同上，追加 |
| `status` | `status` | 项目的取值更多：`answered/clarification/awaiting_approval/approved/rejected/failed/budget_exceeded` |
| — | `budget` | 项目每步检查预算（单元 08） |
| — | `messages` | 项目用 `add_messages` 追加（单元 07） |

> **项目设计选择**：`total=False` 让节点可以只写自己那一块；代价是“字段忘了初始化”只能在运行时发现，所以项目用 `initial_state()` 统一初始化所有字段，并在节点里用 `state.get(...)` 读可选字段。

### 5.6 为什么节点函数要“纯”

项目的节点大多是纯函数（输入相同 → 输出相同），除了三个例外：

| 节点 | 副作用 | 怎么控制 |
| --- | --- | --- |
| `data` | 读 SQLite | 只读查询，不写库 |
| `draft` | 可选调用 LLM | 无 key 时走纯模板，保证可复现 |
| `export` | 写文件 | 幂等：先写临时文件再 `os.replace` |

好处：`parse`/`compute` 能在测试里直接 `node(state)` 断言，不需要起图、不需要真实数据库。

---

## 6. 预期输出与关键状态变化

### 6.1 实验输出（2026-10-19 实测运行通过，退出码 0）

```text
== 单元 06 实验：StateGraph 三节点直线图 ==
入口状态： {"question": "2026 年九月净销售额比八月变化多少？", "status": "running", "errors": []}

--- stream(updates)：每个节点跑完各产出一条 ---
[parse] 返回键：['spec', 'status', 'trace']
     {"spec": {"raw": "2026 年九月净销售额比八月变化多少？", "current_month": "2026-09", "previous_month": "2026-08", "metric": "net_sales", "dimension": "region", "needs_clarification": false}, "trace": [{"node": "parse", "action": "parsed_spec", "current": "2026-09", "previous": "2026-08"}], "status": "running"}
[fetch] 返回键：['data_result', 'trace']
     {"data_result": {"months": {"2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000, "order_count": 61}, "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500, "order_count": 57}}, "unit": "cents"}, "trace": [{"node": "fetch", "action": "loaded_months", "months": ["2026-08", "2026-09"]}]}
[compute] 返回键：['calculation', 'status', 'trace']
     {"calculation": {"current_month": "2026-09", "previous_month": "2026-08", "current_cents": 1286500, "previous_cents": 1190000, "delta_cents": 96500, "pct_change": 0.0811, "base_zero": false, "formula": "delta = net(current) - net(previous)"}, "trace": [{"node": "compute", "action": "calculated", "delta_cents": 96500, "pct_change": 0.0811}], "status": "answered"}

--- invoke：返回最终完整状态 ---
最终状态键： ['calculation', 'data_result', 'errors', 'question', 'spec', 'status', 'trace']
status = answered
spec = {"raw": "2026 年九月净销售额比八月变化多少？", "current_month": "2026-09", "previous_month": "2026-08", "metric": "net_sales", "dimension": "region", "needs_clarification": false}
calculation = {"current_month": "2026-09", "previous_month": "2026-08", "current_cents": 1286500, "previous_cents": 1190000, "delta_cents": 96500, "pct_change": 0.0811, "base_zero": false, "formula": "delta = net(current) - net(previous)"}

--- 校验：三个节点的 trace 是否都在？ ---
trace 里保留的节点 = ['compute']
注意：这里每个节点都返回了整条 trace 列表，最后一个节点的返回值会覆盖前面的。
     这正是单元 07 要解决的“追加而不是覆盖”问题。
```

### 6.2 三个必须亲眼确认的现象

1. **`stream` 的每条是“补丁”**：`[fetch] 返回键：['data_result', 'trace']` —— 没出现 `spec`，说明节点只返回自己那部分。
2. **`invoke` 返回完整状态**：`spec` 和 `data_result` 同时存在，说明补丁被合并了。
3. **`trace` 只剩最后一条**：因为默认 reducer 是覆盖。这是**故意留下的坑**，单元 07 用 `operator.add` 修掉。

### 6.3 关键状态变化

| 对象 | 之前 | 之后 |
| --- | --- | --- |
| 对“图”的认识 | 觉得是黑盒 | 知道 = 状态声明 + 节点 + 边 + 编译 |
| 节点写法 | 想返回完整状态 | 只返回自己改的字段（补丁） |
| 调试手段 | 只能看最终结果 | 会用 `stream(updates)` 逐步看 |
| `trace` 丢失问题 | 没意识到 | 亲眼看到被覆盖，知道要 reducer |
| 项目图 | 只看到一张示意图 | 能说出 `understand → data → compute → draft → review` 是固定边，`understand` 是条件入口 |

---

## 7. 修改练习（至少 2 个，含一个“改坏它”）

### 练习 7.1（简单）：加一个 `status` 之外的 `render` 节点

在 `compute` 与 `END` 之间插入 `render` 节点：读 `calculation`，生成一句话 `answer`，返回 `{"answer": f"净销售额差额 {delta} 分", "status": "answered"}`（其中 `delta = state["calculation"]["delta_cents"]`）。

验收：`stream` 输出多一条 `[render]`；`final["answer"]` 含 `96500` 与 `8.11%`（或 `0.0811`，由你格式化决定）。

### 练习 7.2（中等）：让 `parse` 支持两个问题

把固定的 `current_month="2026-09"` 改成从问题里识别：

- 含“九月” → `2026-09`，`previous_month="2026-08"`
- 含“八月” → `2026-08`，`previous_month="2026-07"`

用 `graph.invoke({"question": "2026 年八月销售额呢？"})` 验证 `spec.current_month == "2026-08"`。

验收：两个问题都能跑，`fetch` 取的月份随 `spec` 变化（`2026-07` 不在样例数据里时，让 `fetch` 返回 `errors` 而不是抛异常）。

### 练习 7.3（改坏它）：让节点返回完整状态

把 `node_fetch` 最后一行 `return` 改成下面这行（其余代码不动）：

```python
return {**state, "data_result": {"months": data, "unit": "cents"}, "trace": trace}
```

重跑，观察两处变化：

1. `stream` 的 `[fetch] 返回键` 是否多了 `question`、`spec`？（提示：会，因为完整状态被当成补丁返回）
2. 最终 `spec` 是否还是 `parse` 写的那份？如果把 `parse` 改成“每次都从 `state.get("spec")` 派生”，会出什么问题？

请回答：为什么“返回完整状态”在有条件分支/并发分支时是危险的？（提示：你返回的旧值会覆盖别的分支刚写入的新值）

### 练习 7.4（进阶）：用 `invoke` 的 config 限制步数

```python
final = graph.invoke(initial, config={"recursion_limit": 2})
```

请记录结果（是否报 `GraphRecursionError` 一类错误）。这就是单元 08 预算机制的底层开关，先建立直觉。

> **未验证**：`recursion_limit` 的具体语义（按“超级步”计数）与异常类型以你的 `langgraph` 版本为准。

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：节点返回 `None`

```python
def node_bad(state: OpsState):
    state["status"] = "running"  # 只改不返回
```

本版本实测：返回 None 被当成无更新。不要原地修改 state；请返回明确的更新字典，并对最终状态写断言。

**要理解的点**：节点不是“原地修改”，**必须 return 补丁**。就地改 `state` 不会生效（甚至可能污染后续）。

### 实验 8.2：节点返回未声明的键

在 `node_parse` 的返回 dict 里多加一个键：

```python
return {"spec": spec, "status": "running", "trace": trace, "unknown_field": 1}
```

本版本普通 TypedDict 示例实测：未知输出键被忽略。拼写错误不一定抛异常，必须用结果断言和类型检查发现。

**要理解的点**：State 声明是契约。写错字段名（`dataResult` vs `data_result`）不会自动报错时非常危险，所以项目里用 `TypedDict` + 单测断言键集合。

### 实验 8.3：忘记连 `END`

删掉 `builder.add_edge("compute", END)`。

本版本示例实测：无后继节点正常终止。显式连 END 表达终止意图，但不是所有末端都必须手工连接才能编译。

**要理解的点**：编译期检查是你的朋友，别等到运行时才发现图没走完。

### 实验 8.4：重复 `add_node` 同名

对 `"parse"` 调用两次 `add_node`。

本版本重复 add_node 同名会抛 ValueError；节点名应唯一。

**要理解的点**：节点 id 是图的地址，重名会让“哪段逻辑在跑”变得不可解释。

### 实验 8.5：状态里放不可 JSON 序列化的对象

在 `initial` 里加 `"conn": sqlite3.connect(":memory:")`（或任意连接对象），跑 `invoke`。

预期（未在本环境执行）：不接 checkpointer 时可能能跑；一旦接 `SqliteSaver` 就会在写入时失败。

**要理解的点**：状态必须可序列化（工程规格：TurnResult 必须可 JSON 序列化）。数据库连接、文件句柄应放在 `AssistantContext`（运行期上下文）而不是 State。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能写出一个 `StateGraph`：声明 State、加 3 个节点、连边、`compile()`、`invoke()`。
- [ ] 我能解释“节点返回的是补丁，不是完整状态”，并举出返回完整状态会坏掉的场景。
- [ ] 我运行了 `06_state_graph.py`，看到 `stream(updates)` 每个节点只返回自己改的键。
- [ ] 我从输出里确认了 `trace` 被覆盖（只剩 `['compute']`），并能说出修法方向（reducer）。
- [ ] 我能说出 `compile()` 至少做两项检查（入口/出口/节点存在性）。
- [ ] 我能把项目的 10 节点图拆成“本单元直线部分”和“单元 08 条件/循环部分”。
- [ ] 我完成了练习 7.3，能说清“返回完整状态”为什么在分支图里危险。
- [ ] 我知道 State 里的值必须可序列化，连接/句柄应放 Context 而不是 State。

---

## 10. 复盘问题与参考答案、下一步

### Q1：`StateGraph(TypedDict)` 中的 `TypedDict` 在运行时真的会校验吗？

**参考**：不会做运行时类型校验。它只是**声明**，用于生成通道（channels）与文档。字段类型写错（例如声明 `dict` 却塞字符串）运行时不会报错，直到下游节点用错。所以项目靠两件事兜底：`initial_state()` 统一初始化 + 单测断言关键字段。

### Q2：为什么节点要返回 `dict` 而不是直接改 `state`？

**参考**：因为框架需要知道“哪些字段被改了”，才能做合并、触发 reducer、决定下一节点、写 checkpoint。直接改 `state` 绕过了这套机制，行为不可预测。这也是“函数式”设计：节点是纯函数，状态由框架托管。

### Q3：`compile()` 能不能跳过？

**参考**：不能。未编译的 `StateGraph` 只是声明，没有可执行的运行时。编译时会做结构校验（入口、出口、边指向），把错误提前到启动阶段——这正是我们想要的“早失败”。

### Q4：`invoke` 与 `stream` 该用哪个？

**参考**：调试与前端展示用 `stream`（能看到每一步）；服务端一次性返回用 `invoke`。项目里 API 的同步端点用 `invoke`，UI 希望“逐步显示进度”时用 `stream`。两者对状态的合并语义完全一致。

### Q5：项目的图为什么要 10 个节点，不能合并成 3 个？

**参考**：因为每个节点对应一个**可独立测试、可独立失败、可独立审计**的职责。例如 `compute` 是纯计算（可单测差值、变化率、地区求和校验），`draft` 是渲染（可单测 Markdown 结构），`review` 是人工审批（可单测 interrupt 载荷）。合并成大节点会让测试粒度变粗、失败原因变糊。代价是状态字段变多、图更复杂——用清晰的字段命名与 `trace` 抵消。

### 下一步

进入 **单元 07：Reducer、MessagesState 与状态设计**。你会：

1. 亲手修掉本单元留下的 `trace` 覆盖问题（`Annotated[list, operator.add]`）。
2. 理解默认覆盖 vs 追加的差别，以及为什么 `messages` 必须用 `add_messages`。
3. 用 `MessagesState` 作为最小可用状态，再对比项目的 `AssistantState`。
4. 学一套“状态字段设计检查清单”，避免把不该进状态的东西塞进去。

进入前请确认：`uv run python ../教程/实验/06_state_graph.py` 能跑完，且你能指出输出里哪一行证明了“默认 reducer 是覆盖”。
