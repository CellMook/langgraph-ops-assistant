# 单元 22（附录）：流式输出——stream_mode 与进度反馈

> 建议时长 45 分钟（读 20 分钟 + 实验 15 分钟 + 复盘 10 分钟） | 前置：单元 06–09、16 | 可跳过条件：你的应用不需要实时反馈，同步 `invoke` 已经够用
>
> 定位：**附录/选修**。它是 22 个主线单元之外的第 23 份材料——主线把 `invoke`（一次性等到结果）讲透了，本附录补上 LangGraph 官方文档里与 `invoke` 并列的另一个核心入口 `stream`。主项目当前没有用它（见第 4 节的取舍），但任何"前端要进度条/打字机效果"的真实项目都会问你这一题。
>
> 版本基线：`langgraph 1.2.16`（2026-10-19 PyPI 最新版）。本单元全部代码与输出**已于 2026-10-19 在 Linux 沙箱实测**（退出码 0），不是"预期值"。

---

## 1. 学习目标与前置

完成本单元后，你应该能：

1. 说清 `invoke` 与 `stream` 的关系：`invoke(x)` ≈ `list(stream(x))[-1]` 的语义（拿到最终状态），而 `stream` 是**把图执行过程拆成事件流**。
2. 区分三种最常用的 `stream_mode`：`values` / `updates` / `debug`，并说出各自适合什么界面。
3. 解释**多模式组合**为什么返回 `(mode, chunk)` 二元组。
4. 指出**审批中断**在流里的形态：`{"__interrupt__": (Interrupt(...),)}`，以及 resume 之后流怎么"续上"。
5. 说明 `subgraphs=True` 的作用与命名空间元组的含义。
6. 讲清本项目为什么暂时没用流式（第 4 节），以及接进去要改哪三处。

前置检查（一条不满足就先回对应单元）：

- 会编译带 checkpointer 的图（单元 09）；
- 理解 `interrupt()` / `Command(resume=...)`（单元 16）；
- 知道节点返回的是"状态增量"（单元 06/07）。

## 2. 官方文档与阅读问题

**官方文档：LangGraph 流式输出（https://docs.langchain.com/oss/python/langgraph/streaming，核对日期 2026-10-19）**

带着 5 个问题读（读不到答案就跳过，回来做实验 1）：

1. `stream_mode` 有哪些取值？官方最推荐新手先学哪一个？
2. `values` 与 `updates` 的区别，用"全量 vs 增量"怎么说？
3. 一次 `stream` 调用能不能同时要多种模式？返回结构变成什么？
4. 子图内部的节点默认会不会出现在流里？哪个参数打开它？
5. 带 checkpointer 的图中断后再 resume，`stream` 与 `invoke` 的行为有什么不同？

> **官方事实（2026-10-19 实测复核）**：`CompiledStateGraph.stream` 的签名（`langgraph 1.2.16`）为
> `stream(input, config=None, *, context=None, stream_mode=None, output_keys=None, subgraphs=False, ...)`；
> `stream_mode` 接受单个字符串或**字符串序列**；`subgraphs: bool` 控制是否输出子图事件。
> 本单元所有行为断言都来自当日实测，不只是文档转述。

## 3. 通俗解释与精确技术定义

### 3.1 一句话比喻

- `invoke` = 你在餐厅点了菜，然后干等到全部上齐。
- `stream` = 后厨每做好一道就端出来一道，你能实时看到"凉菜好了→热菜好了→汤好了"。
- `stream_mode` = 你想看"每道菜本身"（values）、"哪道菜好了"（updates）、还是"后厨操作日志"（debug）。

### 3.2 精确语义（实测）

| 模式 | 每个事件的含义 | 事件示例（本项目工作流实测） | 适合 |
| --- | --- | --- | --- |
| `values` | **每个超级步之后的全量状态**（含最初输入那份） | `{'steps': ['a']}` → `{'steps': ['a', 'b']}` | 每步都要渲染整份界面 |
| `updates` | **单个节点的状态增量**：`{节点名: 返回的dict}` | `{'compute': {'calculation': ..., 'tables': ...}}` | 进度条/步骤高亮（推荐先学） |
| `debug` | 框架级事件：`task`（将执行）/ `task_result`（已执行），带 `step` 序号与时间戳 | `{'step': 2, 'type': 'task', 'payload': {'name': 'b', ...}}` | 排查执行顺序与并行调度 |
| `messages` | 模型 token 级输出（LLM 场景） | 略（本项目无模型时不出现） | 打字机效果 |
| `custom` | 节点里用 `get_stream_writer()` 主动发的自定义事件 | 略 | 业务进度（"正在查询第 2 个地区"） |

三条实测规则：

1. **多模式组合**：`stream_mode=["updates", "values"]` 时，每次产出 `(mode, chunk)` **二元组**，两种事件按发生顺序交错。
2. **中断**：图停在 `interrupt()` 时，`updates` 流会产出 `{'__interrupt__': (Interrupt(value=..., id=...),)}`——它不是节点名，是保留键。
3. **恢复**：`stream(Command(resume=...), config=同thread)` 会从断点继续产出剩余节点的增量（例如 `review`、`export`）。

> **教学简化**：`messages` 与 `custom` 两种模式本单元不展开——前者需要真实模型才有意义，后者适合大型图。先掌握 `updates` + 组合模式，剩下的读官方文档即可平移。

## 4. 在主项目中的位置

**当前状态（2026-10-20 起）**：主项目**两条路径都有**，各自服务不同消费者。

| 路径 | 实现 | 用途 |
| --- | --- | --- |
| 非流式 | `AppRuntime.invoke_turn / resume_turn` 内部用 `graph.invoke`；`POST /api/chat` 返回完整 `TurnResponse` | Streamlit 界面、评估脚本、所有既有测试。**仍是默认路径** |
| 流式 | `AppRuntime.stream_turn` 内部用 `graph.stream(..., stream_mode="updates")`；`GET /api/chat/stream`（SSE）；CLI `--stream` | 需要节点级进度反馈的客户端 |

事件契约（`stream_turn` 与 SSE 完全一致，SSE 只是多一层 `event:`/`data:` 封装）：

```
start      {"event","thread_id","request_id","question"}
node       {"event","node","update"}        # 每个超级步一帧，node 是真实节点名
interrupt  {"event","id","payload"}          # 与 TurnResult.interrupt 同构
final      {"event","turn"}                  # turn 与 POST /api/chat 响应体同构
error      {"event","code","message","request_id"}
```

`final` 与 `error` 互斥且一定是最后一帧：消费端只认这两个之一作为终止信号，`node`/`interrupt` 属于过程信息，**丢掉也不影响终态正确性**。这个设计让流式客户端可以复用既有的 `TurnResponse` 处理逻辑。

**为什么非流式仍是默认路径**（这是设计选择，不是能力缺失）：

1. 本项目的"慢"不在模型（默认纯规则，整轮 < 100ms 实测），SQLite 查询也是毫秒级——**没有值得流式的等待**，UI 不需要打字机；
2. 流式把传输契约从"一个 JSON"变成"一串帧"，错误处理要多一条路径（HTTP 状态码只能在开流前用，开流后只能用 `error` 事件）；
3. 教学优先级：先讲透"状态机 + 审批"，再讲"传输层优化"。

**接进主项目时真正踩到的三个坑**（都已写进代码注释与测试）：

1. **`RLock` 按线程归属**。`stream_turn` 在整个流期间持有运行时锁（与 `invoke_turn` 同样的串行化保证：同一时刻只推进一张图）。但 Starlette 的 `StreamingResponse` 消费同步生成器时会在线程池里逐个 `next()`，**相邻两次可能落在不同线程**，释放锁时抛 `RuntimeError: cannot release un-acquired lock`。所以 `/api/chat/stream` 用**单独一个工作线程**（`asyncio.to_thread`）迭代生成器，再经 `asyncio.Queue` 把帧交回事件循环——锁的获取与释放都发生在同一个线程。
2. **HTTP 状态码只能在开流前用**。SSE 一旦发出 200 与响应头就改不了状态码，所以"待审批"必须在开流前拦成标准 409（为此加了公开方法 `AppRuntime.pending_approval`），参数校验仍是 422，越权 thread 仍是 404。开流之后才发生的竞态只能降级成 `error` 事件——权威拦截仍在 `stream_turn` 内部（锁内完成）。
3. **客户端断开不等于该中止工作流**。断开后本轮会跑完并写入检查点，避免留下半截状态；只是后续帧不再发送。

**仍未接的一处**：

| 位置 | 改动 |
| --- | --- |
| `ui/streamlit_app.py` | 用 `st.write_stream` 或 `st.status` 消费 SSE，把节点名映射成中文步骤提示。改造要重做 `st.session_state` 同步，属于练习 2 |

CLI 已可直接看逐帧输出（这就是练习 2 原来的验收标准）：

```bash
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？" --stream
# · 会话 425f855aeb66
# · 解析问题（understand）
# · 检索知识库（knowledge）
# · 查询经营数据（data）
# · 计算指标与差额（compute）
# · 渲染报告草稿（draft）
# ! 待人工审批：report_approval，可选 approve/reject/edit
# 之后是与非流式完全一致的结果输出

uv run python scripts/run_cli.py -q "..." --stream --json   # JSONL：每事件一行
```

## 5. 文件、完整代码、配置与命令

保存路径：`教程/实验/22_streaming.py`

```python
"""单元 22（附录）实验：把LangGraph 的流式输出接到主项目工作流上。

用法（在 ops-assistant 目录执行）：
    uv run python ../教程/实验/22_streaming.py

本实验不需要模型密钥（纯规则模式），验证四件事：
    1) stream_mode="updates"：每个节点跑完立刻吐一条 {节点名: 状态增量}；
    2) 多模式组合 ["updates", "values"]：返回 (模式, 数据) 二元组；
    3) 审批中断在流里长什么样：{"__interrupt__": (Interrupt(...),)}；
    4) resume 之后继续流式收尾（review/export 的增量）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2] / "ops-assistant"
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ops_assistant.config import load_settings  # noqa: E402
from ops_assistant.workflow.persistence import AppRuntime  # noqa: E402

MAIN_QUESTION = (
    "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
)


def main() -> None:
    settings = load_settings(PROJECT_DIR)
    runtime = AppRuntime.create(settings, use_memory_checkpointer=True)
    try:
        thread_id = "exp22-stream"
        config = {
            "configurable": {"thread_id": thread_id, "user_id": "demo-user"},
        }
        context = runtime._context("demo-user")  # noqa: SLF001 - 教学实验直接复用装配逻辑

        print("== 1) stream_mode='updates'：节点级进度反馈 ==")
        for chunk in runtime.graph.stream(
            {"question": MAIN_QUESTION, "thread_id": thread_id, "user_id": "demo-user"},
            config=config,
            context=context,
            stream_mode="updates",
        ):
            for node, update in chunk.items():
                keys = sorted(update.keys()) if isinstance(update, dict) else ["<非dict>"]
                print(f"  节点 {node:<12} 写入字段: {keys}")

        print("== 2) 状态快照：应停在 awaiting_approval ==")
        snapshot = runtime.graph.get_state(config)
        print("  status:", snapshot.values.get("status"))
        print("  interrupt:", (snapshot.next and "有" ) or "无")

        print("== 3) 多模式组合 ['updates', 'values']（resume 之后）==")
        from langgraph.types import Command  # noqa: E402

        for mode, chunk in runtime.graph.stream(
            Command(resume={"decision": "approve", "comment": "流式实验"}),
            config=config,
            context=context,
            stream_mode=["updates", "values"],
        ):
            if mode == "updates":
                for node, update in chunk.items():
                    keys = sorted(update.keys()) if isinstance(update, dict) else ["<非dict>"]
                    print(f"  [updates] 节点 {node:<12} 写入字段: {keys}")
            else:
                status = chunk.get("status")
                export_path = chunk.get("export_path")
                print(f"  [values ] 全量状态 status={status} export={export_path}")

        print("== 4) 结论对照 ==")
        final = runtime.graph.get_state(config).values
        print("  最终 status    :", final.get("status"))
        print("  导出文件       :", final.get("export_path"))
        print("  轨迹节点顺序   :", [t.get("node") for t in final.get("trace", [])][:12])
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
```

用法（在 `ops-assistant` 目录执行）：

```bash
uv run python ../教程/实验/22_streaming.py
```

三个实现细节值得停下来看：

1. `context=context`：主项目的节点依赖全部从 `Runtime[AssistantContext]` 读取（单元 15），`stream` 与 `invoke` 一样接受 `context=`，**不用改任何节点代码**。
2. `runtime._context(...)` 带下划线是"内部方法"的意思——教学实验里借用装配逻辑可以，生产代码不要这么写：主项目已有公开的 `AppRuntime.stream_turn`，装配细节都封在里面（练习 2 会让你读它）。
3. 第一次 `stream` 的输入是**状态增量字典**（与 `invoke_turn` 内部喂 `initial_state(...)` 不同——这里为了实验简洁只给了三个字段；图是 `total=False` 的 TypedDict，缺省字段由各节点自兜底）。

## 6. 预期输出（2026-10-19 实测，退出码 0）

```text
== 1) stream_mode='updates'：节点级进度反馈 ==
  节点 understand   写入字段: ['budget', 'clarification', 'last_spec', 'route', 'spec', 'status', 'trace']
  节点 knowledge    写入字段: ['budget', 'errors', 'knowledge_hits', 'policy_context', 'sources', 'trace']
  节点 data         写入字段: ['budget', 'data_result', 'sources', 'tables', 'trace']
  节点 compute      写入字段: ['budget', 'calculation', 'chart', 'errors', 'sources', 'tables', 'trace']
  节点 draft        写入字段: ['budget', 'draft_report', 'errors', 'status', 'trace']
  节点 __interrupt__ 写入字段: ['<非dict>']
== 2) 状态快照：应停在 awaiting_approval ==
  status: awaiting_approval
  interrupt: 有
== 3) 多模式组合 ['updates', 'values']（resume 之后）==
  [values ] 全量状态 status=awaiting_approval export=None
  [updates] 节点 review       写入字段: ['budget', 'errors', 'final_report', 'messages', 'review', 'status', 'trace']
  [values ] 全量状态 status=approved export=None
  [updates] 节点 export       写入字段: ['budget', 'export_path', 'final_report', 'status', 'trace']
  [values ] 全量状态 status=approved export=.../data/exports/report-exp22-stream.md
== 4) 结论对照 ==
  最终 status    : approved
  导出文件       : .../data/exports/report-exp22-stream.md
  轨迹节点顺序   : ['understand', 'understand', 'knowledge', 'data', 'data', 'compute', 'compute', 'draft', 'review', 'export']
```

四个必须亲眼确认的现象：

1. **`__interrupt__` 不是节点**：它出现在 `updates` 流的最后，值是 `Interrupt` 对象的元组——这就是"图暂停了"在流里的形态。用 `isinstance(update, dict)` 区分（实验里打印 `<非dict>` 的那行）。
2. **resume 后流是"续"不是"重放"**：第 3 段只出现 `review`/`export` 的增量，**没有**把 understand→draft 再发一遍。检查点记住了进度（单元 09/16 的语义在流式下不变）。
3. **`values` 比 `updates` 多一份"初始输入"**：第 3 段开头那条 `status=awaiting_approval` 的 values 事件，就是 resume 前的最后全量状态。
4. **`review` 的增量里有 `final_report`**：结合单元 16——interrupt 之前的代码重跑了，恢复值变成了这次执行的输入。

## 7. 修改练习

1. **加 `debug` 模式**：把第 1 段改成 `stream_mode="debug"`，数一数 `type` 有几种取值（task / task_result），确认 `step` 从 1 递增。再对照单元 18：把 `understand` 到 `data` 的边改成两条并行边，观察同一 `step` 下两个 `task` 事件的顺序是否稳定（**实测记录**：并行任务事件顺序不保证稳定，这是官方文档明说的"并行分支无顺序保证"）。
2. **`stream_turn` 已在主项目实现——改成读它并扩展**：`AppRuntime.stream_turn(question, *, thread_id=None, user_id=..., request_id=...) -> Iterator[dict]` 已落地，配套 `GET /api/chat/stream`（SSE）与 CLI `--stream`。先跑 `uv run python scripts/run_cli.py -q "..." --stream` 看逐帧输出，再读 `tests/test_streaming.py`。三个动手方向：
   - **把 Streamlit 接上**（唯一还没接的一处）：用 `st.write_stream` 或 `st.status` 消费 SSE，把 `node` 事件的节点名映射成中文步骤提示。注意 `st.session_state` 的同步要重做——终态仍以 `final` 事件里的 `turn` 为准，不能用 `node` 增量拼；
   - **加 `stream_mode="updates"` 之外的模式**：把 `stream_turn` 改成接受 `stream_modes` 参数，试 `("updates", "custom")` 组合，观察帧变成 `(mode, chunk)` 二元组后 `node` 事件要怎么解包（第 3 节讲过这个结构）；
   - **验证线程约束**：`stream_turn` 的 docstring 写明必须在**同一个线程**里迭代到结束（`RLock` 按线程归属）。自己写一个"在两个线程里交替 `next()`"的脚本，看它抛什么错，再回头看 `api/main.py` 里为什么必须用 `asyncio.to_thread` + `asyncio.Queue`。
3. **改坏它**：把第 3 段的 `Command(resume=...)` 换成重新 `stream({"question": ...}, config=同thread)`。预期：LangGraph 对"已有 interrupt 待处理又给新输入"的行为——记录你的版本实际做了什么（报错/忽略/新起一轮），这正好是面试里"checkpoint 语义"的加分题。

## 8. 失败与边界实验

1. **`stream_mode="messages"`（无模型）**：预期：整轮下来几乎没有事件（没有模型调用就没有 token），最后只有收尾事件。结论：`messages` 模式的价值只在真实模型下体现。
2. **子图不打开 `subgraphs=True`**：把单元 18 的子图实验改成 `stream(..., stream_mode="updates")`，预期只看到外层节点的增量；加 `subgraphs=True` 后事件变成 `(namespace_tuple, chunk)` 二元组（**实测**：形如 `(('call:<uuid>',), {'sub': ...})` 与 `((), {'call': ...})`——空元组是根图，带名字的元组是子图命名空间）。注意：加了这个参数后**所有**事件都变成二元组，消费端要同步解包。
3. **流到一半断开**（模拟网络断开）：在 `for chunk in ...stream(...)` 里 `break`，然后 `graph.get_state(config)`。预期：状态已经推进到已执行完的节点（checkpointer 每个超级步后落盘，单元 09），`snapshot.next` 指向未执行的节点；重新 `stream(None, config=...)` 可以继续（**先自己试再查文档**：`stream(None, ...)` 与 `stream(Command(resume=...), ...)` 在"无中断、仅断开"场景下的差异，记录实测结果）。

## 9. 验收标准（可勾选）

- [ ] 我能不看表格说出 `values` / `updates` / `debug` 三者的区别与适用场景。
- [ ] 我运行了 `22_streaming.py`，亲眼看到 `__interrupt__` 事件，并能解释它为什么不是节点。
- [ ] 我能解释 resume 后的流为什么只包含 `review`/`export`。
- [ ] 我知道 `subgraphs=True` 会把事件变成 `(namespace, chunk)` 二元组。
- [ ] 我能说出本项目不用流式的三个理由，以及接进去要改的三处。

## 10. 复盘问题与参考答案、下一步

1. **Q：`invoke` 和 `stream` 最终状态会不一样吗？**
   A：不会。两者驱动同一张图、同一套检查点语义；`invoke` 等价于把流消费完取最终状态（注意 `values` 模式最后一个事件就是终态，`updates` 模式需要自己累积或再 `get_state`）。
2. **Q：为什么推荐先学 `updates` 而不是 `values`？**
   A：`values` 每步发**全量**状态——报告渲染出来可能有几千字符，每步重发一遍浪费带宽；`updates` 只发增量，天然适合"步骤高亮"。`values` 适合状态很小、界面每次都整体重绘的场景。
3. **Q：SSE 和 WebSocket 该选哪个？**
   A：本项目这种"单向通知"用 SSE 足够（HTTP 之上的一条长连接，FastAPI 用 `StreamingResponse` + `text/event-stream`）；只有需要客户端中途插话（打断、改问题）才需要 WebSocket。**教学简化**：这里只讲结论，动手实现属于练习 2 的扩展。
4. **Q：`stream` 传入的 `context=` 和单元 15 的 Runtime Context 是一回事吗？**
   A：是同一个东西。`invoke` / `stream` / `get_state` 都接受 `config`，`invoke` / `stream` 另外接受 `context`（依赖注入），节点里用 `Runtime[AssistantContext]` 读取——实验第 1 段能跑通就证明了这条链路对 `stream` 同样生效。

**下一步**：回到单元 21 完成你的求职材料与 5 分钟演示；如果想继续深挖，官方文档的 streaming 章节还讲了 `astream`（异步版）与 token 级聚合，配合真实模型练习。
