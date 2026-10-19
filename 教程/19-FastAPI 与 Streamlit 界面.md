# 单元 19：FastAPI 与 Streamlit 界面

> 建议时长：50–60 分钟｜前置：单元 09（会话/检查点）、16（审批）｜可跳过条件：如果你不打算演示 Web 界面，可以只读第 3、4 节并跳过第 5 节的 UI 代码。

## 1. 学习目标与前置

1. 看懂 `api/main.py` 的 9 个端点各自负责什么、返回什么状态码；
2. 能解释为什么"需要澄清"是 **200 + status=clarification**，而不是 HTTP 400；
3. 会起 API 与 Streamlit，并完成一次"提问 → 审批 → 下载报告"；
4. 说清 UI 刷新后状态从哪来（不是浏览器缓存，而是 `GET /api/threads/{id}`）；
5. 知道本项目为什么必须**单 worker**、以及用什么锁保护共享连接。

前置检查：会用 PowerShell 发 HTTP 请求（`Invoke-RestMethod`）；知道 HTTP 状态码 200/404/409/422 的区别。

## 2. 官方文档与阅读问题

- LangGraph 持久化（API 读取会话状态的底层能力）：<https://docs.langchain.com/oss/python/langgraph/persistence>
  - 阅读问题 1：`get_state` 与 `get_state_history` 分别适合什么场景？
  - 阅读问题 2：为什么"读状态"不应该推动图继续执行？
- LangChain 概览（本项目的模型层位置）：<https://docs.langchain.com/oss/python/langchain/overview>
- 工具文档（非 LangChain）：[FastAPI](https://fastapi.tiangolo.com/)、[Streamlit](https://docs.streamlit.io/)
  - 阅读问题 3：FastAPI 的 `lifespan` 适合放什么？为什么不在每个请求里重新建连接？

> 核对日期：2026-10-19。

## 3. 通俗解释与精确技术定义

**人话**：

- API 是"点菜窗口"：给问题、拿结果；需要确认时它不会自己上菜，而是停下来等你点"确认"。
- UI 是"餐桌"：显示会话列表、当前状态、执行轨迹、来源、表格、图，以及"批准 / 拒绝 / 修改并继续"三个按钮。
- 刷新页面不丢状态，是因为状态存在**服务端**（检查点），UI 只是重新取一次。

**定义**：

- **无状态 HTTP + 有状态服务**：请求本身不带会话，靠 `thread_id` 找到服务端检查点。
- **业务状态 vs 传输错误**：工作流跑完了但结论是"需要澄清"，这是业务状态（200）；参数不合法才是传输错误（422）。
- **生命周期（lifespan）**：应用启动时装配一次运行时（数据库连接、知识库、模型、检查点、编译好的图），关闭时释放。

## 4. 在主项目中的位置

`src/ops_assistant/api/main.py` 的端点（与源码一致）：

| 方法 | 路径 | 作用 | 关键状态码 |
| --- | --- | --- | --- |
| GET | `/api/health` | 健康检查：db、模型开关、检查点类型、预算 | 200 / 503 |
| GET | `/api/usage` | 用量与指标快照（不含密钥） | 200 |
| POST | `/api/threads` | 分配 thread_id（不写检查点） | 201 |
| GET | `/api/threads` | 会话列表（按更新时间倒序） | 200 |
| GET | `/api/threads/{id}` | 读取会话当前状态（不推进图） | 200 / 404 |
| POST | `/api/chat` | 跑一轮，直到审批或终态 | 200 / 422 |
| POST | `/api/threads/{id}/resume` | 提交审批结论 | 200 / 400 / 404 / 409 |
| GET | `/api/threads/{id}/report` | 读取已导出 Markdown | 200 / 404 |
| GET/PUT | `/api/preferences` | 跨会话偏好（Store 适配器） | 200 |

错误统一结构：

```json
{"detail": {"code": "THREAD_NOT_FOUND", "message": "...", "request_id": "..."}}
```

UI（`ui/streamlit_app.py`）至少覆盖：新建/切换会话、提问、显示 status 与澄清话术、执行轨迹、来源列表、表格、柱状图、审批（approve/reject/edit）、下载报告、偏好设置。**所有状态都从 API 重新拉取**，不在前端自己拼。

## 5. 文件、完整代码、配置与命令

### 5.1 启动

```powershell
cd .\ops-assistant
uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
# 另开一个终端
uv run streamlit run src/ops_assistant/ui/streamlit_app.py
```

浏览器：API 文档 <http://127.0.0.1:8000/docs>；UI 默认 <http://127.0.0.1:8501>。

### 5.2 用 PowerShell 走完一轮（可直接复制）

```powershell
$api = "http://127.0.0.1:8000"

# 1) 健康检查
Invoke-RestMethod -Uri "$api/api/health" | ConvertTo-Json -Depth 4

# 2) 提问（第一轮）
$body = @{
  question = "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
} | ConvertTo-Json
$turn = Invoke-RestMethod -Method Post -Uri "$api/api/chat" -ContentType "application/json" -Body $body
$turn.status            # 预期：awaiting_approval
$turn.thread_id
$turn.report.Substring(0, 120)

# 3) 审批通过
$threadId = $turn.thread_id
$resume = @{ decision = "approve"; comment = "口径确认" } | ConvertTo-Json
$approved = Invoke-RestMethod -Method Post -Uri "$api/api/threads/$threadId/resume" -ContentType "application/json" -Body $resume
$approved.status        # 预期：approved
$approved.export_path

# 4) 下载报告
$report = Invoke-RestMethod -Uri "$api/api/threads/$threadId/report"
$report.filename        # 预期：report-<threadId>.md
$report.markdown.Substring(0, 120)

# 5) 追问（同一 thread）
$body2 = @{ question = "那华东地区呢？"; thread_id = $threadId } | ConvertTo-Json
$follow = Invoke-RestMethod -Method Post -Uri "$api/api/chat" -ContentType "application/json" -Body $body2
$follow.status
$follow.calculation.regions.Count   # 预期：1
```

### 5.3 关键实现片段（与源码一致）

```python
# api/main.py（节选：运行时装配与读取）
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    runtime = AppRuntime.create(settings)   # 单 worker：一个进程一个运行时
    app.state.runtime = runtime
    try:
        yield
    finally:
        runtime.close()

def get_runtime(request: Request) -> AppRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail={"code": "RUNTIME_NOT_READY", ...})
    return runtime
```

```python
# workflow/persistence.py（节选：所有图调用都走同一把锁）
with self.lock:
    values = self.graph.invoke(state, config=config, context=context)
```

## 6. 预期输出与关键状态变化

**预期（未在本环境执行）**：

```text
status            : awaiting_approval
thread_id         : 0f3c1e2a-...
report 前 120 字  : ## 结论\n\n本期（2026-09）净销售额为 ...；...
（审批后）
status            : approved
export_path       : D:\...\ops-assistant\data\exports\report-<threadId>.md
filename          : report-<threadId>.md
markdown 前 120 字 : ## 结论\n\n...
（追问后）
status            : awaiting_approval
regions.Count     : 1
```

关键状态变化：

1. `/api/chat` 第一轮：写入检查点，`status=awaiting_approval`；
2. `/resume`：`review` 恢复 → `export` 落盘 → `status=approved`；
3. `/report`：只读文件，不改状态；
4. 追问：携带 `thread_id`，`understand` 继承上一轮 `last_spec`。

## 7. 修改练习

**练习 1（错误码）**：对不存在的 thread 调 `/report`，再用一个没有待审批的 thread 调 `/resume`。

- 预期：前者 404 `THREAD_NOT_FOUND`，后者 409 `NO_PENDING_INTERRUPT`（或 404，取决于 thread 是否存在）；
- 练习：在 UI 里给这两种情况写不同的提示文案。

**练习 2（改坏它）**：把 `/api/chat` 里的 `thread_id=payload.thread_id` 改成永远 `None`。

- 现象：每次提问都是新会话，追问拿不到上下文；
- 结论：`thread_id` 是"多轮"的唯一开关。

**练习 3（UI 状态来源）**：在 Streamlit 里，把"从 API 拉取状态"改成"用 `st.session_state` 缓存后不再刷新"。

- 现象：审批后页面仍显示 `awaiting_approval`；
- 正确做法：每次交互后调用 `GET /api/threads/{id}` 重新取。

## 8. 失败与边界实验

1. **API 没起就开 UI**：UI 报连接错误。检查顺序：端口 → `uvicorn` 日志 → `/api/health`。
2. **起两个 uvicorn worker**：`--workers 2` 时共享 SQLite 连接与锁会失效，可能出现重复导出/状态错乱。本项目明确要求单 worker。
3. **端口被占用**：换 `--port 8001`，同时改 UI 的 `OPS_API_BASE`（若已配置）。
4. **审批后直接刷新页面**：状态应为 `approved` 且能下载；如果不能，说明 UI 没有重新拉取。
5. **请求体缺字段**：`POST /api/chat` 传 `{}` → 422 且 `detail.code == "VALIDATION_ERROR"`。
6. **SQLite 文件只读**：启动时 health 报 `db_ok=false` 或运行时 `GRAPH_ERROR`；修复文件权限后重启。

## 9. 验收标准

- [ ] 能在 `/docs` 里看到全部端点与请求/响应模型；
- [ ] 用 PowerShell 完成"提问 → 审批 → 下载"三步；
- [ ] 追问返回的 `calculation.regions` 只有华东；
- [ ] 能解释"澄清为什么是 200"；
- [ ] 能说明为什么必须单 worker、锁保护了什么。

## 10. 复盘问题与参考答案、下一步

**Q1：为什么 `GET /api/threads/{id}` 不推进工作流？**
A：UI 刷新需要"读"，不是"跑"。推进由 `/api/chat` 与 `/resume` 负责，读接口只做 `graph.get_state`。

**Q2：为什么运行时放在 `app.state` 而不是全局变量？**
A：便于测试注入（本项目 API 测试直接把测试运行时塞进 `app.state.runtime`），也避免 import 时就建立连接等副作用。

**Q3：审批结论为什么不用 200/400 区分？**
A：审批是业务动作，HTTP 200 表示"请求已处理"；业务结论在 `status` 字段里（approved/rejected）。4xx 只用于"请求本身有问题"。

**Q4：UI 需要自己算差额吗？**
A：不需要，也不应该。UI 只渲染 `tables` / `chart` / `report`，所有数字来自服务端程序计算。

**下一步**：单元 20 讲怎么证明"它是对的"——测试集、确定性评估与可观测性，以及哪些结论**不能**由模拟测试得出。
