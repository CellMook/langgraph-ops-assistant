# 单元 16：人工审批：interrupt 与恢复

> **2026-10-20 修订**：本章迷你图只演示 interrupt 生命周期；固定 `report-demo.md` 不具备主项目的版本隔离。主项目 API 必须回传 `report_id` + `interrupt_id`，可附 `content_hash`；无效编辑保持待审批。版本化验收见 [单元 23](23-契约与版本化验收.md)。

> 建议时长：**60 分钟**（概念 15 分钟 + 项目代码 15 分钟 + 实验 20 分钟 + 改坏与边界 10 分钟）
> 前置：单元 09（Checkpointer、thread 与恢复）、单元 15（多轮记忆、Store 与 Runtime Context）；单元 06/07 的"节点只返回增量"是本节的前提。
> 可跳过条件：你能说清"`interrupt()` 被调用时图发生了什么、恢复时节点执行了几次、副作用代码应该放在 `interrupt()` 前还是后"，并知道 `Command(resume=...)` 的两种写法——那本单元只需做第 7、8 节。
> 版本基线：`langchain==1.4.6`、`langgraph==1.2.16`（PyPI 元数据核对日期 **2026-10-19**）；本单元实验**不调用任何外部模型**，不需要 API Key。
> 事实标注约定：**官方事实**（有官方文档或可核对来源）、**教学简化**（为讲清楚而压缩的模型，不是官方定义）、**项目设计选择**（本项目自己的取舍，不是通用最佳实践）、**未验证**（本环境未执行或未逐句核对）。
> 执行说明：本环境**不能运行 Python**。所有"预期输出"都写成 **预期（未在本环境执行）**，请你按第 5 节命令在自己机器上核对。

---

## 1. 学习目标与前置

学完本单元，你应该能做到下面 6 件事：

1. **解释 `interrupt()` 的真实语义**：它不是"弹一个对话框然后返回结果"，而是"抛出一个可恢复的暂停"——图把当前状态写成检查点后停下；恢复时**从这个节点重新执行**，`interrupt()` 这次直接返回恢复值。
2. **说出"恢复时节点会被重跑"的两个后果**：`interrupt()` 之前的代码会执行第二次；任何有副作用的代码放在 `interrupt()` 之前都会重复发生。
3. **写出恢复的两种调用方式**：`graph.invoke(Command(resume=...), config=...)` 与项目里的 `runtime.resume_turn(thread_id, decision, ...)` 包装。
4. **区分 approve / edit / reject 三条分支**：哪条会写文件、哪条只改状态、`status` 分别是什么、`review` 字段长什么样。
5. **解释"没有待审批就 resume"为什么要报错**：项目抛 `ValueError`（API 层映射为 HTTP 409/422），而不是让图莫名其妙跑一遍。
6. **解释幂等**：同一会话第二次 `approve` 不会重写文件，而是返回第一次的结果（`export_path` 不变）。

前置检查（30 秒）：

- 单元 15 里 `get_state(config).values` 存的是什么？`interrupt` 的载荷存在哪？
- 如果一个节点里 `interrupt()` 之后的代码写了文件，恢复时会不会再写一次？

---

## 2. 官方文档与阅读问题

本单元需要三页官方文档。三个地址我在 **2026-10-19** 抓取过，都返回 `200`；正文被前端框架截断，没有逐句核对，所以下面凡是我写"官方规定"的地方，请点开链接自己确认。

| 链接 | 与本单元的关系 | 读的时候要回答的问题 |
| --- | --- | --- |
| [Interrupts（LangGraph 官方）](https://docs.langchain.com/oss/python/langgraph/interrupts) | `interrupt()` 的语义、恢复方式、重跑行为 | 官方说恢复时节点从哪里开始执行？`interrupt()` 的返回值从哪来？ |
| [Persistence（LangGraph 官方）](https://docs.langchain.com/oss/python/langgraph/persistence) | interrupt 必须配合 checkpointer 才能"停住" | 没有 checkpointer 时 `interrupt()` 会发生什么？ |
| [Command（LangGraph 参考）](https://docs.langchain.com/oss/python/langgraph/graph-api) | `Command(resume=...)` 是恢复入口之一 | `Command` 除了 `resume` 还能带什么？为什么恢复要用 `Command` 而不是普通状态输入？ |

三个阅读问题的"一句话参考答案"放在第 10 节。

> **官方事实**：LangGraph 的人工介入（human-in-the-loop）由 `interrupt()` 与检查点配合实现：图暂停时把状态写入检查点，恢复时从暂停的节点继续（链接见上表，核对日期 2026-10-19）。
>
> **未验证**：同一个节点里连续调用两次 `interrupt()` 的匹配规则（按调用顺序编号）、以及"重跑时 `interrupt()` 之前代码的副作用如何处理"的官方原文表述，本环境没有逐句核对；本单元的实验只使用**一次** `interrupt()`。

---

## 3. 通俗解释与精确技术定义

### 3.1 一句类比：审批单与回执

把 `review` 节点想成一个"制单员"：

1. 他把草稿和摘要填进一张**审批单**（payload），然后把单子递出去，自己**下班**——图在这里停下，检查点记下"停在 review、单子长这样"。
2. 老板在别的系统里批了"通过 / 修改 / 拒绝"（这就是 resume 值）。
3. 第二天制单员**重新上班**：他会把填单子这件事**再做一遍**（因为他是"从头开始执行这个节点"的），但这次递单子的动作会立刻拿到老板的回执，于是继续往下走。

第 3 步的"从头开始"是本单元最重要的一句话：**`interrupt()` 不是 `input()`，恢复不是"从下一行继续"，而是"重跑这个节点、这次 interrupt 直接返回回执"**。

### 3.2 精确一点：一次 interrupt 的完整生命周期

```text
invoke(state)
  └─ draft 节点：渲染报告草稿 → draft_report
  └─ review 节点：
        ① 预算检查（_guard_step）
        ② 校验草稿非空
        ③ 构造 payload（_approval_payload → jsonable）
        ④ interrupt(payload)  ←── 抛出暂停；写检查点；整个 invoke 返回
                                        图状态 status=awaiting_approval
                                        snapshot.next = ("review",)
                                        snapshot.interrupts 里有这个 payload
  ────────────────────────────────────────────────────────────
  外部：人工决定 approve / edit / reject
  resume_turn(thread_id, decision, ...)
        ⑤ graph.invoke(Command(resume={...}), config=...)
        ⑥ review 节点从头重跑：① ② ③ 再执行一次；
           interrupt(payload) 这次**立即返回** resume 值，不再暂停
        ⑦ _parse_resume(resume_value) → decision/comment/edited
        ⑧ 按决策写 review / final_report / status（纯状态更新，无 IO）
        ⑨ route_after_review：approve/edit → export；reject → END
        ⑩ export 节点：唯一的落盘动作（原子写 report-<thread_id>-<report_id>.md）
```

### 3.3 三个必须记住的规则

**规则 1：`interrupt()` 之前的代码必须幂等（可重复执行）**

因为恢复时会重跑。项目在 `review_node` 的 docstring 里把这条写成了硬约束（原文）：

> `interrupt()` 之前只做纯读/纯算，不写文件、不写库，保证节点重跑安全；

所以 `review_node` 在 `interrupt()` 之前只做：预算检查、草稿非空检查、构造 payload。**没有**写文件、**没有**写数据库、**没有**调模型。

**规则 2：payload 必须能 JSON 序列化**

payload 会被写进 SQLite 检查点再读回来。所以项目用 `jsonable()` 先洗一遍（`Path` 转 `str`、`NaN/Inf` 转 `None`、dataclass 转 dict）。如果塞了 `datetime`、`Decimal`、ORM 对象进去，就会出现"第一次能跑，恢复时反序列化炸"的问题。

**规则 3：恢复值要"宽容解析"**

人工输入的形态不可控：可能是裸字符串 `"approve"`，可能是 `{"decision": "edit", "edited_report": "..."}`，也可能是 UI 传来的中文 `"批准"`。所以项目有 `_parse_resume()` 做别名归一；**无法识别时按 reject 处理**（宁可少导出，不可误导出）。

### 3.4 三条分支的精确对照

| 决策 | `status`（review 节点返回） | 是否进 `export` | 文件副作用 | `review` 字段 | 给人看的一句话 |
| --- | --- | --- | --- | --- | --- |
| `approve` | `approved` | 是 | 写 `report-<thread_id>-<report_id>.md` | `{"decision":"approve","comment":...,"edited_report":"","decided_at":...}` | "报告已批准，正在导出文件。" |
| `edit` | `approved` | 是 | 写文件，内容优先用修改稿 | `{"decision":"edit",...,"edited_report": <修改稿或 "">, ...}` | "已收到您的修改稿（或原始草稿），将按其导出报告文件。" |
| `reject` | `rejected` | 否 | 不写 | `{"decision":"reject","comment":...,"edited_report":"","decided_at":...}` | "已按您的要求拒绝本次报告，未导出任何文件。" |
| 无法识别 | 归一成 `reject` | 否 | 不写 | 同上 | 同上，且 `errors` 里多一条 `INVALID_DECISION` |

> **项目设计选择**：`edit` 的正文只读，只能追加无数字备注。修改符号、单位、数字归属或删除正文都拒绝；API 返回 422 并保留待审批。仅核对数字集合无法保证语义不变。

### 3.5 为什么"没有待审批就 resume"必须报错

想象一下如果静默执行：用户点了两次"批准"，第二次图会带着 `Command(resume=...)` 从一个**没有 interrupt 的状态**继续跑。不同版本对这种输入的处理可能不同（可能报错、可能忽略、可能把 resume 值当成普通输入），行为不可预测。项目的选择是：**先读快照，确认有 pending interrupt 才允许恢复**；没有就抛 `ValueError`，API 层再映射成 HTTP 状态码。

---

## 4. 这个知识在主项目中的位置

| 位置 | 文件与函数 | 职责 |
| --- | --- | --- |
| 暂停 | `workflow/nodes.py` → `review_node` | 构造 payload、调用 `interrupt(payload)`、恢复后解析决策并写状态 |
| 载荷构造 | 同文件 → `_approval_payload` / `_review_summary` | 只放 JSON 友好的字段；摘要三行全部来自程序算好的数字 |
| 恢复值解析 | 同文件 → `_parse_resume` | 别名字典归一；无法识别返回空串 |
| 修改稿校验 | 同文件 → `_edited_report_or_draft` | 正文不可改，末尾追加无数字人工备注 |
| 路由 | `workflow/routing.py` → `route_after_review` | `approve`/`edit` → `export`；其它 → `END` |
| 落盘 | `workflow/nodes.py` → `export_node` + `report/export.py` | 唯一写文件的地方；原子写、固定文件名 |
| 恢复入口 | `workflow/persistence.py` → `AppRuntime.resume_turn` | 校验 decision、检查 pending、`Command(resume=payload)` |
| 中断提取 | 同文件 → `_extract_interrupt` | 从 `StateSnapshot` 里取 `interrupts`（兼容 `.tasks[*].interrupts`） |
| HTTP | `api/main.py` → `POST /api/threads/{thread_id}/resume` | 404（会话不存在）/ 409（无待审批）/ 400（decision 非法） |
| UI | `ui/streamlit_app.py` → 审批区 | 只有 `status=awaiting_approval` 且有 `interrupt` 才渲染按钮 |

> **项目设计选择**：为什么暂停点放在 `review`，而不是放到 `draft` 之前？因为"值不值得审"取决于有没有草稿可看；在草稿之前暂停，审批人看不到任何东西，只能盲批。

---

## 5. 文件、完整代码、配置与命令

### 5.1 当前生产契约（2026-10-20 修订）

- review 节点先 interrupt；恢复时从节点开头运行。
- HTTP 审批必须带当前 `report_id` 和 `interrupt_id`，可附 `content_hash`。过期审批返回 409。
- 待审批时 `/api/chat` 返回 409，先批准、拒绝或编辑当前草稿。
- 正文只读；edit 仅允许在末尾追加无数字人工备注。非法编辑返回 422，并保持待审批，不自动批准原稿。
- 只有 export 写业务产物，文件以 thread_id + report_id 标识；不同报告不覆盖。
- `resume_turn` 的读取、校验、推进与结果快照由同一 RLock 保护；HTTP 与 CLI 都通过该入口。
- 导出暂时失败后，对同一已批准 report_id 重试 approve 可以恢复导出，不重新算数或重新生成草稿。

源码：`workflow/nodes.py` 的 review_node/export_node、`workflow/persistence.py` 的 resume_turn。以下实验展示框架中断语义；HTTP 集成示例见单元 19。

### 5.2 可运行实验：`教程/实验/16_interrupt_resume.py`（一步步搭出来）

迷你图 `draft → review（interrupt）→ export`，把审批的**九种局面**全部演一遍：暂停、approve、reject、edit 采纳、edit 拒绝、非法值、重复 resume 幂等、无待审批报错、落盘核对。不导入 ops_assistant、不联网、不需要模型。脚本最大（307 行），拆 **8 步**搭出，最终与仓库完全一致。

#### 步骤 1：别名表、状态与"重放记录仪"

**这一步解决什么**：审批入口必须**宽容输入**（用户可能打"通过/ok/yes"）、**严格语义**（只有三种决定）；再加一个模块级 REPLAYS 列表，把"哪些节点被重新执行了"暴露给肉眼。

新建 `教程/实验/16_interrupt_resume.py`：

```python
"""单元 16 实验（步骤 1）：别名表、状态与重放记录仪。"""

from __future__ import annotations

import operator
import re
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.types import interrupt

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
REPORT_PATH = OUTPUT_DIR / "report-demo.md"

APPROVAL_OPTIONS = ("approve", "reject", "edit")
ALIASES = {
    "approve": "approve",
    "approved": "approve",
    "ok": "approve",
    "yes": "approve",
    "通过": "approve",
    "批准": "approve",
    "reject": "reject",
    "rejected": "reject",
    "no": "reject",
    "拒绝": "reject",
    "驳回": "reject",
    "edit": "edit",
    "modify": "edit",
    "修改": "edit",
    "编辑": "edit",
}

# 模块级列表：用来观察"节点被重复执行"（真实项目里应改成 trace 字段）
REPLAYS: list[str] = []


class St(TypedDict, total=False):
    """迷你状态：草稿、决策、最终文本、状态、错误。"""

    topic: str
    draft: str
    decision: str
    comment: str
    final: str
    status: str
    errors: Annotated[list[str], operator.add]
```

**逐行讲解**：

- `ALIASES` 是**入口归一表**：中英文、正式口语全收——但**值只有三个**。宽容只发生在这一层，后面全部是严格三值世界。
- `REPLAYS` 注释里自我检讨"真实项目应改成 trace 字段"：模块级可变全局在教学脚本里可接受，生产代码里是坏味道——**作者自己标注**，比读者踩坑强。
- `errors` 用 `operator.add`：错误累积不覆盖（单元 07 的纪律，第三次出现）。

**运行与观察**（临时验证）：

```python
if __name__ == "__main__":
    print(ALIASES["通过"], ALIASES["no"], ALIASES["编辑"])
```

```text
approve reject edit
```

#### 步骤 2：两个纯函数——编辑校验与恢复值归一

**这一步解决什么**：审批的两个**安全关键**规则先写成纯函数（可单测、可复用）：①"正文只读，只能追加无数字备注"（编辑不得改数字）；②把任意恢复值（字符串/字典）归一成 (decision, comment, edited)。

```python
def edited_or_draft(edited: str, draft: str) -> tuple[str, list[str]]:
    """正文不可改；只允许追加不含数字的备注。"""
    text = str(edited or "").strip()
    if not text.startswith(draft):
        raise ValueError("正文只读，只能追加人工备注")
    note = text[len(draft) :]
    if re.search(r"[\d一二三四五六七八九十百千万亿零〇两%％]|https?://|\[来源", note):
        raise ValueError("备注不能新增数字、百分比、URL 或引用")
    return text, []


def parse_resume(value: Any) -> tuple[str, str, str]:
    """把恢复值归一成 (decision, comment, edited_report)；无法识别返回空串。"""
    if isinstance(value, str):
        return ALIASES.get(value.strip().lower(), ""), "", ""
    if not isinstance(value, dict):
        return "", "", ""
    decision = str(value.get("decision") or value.get("action") or "").strip().lower()
    comment = str(value.get("comment") or value.get("feedback") or "").strip()
    edited = value.get("edited_report")
    if edited is None:
        edited = value.get("report_markdown")
    return ALIASES.get(decision, ""), comment, str(edited or "").strip()
```

**逐行讲解**：

- `text.startswith(draft)`：编辑稿必须**原样包含**草稿开头——改一个字都不行；追加的部分叫 note。
- note 的黑名单**连中文数字都列全了**（一二三…零〇两）——人工备注只许说人话，**不许夹带新数字**：报告里的每个数字都必须来自程序计算，这是"对账失败禁出稿"的微观执行。
- `parse_resume` 接受 `decision/action`、`comment/feedback`、`edited_report/report_markdown` **两套字段名**：API 演进常见的兼容层；认不出来就返回空串——**由调用方决定空串的后果**（步骤 4 会看到：按 reject 处理）。

**运行与观察**（临时验证）：

```python
    print(parse_resume("OK"))
    print(parse_resume({"decision": "编辑", "edited_report": "x"}))
    try:
        edited_or_draft("改头换面的稿子", "原稿")
    except ValueError as exc:
        print("ValueError:", exc)
```

```text
('approve', '', '')
('edit', '', 'x')
ValueError: 正文只读，只能追加人工备注
```

#### 步骤 3：`draft_node` 与 `review_node` 上半——interrupt 的"发球"

**这一步解决什么**：草稿节点渲染固定文本；review 节点在 interrupt **之前**只做纯读纯算，把审批人需要的一切装进 payload，然后**把球抛出去**（`interrupt(payload)`）。

```python
def draft_node(state: St) -> dict[str, Any]:
    """渲染一份固定草稿（真实项目里由 report/format.py 渲染）。"""
    REPLAYS.append("draft")
    topic = str(state.get("topic") or "（未指定）")
    draft = (
        f"# 经营分析报告：{topic}\n"
        "## 结论\n"
        "本期净销售额 1,234.00 元，比上期减少 12.5%。\n"
        "## 明细\n"
        "差额 -176.00 元，其中退款 200.00 元。"
    )
    return {"draft": draft, "status": "awaiting_approval"}


def review_node(state: St) -> dict[str, Any]:
    """审批节点：interrupt 之前只做纯读/纯算，恢复后只写状态。"""
    REPLAYS.append("review:before_interrupt")
    payload = {
        "type": "report_approval",
        "topic": str(state.get("topic") or ""),
        "options": list(APPROVAL_OPTIONS),
        "summary": "本期 1,234.00 元 / 上期 1,410.00 元 / 差额 -176.00 元",
        "report_markdown": str(state.get("draft") or ""),
        "instructions": "approve 导出；edit 提供修改稿后导出（正文只读，仅追加无数字备注）；reject 不导出。",
    }
    value = interrupt(payload)
    REPLAYS.append("review:after_interrupt")
```

**逐行讲解**：

- `REPLAYS.append` 夹在 interrupt **前后各一条**——恢复后你会看到 `before_interrupt` 出现**两次**（重放），这正是 interrupt 的机制证据（步骤 7 的段 2 观察）。
- payload 五个字段 = 审批人做决定需要的**全部信息**：类型、主题、选项、摘要、全文、规则说明——**中断载荷是给人看的界面契约**。
- `interrupt(payload)` 一行就是"暂停"：抛出异常给框架、框架存进检查点、invoke 返回——**图没崩，是在等人**。它的返回值 `value` 要等到 resume 时才拿到。
- **interrupt 前不写任何状态**：draft 在 interrupt 前早已落进状态（draft_node 完成的），review 在中断前只读——保证重放时**无副作用可重复**。

#### 步骤 4：`review_node` 下半与 `export_node`——恢复后的三分支

**这一步解决什么**：拿到恢复值后的裁决：非法→按 reject（宁拒不误出）、reject→不导出、approve→原稿导出、edit→校验通过才导出。

```python
    decision, comment, edited = parse_resume(value)
    errors: list[str] = []
    if decision not in APPROVAL_OPTIONS:
        errors.append(
            f"INVALID_DECISION: 无法识别“{decision or '（空）'}”，按拒绝处理以免误导出文件。"
        )
        decision = "reject"

    if decision == "reject":
        return {
            "decision": "reject",
            "comment": comment,
            "final": "",
            "status": "rejected",
            "errors": errors,
        }

    draft = str(state.get("draft") or "")
    final, notes = edited_or_draft(edited, draft) if decision == "edit" else (draft, [])
    errors.extend(notes)
    return {
        "decision": decision,
        "comment": comment,
        "final": final,
        "status": "approved",
        "errors": errors,
    }


def export_node(state: St) -> dict[str, Any]:
    """唯一的落盘节点；真实项目用"临时文件 + os.replace"原子写。"""
    REPLAYS.append("export")
    text = str(state.get("final") or state.get("draft") or "")
    REPORT_PATH.write_text(text + "\n", encoding="utf-8")
    return {"status": "approved", "final": text}
```

**逐行讲解**：

- **非法决定按 reject 而不是报错崩掉**：审批是安全闸门，认不出的输入**宁可不放行**——错误码 INVALID_DECISION 进 errors，人能看到原因。
- `edited_or_draft(...) if decision == "edit" else (draft, [])`：**只有 edit 才碰编辑校验**；校验抛 ValueError 会一路传到 resume 的调用方（步骤 6 会先拦截）。
- `export_node` 是**全图唯一的落盘点**：文件操作集中在一点，"什么会写盘"一眼可查——生产版还要原子写（临时文件 + os.replace），教学版直接 write。
- `route_after_review`（下一步装配时接上）：`decision in {"approve","edit"}` 才去 export，reject 直接到 END。

#### 步骤 5：装配与"挖中断"——`build_graph` / `extract_interrupt`

**这一步解决什么**：把图接起来（review 后条件分支）；再写一个**从快照里把 pending interrupt 挖出来**的函数——不同版本快照结构不同（interrupts 直挂 / 藏在 tasks 里），两处都找。

```python
from langgraph.graph import END, START, StateGraph


def route_after_review(state: St) -> str:
    """批准或编辑才导出，其它一律结束。"""
    return (
        "export" if str(state.get("decision") or "") in {"approve", "edit"} else "end"
    )


def build_graph(checkpointer: Any) -> Any:
    """装配迷你图：draft → review → (export | END)。"""
    builder = StateGraph(St)
    builder.add_node("draft", draft_node)
    builder.add_node("review", review_node)
    builder.add_node("export", export_node)
    builder.add_edge(START, "draft")
    builder.add_edge("draft", "review")
    builder.add_conditional_edges(
        "review", route_after_review, {"export": "export", "end": END}
    )
    builder.add_edge("export", END)
    return builder.compile(checkpointer=checkpointer)


def extract_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """从状态快照里取待处理中断（与项目 _extract_interrupt 同构的简化版）。"""
    candidates: list[Any] = []
    raw = getattr(snapshot, "interrupts", None)
    if raw:
        candidates.extend(list(raw))
    if not candidates:
        for task in getattr(snapshot, "tasks", ()) or ():
            for item in getattr(task, "interrupts", ()) or ():
                candidates.append(item)
    if not candidates:
        return None
    first = candidates[0]
    if isinstance(first, dict):
        return {"id": str(first.get("id") or ""), "payload": first.get("value")}
    return {
        "id": str(getattr(first, "id", "") or ""),
        "payload": getattr(first, "value", None),
    }
```

**逐行讲解**：

- `add_conditional_edges("review", route_after_review, ...)`：审批结论变路由——**决定权在状态里，不在图结构里**。
- `extract_interrupt` 的**两栖搜索**：新版本快照有 `snapshot.interrupts`，旧版本藏在 `snapshot.tasks[*].interrupts`——`getattr(..., None)` 两边都不赌。
- 返回 `{id, payload}` 或 None：**中断id 是 resume 的精确锚点**（主项目 resume 要 report_id + interrupt_id 双锚，这里简化版只透传）。
- `build_graph(checkpointer)` 参数化 checkpointer：**interrupt 依赖检查点**——没有它，"暂停"无处可存（主项目用 SQLite 版）。

#### 步骤 6：`resume_mini`——恢复的门卫

**这一步解决什么**：模拟项目的 `resume_turn`：**先查有没有 pending**（没有就分两种情况：已批准→幂等返回、没草稿→报错）、edit 先做**预校验**（不让坏编辑稿进图）、然后 `Command(resume=value)`。

```python
from langgraph.types import Command


def resume_mini(graph: Any, config: dict[str, Any], value: Any) -> dict[str, Any]:
    """模拟项目 resume_turn：先确认有 pending interrupt，再 Command(resume=...)。"""
    snapshot = graph.get_state(config)
    pending = extract_interrupt(snapshot)
    values = dict(snapshot.values or {})
    if pending is None:
        if values.get("status") == "approved" and values.get("final"):
            return values
        raise ValueError("该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。")
    decision, _, edited = parse_resume(value)
    if decision == "edit":
        edited_or_draft(edited, str(values.get("draft") or ""))
    graph.invoke(Command(resume=value), config=config)
    return dict(graph.get_state(config).values)


def config_for(thread_id: str) -> dict[str, Any]:
    """thread_id 决定检查点归属；没有它 interrupt 无处可存。"""
    return {"configurable": {"thread_id": thread_id}}
```

**逐行讲解**：

- pending 为 None 的**两分支**：已 approved 且有 final → **幂等地把原结果再给一次**（重复点击"批准"不该出错也不该重复导出）；否则 → 明确报错"没有待审批的报告"。报错文案带**下一步指引**（"请先调用 /api/chat"）——与主项目 API 的语义一致。
- `edited_or_draft(...)` 在 invoke **之前**预校验：坏的编辑稿（改了正文/夹带数字）**根本不进图**——图内校验（步骤 4）还在，但门外先挡一道，**报错更早、状态更干净**（中断仍保持 pending）。
- `Command(resume=value)`：LangGraph 恢复的标准姿势——value 就是 `interrupt()` 的返回值；`config` 必须同一个 thread_id。

#### 步骤 7：`main()` 上半——暂停、approve、reject、edit 两面

**这一步解决什么**：前五种局面：图怎么停、approve 怎么续、reject 怎么干净地结束、edit 采纳与拒绝。

```python
import json
import shutil

from langgraph.checkpoint.memory import InMemorySaver


def main() -> int:
    """依次演示：暂停 → approve → reject → edit（追加/拒绝）→ 非法值 → 幂等 → 报错。"""
    print("== 单元 16 实验：interrupt 与恢复 ==")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    graph = build_graph(InMemorySaver())

    print("\n== 1) 第一次 invoke：图停在 review 的 interrupt 上 ==")
    cfg1 = config_for("t1")
    graph.invoke({"topic": "九月华东净销售额"}, config=cfg1)
    snap = graph.get_state(cfg1)
    pending = extract_interrupt(snap)
    print("status =", snap.values["status"])
    print("next =", snap.next)
    print("interrupt.payload.type =", pending["payload"]["type"])
    print("interrupt.payload.options =", pending["payload"]["options"])
    print(
        "payload 可 JSON 序列化 =",
        bool(json.dumps(pending["payload"], ensure_ascii=False)),
    )
    print("REPLAYS =", REPLAYS)

    print("\n== 2) resume approve：继续跑 export ==")
    after = resume_mini(graph, cfg1, "approve")
    print("status =", after["status"])
    print("final 非空 =", bool(after["final"]))
    print("REPLAYS =", REPLAYS)
    print("review 节点被执行的次数 =", REPLAYS.count("review:before_interrupt"))

    print("\n== 3) resume reject：不进 export，不写文件 ==")
    cfg2 = config_for("t2")
    graph.invoke({"topic": "八月西南退款"}, config=cfg2)
    rejected = resume_mini(graph, cfg2, {"decision": "reject", "comment": "口径不对"})
    print("status =", rejected["status"])
    print("final =", repr(rejected["final"]))
    print("最后执行的是 =", REPLAYS[-1])

    print("\n== 4) resume edit：追加无数字备注会被采纳 ==")
    cfg3 = config_for("t3")
    graph.invoke({"topic": "九月西南净销售额"}, config=cfg3)
    draft3 = str(graph.get_state(cfg3).values["draft"])
    edited_ok = draft3 + "\n（本报告已由运营负责人复核）"
    edited_out = resume_mini(
        graph, cfg3, {"decision": "edit", "edited_report": edited_ok}
    )
    print("status =", edited_out["status"])
    print("errors =", edited_out["errors"])
    print("采纳了修改稿 =", "运营负责人复核" in edited_out["final"])

    print("\n== 5) resume edit：改了正文会被拒绝 ==")
    cfg4 = config_for("t4")
    graph.invoke({"topic": "九月华南净销售额"}, config=cfg4)
    draft4 = str(graph.get_state(cfg4).values["draft"])
    bad = draft4.replace("1,234.00", "9,999.00")
    try:
        resume_mini(graph, cfg4, {"decision": "edit", "edited_report": bad})
    except ValueError as exc:
        print("ValueError:", exc)
    print("仍待审批 =", bool(extract_interrupt(graph.get_state(cfg4))))
    assert graph.get_state(cfg4).values["status"] == "awaiting_approval"
```

**运行与观察**（实测 2026-10-23，段 1–5）：

```text
== 1) 第一次 invoke：图停在 review 的 interrupt 上 ==
status = awaiting_approval
next = ('review',)
interrupt.payload.type = report_approval
interrupt.payload.options = ['approve', 'reject', 'edit']
payload 可 JSON 序列化 = True
REPLAYS = ['draft', 'review:before_interrupt']

== 2) resume approve：继续跑 export ==
status = approved
final 非空 = True
REPLAYS = ['draft', 'review:before_interrupt', 'review:before_interrupt', 'review:after_interrupt', 'export']
review 节点被执行的次数 = 2

== 3) resume reject：不进 export，不写文件 ==
status = rejected
final = ''
最后执行的是 = review:after_interrupt

== 4) resume edit：追加无数字备注会被采纳 ==
status = approved
errors = []
采纳了修改稿 = True

== 5) resume edit：改了正文会被拒绝 ==
ValueError: 正文只读，只能追加人工备注
仍待审批 = True
```

**段 2 是全实验的题眼**：`review:before_interrupt` 出现两次——resume 时**整个节点从头重放**（interrupt 之前的部分再跑一遍），然后 `after_interrupt` 与 `export` 才出现。所以步骤 3 反复强调"中断前只做纯读纯算"。段 5 的坏编辑稿被门卫拦下，中断**原封不动还在**（仍待审批 = True）。

#### 步骤 8：`main()` 下半与整合——非法值、幂等、报错、落盘核对

**这一步解决什么**：后四种局面：认不出的决定、重复 resume、resume 空会话、以及最终**盘点的权利**（文件只该有两份语义：t1 原稿、t3 加尾注）。

```python
    print("\n== 6) 无法识别的结论：按拒绝处理 ==")
    cfg5 = config_for("t5")
    graph.invoke({"topic": "九月华北净销售额"}, config=cfg5)
    unknown = resume_mini(graph, cfg5, "同意")
    print("status =", unknown["status"])
    print("errors =", unknown["errors"])

    print("\n== 7) 重复 resume 已批准的会话：幂等返回同一结果 ==")
    again = resume_mini(graph, cfg1, "approve")
    print("status =", again["status"])
    print("final 与第一次相同 =", again["final"] == after["final"])

    print("\n== 8) resume 一个没有待审批的会话：报错而不是瞎跑 ==")
    try:
        resume_mini(graph, cfg2, "approve")
    except ValueError as exc:
        print("ValueError:", exc)

    print("\n== 9) 落盘结果：只有 approve/edit 的两条线程各写了一次 ==")
    print("_output 下的文件 =", sorted(path.name for path in OUTPUT_DIR.iterdir()))
    print(
        "文件里含人工复核尾注 =",
        "运营负责人复核" in REPORT_PATH.read_text(encoding="utf-8"),
    )
    return 0
```

（文件末尾补 `if __name__ == "__main__": raise SystemExit(main())`。）

**运行与观察**（实测，段 6–9）：

```text
== 6) 无法识别的结论：按拒绝处理 ==
status = rejected
errors = ['INVALID_DECISION: 无法识别“（空）”，按拒绝处理以免误导出文件。']

== 7) 重复 resume 已批准的会话：幂等返回同一结果 ==
status = approved
final 与第一次相同 = True

== 8) resume 一个没有待审批的会话：报错而不是瞎跑 ==
ValueError: 该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。

== 9) 落盘结果：只有 approve/edit 的两条线程各写了一次 ==
_output 下的文件 = ['report-demo.md']
文件里含人工复核尾注 = True
```

（"同意"不在 ALIASES 里 → decision 为空串 → 按 reject；t2 是 reject 线程，resume 它触发"没有待审批"报错——注意报错与"重复批准"是两种不同局面；文件只有一份（t1 与 t3 写同一路径，后者含尾注）。）

清理各步临时验证，完整文件应与仓库完全一致：

```python
"""单元 16 实验：interrupt 与恢复（approve / edit / reject）。

迷你图：draft → review（interrupt）→ export（只有批准/编辑才走）。
不导入 ops_assistant、不联网、不需要模型。

用法：
    cd ops-assistant
    uv run python ../教程/实验/16_interrupt_resume.py
"""

from __future__ import annotations

import json
import operator
import re
import shutil
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

OUTPUT_DIR = Path(__file__).resolve().parent / "_output"
REPORT_PATH = OUTPUT_DIR / "report-demo.md"

APPROVAL_OPTIONS = ("approve", "reject", "edit")
ALIASES = {
    "approve": "approve",
    "approved": "approve",
    "ok": "approve",
    "yes": "approve",
    "通过": "approve",
    "批准": "approve",
    "reject": "reject",
    "rejected": "reject",
    "no": "reject",
    "拒绝": "reject",
    "驳回": "reject",
    "edit": "edit",
    "modify": "edit",
    "修改": "edit",
    "编辑": "edit",
}

# 模块级列表：用来观察"节点被重复执行"（真实项目里应改成 trace 字段）
REPLAYS: list[str] = []


class St(TypedDict, total=False):
    """迷你状态：草稿、决策、最终文本、状态、错误。"""

    topic: str
    draft: str
    decision: str
    comment: str
    final: str
    status: str
    errors: Annotated[list[str], operator.add]


def edited_or_draft(edited: str, draft: str) -> tuple[str, list[str]]:
    """正文不可改；只允许追加不含数字的备注。"""
    text = str(edited or "").strip()
    if not text.startswith(draft):
        raise ValueError("正文只读，只能追加人工备注")
    note = text[len(draft) :]
    if re.search(r"[\d一二三四五六七八九十百千万亿零〇两%％]|https?://|\[来源", note):
        raise ValueError("备注不能新增数字、百分比、URL 或引用")
    return text, []


def parse_resume(value: Any) -> tuple[str, str, str]:
    """把恢复值归一成 (decision, comment, edited_report)；无法识别返回空串。"""
    if isinstance(value, str):
        return ALIASES.get(value.strip().lower(), ""), "", ""
    if not isinstance(value, dict):
        return "", "", ""
    decision = str(value.get("decision") or value.get("action") or "").strip().lower()
    comment = str(value.get("comment") or value.get("feedback") or "").strip()
    edited = value.get("edited_report")
    if edited is None:
        edited = value.get("report_markdown")
    return ALIASES.get(decision, ""), comment, str(edited or "").strip()


def draft_node(state: St) -> dict[str, Any]:
    """渲染一份固定草稿（真实项目里由 report/format.py 渲染）。"""
    REPLAYS.append("draft")
    topic = str(state.get("topic") or "（未指定）")
    draft = (
        f"# 经营分析报告：{topic}\n"
        "## 结论\n"
        "本期净销售额 1,234.00 元，比上期减少 12.5%。\n"
        "## 明细\n"
        "差额 -176.00 元，其中退款 200.00 元。"
    )
    return {"draft": draft, "status": "awaiting_approval"}


def review_node(state: St) -> dict[str, Any]:
    """审批节点：interrupt 之前只做纯读/纯算，恢复后只写状态。"""
    REPLAYS.append("review:before_interrupt")
    payload = {
        "type": "report_approval",
        "topic": str(state.get("topic") or ""),
        "options": list(APPROVAL_OPTIONS),
        "summary": "本期 1,234.00 元 / 上期 1,410.00 元 / 差额 -176.00 元",
        "report_markdown": str(state.get("draft") or ""),
        "instructions": "approve 导出；edit 提供修改稿后导出（正文只读，仅追加无数字备注）；reject 不导出。",
    }
    value = interrupt(payload)
    REPLAYS.append("review:after_interrupt")

    decision, comment, edited = parse_resume(value)
    errors: list[str] = []
    if decision not in APPROVAL_OPTIONS:
        errors.append(
            f"INVALID_DECISION: 无法识别“{decision or '（空）'}”，按拒绝处理以免误导出文件。"
        )
        decision = "reject"

    if decision == "reject":
        return {
            "decision": "reject",
            "comment": comment,
            "final": "",
            "status": "rejected",
            "errors": errors,
        }

    draft = str(state.get("draft") or "")
    final, notes = edited_or_draft(edited, draft) if decision == "edit" else (draft, [])
    errors.extend(notes)
    return {
        "decision": decision,
        "comment": comment,
        "final": final,
        "status": "approved",
        "errors": errors,
    }


def export_node(state: St) -> dict[str, Any]:
    """唯一的落盘节点；真实项目用"临时文件 + os.replace"原子写。"""
    REPLAYS.append("export")
    text = str(state.get("final") or state.get("draft") or "")
    REPORT_PATH.write_text(text + "\n", encoding="utf-8")
    return {"status": "approved", "final": text}


def route_after_review(state: St) -> str:
    """批准或编辑才导出，其它一律结束。"""
    return (
        "export" if str(state.get("decision") or "") in {"approve", "edit"} else "end"
    )


def build_graph(checkpointer: Any) -> Any:
    """装配迷你图：draft → review → (export | END)。"""
    builder = StateGraph(St)
    builder.add_node("draft", draft_node)
    builder.add_node("review", review_node)
    builder.add_node("export", export_node)
    builder.add_edge(START, "draft")
    builder.add_edge("draft", "review")
    builder.add_conditional_edges(
        "review", route_after_review, {"export": "export", "end": END}
    )
    builder.add_edge("export", END)
    return builder.compile(checkpointer=checkpointer)


def extract_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """从状态快照里取待处理中断（与项目 _extract_interrupt 同构的简化版）。"""
    candidates: list[Any] = []
    raw = getattr(snapshot, "interrupts", None)
    if raw:
        candidates.extend(list(raw))
    if not candidates:
        for task in getattr(snapshot, "tasks", ()) or ():
            for item in getattr(task, "interrupts", ()) or ():
                candidates.append(item)
    if not candidates:
        return None
    first = candidates[0]
    if isinstance(first, dict):
        return {"id": str(first.get("id") or ""), "payload": first.get("value")}
    return {
        "id": str(getattr(first, "id", "") or ""),
        "payload": getattr(first, "value", None),
    }


def resume_mini(graph: Any, config: dict[str, Any], value: Any) -> dict[str, Any]:
    """模拟项目 resume_turn：先确认有 pending interrupt，再 Command(resume=...)。"""
    snapshot = graph.get_state(config)
    pending = extract_interrupt(snapshot)
    values = dict(snapshot.values or {})
    if pending is None:
        if values.get("status") == "approved" and values.get("final"):
            return values
        raise ValueError("该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。")
    decision, _, edited = parse_resume(value)
    if decision == "edit":
        edited_or_draft(edited, str(values.get("draft") or ""))
    graph.invoke(Command(resume=value), config=config)
    return dict(graph.get_state(config).values)


def config_for(thread_id: str) -> dict[str, Any]:
    """thread_id 决定检查点归属；没有它 interrupt 无处可存。"""
    return {"configurable": {"thread_id": thread_id}}


def main() -> int:
    """依次演示：暂停 → approve → reject → edit（追加/拒绝）→ 非法值 → 幂等 → 报错。"""
    print("== 单元 16 实验：interrupt 与恢复 ==")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    graph = build_graph(InMemorySaver())

    print("\n== 1) 第一次 invoke：图停在 review 的 interrupt 上 ==")
    cfg1 = config_for("t1")
    graph.invoke({"topic": "九月华东净销售额"}, config=cfg1)
    snap = graph.get_state(cfg1)
    pending = extract_interrupt(snap)
    print("status =", snap.values["status"])
    print("next =", snap.next)
    print("interrupt.payload.type =", pending["payload"]["type"])
    print("interrupt.payload.options =", pending["payload"]["options"])
    print(
        "payload 可 JSON 序列化 =",
        bool(json.dumps(pending["payload"], ensure_ascii=False)),
    )
    print("REPLAYS =", REPLAYS)

    print("\n== 2) resume approve：继续跑 export ==")
    after = resume_mini(graph, cfg1, "approve")
    print("status =", after["status"])
    print("final 非空 =", bool(after["final"]))
    print("REPLAYS =", REPLAYS)
    print("review 节点被执行的次数 =", REPLAYS.count("review:before_interrupt"))

    print("\n== 3) resume reject：不进 export，不写文件 ==")
    cfg2 = config_for("t2")
    graph.invoke({"topic": "八月西南退款"}, config=cfg2)
    rejected = resume_mini(graph, cfg2, {"decision": "reject", "comment": "口径不对"})
    print("status =", rejected["status"])
    print("final =", repr(rejected["final"]))
    print("最后执行的是 =", REPLAYS[-1])

    print("\n== 4) resume edit：追加无数字备注会被采纳 ==")
    cfg3 = config_for("t3")
    graph.invoke({"topic": "九月西南净销售额"}, config=cfg3)
    draft3 = str(graph.get_state(cfg3).values["draft"])
    edited_ok = draft3 + "\n（本报告已由运营负责人复核）"
    edited_out = resume_mini(
        graph, cfg3, {"decision": "edit", "edited_report": edited_ok}
    )
    print("status =", edited_out["status"])
    print("errors =", edited_out["errors"])
    print("采纳了修改稿 =", "运营负责人复核" in edited_out["final"])

    print("\n== 5) resume edit：改了正文会被拒绝 ==")
    cfg4 = config_for("t4")
    graph.invoke({"topic": "九月华南净销售额"}, config=cfg4)
    draft4 = str(graph.get_state(cfg4).values["draft"])
    bad = draft4.replace("1,234.00", "9,999.00")
    try:
        resume_mini(graph, cfg4, {"decision": "edit", "edited_report": bad})
    except ValueError as exc:
        print("ValueError:", exc)
    print("仍待审批 =", bool(extract_interrupt(graph.get_state(cfg4))))
    assert graph.get_state(cfg4).values["status"] == "awaiting_approval"

    print("\n== 6) 无法识别的结论：按拒绝处理 ==")
    cfg5 = config_for("t5")
    graph.invoke({"topic": "九月华北净销售额"}, config=cfg5)
    unknown = resume_mini(graph, cfg5, "同意")
    print("status =", unknown["status"])
    print("errors =", unknown["errors"])

    print("\n== 7) 重复 resume 已批准的会话：幂等返回同一结果 ==")
    again = resume_mini(graph, cfg1, "approve")
    print("status =", again["status"])
    print("final 与第一次相同 =", again["final"] == after["final"])

    print("\n== 8) resume 一个没有待审批的会话：报错而不是瞎跑 ==")
    try:
        resume_mini(graph, cfg2, "approve")
    except ValueError as exc:
        print("ValueError:", exc)

    print("\n== 9) 落盘结果：只有 approve/edit 的两条线程各写了一次 ==")
    print("_output 下的文件 =", sorted(path.name for path in OUTPUT_DIR.iterdir()))
    print(
        "文件里含人工复核尾注 =",
        "运营负责人复核" in REPORT_PATH.read_text(encoding="utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**回看地图**：

| 步骤 | 最终脚本里的位置 |
| --- | --- |
| 1 入口 | `APPROVAL_OPTIONS`/`ALIASES`、`REPLAYS`、`St` |
| 2 纯函数 | `edited_or_draft()`、`parse_resume()` |
| 3 发球 | `draft_node()`、`review_node()` 上半（payload + interrupt） |
| 4 裁决 | `review_node()` 下半、`export_node()` |
| 5 装配 | `route_after_review()`、`build_graph()`、`extract_interrupt()` |
| 6 门卫 | `resume_mini()`、`config_for()` |
| 7 五局面 | `main()` 段 1–5 |
| 8 四局面 | `main()` 段 6–9 + 整合 |

运行与验收：见 §5.3 的命令。核心验收：`review:before_interrupt` 计数为 2（重放）、坏编辑稿后仍待审批、段 7 幂等、段 8 报错——四件事都对，interrupt 的"暂停—重放—续跑"就真的通了。

### 5.3 运行命令

```powershell
cd "D:\MySpace\LangChain学习路线\ops-assistant"
uv run python ../教程/实验/16_interrupt_resume.py
```

生成物：`教程/实验/_output/report-demo.md`（每次运行前会删掉整个 `_output` 目录重建）。

### 5.4 逐段解释

| 代码段 | 对应项目里的什么 | 观察重点 |
| --- | --- | --- |
| `payload` 里的 6 个键 | `_approval_payload` | 全是字符串/列表，`json.dumps` 一定成功 |
| `value = interrupt(payload)` | `review_node` 第 1873 行 | 第一次执行时**不返回**，图直接停住 |
| `REPLAYS.append("review:before_interrupt")` 在 interrupt 之前 | 项目用 trace 记录 | 恢复后这一行会再执行一次，`count == 2` |
| `parse_resume` 别名字典 | `_parse_resume` | `"同意"` 不在别名表里 → 归一成空串 → reject |
| `edited_or_draft` 的数字校验 | `_edited_report_or_draft` | 更改正文就拒绝，保留待审批 |
| `route_after_review` | `routing.route_after_review` | reject 直接 `END`，export 不执行 |
| `resume_mini` 里的 `pending is None` 分支 | `resume_turn` 第 636–642 行 | 已导出 → 幂等返回；其它 → `ValueError` |
| `export_node` 里的 `write_text` | `export.export_report` | 只有这一处写文件；真实实现更严格（原子写） |

### 5.5 项目里 payload 长什么样（真实样例，字段名与源码一致）

```json
{
  "type": "report_approval",
  "thread_id": "<thread_id>",
  "question": "2026 年九月销售额比八月变化多少，并按地区分解",
  "current_month": "2026-09",
  "previous_month": "2026-08",
  "summary": "本期（2026-09）净销售额：1,234.00 元\n上期（2026-08）净销售额：1,410.00 元\n差额：-176.00 元，变化率：-12.5%",
  "options": ["approve", "reject", "edit"],
  "report_markdown": "# ...（完整草稿）",
  "instructions": "请审批本报告：approve 直接导出；edit 提供修改稿后导出（正文只读，只能追加无数字人工备注）；reject 不导出文件。"
}
```

> 上面的数字是示例值，不是本项目演示数据的真实计算结果（那需要运行 `scripts/bootstrap.py` 与一轮问答）。**未验证**：`summary` 三行的具体格式以你运行时 `_review_summary` 的输出为准。

---

## 6. 预期输出与关键状态变化

### 6.1 实验输出

**预期（未在本环境执行）**：

```text
== 单元 16 实验：interrupt 与恢复 ==

== 1) 第一次 invoke：图停在 review 的 interrupt 上 ==
status = awaiting_approval
next = ('review',)
interrupt.payload.type = report_approval
interrupt.payload.options = ['approve', 'reject', 'edit']
payload 可 JSON 序列化 = True
REPLAYS = ['draft', 'review:before_interrupt']

== 2) resume approve：继续跑 export ==
status = approved
final 非空 = True
REPLAYS = ['draft', 'review:before_interrupt', 'review:before_interrupt', 'review:after_interrupt', 'export']
review 节点被执行的次数 = 2

== 3) resume reject：不进 export，不写文件 ==
status = rejected
final = ''
最后执行的是 = review:after_interrupt

== 4) resume edit：只改措辞会被采纳 ==
status = approved
errors = []
采纳了修改稿 = True

== 5) resume edit：改了正文会被拒绝 ==
ValueError: 正文只读，只能追加人工备注
仍待审批 = True

== 6) 无法识别的结论：按拒绝处理 ==
status = rejected
errors = ['INVALID_DECISION: 无法识别“（空）”，按拒绝处理以免误导出文件。']

== 7) 重复 resume 已批准的会话：幂等返回同一结果 ==
status = approved
final 与第一次相同 = True

== 8) resume 一个没有待审批的会话：报错而不是瞎跑 ==
ValueError: 该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。

== 9) 落盘结果：只有 approve/edit 的两条线程各写了一次 ==
_output 下的文件 = ['report-demo.md']
文件里含人工复核尾注 = True
```

> 第 1 段的 `REPLAYS` 只有 2 条，是关键证据：`interrupt()` 让图**停住**了，后面的 `review:after_interrupt` 根本没执行。第 2 段的 `REPLAYS` 里 `review:before_interrupt` 出现**两次**，这就是"恢复时节点重跑"。
>
> **已验证（2026-10-19，langgraph 1.2.16 实测）**：`StateSnapshot.next`、`snapshot.interrupts`、`task.interrupts` 三者都是 **tuple**（例如 `next = ('n2',)`）。项目源码 `extract_interrupt` 对 `.interrupts` 与 `.tasks[*].interrupts` 两条路径都做了兼容，实验照抄了同样的兼容写法——在 1.2.16 上生效的是 `snapshot.interrupts` 这条。

### 6.2 关键状态变化

| 对象 | 之前 | 之后 |
| --- | --- | --- |
| 对 `interrupt()` 的理解 | "暂停后从下一行继续" | "暂停后从节点开头重跑，interrupt 这次直接返回" |
| 副作用位置 | 认为放哪都行 | 必须放在 interrupt 之后（或干脆只在 export 里） |
| reject 的处理 | 以为"拒绝=报错" | 是一个正常终态：`status=rejected`，有中文回复，只是不导出 |
| edit 的处理 | 以为"人工改的一定采纳" | 正文被改就返回错误，并保留待审批 |
| 重复审批 | 没想过 | 已导出的会话再 resume 会幂等返回，不会重写 |

---

## 7. 修改练习（至少 2 个，含一个"改坏它"）

### 练习 7.1（简单）：加一个"要求补充信息"的第四条分支

在 `review` 里支持 `decision="revise"`：不导出，也不结束，而是回到 `draft` 重新渲染（草稿里加一行"（第 2 版）"）。

提示：需要改 `route_after_review` 的返回集合与图的条件边映射，并在 `draft_node` 里根据某个计数字段决定是否加版本号（状态里加 `revise_count`）。

验收：`resume` 传 `{"decision": "revise"}` 后，图回到 `draft` 再停一次在 `review`，且 `REPLAYS` 里 `draft` 出现两次。想一想：项目为什么**没有**这条分支？（提示：重跑 `draft` 会再调一次模型/再渲染一次，与"预算 + 幂等"冲突；产品上更简单的做法是让人直接改稿。）

### 练习 7.2（中等）：把"审批等待时长"记录准

项目里 `waited_ms = elapsed_ms_since(started)` 是在 interrupt **之前**算的，恢复后写进 trace 的值其实等于"重跑开始到 interrupt 那一刻"的耗时（≈0）。

请改成真正记录等待时长：在 `interrupt()` 之前把 `time.time()` 写进 payload（作为 `created_at`），恢复后用 `time.time() - created_at` 算差值。要求：payload 仍必须 JSON 可序列化（用浮点秒数，不要塞 `datetime`）。

验收：trace 里的 `waited_ms` 与你手动等待的秒数同量级。

### 练习 7.3（改坏它）：把 export 的动作挪到 `interrupt()` 之前

把 `export_node` 里的写文件代码复制到 `review_node` 的 `payload = ...` 之前，跑一次 approve。

观察并解释：

- 为什么"第一次 invoke 就写了一个文件"（此时还没审批）？
- 为什么恢复后文件被写了**第二次**？
- 如果写的是"追加"模式，文件会变成什么样？

参考答案要点：`interrupt()` 之前是"节点第一次执行"就会跑到的位置，且恢复会重跑；两处都会写。项目把写文件严格限制在 `export_node`，并且在 `review_node` 的 docstring 里写了硬约束，就是为了防这一类错误。**恢复方式**：删掉那几行；如果确实需要"审批前的预览文件"，应该写到临时目录并在 `export` 里改名。

### 练习 7.4（进阶，可选）：给 payload 加"草稿哈希"

在 payload 里加 `draft_sha256`（`hashlib.sha256(draft.encode("utf-8")).hexdigest()`），恢复时校验 `state["draft_report"]` 的哈希是否仍与 payload 一致；不一致就拒绝导出并在 `errors` 里写 `DRAFT_CHANGED`。

验收：正常情况下不影响任何分支；把 `draft_report` 手动改掉再 resume，会走到拒绝分支。想一想：现实中 `draft_report` 会不会在 interrupt 期间变？**不会**——图停住后没有别的节点能改状态，除非有人直接 `update_state`（单元 17 会讲）。

---

## 8. 失败与边界实验（至少 3 个）

### 实验 8.1：忘记传 `thread_id`

把 `config_for("t1")` 改成 `{"configurable": {}}` 再跑第 1 段。

预期现象：`invoke` 会报错（缺少 `thread_id` 无法写检查点）。**结论**：`interrupt()` 依赖 checkpointer + thread；两者缺一，人工审批就不成立。项目里 `invoke_turn` 在 `thread_id` 为空时会 `new_request_id()` 兜底生成，所以从 API/CLI 进来不会踩到。

### 实验 8.2：把 payload 换成不可序列化的对象

在 payload 里加一行 `"now": __import__("datetime").datetime.now()`，再跑第 1 段。

预期现象：`invoke` 在写检查点时失败（SQLite saver 无法序列化 `datetime`）。具体异常类型**未验证**。**结论**：这正是项目 `jsonable()` 存在的理由；也是 `_approval_payload` 只放字符串/列表的理由。

### 实验 8.3：不检查 pending 直接 resume

删掉 `resume_mini` 里的 `if pending is None:` 整段（包括幂等与 `ValueError`），直接 `graph.invoke(Command(resume=...))`，然后对 `t2`（已 reject）再 resume 一次。

预期现象：行为随版本变化——可能抛异常、可能被忽略、可能把 resume 值当输入。**未验证**。**结论**：不要依赖框架"善意的猜测"，在应用层先校验（项目就是这么做的，好处是错误信息可控、HTTP 状态码可控）。

### 实验 8.4：连续两次 approve

对 `t1` 连续调 `resume_mini(..., "approve")` 三次。

预期现象：第 2、3 次都返回与第 1 次相同的 `status`/`final`，且 `REPLAYS` 不再增长（没有新的 `export`）。**结论**：项目的幂等来自"先查快照、已导出就直接返回"，不是框架自动做的。

### 实验 8.5：中文与英文别名

分别用 `"批准"`、`"APPROVED"`、`"ok"`、`"modify"`（配合 `edited_report`）测试 `parse_resume`。

预期现象：`批准`/`APPROVED`/`ok` → approve；`modify` → edit（不带修改稿会校验失败并保留待审批）；`"通过一下"` → 不识别 → reject。**结论**：别名字典是"宽容输入"的边界；超出边界一律降级为最安全的动作（不导出）。

---

## 9. 验收标准（可勾选、可观察）

- [ ] 我能解释"恢复时 review 节点被重跑"这件事，并能指出实验输出里 `review:before_interrupt` 出现两次的那一行。
- [ ] 我能说出 `interrupt()` 之前为什么不能写文件、不能调模型、不能写库。
- [ ] 我能写出 `graph.invoke(Command(resume="approve"), config={"configurable": {"thread_id": ...}})`，并说出缺 `thread_id` 会怎样。
- [ ] 我能列出 approve / edit / reject 三条分支的 `status`、是否导出、`review` 字段。
- [ ] 我能解释"编辑稿改了正文为什么会被拒绝"，并说出校验函数名（`_edited_report_or_draft`）。
- [ ] 我能解释"没有 pending interrupt 还 resume"的项目行为（`ValueError` → HTTP 409/422）。
- [ ] 我能解释幂等：第二个 approve 为什么返回同一 `export_path` 而不是重写。
- [ ] 我完成了练习 7.3，并亲眼看到"interrupt 之前写文件"会被写两次。
- [ ] 我完成了实验 8.2，并看到不可序列化对象让 invoke 失败。
- [ ] 我能说出项目里"唯一允许写文件的节点"是哪个，以及为什么这样设计。

---

## 10. 复盘问题与参考答案、下一步

**Q1：为什么 `review_node` 恢复后不重新渲染报告，而是直接用状态里的 `draft_report`？**

因为渲染是纯函数，重跑一次结果一样，但成本白花；更重要的是"审批人看到的东西"和"最终导出的东西"必须是**同一份**。如果恢复时重渲染，可能出现"审批时看到 1,234、导出时变成 1,230"的错位（例如数据在这期间被修了）。项目选择"草稿写进状态、审批期间冻结"，代价是草稿占检查点空间。

**Q2：`edit` 的修改稿存在哪？为什么不直接覆盖 `draft_report`？**

存在 `review["edited_report"]` 里，`draft_report` 保持原样。原因有两个：审计（能看出"程序渲染的原文"与"人工改的稿"）、校验（`_edited_report_or_draft` 要拿原草稿的数字去比修改稿）。`export_node` 里 `content = edited if decision == "edit" and edited.strip() else final` 就是"优先修改稿"的落地。

**Q3：如果两个人在同一 thread 上同时点"批准"和"拒绝"，会发生什么？**

项目的 `AppRuntime` 里所有图推进都在 `with self.lock:` 里串行执行，所以两个请求会被排队：第一个改掉状态（例如 approve 并导出），第二个进来时 `_extract_interrupt` 已经拿不到 pending，于是走"已导出 → 幂等返回"或"`ValueError`"。**未验证**：多进程部署（多个 worker）时 `SqliteSaver` 的文件锁行为、以及 `lock` 是否够用，本环境没有验证；这一点必须写进项目的"已知限制"。

**Q4：为什么 `route_after_review` 要先看 `TERMINAL_STATUSES`？**

因为 `review` 之前的任何一个节点都可能把 `status` 写成 `failed` / `budget_exceeded`（例如预算在 review 里耗尽）。这种情况下"review 的 decision"根本没被写入，路由必须直接结束，否则会在没有决策的情况下尝试 `export`。`_status(state) in TERMINAL_STATUSES → "end"` 就是这道保险。

**Q5：UI 上"审批按钮"为什么只在 `status=awaiting_approval` 时出现？**

因为按钮能做的只有一件事：把 `interrupt` 的 payload 展示给人、把选择发回去。没有 pending interrupt 时按钮没有目标，点了只会拿到 409。项目 `ui/streamlit_app.py` 的注释直接写了："没有待审批 interrupt 时，审批区不渲染，避免误发请求。"

**下一步**：

- 单元 17 会把"重启之后还能审批"补上：`SqliteSaver` 让检查点跨进程存活；同一份导出为什么天然幂等；以及"时间旅行"（读历史快照、从旧快照分叉）能做到什么、不能做到什么。
- 在那之前，建议把本单元实验的第 1 段输出抄下来，特别是 `next` 与 `REPLAYS` 两行——17 里会用同一组观察手段检查"重启后 `next` 还在不在"。

---

### 附：本单元引用的官方链接与核对记录

| 链接 | 核对日期 | 核对方式 | 结论 |
| --- | --- | --- | --- |
| <https://docs.langchain.com/oss/python/langgraph/interrupts> | 2026-10-19 | HTTP 抓取，返回 200 | 页面存在；"暂停-恢复由检查点承载"属长期稳定概念，正文未逐句核对（**未验证**细节） |
| <https://docs.langchain.com/oss/python/langgraph/persistence> | 2026-10-19 | HTTP 抓取，返回 200 | 页面存在（与单元 09、15 同一来源），正文未逐句核对（**未验证**细节） |
| <https://docs.langchain.com/oss/python/langgraph/graph-api> | 2026-10-19 | HTTP 抓取，返回 200 | 页面存在；`Command` 的用法以项目源码 `persistence.py` 的实际写法为准，官方正文未逐句核对（**未验证**细节） |

如果将来这些地址跳转或改名，请以 [LangGraph 文档首页](https://docs.langchain.com/oss/python/langgraph/overview) 的导航为准，并把新的核对日期写回本表。
