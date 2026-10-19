# ops-assistant

经营数据分析与知识库智能助手：**SQLite 经营数据 + 本地知识库 + LangGraph 固定工作流 + 人工审批**。

它回答的是这类问题：

> 「2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。」

回答过程不是"模型自由发挥"，而是十个节点按固定顺序执行：解析问题 → 检索口径文档 → SQL 取数 → 计算差额与分解 → 用固定 8 段模板渲染报告 → **暂停等人工审批** → 审批通过后原子落盘。所有金额都由程序计算，模型最多被允许改写一句**不含数字**的措辞。

> **这不是生产系统**：数据由固定种子生成，无鉴权、无多租户、无成本核算，单进程单 worker。完整边界见 [../docs/limitations.md](../docs/limitations.md)。

---

## 1. 前置要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | **≥ 3.11** | `pyproject.toml` 的 `requires-python` |
| [uv](https://docs.astral.sh/uv/) | 任意较新版本 | 全程使用 `uv sync` / `uv run`，不需要手动建 venv |
| API Key | **可选** | 留空即纯规则模式；模型只影响润色与知识库答案的措辞 |
| 端口 | 8000（API）、8501（Streamlit 默认） | 只跑 CLI 时不需要端口 |

依赖版本已在 `pyproject.toml` 中锁定：`langchain==1.4.6`、`langgraph==1.2.16`、`langchain-openai==1.7.2`、`langchain-text-splitters==1.1.3`、`langgraph-checkpoint-sqlite==3.1.2`，其余按范围安装。**没有 `uv.lock` 随仓库分发**，首次 `uv sync` 会按上述约束解析。

---

## 2. 安装

```bash
cd ops-assistant
uv sync
```

`uv sync` 会以可编辑方式安装 `src/ops_assistant`；`pyproject.toml` 同时配置了 `[tool.pytest.ini_options] pythonpath = ["src"]`，所以即使不安装也能 `import ops_assistant`。

安装开发依赖（跑测试 / lint 才需要）：

```bash
uv sync --group dev     # pytest>=8、ruff>=0.6
```

---

## 3. 配置（全部可选）

```bash
cp .env.example .env        # Windows PowerShell: copy .env.example .env
```

`.env` 已被 `.gitignore` 忽略。**加载规则**：项目根的 `.env` 只在变量尚未存在于当前进程环境时生效（不覆盖已有环境变量），见 `config.py` 的 `_load_dotenv`。

| 变量 | 默认 | 作用 | 留空/缺失时 |
| --- | --- | --- | --- |
| `OPS_MODEL` | 空 | 对话模型，形如 `openai:gpt-4.1-mini`、`deepseek:deepseek-chat`、`dashscope:qwen-plus` | 纯规则模式：不调用任何模型 |
| `OPS_MODEL_PROVIDER` | 空 | `OPS_MODEL` 不带 `provider:` 前缀时使用 | 必须能推断 provider，否则不启用模型 |
| `OPS_API_KEY` | 空 | 模型密钥，只留在本机 | 有 `OPS_MODEL` 但无 key → `llm_enabled=False`，日志 WARNING，**程序继续** |
| `OPS_BASE_URL` | 空 | OpenAI 兼容端点（OpenAI / DeepSeek / DashScope / Ollama） | 用 provider 官方默认地址 |
| `OPS_EMBEDDING_MODEL` | 空 | 真实 embedding 模型 | 用确定性哈希向量 `HashingEmbeddings`（教学用，不是语义模型） |
| `OPS_EMBEDDING_BASE_URL` / `OPS_EMBEDDING_API_KEY` | 空 | embedding 专用端点/密钥 | 复用 `OPS_BASE_URL` / `OPS_API_KEY` |
| `OPS_EMBEDDING_DIM` | `512` | 哈希向量维度 | **改成别的值后必须重建知识索引** |
| `OPS_MAX_STEPS` | `12` | 单轮最大步数 | 超过后以 `budget_exceeded` 结束，不会死循环 |
| `OPS_MAX_TOOL_CALLS` | `8` | 单轮最大工具调用数 | 每次工具调用前检查 |
| `OPS_REQUEST_TIMEOUT_S` | `60` | 单次模型请求超时（秒） | — |
| `OPS_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` | 非法值打警告并回退 `INFO` |
| `OPS_APP_ENV` | `dev` | `dev`/`test`/`prod` | 非法值打警告并回退 `dev` |

> 注意：单轮预算只统计 `knowledge` 与 `compute` 两个节点，**不统计**规则解析、取数、渲染等确定性步骤；`recursion_limit` 取 `max(4, max_steps * 2)`（见 `workflow/persistence.py` 的 `_config`）。所以把 `OPS_MAX_STEPS` 调到 1 会立刻触发预算失败——这正是演示"失败路径"最省事的方法。

### 探测模型能力（有 key 时先做这一步）

```bash
uv run python scripts/check_model.py
```

输出一份 JSON 报告（**不打印密钥**，URL 只保留协议/主机/端口），分别探测：配置是否存在、一次文本调用、`bind_tools`、`with_structured_output`。任何一项失败都只降级对应能力，不影响"取数 + 计算 + 模板报告"这条主链路。

---

## 4. 初始化：生成演示数据

```bash
uv run python scripts/bootstrap.py
```

它做三件事，全部**不调用模型**：

| 产物 | 路径 | 内容 |
| --- | --- | --- |
| 演示数据库 | `data/ops.db` | 5 个地区、12 个商品、4 个渠道、`2025-01` 至 `2026-09` 的订单与退款，种子 `SEED = 20260901` |
| 知识索引 | `data/knowledge_index.json` | 由 `data/knowledge/*.md` 四篇文档（指标口径 / 退款与归属 / 地区说明 / 数据字典）切分并向量化 |
| 标准答案 | `data/ground_truth.json` | 由**独立脚本**重算，用于和项目自身计算做交叉核对 |
| 日志 | `data/logs/ops-assistant.log` | UTF-8 追加，密钥字段自动脱敏 |

常用变体：

```bash
uv run python scripts/bootstrap.py --skip-ground-truth    # 只建库+索引，跳过标准答案
uv run python scripts/compute_ground_truth.py --db data/ops.db --out data/ground_truth.json
```

**重复执行是安全的**：`bootstrap.py` 会重建数据库（覆盖 `data/ops.db`），不会清空 `data/exports/` 和 `data/checkpoints.db`。若希望"从零开始"，删掉整个 `data/` 再跑一次即可（但会丢失历史会话与导出报告）。

### 演示数据里刻意埋的 8 类边界

面试时这些就是"你考虑过什么"的证据：

| 边界 | 位置 | 用来演示什么 |
| --- | --- | --- |
| 整月无订单 | `2025-01` | "无记录"≠"销售额 0 元" |
| 地区 + 月份无订单 | 东北 `2025-09`、`2026-08` | 分解表里缺行 ≠ 该地区为 0 |
| 净额恰好为 0 | 西南 `2026-09` | 真零 vs 缺数据 |
| 只有 pending 退款 | 华北 `2026-07` | pending 不计入净额 |
| 跨月退款 | 订单在 `2026-08`、退款在 `2026-09` | 退款按**退款发生月**归属 |
| 净额为负 | 华南 `2026-03` | 退款 > 毛销售，百分比无意义 |
| 商品级零基数 | `2026-05` | 基数为 0 时不给百分比 |
| 渠道维度不支持 | 任意 | `unsupported_dimension` 显式返回而不是瞎算 |

---

## 5. 运行

### 5.1 CLI（最快，不需要起服务）

```bash
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
```

**预期（未运行，静态推演）**：打印 `会话 / 状态：awaiting_approval / 答复 / 8 段报告 / 执行轨迹 / 来源 / 预算 / 审批摘要`。

```bash
uv run python scripts/run_cli.py --thread-id <thread_id> --resume approve --comment "OK"
uv run python scripts/run_cli.py --thread-id <thread_id> --resume reject
uv run python scripts/run_cli.py --thread-id <thread_id> --resume edit --edited-report-file draft.md

uv run python scripts/run_cli.py --health              # 运行时健康（JSON）
uv run python scripts/run_cli.py --list-threads        # 会话列表
uv run python scripts/run_cli.py --read-thread <id>    # 从检查点读状态，不推进
uv run python scripts/run_cli.py -q "..." --json       # 机器可读（含完整 report/trace/sources）
```

约束：五种动作（`-q` / `--resume` / `--read-thread` / `--list-threads` / `--health`）**互斥**；`--resume` 必须配 `--thread-id`；`--edited-report-file` 只能和 `--resume` 一起用。退出码：`0` 成功、`1` 工作流失败或运行时错误、`2` argparse 用法错误。

### 5.2 HTTP API

```bash
uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
```

- Swagger：<http://127.0.0.1:8000/docs>；根路径 `/` 返回服务名与文档地址；`/api/health` 健康检查。
- **必须单 worker**：运行时是进程内单例（SQLite 连接、检查点、偏好存储、`threading.Lock`）。加 `--workers N` 会让每个进程各写一份检查点，同一 thread 的历史互相覆盖。
- 所有触图接口都是同步 `def`（由 FastAPI 丢进线程池），内部再用 `AppRuntime.lock` 串行化。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/threads` | 预分配 `thread_id`（**不写检查点**，第一轮 chat 才产生状态） |
| `GET` | `/api/threads` | 会话列表（`user_id`、`limit` 可选） |
| `GET` | `/api/threads/{id}` | 从检查点读当前状态（不推进工作流） |
| `POST` | `/api/chat` | 跑一轮，直到待审批或终态 |
| `POST` | `/api/threads/{id}/resume` | 提交审批：`{"decision": "approve"|"reject"|"edit", "comment": "", "edited_report": null}` |
| `GET` | `/api/threads/{id}/report` | 读取已导出的 Markdown |
| `GET` / `PUT` | `/api/preferences` | 读取/写入用户偏好（`amount_unit` ∈ 元/万元，`default_dimension` ∈ region/product/month） |
| `GET` | `/api/health` | `status / model_enabled / kb_chunks / db_ok / max_steps / max_tool_calls / checkpointer / closed` |
| `GET` | `/api/usage` | 进程级累计用量 + 最近一次指标快照 |

`status` 是**工作流层**的结果，不是 HTTP 状态码：`awaiting_approval` / `approved` / `rejected` / `clarification` / `failed` / `budget_exceeded`。只有传输层问题才用 4xx/5xx（统一错误体 `{"detail": {"code", "message", "request_id", "detail"}}`）。

先验接口再碰 UI：

```bash
curl -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" \
  -d '{"question":"2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"}'
```

### 5.3 Streamlit UI

```bash
# 终端 1
uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
# 终端 2
uv run streamlit run src/ops_assistant/ui/streamlit_app.py
```

环境变量：`OPS_API_BASE_URL`（默认 `http://127.0.0.1:8000`）、`OPS_USER_ID`（默认 `demo-user`）。

前端只通过 HTTP 调后端（不碰图/数据库），`st.session_state` 只存 `thread_id` / `user_id`，业务状态每次从 API 重新拉——所以**刷新页面不会丢状态**，也不会和检查点不一致。审批按钮只在存在待审批 `interrupt` 时渲染。

---

## 6. 测试

```bash
uv run pytest
uv run ruff check .        # 需要 --group dev
```

- `pyproject.toml` 已配置 `testpaths = ["tests"]`、`pythonpath = ["src"]`、`addopts = "-ra"`。
- 测试按 T01–T23 组织：`test_data.py` / `test_queries.py` / `test_question.py` / `test_metrics.py` /
  `test_ground_truth.py` / `test_knowledge.py` / `test_workflow.py` / `test_hitl.py` / `test_api.py`。
- 评测入口：`uv run python eval/run_eval.py --verbose`（只评确定性路径，**不评估模型行为**）。
- 汇总说明与排查顺序见 [../docs/verification.md](../docs/verification.md)。
- 本仓库的文档与代码**没有在当前环境执行过任何测试**（环境无法启动进程），因此任何地方都不写"测试已通过"。

---

## 7. 演示

完整的 5 分钟台词、屏幕预期、6 条失败路径命令和面试追问，见 [../docs/demo-script.md](../docs/demo-script.md)。最短可跑版本：

```bash
# 正常路径
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
uv run python scripts/run_cli.py --thread-id <thread_id> --resume approve --comment "口径已核对"

# 三条失败路径
uv run python scripts/run_cli.py -q "九月销售额多少？"                       # 缺年份 → clarification
uv run python scripts/run_cli.py -q "2025 年一月销售额多少？"                 # 无记录 ≠ 0 元
$env:OPS_MAX_STEPS="1"; uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？"   # budget_exceeded
```

---

## 8. 故障排查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `uv sync` 报 `Readme file does not exist: README.md` | 本文件缺失（历史状态） | 本文件已补齐；若仍报错，确认在 `ops-assistant/` 目录内执行 |
| `init 运行时失败` / `健康检查 db_ok=false` | `data/ops.db` 不存在或损坏 | 重跑 `uv run python scripts/bootstrap.py` |
| 回答里出现 `METADATA_ERROR: 读取可用月份/地区失败（…）` | 数据库不可读（缺表/被占用/路径错） | 重跑 bootstrap；确认没有别的进程锁住 `data/ops.db` |
| 回答里出现 `KNOWLEDGE_ERROR: 知识库检索失败（…）` | 索引缺失/损坏，或 `OPS_EMBEDDING_DIM` 改过但没重建 | 删 `data/knowledge_index.json` 后重跑 bootstrap（哈希向量不需要网络） |
| 报告没有"模型润色"痕迹 | 未配置 key，或润色被拒（含数字/因果词） | 预期行为；`--read-thread` 看 trace 里的 `LLM_POLISH_*` |
| `status=clarification` 反复出现 | 问题缺年份/月份，或多轮追问在新会话里 | 补全时间范围；追问要带 `--thread-id` 沿用上下文 |
| `status=budget_exceeded` | `OPS_MAX_STEPS` / `OPS_MAX_TOOL_CALLS` 太小 | 调回默认 12 / 8，或查 trace 是否异常循环 |
| `resume` 返回 409 `NO_PENDING_INTERRUPT` | 该 thread 不在待审批状态 | 先 `/api/chat` 生成报告草稿 |
| Streamlit 页面报连接错误 | API 没起，或 `OPS_API_BASE_URL` 指错端口 | 先验证 `/api/health`，再检查环境变量 |
| 端口占用 | 8000/8501 被占 | 换 `--port 8001`，UI 侧同步改 `OPS_API_BASE_URL` |
| Windows 控制台中文乱码 | 终端编码不是 UTF-8 | `chcp 65001`；`scripts/run_cli.py` 已尝试 `sys.stdout.reconfigure(encoding="utf-8")` |

看日志：`data/logs/ops-assistant.log`（UTF-8 追加，`OPS_LOG_LEVEL=DEBUG` 可加详）。日志字段含 `event / request_id / thread_id / node / ok / duration_ms / error_code`，便于按请求串联。

---

## 9. 数据目录

| 路径 | 谁写的 | 说明 |
| --- | --- | --- |
| `data/ops.db` | `bootstrap.py` | 演示经营库（SQLite） |
| `data/knowledge_index.json` | `bootstrap.py` / 运行时重建 | 知识库切分与向量 |
| `data/ground_truth.json` | `compute_ground_truth.py` | 独立标准答案 |
| `data/checkpoints.db` | 运行时（`SqliteSaver`） | LangGraph 检查点，`thread` 状态来源 |
| `data/preferences.db` | 运行时（`PreferenceStore`） | 用户偏好（元/万元、默认维度） |
| `data/exports/report-<thread_id>.md` | `review` 通过后的 `export` 节点 | 报告落盘（临时文件 + fsync + `os.replace` 原子替换） |
| `data/logs/ops-assistant.log` | 脚本 / API | 结构化日志 |

---

## 10. 边界与延伸阅读

- 已知限制、未验证项、已知设计权衡、改进清单：[../docs/limitations.md](../docs/limitations.md)
- 架构图（模块 / 路由 / 数据流 / HITL 时序）：[../docs/architecture.md](../docs/architecture.md)
- 面试问答与项目介绍模板：[../docs/interview.md](../docs/interview.md)
- 每个模块"为什么这么设计"的手把手讲解：[../教程/](../教程/)（单元 00–14）
