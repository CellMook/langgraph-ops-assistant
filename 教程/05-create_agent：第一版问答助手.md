# 单元 05：create_agent：第一版问答助手

> 建议时长 55 分钟 | 前置：单元 04 | 可跳过条件：能说清 `create_agent` 帮你做了什么、没帮你做什么，并能判断一个需求该用 Agent 还是固定步骤
>
> 版本基线：`langchain 1.4.6`（`create_agent` 位于 `langchain.agents`）（核对日期 2026-10-19）
>
> 本单元实验在**没有密钥**时会走“结构与规则”分支：打印 agent 的组成、跑确定性的业务回答。有密钥时才会真正运行 agent。所有输出为“预期（未在本环境执行）”。

---

## 1. 学习目标与前置

完成后你应该能：

1. 用一行 `create_agent(model=..., tools=..., system_prompt=...)` 建出第一版问答助手，并解释它内部是什么（一个带模型节点与工具节点的图）。
2. 用“**Model + Harness**”这个框架描述它的定位：模型负责“想”，harness（脚手架）负责“循环、绑定工具、管消息、接状态、接持久化”。
3. 说出 `create_agent` **没有**替你做的事：业务规则校验、预算、审批、失败出口、报告渲染、引用核对。
4. 判断一个需求该用 `create_agent`（探索型、路径不定）还是固定代码步骤（数字必须确定、要审计、要测试）。
5. 跑通项目的 `build_simple_agent` / `run_simple_agent` 契约，并解释 `used_fallback=True` 时的行为。

前置：单元 04 完成，能手写工具循环并解释 `tool_calls`。

---

## 2. 官方文档与阅读问题

**官方文档：Agents（https://docs.langchain.com/oss/python/langchain/agents，核对日期 2026-10-19）**

**官方文档：Tools（https://docs.langchain.com/oss/python/langchain/tools，核对日期 2026-10-19）**

带着这些问题读：

1. 官方 Agents 页给的最小示例长什么样？`create_agent` 的必填参数是哪几个？
2. 官方是怎么描述 `create_agent` 返回值的？（是一个可 `invoke` 的对象？是一个编译后的图？）
3. 官方有没有说可以传 `checkpointer` / `state_schema` / `middleware`？分别在什么场景用？
4. 官方示例里怎么读取最终答案？（`result["messages"][-1].content`？）
5. 官方有没有提醒“Agent 适合开放任务，固定流程用图/工作流更好”？

> **官方事实**：`create_agent` 是 LangChain 1.x 的高层智能体构造函数（`from langchain.agents import create_agent`），接收模型与工具，返回一个可调用对象；官方把它定位为“用少量代码获得一个工具调用循环”。
>
> **未验证**：`create_agent` 的具体参数名（如 `system_prompt` 还是 `prompt`）、返回值类型名、是否默认带 checkpointer，随版本可能变化。本文按 1.4.6 的公共用法书写；若你的版本报 `TypeError: unexpected keyword argument`，请以官方 Agents 页当前示例为准，并把它记进你的学习笔记。

---

## 3. 通俗解释与精确技术定义

### 3.1 把 Agent 拆成“Model + Harness”

面试里最容易被问倒的一句话是“你这个 Agent 是怎么工作的”。可以这样回答：

```
Agent = Model（模型，负责决定下一步）
      + Harness（脚手架，负责把决定变成动作）
```

Harness 具体做五件事：

| 职责 | 通俗说 | `create_agent` 里的对应 |
| --- | --- | --- |
| 工具绑定 | 告诉模型有哪些工具可用 | 把 `tools` 序列化成工具声明 |
| 循环驱动 | 模型要工具 → 执行 → 再问模型 | 内部的“模型节点 + 工具节点 + 条件边” |
| 消息管理 | 把 `AIMessage` / `ToolMessage` 追加进历史 | 内部状态里的 `messages` |
| 状态与记忆 | 多轮对话、断点续跑 | 可选 `checkpointer` + `thread_id` |
| 可观测 | 看到每一步在干什么 | 流式事件、回调、`middleware` |

**所以 `create_agent` 不是“更聪明的模型”**，它是把单元 04 你手写的那套循环标准化了。模型能力不变，变的是你不用重复写循环。

### 3.2 它不做什么（本项目最在意的部分）

| 不做 | 后果 | 本项目怎么补 |
| --- | --- | --- |
| 不懂你的业务口径 | 会把“销售额”当毛销售额 | 口径写进系统提示词 + 知识库引用 + 程序计算 |
| 不校验数字 | 模型可能心算错 | 数字全部来自工具/纯计算，模型只复述 |
| 不控制预算 | 可能反复调工具烧钱 | 图里加 `budget` + `max_tool_calls`（单元 08） |
| 不做审批 | 报告可能未经人看就发出 | `review` 节点 + `interrupt`（单元 16） |
| 不保证可恢复 | 进程一挂全丢 | `SqliteSaver`（单元 09、17） |
| 不生成可审计报告 | 只有一段聊天记录 | 模板渲染 + `sources` + 导出（单元 17） |

一句话：**`create_agent` 给你一个能跑的原型，不给你一个能交付的产品。**

### 3.3 什么时候该固定代码步骤

| 判断标准 | 用 `create_agent` | 用固定图/代码 |
| --- | --- | --- |
| 下一步做什么是否确定 | 不确定，看情况选工具 | 确定，步骤固定 |
| 结果是否必须逐位正确 | 定性分析、草稿 | 金额、比例、日期（**必须**） |
| 是否要审计“谁算了什么” | 不太要 | 必须可追溯 |
| 是否要单测 | 难测（模型不确定） | 好测（纯函数） |
| 出错的代价 | 说错一句话 | 发错一份财务报告 |

本项目两类都用，但**分工明确**：

- 探索型问答（“你能分析哪些维度？”“这份资料怎么说的？”）→ `create_agent` 简版。
- 正式分析流程（解析 → 查数 → 计算 → 校验 → 报告 → 审批 → 导出）→ 自己画的图（单元 06 起）。

> **项目设计选择**：`ops-assistant` 的“正式路径”不依赖 `create_agent` 的自主决策，因为财务数字必须可复现、可测试。`create_agent` 只作为教学对照与可选的自然语言入口。代价是代码量更大；收益是每个数字都能指到具体函数与 SQL。

### 3.4 术语（双语）

- **智能体（agent）**：模型可自主决定调用哪些工具、按什么顺序。
- **脚手架（harness）**：围绕模型的那层工程代码，负责循环、工具、状态、日志、预算。
- **工具调用循环（tool-calling loop）**：模型请求 → 执行 → 回传 → 再请求，直到模型给出最终答案或无预算。
- **中间件（middleware）**：在模型/工具调用前后插入逻辑（日志、截断、重试、权限）。
- **回退（fallback）**：模型不可用时退到确定性路径。

---

## 4. 这个知识在主项目中的位置

| 项目位置 | 内容 |
| --- | --- |
| `src/ops_assistant/agent/simple_agent.py` | `build_simple_agent(llm, tools, checkpointer=None) -> CompiledStateGraph \| None`；模型不可用返回 `None` |
| 同上 | `run_simple_agent(agent, thread_id, question) -> {"answer": str, "used_fallback": bool, "sources": list[dict]}` |
| 同上 | 回退时调用 `domain/` 的规则解析 + 计算，返回 `used_fallback=True` |
| `src/ops_assistant/agent/tools.py` | 传入的工具（单元 04 的六个） |
| `src/ops_assistant/config.py` | `llm_enabled` 决定传不传模型 |
| `src/ops_assistant/api/main.py` | API 层（单元 19）；聊天端点走**图**，不走这个简版 |

**为什么项目里 `build_simple_agent` 可能返回 `None`**：这不是“代码没写完”，而是契约：模型不可用时，调用方必须显式处理回退，而不是让程序崩。`run_simple_agent` 的 `used_fallback` 字段就是给 UI 提示“本次答案来自规则模式”的。

> **接口以工程规格 5.5 为准**；项目源码由团队成员并行实现，本单元只引用这些确定的名字与返回字段，不猜测内部实现细节。

---

## 5. 文件、完整代码、配置与命令

### 5.1 最小用法（3 行）

```python
from langchain.agents import create_agent
from langchain_core.tools import tool


@tool
def calculate_change(current_cents: int, previous_cents: int) -> str:
    """计算两期净销售额的差额与变化率，输入输出单位都是分。"""
    import json

    delta = current_cents - previous_cents
    pct = None if previous_cents == 0 else round(delta / abs(previous_cents), 4)
    return json.dumps({"ok": True, "data": {"delta_cents": delta, "pct_change": pct}})


agent = create_agent(
    model=model,  # 单元 01 里构造的 ChatOpenAI
    tools=[calculate_change],
    system_prompt="你是经营数据分析助手。需要数字时必须调用工具，禁止心算。",
)
result = agent.invoke({"messages": [{"role": "user", "content": "九月比八月净销售额变化多少？"}]})
print(result["messages"][-1].content)
```

要点：

- 输入是 `{"messages": [...]}`，**消息要用 dict 或消息对象**，不是纯字符串。
- 输出里 `result["messages"]` 是完整历史（含工具消息），最后一条通常是最终答案。
- 没传 `checkpointer` 时**不保存历史**：每次 `invoke` 都是新会话（单元 09 细讲）。

### 5.2 有密钥时的完整实验（完整代码）

保存路径：`教程/实验/05_simple_agent.py`

```python
"""单元 05 实验：用 create_agent 搭第一版问答助手（含无密钥回退）。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/05_simple_agent.py
有密钥：真实运行 agent；无密钥：打印 agent 组成 + 走确定性规则回答。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "ops-assistant" / ".env"

FAKE_MONTHS = {
    "2026-09": {"gross_cents": 1400000, "refund_cents": 113500, "net_cents": 1286500,
                "order_count": 57, "has_orders": True},
    "2026-08": {"gross_cents": 1310000, "refund_cents": 120000, "net_cents": 1190000,
                "order_count": 61, "has_orders": True},
}


@tool
def query_month_sales(month: str) -> str:
    """查询某个月的销售汇总；月份格式 YYYY-MM，金额单位分。

    Args:
        month: 月份，例如 "2026-09"。
    """
    data = FAKE_MONTHS.get(month)
    if data is None:
        return json.dumps(
            {
                "ok": False,
                "error": {"code": "NO_DATA", "message": f"没有 {month} 的数据"},
                "hint": "可用月份：2026-08、2026-09",
            },
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, "data": data, "unit": "cents"}, ensure_ascii=False)


@tool
def calculate_change(current_cents: int, previous_cents: int) -> str:
    """计算两期净销售额的差额与变化率；previous 为 0 时 pct_change 为 null。

    Args:
        current_cents: 本期净销售额，单位分。
        previous_cents: 上期净销售额，单位分。
    """
    delta = current_cents - previous_cents
    pct = None if previous_cents == 0 else round(delta / abs(previous_cents), 4)
    return json.dumps(
        {"ok": True, "data": {"delta_cents": delta, "pct_change": pct}, "unit": "cents"},
        ensure_ascii=False,
    )


TOOLS = [query_month_sales, calculate_change]
SYSTEM_PROMPT = (
    "你是经营数据分析助手。规则："
    "1) 金额单位是分；2) 需要数字时必须调用工具，禁止心算；"
    "3) 工具返回 ok=false 时如实告知，不要编造；"
    "4) 回答先给结论，再给数字依据。"
)
QUESTION = "2026 年九月净销售额比八月变化多少？"


def mask(secret: str | None) -> str:
    if not secret:
        return "未设置"
    return f"{secret[:4]}…{secret[-2:]}"


def load_model():
    """返回 (model, description)；不可用时 model 为 None。"""
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    raw_model = (os.getenv("OPS_MODEL") or "").strip()
    api_key = (os.getenv("OPS_API_KEY") or "").strip() or os.getenv("OPENAI_API_KEY")
    base_url = (os.getenv("OPS_BASE_URL") or "").strip() or None
    if not raw_model or not api_key:
        return None, "未配置 OPS_MODEL / OPS_API_KEY"
    provider, model_name = "openai", raw_model
    if ":" in raw_model:
        provider, _, model_name = raw_model.partition(":")
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=model_name, api_key=api_key, base_url=base_url,
        temperature=0.0, timeout=60.0, max_retries=0,
    )
    return model, f"{provider}:{model_name}（key={mask(api_key)}）"


def rule_fallback(question: str) -> dict:
    """无模型时的确定性回答：解析月份 → 查数 → 算差额 → 组装文字。"""
    current = "2026-09"
    previous = "2026-08"
    cur = FAKE_MONTHS[current]["net_cents"]
    prev = FAKE_MONTHS[previous]["net_cents"]
    delta = cur - prev
    pct = round(delta / prev, 4)
    answer = (
        f"【规则模式·used_fallback=True】{current} 净销售额 {cur} 分，"
        f"{previous} 净销售额 {prev} 分，差额 {delta} 分，变化率 {pct:.2%}。"
        f"口径：净销售额 = 毛销售额 − 已批准退款；本条回答由程序计算，未调用模型。"
    )
    sources = [
        {"type": "sql", "month": current, "field": "net_cents", "value": cur},
        {"type": "sql", "month": previous, "field": "net_cents", "value": prev},
        {"type": "calc", "formula": "delta = current - previous", "value": delta},
    ]
    return {"answer": answer, "used_fallback": True, "sources": sources}


def describe_agent(model_description: str) -> None:
    print("== Agent 组成（Model + Harness）==")
    print(f"  模型（Model）：{model_description}")
    print(f"  工具（tools）：{[item.name for item in TOOLS]}")
    print(f"  系统提示词长度：{len(SYSTEM_PROMPT)} 字")
    print("  Harness 提供：工具绑定 / 工具调用循环 / 消息管理 / 可选 checkpointer")
    print("  Harness 不提供：业务校验 / 预算 / 审批 / 失败出口 / 报告导出")


def run_with_agent(model) -> dict:
    from langchain.agents import create_agent

    agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
    result = agent.invoke({"messages": [{"role": "user", "content": QUESTION}]})
    messages = result["messages"]
    tool_calls = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append({"name": call.get("name"), "args": call.get("args")})
    return {
        "answer": messages[-1].content,
        "used_fallback": False,
        "message_count": len(messages),
        "tool_calls": tool_calls,
    }


def main() -> int:
    print("== 单元 05 实验：create_agent 第一版问答助手 ==")
    print(f"问题：{QUESTION}\n")

    model, description = load_model()
    describe_agent(description)

    if model is None:
        print(f"\n[回退] {description}；运行规则模式，仍可得到正确业务结论：")
        result = rule_fallback(QUESTION)
        print(f"\n回答：{result['answer']}")
        print(f"used_fallback={result['used_fallback']}")
        for source in result["sources"]:
            print(f"  source: {source}")
        print("\n[提示] 配置密钥后重跑，可看到 create_agent 的真实输出与工具调用记录。")
        return 0

    print("\n[运行] create_agent + invoke ...")
    try:
        result = run_with_agent(model)
    except Exception as exc:
        print(f"[失败] {type(exc).__name__}: {exc}")
        print("排查：1) create_agent 参数名是否与你的版本一致；2) 模型是否支持工具调用；3) 端点是否可用。")
        return 1

    print(f"\n消息条数：{result['message_count']}")
    print("工具调用记录：")
    for call in result["tool_calls"]:
        print(f"  - {call['name']}({call['args']})")
    print(f"\n最终回答：{result['answer']}")
    print("\n[观察] 回答里的数字应当来自工具结果；若模型自己算了，说明提示词约束不够，需加强校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

运行：

```powershell
cd "D:\MySpace\LangChain学习路线\ops-assistant"
uv run python ../教程/实验/05_simple_agent.py
```

### 5.3 项目的 `build_simple_agent`：契约行为

工程规格 5.5 定的契约（**以项目源码为准**）：

```python
def build_simple_agent(llm, tools, checkpointer=None):
    """返回编译后的 agent 图；llm 为 None 时返回 None（调用方走回退）。"""


def run_simple_agent(agent, thread_id: str, question: str) -> dict:
    """返回 {"answer": str, "used_fallback": bool, "sources": list[dict]}。"""
```

调用方（例如 API 层或教学脚本）必须写成：

```python
agent = build_simple_agent(llm, tools, checkpointer=saver)
result = run_simple_agent(agent, thread_id="demo-thread", question="九月比八月变化多少？")
if result["used_fallback"]:
    print("注意：本次回答来自规则模式，不是模型。")
```

三个关键点：

1. `agent is None` 不等于失败，而是“请走回退”。
2. `used_fallback` 必须透传到 UI/API，否则用户会以为看到的是模型答案。
3. `thread_id` 即使简版 agent 也保留参数，方便以后接 checkpointer 时不改调用方。

### 5.4 `create_agent` 与“自己画图”的代码量对比（定性）

| 需求 | `create_agent` | 自己画图 |
| --- | --- | --- |
| 一个工具循环 | 约 5 行 | 约 80 行（单元 04 手写版） |
| 加预算限制 | 需要 middleware 或自己包一层 | 一个 `route` 函数 + 一个状态字段 |
| 加人工审批 | 需要 interrupt 支持 | 一个节点 + `interrupt` |
| 加失败出口 | 要在外层 try/except 里判断 | 一个 `fail` 节点 + 条件边 |
| 单测 | 难（模型不确定） | 容易（节点是纯函数） |
| 数字可复现 | 不保证 | 保证（数字来自计算节点） |

这张表的结论：**原型用 `create_agent`，产线用图**。本书后面的章节会把这句话反复兑现。

---

## 6. 预期输出与关键状态变化

### 6.1 无密钥（预期，未在本环境执行）

```text
== 单元 05 实验：create_agent 第一版问答助手 ==
问题：2026 年九月净销售额比八月变化多少？

== Agent 组成（Model + Harness）==
  模型（Model）：未配置 OPS_MODEL / OPS_API_KEY
  工具（tools）：['query_month_sales', 'calculate_change']
  系统提示词长度：96 字
  Harness 提供：工具绑定 / 工具调用循环 / 消息管理 / 可选 checkpointer
  Harness 不提供：业务校验 / 预算 / 审批 / 失败出口 / 报告导出

[回退] 未配置 OPS_MODEL / OPS_API_KEY；运行规则模式，仍可得到正确业务结论：

回答：【规则模式·used_fallback=True】2026-09 净销售额 1286500 分，2026-08 净销售额 1190000 分，差额 96500 分，变化率 8.11%。口径：净销售额 = 毛销售额 − 已批准退款；本条回答由程序计算，未调用模型。
used_fallback=True
  source: {'type': 'sql', 'month': '2026-09', 'field': 'net_cents', 'value': 1286500}
  source: {'type': 'sql', 'month': '2026-08', 'field': 'net_cents', 'value': 1190000}
  source: {'type': 'calc', 'formula': 'delta = current - previous', 'value': 96500}

[提示] 配置密钥后重跑，可看到 create_agent 的真实输出与工具调用记录。
```

### 6.2 有密钥（预期，未在本环境执行，文字由模型决定）

```text
[运行] create_agent + invoke ...

消息条数：6
工具调用记录：
  - query_month_sales({'month': '2026-09'})
  - query_month_sales({'month': '2026-08'})
  - calculate_change({'current_cents': 1286500, 'previous_cents': 1190000})

最终回答：2026 年九月净销售额为 1286500 分，八月为 1190000 分，差额 96500 分，增长约 8.11%。净销售额已扣除退款。
```

`消息条数 6` = 1 条用户 + 1 条带 2 个工具请求的 `AIMessage` + 2 条 `ToolMessage` + 1 条带 1 个工具请求的 `AIMessage` + 1 条 `ToolMessage` + 最终答案，具体条数取决于模型一次性请求几个工具，**不要背这个数字**。

### 6.3 关键状态变化

| 对象 | 之前 | 之后 |
| --- | --- | --- |
| 对 Agent 的认识 | “神秘的黑盒” | “Model + Harness：模型决策 + 脚手架执行” |
| 代码量 | 手写循环 80 行 | 5 行，但要接受它不保证业务正确 |
| 回退设计 | 程序崩 | `build_simple_agent` 返回 `None` + `used_fallback` |
| 选型判断 | 一律上 Agent | 按“确定性/可审计/可测试”三条判断 |
| 项目定位 | 不知道简版 agent 干嘛用 | 教学对照 + 可选自然语言入口 |

---

## 7. 修改练习（至少 2 个，含一个“改坏它”）

### 练习 7.1（简单）：换系统提示词，观察行为差异

把 `SYSTEM_PROMPT` 里的“禁止心算”删掉，重跑（有密钥时）。

请记录：模型是否还调用 `calculate_change`？回答里的百分比是否与 8.11% 一致？

**要理解的点**：提示词影响“是否调用工具”，但**不能保证**。真正的保证来自“让模型没有机会算”——例如只提供工具、不提供原始数字（本项目做法：让工具返回结果，模型只读结论）。

### 练习 7.2（中等）：给 agent 加 `checkpointer`，做两轮追问

```python
from langgraph.checkpoint.memory import InMemorySaver

saver = InMemorySaver()
agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT, checkpointer=saver)
config = {"configurable": {"thread_id": "demo-05"}}
first = agent.invoke({"messages": [{"role": "user", "content": QUESTION}]}, config)
second = agent.invoke({"messages": [{"role": "user", "content": "那华东地区呢？"}]}, config)
print(second["messages"][-1].content)
```

请回答：

1. 第二个问题里没有时间词，模型还能不能答“九月/八月”？
2. `thread_id` 换成 `"demo-05-b"` 后，第二轮还知不知道上文？
3. 重启进程后（把脚本重新跑一次），内存 saver 里的历史还在吗？（提示：`InMemorySaver` 只在进程内）

> **未验证**：`create_agent` 是否接受 `checkpointer` 参数、状态键名是否完全一致，随版本可能变化；报错时查官方 Agents 页。

### 练习 7.3（改坏它）：把工具列表清空

把 `tools=TOOLS` 改成 `tools=[]`，保留原提示词里的“需要数字时必须调用工具”。

预期（未在本环境执行）：模型要么回答“我无法查数据”，要么**编一个数字**。

请回答：

1. 它有没有可能编数字？编了你怎么发现？
2. 这说明了什么？（提示：Agent 的能力边界由工具决定，不由提示词决定）
3. 如果产品必须支持“没有工具时也不能编”，你会加什么代码校验？

### 7.4（进阶）：给 `run_simple_agent` 写“契约测试”

不用真实项目，先在本实验里写一个：

```python
def test_run_simple_agent_contract():
    result = rule_fallback("2026 年九月销售额比八月变化多少？")
    assert set(result) == {"answer", "used_fallback", "sources"}
    assert result["used_fallback"] is True
    assert isinstance(result["sources"], list) and result["sources"]
```

验收：把这段放进 `tests/` 能通过（它是纯函数，不需要密钥）。

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：`create_agent` 参数名不匹配

故意写 `create_agent(model=model, tools=TOOLS, prompt=SYSTEM_PROMPT)`（若当前版本用 `system_prompt`）。

预期（未在本环境执行）：`TypeError: create_agent() got an unexpected keyword argument 'prompt'`。

**要理解的点**：版本差异最先暴露在参数名上。处理方式：读报错 → 查官方 Agents 页当前示例 → 记录到笔记，不要猜。

### 实验 8.2：模型不支持工具调用

用一个不支持 tool calling 的模型跑 6.2。

预期（未在本环境执行）：可能直接回答（不调用工具），也可能报“tool calling not supported”。

**要理解的点**：这时 agent 退化成聊天机器人，**数字可能全错**。项目里的防线是“数字只来自工具”，而不是“相信 agent”。

### 实验 8.3：工具返回 `ok:false`

把 `query_month_sales` 的假数据里删掉 `2026-08`，然后问同样的问题。

预期（未在本环境执行）：模型收到 `NO_DATA`，可能回答“没有八月数据”。也可能继续用九月数字硬算——这是**模型行为**，不是契约。

**要理解的点**：`ok:false` 只是“告知”，不是“阻止”。真正的阻止要在图里做（`data` 节点发现缺失 → `route="fail"`，单元 08）。

### 实验 8.4：空工具结果 + 追问

问“那华南地区呢？”但不提供地区工具。

预期（未在本环境执行）：模型很可能说“没有该数据”，也可能编造。

**要理解的点**：没有工具支撑的维度必须明确“不支持”，这个判断应由程序给出（`dimension=channel → 不支持`），不能指望模型自觉。

### 实验 8.5：把 `system_prompt` 设成空字符串

预期（未在本环境执行）：模型行为变得随意，单位/口径可能出错。

**要理解的点**：Harness 不替你写规则。系统提示词是“产品规则”的一部分，必须写、必须测。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能用一句话解释 `create_agent` 是什么（Model + Harness），并说出 Harness 的五个职责。
- [ ] 我能列出至少四件 `create_agent` **不**做的事，以及本项目分别怎么补。
- [ ] 我能按“确定性/可审计/可测试/出错代价”四条判断该用 Agent 还是固定步骤。
- [ ] 我运行了 `05_simple_agent.py`（无密钥分支也算），看到了 Agent 组成与规则模式回答。
- [ ] 我知道 `build_simple_agent` 在模型不可用时返回 `None`，且 `used_fallback=True` 必须透传给用户。
- [ ] 我完成练习 7.2，能说清 `thread_id` 与 `InMemorySaver` 的作用边界（进程内有效）。
- [ ] 我完成练习 7.3，能说清“工具为空时模型可能编数字”，并给出至少一条代码级防护。
- [ ] 我能说出项目为什么把正式分析流程画成图，而不是交给 `create_agent` 自主决策。

---

## 10. 复盘问题与参考答案、下一步

### Q1：`create_agent` 与单元 04 的手写循环，本质区别是什么？

**参考**：**没有本质区别**。手写循环是“模型节点 + 工具节点 + 条件边 + 消息状态”的最小实现；`create_agent` 是同一结构的标准化封装，外加中间件、流式、可选的持久化。会手写才知道哪里可能出问题；会用封装才能少写重复代码。

### Q2：为什么本项目不把整个报告流程交给 Agent？

**参考**：报告流程有强顺序（先解析再查数再算再校验再渲染）、强正确性（金额逐位）、强审计（每个数字要能指到 SQL）。Agent 的价值在“路径不确定”；这些环节路径完全确定。用 Agent 反而牺牲可测试性与可复现性。**项目设计选择**：Agent 只做“探索型问答”入口。

### Q3：`used_fallback=True` 为什么重要？

**参考**：因为它决定了用户看到的答案是“模型说的”还是“程序算的”。在财务场景，混淆两者是事故。API 层要把它透传，UI 要显示提示，日志要记录。这也是可观测性的一部分。

### Q4：如果老板要求“全部用 Agent，不要写死流程”，你怎么回应？

**参考**：先承认 Agent 的灵活价值，再用三个问题把讨论拉回工程：数字必须逐位正确吗？出错要能追溯到哪一步吗？要不要单测？如果都要，就必须有确定性节点；Agent 可以作为“入口/助手”，但不能替代计算与校验。这不是技术偏好，是责任边界。

### Q5：`create_agent` 的输出为什么是 `{"messages": [...]}` 而不是一个字符串？

**参考**：因为它保留了完整执行轨迹：用户消息、带 `tool_calls` 的 `AIMessage`、`ToolMessage`、最终答案。这些是审计与调试的依据。只取最后一条 `content` 会丢掉“它到底调了什么”，在生产里等于自断证据。

### 下一步

进入 **单元 06：StateGraph：状态、节点、边**。你会：

1. 自己定义 State schema（`TypedDict` + 字段语义）。
2. 写第一个节点函数：接收状态、返回**部分更新**。
3. 用 `START` / `END` 连一条直线图，`compile()` 后 `invoke()`。
4. 把“九月 vs 八月”这个业务问题拆成 3 个节点，观察状态如何一步步长出来。

进入前请确认：`uv run python ../教程/实验/05_simple_agent.py` 能跑完（无密钥走回退分支），且你能说出 `used_fallback` 的含义。
