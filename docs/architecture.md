# 架构说明

本文回答四个问题：**系统由哪些层组成**、**一轮问答在图里怎么走**、**状态存在哪**、**人工审批如何暂停与恢复**。

> 事实基线：代码路径与函数名以 `ops-assistant/src/ops_assistant/` 当前文件为准；本文与源码不一致时以源码为准备。
> 诚实声明：初稿编写环境无法启动 Python 进程，本文的图与流程最初是**读代码画出来的**；2026-10-19 审查已在 Linux 沙箱实测运行整条链路（含审批中断/恢复、重启恢复），图的走向与实测轨迹一致（understand → knowledge → data → compute → draft → review 中断 → export）。

## 1. 模块分层

```mermaid
flowchart TB
    subgraph 入口层
        CLI[scripts/run_cli.py]
        API[api/main.py FastAPI]
        UI[ui/streamlit_app.py Streamlit]
    end

    subgraph 编排层
        WF[workflow/graph.py 10 节点]
        RT[workflow/persistence.py AppRuntime]
        ST[workflow/state.py AssistantState/Context]
        RO[workflow/routing.py 四处条件路由]
    end

    subgraph 业务层
        DOM[domain/question.py 解析]
        MET[domain/metrics.py 比较]
        REP[report/format.py 渲染<br/>report/export.py 落盘]
    end

    subgraph 能力层
        AG[agent/tools.py 6 个 @tool<br/>agent/simple_agent.py create_agent]
        KB[knowledge/ 加载 切分 检索 引用]
        DATA[data/ SQLite schema 生成 查询]
    end

    subgraph 基础层
        CFG[config.py Settings]
        LOG[logging_utils.py 脱敏日志]
        OBS[observability.py 指标]
    end

    CLI --> RT
    API --> RT
    UI -->|HTTP| API
    RT --> WF
    WF --> ST
    WF --> RO
    WF --> DOM
    WF --> MET
    WF --> KB
    WF --> DATA
    WF --> REP
    DOM --> DATA
    MET --> DATA
    AG --> DATA
    AG --> MET
    AG --> KB
    RT --> CFG
    WF --> OBS
    OBS --> LOG
```

要点：

- **入口层不含业务逻辑**：CLI/API/UI 都只调 `AppRuntime`，保证三种入口行为一致。
- **编排层不碰 SQL**：`nodes.py` 调 `queries`/`metrics`/`qa`，不自己拼 SQL（展示用 SQL 常量除外，见限制）。
- **能力层可单独使用**：`data`、`knowledge`、`domain` 都能在 pytest 里独立测。

## 2. 一轮问答的图路由

```mermaid
flowchart TD
    START([START]) --> U[understand<br/>规则解析 + 继承上一轮]
    U -->|route=clarify| C[clarify]
    U -->|route=knowledge| K[knowledge]
    U -->|route=data| D[data]
    U -->|route=fail| F[fail]

    K -->|混合问题| D
    K -->|纯知识问答 answered| END5([END])
    D -->|ok| P[compute]
    D -->|retryable 且未修复| R[repair]
    D -->|不可重试| F
    R --> D
    P --> DR[draft]
    DR --> RV[review<br/>interrupt 等审批]
    RV -->|approve/edit| E[export]
    RV -->|reject| END1([END])
    E --> END2([END])
    C --> END3([END])
    F --> END4([END])
```

四处条件路由（`routing.py` 和 `graph.py`）：

| 路由函数 | 判定依据 | 去向 |
| --- | --- | --- |
| `route_after_understand` | `status` 是否为终态；`route` 是否在允许集合 | `clarify` / `knowledge` / `data` / `fail` |
| `graph.py` 的 knowledge 路由 | 纯知识问答已 answered | `end` / `data` |
| `route_after_data` | 最近一条 `node="data"` 的 trace 的 `ok` / `retryable`；已修复次数 | `compute` / `repair` / `fail` |
| `route_after_review` | 审批结论（approve/edit → export；reject → 结束） | `export` / `end` |

预算（`state.budget`，默认 `steps=12`、`tool_calls=8`）：每个节点进入前 `_guard_step`，工具调用前 `_guard_tools`；超限写 `status="budget_exceeded"`，由路由送到 `fail`，**不抛 GraphRecursionError**。

## 3. 状态、检查点、线程、Store、运行时上下文

```mermaid
flowchart LR
    subgraph 一次进程
        RT[AppRuntime]
        RT --> CONN[(SQLite ops.db)]
        RT --> KBX[KnowledgeBase 索引]
        RT --> LLMX[LLMClient 可选]
        RT --> CKPT[Checkpointer]
        RT --> STORE[PreferenceStore]
        RT --> GRAPH[编译后的图]
    end
    CONN --> ORDERS[orders / refunds / regions / products]
    CKPT -->|SqliteSaver| CKDB[(checkpoints.db)]
    STORE -->|教学适配器| PREF[(preferences.db)]
    GRAPH -->|按 thread_id 存取| CKDB
```

这五者最容易混，对照表：

| 概念 | 存在哪 | 生命周期 | 本项目用法 | 官方对应 |
| --- | --- | --- | --- | --- |
| `AssistantState` | 图执行期间在内存；每个 superstep 合并 | 一轮/多轮（写入检查点后跨轮） | 问题、spec、检索结果、计算、报告、trace、errors、budget | LangGraph State |
| `Checkpointer` | `data/checkpoints.db`（演示）或内存（测试） | 跨进程（SqliteSaver） | `thread_id` 维度的图快照与中断 | LangGraph checkpointer |
| `thread_id` | 状态键 / 配置里的 `configurable.thread_id` | 会话级 | 多轮追问、审批恢复、导出文件命名 | LangGraph thread |
| `Store` | `data/preferences.db` | 跨会话 | `amount_unit` / `default_dimension` 偏好 | LangGraph Store（本项目用 SQLite duck-type 适配器，**教学简化**） |
| `Runtime Context` | 调用时注入，不写检查点 | 单次 `invoke` | 连接、知识库、模型、偏好、指标集合 | LangGraph Runtime Context |

**为什么 Store 适配器是教学简化**：官方 Store 有命名空间、批量检索等语义；`PreferenceStore` 只实现 `get/put/search/all_for_user` 这几个被用到的鸭子类型方法，行为与官方实现不保证一致。真正的生产项目应使用官方 Store 实现（或数据库 + 明确的接口）。

## 4. 人工审批（HITL）时序

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as API/CLI
    participant RT as AppRuntime
    participant G as 图
    participant CK as SqliteSaver
    participant FS as data/exports

    U->>API: POST /api/chat（问题）
    API->>RT: invoke_turn(question, thread_id)
    RT->>G: graph.invoke(state, config, context)
    G->>G: understand → knowledge → data → compute → draft
    G->>G: review_node 组装 payload
    G->>CK: 写入检查点（含 interrupt）
    G-->>RT: 返回，status=awaiting_approval
    RT-->>API: TurnResult（interrupt{id,payload}）
    API-->>U: 200 + 报告草稿 + 审批选项

    U->>API: POST /api/threads/{id}/resume {decision:approve}
    API->>RT: resume_turn(thread_id, "approve")
    RT->>G: graph.invoke(Command(resume=payload), config)
    G->>G: review_node 从函数开头重跑 → 读取 resume 值
    G->>G: export_node 原子写文件
    G->>CK: 写入最终状态
    RT-->>API: TurnResult（status=approved, export_path）
    API-->>U: 200 + 导出路径
```

关键约束（与 `workflow/nodes.py` 一致）：

1. `review_node` 在 `interrupt()` 之前**只做纯读/纯算**，不写文件、不写库；
2. 恢复时该节点从头重跑，所以副作用必须放在 `export_node`；
3. `export_node` 用临时文件 + `os.replace`，文件名为 `report-<thread_id>-<report_id>.md`；
4. 重复 `approve`：`resume_turn` 检测到无待处理中断且已导出，直接返回同一结果（幂等）；
5. 换进程后仍可恢复的前提是 `SqliteSaver`（`InMemorySaver` 重启即丢）。

### 4.1 流式路径（同一个图，另一种送达方式）

`GET /api/chat/stream`（SSE）与 `POST /api/chat` 跑的是**同一张图、同一套审批语义**，区别只在"进度怎么送达"：

```mermaid
sequenceDiagram
    participant U as 客户端
    participant API as api/main.py
    participant W as 工作线程(asyncio.to_thread)
    participant RT as AppRuntime
    participant G as 图

    U->>API: GET /api/chat/stream?question=...
    API->>RT: pending_approval(thread_id, user_id)
    Note over API,RT: 开流前拦截：待审批→409、越权→404、校验→422<br/>（SSE 发出 200 后就改不了状态码）
    API-->>U: 200 text/event-stream
    API->>W: pump()
    W->>RT: stream_turn(...)（持有运行时锁）
    RT->>G: graph.stream(state, config, context, stream_mode="updates")
    loop 每个超级步
        G-->>RT: {"<节点名>": 状态增量}
        RT-->>W: {"event":"node", ...}
        W-->>API: asyncio.Queue（call_soon_threadsafe）
        API-->>U: event: node / data: {...}
    end
    G-->>RT: {"__interrupt__": (Interrupt,)}
    RT-->>W: {"event":"interrupt", id, payload}
    API-->>U: event: interrupt
    RT-->>W: {"event":"final", turn}
    API-->>U: event: final（终止帧，turn 与 POST /api/chat 同构）
```

两个必须知道的约束：

1. **`RLock` 按线程归属**：`stream_turn` 在整个流期间持锁（与 `invoke_turn` 同样的串行化保证）。Starlette 消费同步生成器时会在线程池里逐个 `next()`，相邻两次可能落在不同线程，直接交给 `StreamingResponse` 会在释放锁时抛 `RuntimeError`。所以这里用单独一个工作线程迭代生成器，锁的获取与释放都在同一线程；
2. **客户端断开不中止工作流**：本轮会跑完并写入检查点，避免留下半截状态，只是后续帧不再发送。

审批仍走 `POST /api/threads/{id}/resume`：流式只送到 `interrupt` 为止，报告不会因为"流完了"就落盘。

## 5. 数据与知识两条链路

```mermaid
flowchart LR
    subgraph 知识链路
        Q1[问题] --> P1[parse_question]
        P1 --> S1[kb.search hybrid]
        S1 --> A1[answer_from_knowledge]
        A1 --> C1[引用 chunk_id]
        C1 --> R1[sources: type=doc]
    end
    subgraph 数据链路
        P1 --> SP[spec.current/previous]
        SP --> Q2[get_month_sales]
        SP --> Q3[get_region_breakdown]
        Q2 --> M[compare_months]
        Q3 --> M
        M --> T[table + chart + 报告]
        M --> R2[sources: type=sql]
    end
    R1 --> REP[render_analysis_report]
    R2 --> REP
    REP --> HITL[review 审批]
```

两条链路的产物都进入 `sources`，报告第 8 节把它们逐条列出——这是"数字可追溯"的实现方式：**报告里的每个数字都能指回一条 SQL 或一个知识块**。

## 6. 部署形态与已知约束

- **单进程、单 worker**：`AppRuntime` 持有 SQLite 连接与一把 `threading.Lock`，图调用串行；不要用 `uvicorn --workers 2`。
- **本地优先**：导出目录 `data/exports/`；无外网依赖（模型可选）。
- **可观测**：结构化日志（脱敏）+ `MetricsCollector`（模型调用/工具调用/token/错误）+ `TurnResult.trace`。
- **不做的事**：多租户鉴权、分布式队列、真实数据接入、模型微调。

更完整的限制与未验证项见 [limitations.md](limitations.md)，验证命令见 [verification.md](verification.md)。
