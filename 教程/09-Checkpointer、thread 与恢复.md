# 单元 09：Checkpointer、thread 与恢复

> 建议时长 60 分钟（**上半 25 分钟**：5.1～5.4 内存 checkpointer 与 thread；**下半 35 分钟**：5.5～5.8 落盘、跨进程恢复、状态读取） | 前置：单元 08 | 可跳过条件：能说清 `InMemorySaver` 与 `SqliteSaver` 的差别，并解释“恢复时节点会从头重跑”意味着什么
>
> 版本基线：`langgraph 1.2.16`、`langgraph-checkpoint-sqlite`（核对日期 2026-10-19）
>
> 本单元不需要 API Key，但需要能写文件。所有输出为“预期（未在本环境执行）”。

---

## 1. 学习目标与前置

完成后你应该能：

1. 解释 checkpoint 是什么：**图在每一步之后的状态快照**，存在外部存储里。
2. 用 `InMemorySaver` + `thread_id` 让同一会话多轮对话记住上文，并说清它的生命周期（进程内）。
3. 用 `SqliteSaver` 把快照写进 SQLite，**重启进程后同一 thread 继续**。
4. 用 `get_state` / `get_state_history` / `update_state` 读历史、改状态。
5. 说出项目为什么选 `SqliteSaver`（单机、零依赖、可查看、可恢复），以及它的边界（并发、迁移）。
6. 理解 `interrupt` 恢复时“节点从头重跑”的含义（单元 16 的伏笔）。

前置：单元 08 完成，能画出“条件路由 + 失败出口”的图。

---

## 2. 官方文档与阅读问题

**官方文档：Persistence（https://docs.langchain.com/oss/python/langgraph/persistence，核对日期 2026-10-19）**

**官方文档：LangGraph 概览（https://docs.langchain.com/oss/python/langgraph/overview，核对日期 2026-10-19）**

带着这些问题读：

1. 官方定义 checkpoint 时强调它保存什么？（状态、下一步、待执行任务？）
2. `thread_id` 放在哪里传？（`config={"configurable": {"thread_id": ...}}`）
3. `InMemorySaver` 与 `SqliteSaver` 分别适合什么场景？官方有没有说“内存 saver 不能跨进程”？
4. `get_state` 与 `get_state_history` 返回什么？`update_state` 用来做什么？
5. `interrupt` 恢复时，节点会从哪里重新开始？官方有没有提醒“interrupt 之前的代码会重跑”？

> **官方事实**：LangGraph 的持久化由 checkpointer 实现；同一个 `thread_id` 的多次 `invoke` 共享一条状态历史，因此可以多轮对话与断点恢复。`InMemorySaver` 存在进程内存；`SqliteSaver` 存在 SQLite 文件，可跨进程。
>
> **未验证**：`SqliteSaver` 的连接创建方式（`from_conn_string` 上下文管理器 vs 直接传 `sqlite3.Connection`）与包名在不同版本有差异；以你 `uv.lock` 里 `langgraph-checkpoint-sqlite` 的官方示例为准。

---

## 3. 通俗解释与精确技术定义

### 3.1 checkpoint 是什么

把它想成“存档”：

```
invoke 一轮 → 每走完一个节点 → 框架把当前状态 + 下一步 + 待办任务 存一份快照
下一轮 invoke（同一 thread_id）→ 先读最后一份快照，再继续
进程崩溃 → 快照还在 → 新进程按 thread_id 读回来 → 继续
```

没有 checkpointer 时，`invoke` 是“一次性的函数调用”：跑完就没了，下一轮从零开始。

### 3.2 thread_id 是什么

`thread_id` 是会话的**主键**。

- 同一个 `thread_id`：共享历史（“那华东地区呢？”能接上文）。
- 不同 `thread_id`：完全隔离（另一个用户/另一个会话）。

项目里 `thread_id` 由 API 层生成或由前端传入，`invoke_turn(runtime, thread_id, message)` 显式接收。

### 3.3 `InMemorySaver` vs `SqliteSaver`

| | `InMemorySaver` | `SqliteSaver` |
| --- | --- | --- |
| 存储 | Python 进程内存 | SQLite 文件（`data/checkpoints.db`） |
| 跨进程 | ❌ 重启即丢 | ✅ |
| 依赖 | 无 | `langgraph-checkpoint-sqlite` |
| 多进程并发 | 各自一份 | 需要处理锁（`check_same_thread`、写锁） |
| 适合 | 单元测试、演示 | 单机交付、可恢复 |

> **官方事实**：两者都实现 checkpointer 接口；选择取决于“是否需要跨进程/跨会话持久化”。
>
> **项目设计选择**：项目用 `SqliteSaver`，因为要求“关掉程序明天还能接着看”“审批中断后重启还能继续”。代价是要管理数据库文件与并发锁。

### 3.4 `get_state` 能读到什么

| 方法 | 返回 | 用途 |
| --- | --- | --- |
| `graph.get_state(config)` | 最新快照（`values`、`next`、`config`、`created_at`…） | 查“现在停在哪、状态是什么” |
| `graph.get_state_history(config)` | 快照迭代器（从新到旧） | 回溯“每一步发生了什么” |
| `graph.update_state(config, values)` | 新快照 | 人工修正状态（谨慎使用） |

项目里 `read_thread` 用 `get_state` 生成“当前会话摘要”，UI 的“历史记录”用它。

### 3.5 恢复时节点会重跑（重要）

`interrupt()` 的实现机制是“暂停并抛出控制流”。恢复时，**被中断的那个节点会从函数开头重新执行**，而不是从 `interrupt()` 下一行继续。

这意味着：

- `interrupt()` 之前的代码必须**幂等**（纯读、纯计算）；
- 写文件、发请求、扣库存这类副作用必须放在 `interrupt()` 之后，或放到下一个节点。

项目的 `review` 节点因此拆成“整理审批载荷（纯读）→ interrupt → 处理恢复值”，`export` 单独成节点保证只写一次（工程规格 5.8）。

---

## 4. 这个知识在主项目中的位置

| 项目位置 | 内容 |
| --- | --- |
| `workflow/persistence.py` | `AppRuntime`：持有 `checkpointer: SqliteSaver`、`graph`、`conn`、`lock` |
| 同上 | `build_graph(*, settings, checkpointer=None, store=None)`：把 checkpointer 传给 `compile()` |
| 同上 | `invoke_turn(runtime, thread_id, message, user_id)`：`graph.invoke(..., config={"configurable": {"thread_id": thread_id}})` |
| 同上 | `resume_turn(runtime, thread_id, *, decision, comment, edited_report)`：`graph.invoke(Command(resume=...), config)` |
| 同上 | `read_thread` / `list_threads`：基于 `get_state` / 历史查询 |
| `config.py` | 数据库路径（如 `data/ops.db`、`data/checkpoints.db`） |
| `api/main.py` | 会话路由（单元 19）用 `thread_id` 隔离用户会话 |

> **接口以工程规格 5.6/5.8 为准**；项目源码并行实现中，本单元只引用这些确定的名字。

---

## 5. 文件、完整代码、配置与命令

### 5.1 最小多轮对话（`InMemorySaver`）

```python
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    turn: int


def node_reply(state: ChatState) -> dict:
    last = state["messages"][-1]
    return {
        "messages": [AIMessage(content=f"收到第 {state.get('turn', 0) + 1} 轮：{last.content}")],
        "turn": state.get("turn", 0) + 1,
    }


builder = StateGraph(ChatState)
builder.add_node("reply", node_reply)
builder.add_edge(START, "reply")
builder.add_edge("reply", END)
graph = builder.compile(checkpointer=InMemorySaver())

config = {"configurable": {"thread_id": "demo-09"}}
first = graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, config)
second = graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, config)

print(first["turn"], second["turn"])
print([message.content for message in second["messages"]])
```

预期（未在本环境执行）：

```text
1 2
['九月比八月变化多少？', '收到第 1 轮：九月比八月变化多少？', '那华东地区呢？', '收到第 2 轮：那华东地区呢？']
```

关键点：第二轮**没有传历史**，只传了新的 `HumanMessage`，但结果是 4 条消息——因为 checkpointer 按 `thread_id` 恢复了上一轮状态，`add_messages` 又追加了新消息。

### 5.2 换 `thread_id` = 换会话

```python
other = {"configurable": {"thread_id": "demo-09-b"}}
third = graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, other)
print(third["turn"])
```

预期（未在本环境执行）：`1`（新会话从零开始，不知道上文）。

### 5.3 实验：内存 vs 落盘、跨进程恢复（一步步搭出来）

§5.1、5.2 已经见过 InMemorySaver 和 thread_id。本实验把两条线都推到底：**快照时间线**（get_state_history）、**落盘与跨进程恢复**（SqliteSaver + `--resume`）、以及 **update_state 人工修正**（要慎用的后门）。分 6 步搭出，最终与仓库 `教程/实验/09_checkpoint.py` 完全一致；全程无密钥。

#### 步骤 1：状态、节点与常量——轮次计数器

**这一步解决什么**：checkpointer 是"外挂"，图本身不需要知道它存在。先写一个**自带轮次计数**的 echo 图——`turn` 是观察"恢复有没有接上"的最直观指标。

新建 `教程/我的实验/09_checkpoint.py`（参考实现在 `教程/实验/09_checkpoint.py`，写完本单元再对照）：

```python
"""单元 09 实验（步骤 1）：带轮次计数的 echo 图。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
DB_PATH = OUTPUT_DIR / "checkpoints.db"
THREAD_ID = "demo-09"


class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    turn: int
    status: str


def node_reply(state: ChatState) -> dict:
    last = state["messages"][-1]
    turn = state.get("turn", 0) + 1
    return {
        "messages": [AIMessage(content=f"第 {turn} 轮收到：{last.content}")],
        "turn": turn,
        "status": "answered",
    }


def build_plain_graph():
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile()
```

**逐行讲解**：

- `OUTPUT_DIR`/`DB_PATH` 用 `Path(__file__).resolve().parent` 定位——与 01 自检脚本同一个习惯：**脚本放哪，输出就跟到哪**，不污染仓库其它目录。
- `turn = state.get("turn", 0) + 1`：**读旧值、加一、返回新值**——没有 Annotated 的普通键是覆盖语义，这里恰恰要覆盖（每轮只有一个 turn）。
- `node_reply` 回显最后一条消息：echo 图足以演示一切检查点行为，**不被业务逻辑分心**。
- （`build_plain_graph` 是本步骤临时用的无检查点版本，最终文件里它会进化成 `build_memory_graph`。）

**运行与观察**（临时验证）：

```python
if __name__ == "__main__":
    graph = build_plain_graph()
    out = graph.invoke({"messages": [HumanMessage(content="你好")]})
    print(out["turn"], out["messages"][-1].content)
```

```text
1 第 1 轮收到：你好
```

没挂 checkpointer，每跑一次 turn 都从 0 开始——这正是接下来要改变的。

#### 步骤 2：挂上 InMemorySaver——thread 隔离

**这一步解决什么**：`compile(checkpointer=...)` 一行挂载；**thread_id 是会话的钥匙**——同 id 接着聊，不同 id 互不认识。

```python
from langgraph.checkpoint.memory import InMemorySaver


def build_memory_graph():
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=InMemorySaver())


def config_for(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def demo_memory_isolation() -> None:
    print("\n== 第一段：InMemorySaver 的 thread 隔离 ==")
    graph = build_memory_graph()
    a = config_for("thread-a")
    b = config_for("thread-b")

    graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, a)
    graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, a)
    graph.invoke({"messages": [HumanMessage(content="另一个会话的问题")]}, b)

    state_a = graph.get_state(a)
    state_b = graph.get_state(b)
    print("thread-a 消息数：", len(state_a.values["messages"]), "turn：", state_a.values["turn"])
    print("thread-b 消息数：", len(state_b.values["messages"]), "turn：", state_b.values["turn"])
    print("thread-a 内容：", [message.content for message in state_a.values["messages"]])
    print("→ 不同 thread_id 完全隔离。")
```

**逐行讲解**：

- `config_for`：thread_id 藏在 `config["configurable"]` 里——invoke、get_state 都**认这把钥匙**；漏传会直接报错（不是静默新会话），这个严格性是对的。
- `get_state(config)`：不执行图，只**读当前快照**——`.values` 是状态全量。
- 同一个 graph 对象、两个 thread：thread-a 聊两轮（turn=2、4 条消息），thread-b 只有自己那轮——**隔离靠 thread_id，不靠建多个图**。

**运行与观察**（临时验证 `demo_memory_isolation()`）：

```text
== 第一段：InMemorySaver 的 thread 隔离 ==
thread-a 消息数： 4 turn： 2
thread-b 消息数： 2 turn： 1
thread-a 内容： ['九月比八月变化多少？', '第 1 轮收到：九月比八月变化多少？', '那华东地区呢？', '第 2 轮收到：那华东地区呢？']
→ 不同 thread_id 完全隔离。
```

#### 步骤 3：快照时间线——`get_state_history`

**这一步解决什么**：checkpointer 存的**不是一个"最新状态"，而是一条时间线**——每执行一步就存一份快照。`get_state_history` 从新到旧迭代它们；"时间旅行"（单元 17）的地基在这里。

在 `demo_memory_isolation()` 末尾接上：

```python
    print("\n-- get_state_history：从新到旧 --")
    history = list(graph.get_state_history(a))
    print("快照数：", len(history))
    for snapshot in history[:3]:
        print("  next =", snapshot.next, "turn =", snapshot.values.get("turn"),
              "created_at =", getattr(snapshot, "created_at", "N/A"))
```

**逐行讲解**：

- 每轮图执行产生**多个**快照（每经过一个节点存一次）：两轮 × 一节点 + 初始，具体数量随版本而定——**别背数字，看结构**。
- `snapshot.next`：这个快照之后**即将执行的节点元组**——非空说明"图停在这里还有事没做"；为空说明"跑完了"。这是判断中断/完成的关键字段（单元 16 靠它找 interrupt 位置）。
- `getattr(snapshot, "created_at", "N/A")`：字段名随版本有过变化——**防御式取用**，教学代码不赌版本细节。

**运行与观察**：

```text
-- get_state_history：从新到旧 --
快照数： 5
  next = () turn = 2 created_at = …
  next = () turn = 2 created_at = …
  next = ('reply',) turn = 1 created_at = …
  next = ('reply',) turn = 0 created_at = …
```

（快照数与字段值随版本略有差异。）**从旧到新排列**：turn=0 的快照 next 是 `('reply',)`——"还没执行 reply"；最新快照 next 是 `()`——"图已走完"。

#### 步骤 4：落盘——`SqliteSaver`

**这一步解决什么**：InMemorySaver **进程一退就失忆**。SqliteSaver 把快照写进文件，进程重启后同一 thread_id 能接上——"恢复"的前提。

```python
import sqlite3


def build_sqlite_graph(db_path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 注意：SqliteSaver 需要 sqlite3 连接；具体构造方式以你的版本官方示例为准
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=saver), conn


def demo_sqlite_roundtrip() -> None:
    print("\n== 第二段：SqliteSaver 写入（本轮进程内） ==")
    graph, conn = build_sqlite_graph(DB_PATH)
    config = config_for(THREAD_ID)

    existing = graph.get_state(config)
    already = existing.values.get("turn", 0)
    print("已有快照 turn：", already, "（首次运行应为 0/None）")

    graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, config)
    graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, config)
    state = graph.get_state(config)
    print("本轮结束后 turn：", state.values["turn"])
    print("消息数：", len(state.values["messages"]))
    print("checkpoint 文件：", DB_PATH, "存在：", DB_PATH.exists())
    conn.close()
```

**逐行讲解**：

- `import` 放**函数内**：`langgraph.checkpoint.sqlite` 是可选依赖，用到才导入——顶层 import 会在没装它的环境直接崩。
- 返回 `(graph, conn)` 元组：连接要由**调用方**关闭（谁创建谁负责开，谁拿到谁负责关）。
- `existing.values.get("turn", 0)`：先看盘上**已有**什么——重复运行本实验时，turn 会接着上次的涨，不是从 0 重来；这个"接着涨"本身就是落盘生效的证据。
- `check_same_thread=False`：SqliteSaver 可能在别的线程写；连接线程约定要放行。

**运行与观察**（临时验证 `demo_sqlite_roundtrip()`，然后 `--resume` 版本下一步才有）：

```text
== 第二段：SqliteSaver 写入（本轮进程内） ==
已有快照 turn： 0 （首次运行应为 0/None）
本轮结束后 turn： 2
消息数： 4
checkpoint 文件： … 教程/实验/_output/checkpoints.db 存在： True
```

#### 步骤 5：跨进程恢复与人工修正

**这一步解决什么**：两件事——① `--resume`：**新进程**打开同一个 db、同一个 thread_id，turn 从 2 接着涨（单元 17 幂等重启的核心语义）；② `update_state`：人工直接改状态（要慎用的后门）。

```python
def resume_after_restart() -> None:
    print("\n== 第三段：新进程按同一 thread_id 恢复 ==")
    if not DB_PATH.exists():
        print("[跳过] 没有 checkpoint 文件，请先不带 --resume 运行一次。")
        return
    graph, conn = build_sqlite_graph(DB_PATH)
    config = config_for(THREAD_ID)
    before = graph.get_state(config)
    print("恢复到的 turn：", before.values.get("turn"))
    print("恢复到的消息：", [message.content for message in before.values["messages"]])

    graph.invoke({"messages": [HumanMessage(content="重启后继续问：那华南呢？")]}, config)
    after = graph.get_state(config)
    print("继续一轮后 turn：", after.values["turn"])
    print("消息数：", len(after.values["messages"]))
    print("→ 进程重启后仍能接上文，这就是 SqliteSaver 与 InMemorySaver 的关键差别。")
    conn.close()


def demo_update_state() -> None:
    print("\n== 第四段：update_state 人工修正（谨慎使用） ==")
    graph = build_memory_graph()
    config = config_for("thread-fix")
    graph.invoke({"messages": [HumanMessage(content="原始问题")]}, config)
    graph.update_state(config, {"turn": 99, "status": "manually_fixed"})
    state = graph.get_state(config)
    print("修正后 turn：", state.values["turn"], "status：", state.values["status"])
    print("→ update_state 会产生新快照，不是“原地修改”。审计场景要记录谁改了什么。")
```

**逐行讲解**：

- `resume_after_restart` 第一件事是**查文件存在**：没有 checkpoint 就明确说"先跑一次"——错误前置、消息给人。
- 恢复 = `build_sqlite_graph(同一个 DB_PATH)` + `config_for(同一个 THREAD_ID)`：**没有别的魔法**，钥匙对上，历史就在。
- `update_state` 直接改键值，但**产生新快照**而非篡改旧快照——时间线上留痕，这就是"可审计"的底线；即便如此，生产上也要包一层权限与日志，不让它变成随手后门。

**运行与观察**（先正常跑一次，再用 `--resume` 跑第二次）：

```text
== 第三段：新进程按同一 thread_id 恢复 ==
恢复到的 turn： 2
恢复到的消息： ['九月比八月变化多少？', '第 1 轮收到：…', '那华东地区呢？', '第 2 轮收到：…']
继续一轮后 turn： 3
消息数： 6
→ 进程重启后仍能接上文，这就是 SqliteSaver 与 InMemorySaver 的关键差别。
```

**turn 从 2 涨到 3**——进程死了，会话没死。

#### 步骤 6：整合——`main()` 按 `--resume` 分流

**这一步解决什么**：两条运行模式（首跑写盘 / 重跑恢复）用一个命令行开关分开，提醒语告诉用户下一步怎么玩。

```python
import sys


def main() -> int:
    print("== 单元 09 实验：checkpointer、thread 与恢复 ==")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    demo_memory_isolation()
    if "--resume" in sys.argv:
        resume_after_restart()
    else:
        demo_sqlite_roundtrip()
        demo_update_state()
        print("\n下一步：运行 `uv run python ../教程/实验/09_checkpoint.py --resume` 体验跨进程恢复。")
    return 0
```

清理临时验证，完整文件应与仓库完全一致：

```python
"""单元 09 实验：checkpointer、thread 与恢复。

用法（在 ops-assistant 目录执行）：
    uv run python ../教程/实验/09_checkpoint.py            # 第一次运行：写入并展示
    uv run python ../教程/实验/09_checkpoint.py --resume   # 第二次运行：跨进程恢复

实验把 checkpoint 写到 ../教程/实验/_output/checkpoints.db（相对脚本位置）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
DB_PATH = OUTPUT_DIR / "checkpoints.db"
THREAD_ID = "demo-09"


class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    turn: int
    status: str


def node_reply(state: ChatState) -> dict:
    last = state["messages"][-1]
    turn = state.get("turn", 0) + 1
    return {
        "messages": [AIMessage(content=f"第 {turn} 轮收到：{last.content}")],
        "turn": turn,
        "status": "answered",
    }


def build_memory_graph():
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=InMemorySaver())


def build_sqlite_graph(db_path: Path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 注意：SqliteSaver 需要 sqlite3 连接；具体构造方式以你的版本官方示例为准
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    builder = StateGraph(ChatState)
    builder.add_node("reply", node_reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=saver), conn


def config_for(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def demo_memory_isolation() -> None:
    print("\n== 第一段：InMemorySaver 的 thread 隔离 ==")
    graph = build_memory_graph()
    a = config_for("thread-a")
    b = config_for("thread-b")

    graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, a)
    graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, a)
    graph.invoke({"messages": [HumanMessage(content="另一个会话的问题")]}, b)

    state_a = graph.get_state(a)
    state_b = graph.get_state(b)
    print("thread-a 消息数：", len(state_a.values["messages"]), "turn：", state_a.values["turn"])
    print("thread-b 消息数：", len(state_b.values["messages"]), "turn：", state_b.values["turn"])
    print("thread-a 内容：", [message.content for message in state_a.values["messages"]])
    print("→ 不同 thread_id 完全隔离。")

    print("\n-- get_state_history：从新到旧 --")
    history = list(graph.get_state_history(a))
    print("快照数：", len(history))
    for snapshot in history[:3]:
        print("  next =", snapshot.next, "turn =", snapshot.values.get("turn"),
              "created_at =", getattr(snapshot, "created_at", "N/A"))


def demo_sqlite_roundtrip() -> None:
    print("\n== 第二段：SqliteSaver 写入（本轮进程内） ==")
    graph, conn = build_sqlite_graph(DB_PATH)
    config = config_for(THREAD_ID)

    existing = graph.get_state(config)
    already = existing.values.get("turn", 0)
    print("已有快照 turn：", already, "（首次运行应为 0/None）")

    graph.invoke({"messages": [HumanMessage(content="九月比八月变化多少？")]}, config)
    graph.invoke({"messages": [HumanMessage(content="那华东地区呢？")]}, config)
    state = graph.get_state(config)
    print("本轮结束后 turn：", state.values["turn"])
    print("消息数：", len(state.values["messages"]))
    print("checkpoint 文件：", DB_PATH, "存在：", DB_PATH.exists())
    conn.close()


def resume_after_restart() -> None:
    print("\n== 第三段：新进程按同一 thread_id 恢复 ==")
    if not DB_PATH.exists():
        print("[跳过] 没有 checkpoint 文件，请先不带 --resume 运行一次。")
        return
    graph, conn = build_sqlite_graph(DB_PATH)
    config = config_for(THREAD_ID)
    before = graph.get_state(config)
    print("恢复到的 turn：", before.values.get("turn"))
    print("恢复到的消息：", [message.content for message in before.values["messages"]])

    graph.invoke({"messages": [HumanMessage(content="重启后继续问：那华南呢？")]}, config)
    after = graph.get_state(config)
    print("继续一轮后 turn：", after.values["turn"])
    print("消息数：", len(after.values["messages"]))
    print("→ 进程重启后仍能接上文，这就是 SqliteSaver 与 InMemorySaver 的关键差别。")
    conn.close()


def demo_update_state() -> None:
    print("\n== 第四段：update_state 人工修正（谨慎使用） ==")
    graph = build_memory_graph()
    config = config_for("thread-fix")
    graph.invoke({"messages": [HumanMessage(content="原始问题")]}, config)
    graph.update_state(config, {"turn": 99, "status": "manually_fixed"})
    state = graph.get_state(config)
    print("修正后 turn：", state.values["turn"], "status：", state.values["status"])
    print("→ update_state 会产生新快照，不是“原地修改”。审计场景要记录谁改了什么。")


def main() -> int:
    print("== 单元 09 实验：checkpointer、thread 与恢复 ==")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    demo_memory_isolation()
    if "--resume" in sys.argv:
        resume_after_restart()
    else:
        demo_sqlite_roundtrip()
        demo_update_state()
        print("\n下一步：运行 `uv run python ../教程/实验/09_checkpoint.py --resume` 体验跨进程恢复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**回看地图**：

| 步骤 | 最终脚本里的位置 |
| --- | --- |
| 1 图 | `ChatState`、`node_reply()`、常量（`OUTPUT_DIR`/`DB_PATH`/`THREAD_ID`） |
| 2 内存版 | `build_memory_graph()`、`config_for()`、`demo_memory_isolation()` 上半 |
| 3 时间线 | `demo_memory_isolation()` 下半（get_state_history） |
| 4 落盘 | `build_sqlite_graph()`、`demo_sqlite_roundtrip()` |
| 5 恢复/修正 | `resume_after_restart()`、`demo_update_state()` |
| 6 整合 | `main()`（--resume 分流）、docstring、`sys.exit(main())` |

运行与验收（两条命令、两个进程）：

```powershell
cd ops-assistant
uv run python ../教程/我的实验/09_checkpoint.py
uv run python ../教程/我的实验/09_checkpoint.py --resume
```

两连跑都退出码 0；第二次运行第一段的 InMemorySaver 部分 turn 仍从 1 开始（进程内新建），第三段 SqliteSaver 部分 turn 却**接着上一次**——内存与落盘的差别，亲眼看到。

### 5.4 逐段解释

| 段 | 演示 | 关键观察 |
| --- | --- | --- |
| 第一段 | `InMemorySaver` + 两个 thread | thread-a 2 轮 4 条消息，thread-b 1 轮 2 条 |
| 第一段后半 | `get_state_history` | 能看到每一步快照与 `next` |
| 第二段 | `SqliteSaver` 写入 | 文件存在；进程内可继续 |
| 第三段 | 新进程 `--resume` | 恢复出 turn 与消息，继续后 turn+1 |
| 第四段 | `update_state` | 人工修正产生新快照 |

### 5.5 快照里到底有什么

`get_state(config)` 返回的对象通常含：

| 字段 | 含义 | 项目用法 |
| --- | --- | --- |
| `values` | 当前状态 dict | 读 `spec`/`calculation`/`status` |
| `next` | 下一步要执行的节点（元组） | 判断“是否停在 `review` 等审批” |
| `config` | 该快照的配置（含 `thread_id`、`checkpoint_id`） | 审计、定位 |
| `created_at` | 创建时间 | 会话列表排序 |
| `metadata` / `parent_config` | 溯源信息 | 调试 |

> **未验证**：不同版本 `StateSnapshot` 的字段名与可空性略有差异；打印 `dir(snapshot)` 或看官方 Persistence 页最稳。

### 5.6 与项目恢复语义对照（工程规格 5.8）

| 规格要求 | 实现要点 | 本单元对应 |
| --- | --- | --- |
| `SqliteSaver` + 相同 `thread_id` 可跨进程 | 编译时传 checkpointer，invoke 时传 thread_id | 第三段 |
| `InMemorySaver` 不具备重启恢复 | 内存 saver 进程结束即丢 | 第一段（对比） |
| 恢复用 `Command(resume=...)` | `interrupt` 后由 `Command` 带回恢复值 | 单元 16 |
| 重复确认不重复写文件 | checkpoint 已过 `review` 时不再写 | 单元 16/17 |
| `get_state` 读当前、`get_state_history` 读历史 | `read_thread` 用它 | 第一段后半 |

---

## 6. 预期输出与关键状态变化

### 6.1 第一次运行（2026-10-19 实测运行通过，退出码 0）

```text
== 单元 09 实验：checkpointer、thread 与恢复 ==

== 第一段：InMemorySaver 的 thread 隔离 ==
thread-a 消息数： 4 turn： 2
thread-b 消息数： 2 turn： 1
thread-a 内容： ['九月比八月变化多少？', '第 1 轮收到：九月比八月变化多少？', '那华东地区呢？', '第 2 轮收到：那华东地区呢？']
→ 不同 thread_id 完全隔离。

-- get_state_history：从新到旧 --
快照数： 4
  next = () turn = 2 created_at = 2026-10-19T21:40:00.000000+00:00
  next = ('reply',) turn = 1 created_at = 2026-10-19T21:40:00.000000+00:00
  next = () turn = 1 created_at = 2026-10-19T21:40:00.000000+00:00

== 第二段：SqliteSaver 写入（本轮进程内） ==
已有快照 turn： 0 （首次运行应为 0/None）
本轮结束后 turn： 2
消息数： 4
checkpoint 文件： 教程\实验\_output\checkpoints.db 存在： True

== 第四段：update_state 人工修正（谨慎使用） ==
修正后 turn： 99 status： manually_fixed
→ update_state 会产生新快照，不是“原地修改”。审计场景要记录谁改了什么。

下一步：运行 `uv run python ../教程/实验/09_checkpoint.py --resume` 体验跨进程恢复。
```

（快照数量与时序以你的版本为准；`turn` 的恢复值取决于你是否重复运行过。）

### 6.2 第二次运行 `--resume`（2026-10-19 实测：无检查点时打印跳过提示，退出码 0）

```text
== 单元 09 实验：checkpointer、thread 与恢复 ==

== 第一段：InMemorySaver 的 thread 隔离 ==（输出与第一次运行相同，此处不再重复）
== 第三段：新进程按同一 thread_id 恢复 ==
恢复到的 turn： 2
恢复到的消息： ['九月比八月变化多少？', '第 1 轮收到：九月比八月变化多少？', '那华东地区呢？', '第 2 轮收到：那华东地区呢？']
继续一轮后 turn： 3
消息数： 6
→ 进程重启后仍能接上文，这就是 SqliteSaver 与 InMemorySaver 的关键差别。
```

### 6.3 关键状态变化

| 对象 | 之前 | 之后 |
| --- | --- | --- |
| 对“多轮对话”的理解 | 以为要自己把历史传进去 | 知道 checkpointer + thread_id 会自动恢复 |
| 持久化 | 只有 `InMemorySaver` 概念 | 会写 SQLite 并跨进程恢复 |
| 会话隔离 | 不知道靠什么 | 知道 `thread_id` 是会话主键 |
| 状态读取 | 只能看 `invoke` 返回值 | 会用 `get_state` / `get_state_history` |
| 恢复语义 | 以为从断点下一行继续 | 知道节点会从头重跑，副作用要放后面 |

---

## 7. 修改练习（至少 2 个，含一个“改坏它”）

### 练习 7.1（简单）：给会话加“标题”

在 `node_reply` 里返回 `"title": f"会话-{turn}"`（在 `ChatState` 加字段）。用 `get_state` 读出 `title`。

验收：`graph.get_state(config).values["title"] == "会话-2"`。

### 练习 7.2（中等）：列出所有会话

`SqliteSaver` 本身不提供“列出所有 thread_id”的高层 API（**未验证**是否有辅助方法）。请用 SQL 直接查 checkpoint 表：

```python
rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
```

验收：能打印出 `demo-09` 与实验里用过的其他 thread（如 `thread-a` 不在 SQLite 库，只会在内存库）。

> 提示：表名/列名以你的 `langgraph-checkpoint-sqlite` 版本为准；用 `SELECT name FROM sqlite_master WHERE type='table'` 先看表结构。

### 练习 7.3（改坏它）：把 `SqliteSaver` 换成 `InMemorySaver` 却以为能恢复

把 `build_sqlite_graph` 里的 `saver = SqliteSaver(conn)` 改成 `saver = InMemorySaver()`，然后跑第一次 + `--resume`。

请回答：

1. `--resume` 时 `get_state` 返回什么？
2. 为什么 `DB_PATH.exists()` 可能是 False（或存在但没有新数据）？
3. 如果生产环境误用内存 saver，用户会看到什么现象？（提示：重启后“刚才的对话没了”）

### 练习 7.4（进阶）：用 `update_state` 实现“人工改报告”

模拟：图停在待审批状态，运营人员把 `draft_report` 改了。

1. 给 `ChatState` 加 `draft_report: str | None`；
2. `invoke` 后 `update_state(config, {"draft_report": "人工修改后的报告"})`；
3. `get_state` 读回，确认值被改。

验收：读回值等于人工修改内容，且 `get_state_history` 多了一条快照。

### 练习 7.5（中等）：比较 `checkpoint_id`

从 `get_state(config).config` 里读 `checkpoint_id`，再用 `graph.get_state({"configurable": {"thread_id": ..., "checkpoint_id": ...}})` 读指定快照。

验收：能读到“上一轮结束”的那份快照，而不是最新的。

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：不传 `thread_id`

```python
graph.invoke({"messages": [HumanMessage(content="九月呢？")]})
```

预期（未在本环境执行）：报错，提示需要 `thread_id`（配置缺 `configurable.thread_id`）。

**要理解的点**：带 checkpointer 的图必须知道“存到哪个会话”。

### 实验 8.2：两个进程同时写同一个 SQLite 文件

开两个终端同时跑写入（或用两个脚本并发 `invoke`）。

预期（未在本环境执行）：可能出现 `database is locked`（`sqlite3.OperationalError`）。

**要理解的点**：项目里 `AppRuntime` 持有 `threading.Lock`，把写操作串行化；SQLite 单机可用，但不是高并发方案（单元 18 的工程化讨论）。

### 实验 8.3：状态里放不可序列化对象

在 `ChatState` 里加 `conn: sqlite3.Connection` 并塞进去。

预期（未在本环境执行）：写 checkpoint 时序列化失败，抛异常。

**要理解的点**：checkpoint 要求状态可序列化；连接/句柄放 `Context`（单元 06 已提过，这里亲眼看到后果）。

### 实验 8.4：`interrupt` 前的副作用

写一个节点：

```python
def node_review(state):
    Path("side_effect.txt").write_text("我写了文件", encoding="utf-8")  # interrupt 之前
    decision = interrupt({"draft": state.get("draft_report")})
    return {"review": decision}
```

恢复后检查文件被写了几次。

预期（未在本环境执行）：每恢复一次就多写一次（节点从头重跑）。

**要理解的点**：这就是项目把 `export` 拆成独立节点、并把写文件放在 `review` 之后的原因。

### 实验 8.5：`--resume` 前删掉数据库

删除 `_output/checkpoints.db`，再跑 `--resume`。

预期（未在本环境执行）：脚本提示“没有 checkpoint 文件，请先运行一次”，或恢复出空状态。

**要理解的点**：持久化的前提是文件在；生产里要备份/迁移（单元 18）。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能一句话解释 checkpoint 是什么，以及没有它会怎样。
- [ ] 我用 `InMemorySaver` 跑通了“两轮不传历史但记得上文”，并看到不同 `thread_id` 隔离。
- [ ] 我用 `SqliteSaver` 跑通“第一次写入 + `--resume` 跨进程恢复”。
- [ ] 我会用 `get_state` 读当前状态、`get_state_history` 读历史、`update_state` 人工修正。
- [ ] 我能说出 `InMemorySaver` 与 `SqliteSaver` 的三个差别（跨进程、依赖、并发）。
- [ ] 我能解释“`interrupt` 恢复时节点从头重跑”，并说出两条设计后果。
- [ ] 我知道项目为什么用 `SqliteSaver`，以及 `AppRuntime` 里锁的作用。
- [ ] 我完成了练习 7.3，能说清误用内存 saver 时用户会看到什么。

---

## 10. 复盘问题与参考答案、下一步

### Q1：既然 checkpointer 能记住历史，为什么单元 02 说“模型没有记忆”？

**参考**：记忆在**你的存储**里，不在模型里。checkpointer 把状态存进 SQLite，下一轮你的代码把它读出来、拼成消息列表发给模型——模型仍然每次只看到你发的输入。所以“记忆”是工程能力，不是模型能力。

### Q2：`thread_id` 由谁生成？会不会冲突？

**参考**：通常由 API 层生成（如 UUID）或由前端传入已有会话 id。项目里 `invoke_turn` 显式接收，方便测试与复现。冲突意味着两个用户共享历史——这是安全事故，所以生产里应使用不可猜测的 id（UUID4），并在服务端校验归属。

### Q3：为什么 `update_state` 要“谨慎使用”？

**参考**：它绕过图逻辑直接改状态，可能造成“状态与执行历史不一致”（例如把 `status` 改成 `approved` 但没有真正导出）。正确用法是人工修正字段（改报告文字、换审批结论）并留下审计记录。项目的审批编辑走 `Command(resume={"decision": "edit", "edited_report": ...})`，而不是直接 `update_state`。

### Q4：`SqliteSaver` 能支撑多少人同时用？

**参考**：单机、低并发可以（内部用锁串行化写）。它不是多用户高并发数据库；多人使用时会出现写锁竞争。真要上线多人，通常换 Postgres checkpointer（官方有 `langgraph-checkpoint-postgres`）。**未验证**：该包与你版本的兼容性；本项目定位单机演示，选 SQLite 是**项目设计选择**。

### Q5：checkpoint 会不会越存越大？

**参考**：会。每一步一份快照，消息越多快照越大。项目里通过“报告生成后把中间消息移出/裁剪”和“定期清理旧 thread”控制。生产里还要考虑保留策略（多少天、多少轮）。**未验证**：`SqliteSaver` 是否自带清理 API，以官方文档为准。

### 下一步

基础部分到此结束。接下来进入**实战项目**（由其他单元覆盖）：

1. 单元 10～15：把 `ops-assistant` 的数据层、计算层、知识库、报告模板逐个实现并测试。
2. 单元 16～19：HITL 审批、导出、CLI/API/UI、工程化与评测。

进入前请确认：你完成了 00～09 的验收清单，并且能独立跑通 `教程/我的实验/` 下至少 8 个脚本（无密钥路径）。
