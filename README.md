# 经营数据分析与知识库智能助手 · 学习仓库

本仓库包含两部分内容：

1. **`ops-assistant/`**：一个可运行的演示项目——用 **LangGraph 固定工作流**（不是让模型自由选工具）把「自然语言问题 → 规则解析 → 知识检索 → SQLite 取数 → 指标计算 → 模板报告 → 人工审批 → 落盘」串成一条可审计、可复现、可中断恢复的链路。
2. **`教程/`**：从零讲解 LangChain / LangGraph 概念的 22 篇基础单元 + 流式输出附录、契约与版本化验收章及课程总览，用来解释这个项目里每一处设计为什么这么写。

配套文档：`docs/`（架构、演示、限制、面试、验证）、`reference/`（两份离线速查表）、`交付清单.md`（全部文件与状态）。

> **诚实声明（先读这一段）**
> - 这是**教学与求职演示项目**，不是生产系统；演示数据由 `data/generate.py` 按固定随机种子（`SEED = 20260901`）生成，不包含任何真实业务数据。
> - 本仓库**不做任何就业承诺**，也不声称"多少天精通"；它只展示我能讲清楚、能跑通、能解释取舍的东西。
> - **验证状态（2026-10-20）**：Linux / Python 3.12.14 / uv 0.12.23，锁定依赖安装成功；220 项测试、20 条确定性评估、23 个实验脚本通过。真实本地 API + Streamlit 交互及三进程恢复已验证；真实模型、真实 embedding 与 Windows 本地执行未验证。详见 [验证记录](docs/verification.md) 与 [修复记录](docs/audit-fixes-2026-10-20.md)。
> - 最可靠的验收方式仍然是：你在自己的机器上按 [ops-assistant/README.md](ops-assistant/README.md) 跑一遍，并对照 [docs/verification.md](docs/verification.md) 记录结果。

---

## 30 秒上手：**不需要 API Key** 也能跑通完整链路

项目的模型层是可选的（`OPS_MODEL` 留空 = 纯规则模式）。没有 Key 时，检索用确定性哈希向量、知识答案用模板、报告不润色，但**工作流、审批、导出全部照常工作**。

```bash
cd ops-assistant
uv sync --locked                                     # 安装依赖（Python ≥ 3.11）
uv run python scripts/bootstrap.py                   # 生成演示库 + 知识索引 + 标准答案
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
```

**实测（2026-10-19，Linux 沙箱）**：终端打印 `状态：awaiting_approval`、一份 8 段 Markdown 报告、来源列表（SQL + 文档）与审批摘要；报告**尚未**落盘。

```bash
uv run python scripts/run_cli.py --list-threads                 # 拿到 thread_id
uv run python scripts/run_cli.py --thread-id <thread_id> --resume approve --comment "OK"
```

**实测（2026-10-19）**：`状态：approved`，`导出文件：data/exports/report-<thread_id>-<report_id>.md`；重复 `--resume approve` 返回同一路径（幂等）。

> 为什么"没有模型也能跑"是重要卖点而不是缺陷：它说明**数字不依赖模型**——所有金额、差额、百分比都由 `data/queries.py` 和 `domain/metrics.py` 算出，模型最多改一句不含数字的措辞。详见 [docs/architecture.md](docs/architecture.md) 与 [教程/14-把检索与数据接进工作流.md](教程/14-把检索与数据接进工作流.md)。

---

## 有 API Key 时：先探测模型能力，再决定开不开

```bash
cd ops-assistant
cp .env.example .env        # Windows: copy .env.example .env
# 编辑 .env，填 OPS_MODEL（如 openai:gpt-4.1-mini）与 OPS_API_KEY；base_url 按需
uv run python scripts/check_model.py
```

`check_model.py` 输出一份 JSON 报告（不打印任何密钥，URL 只保留协议/主机/端口，key 会被掩码），逐项探测：

| 探测项 | 看什么 | 失败时项目会怎样 |
| --- | --- | --- |
| 配置是否存在 | `OPS_MODEL` / `OPS_API_KEY` 是否齐全 | 缺失时 `llm_enabled=False`，日志给 WARNING，**程序继续运行**（纯规则模式） |
| 文本调用 | 能否完成一次最小对话 | 知识答案与润色降级为模板/跳过，链路不变 |
| `bind_tools` | 该模型是否支持工具调用 | 只影响 `agent/` 那条"模型选工具"路线，不影响主工作流 |
| 结构化输出 | 是否支持 `with_structured_output` | 同上，仅影响 Agent 路线的解析方式 |

结论：**模型是增强项，不是前置条件**。这也意味着面试时不该说"我的系统依赖大模型"，而应该说"我的系统在模型不可用时仍能给出可审计的数字"。

---

## 三条运行入口

| 入口 | 命令 | 依赖 | 适合 |
| --- | --- | --- | --- |
| CLI | `uv run python scripts/run_cli.py -q "..."` | 无（不用起服务） | 演示、脚本化、CI 冒烟 |
| HTTP API | `uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000` | 端口 8000 | 看 Swagger（`/docs`）、跑前端、面试现场展示 HITL |
| Streamlit UI | `uv run streamlit run src/ops_assistant/ui/streamlit_app.py` | 先起 API；`OPS_API_BASE_URL` 默认 `http://127.0.0.1:8000` | 给非技术观众看"审批卡片 + 报告下载" |

> **API 必须单 worker**：运行时（SQLite 连接、检查点、偏好存储、进程内锁）是进程内单例，多 worker 会让同一 thread 的历史互相覆盖。这条限制写在 `api/main.py` 的模块 docstring 里，也是 [docs/limitations.md](docs/limitations.md) 的一条已知限制。

---

## 仓库地图

| 路径 | 是什么 | 状态 |
| --- | --- | --- |
| [ops-assistant/](ops-assistant/) | 演示项目本体（src 布局 + scripts + data） | 已实现 |
| [ops-assistant/README.md](ops-assistant/README.md) | 安装/配置/初始化/运行/测试/演示/故障排查 | 本次新增 |
| [docs/architecture.md](docs/architecture.md) | 4 张 Mermaid 图 + 概念辨析（State / Checkpointer / thread / Store / Runtime Context） | 本次新增 |
| [docs/demo-script.md](docs/demo-script.md) | 5 分钟演示台词、5 条失败路径命令、面试追问 | 本次新增 |
| [docs/limitations.md](docs/limitations.md) | 已知限制、未验证项、改进清单 | 本次新增 |
| [docs/interview.md](docs/interview.md) | 30 道自测题 + 项目介绍模板 + 设计权衡 | 本次新增 |
| [docs/verification.md](docs/verification.md) | 诚实验证记录：哪些没跑、学习者机器上的完整命令与排查顺序 | 已实测，详见验证记录 |
| [教程/](教程/) | 22 个基础单元 + 2 个补充单元 + 课程总览（每篇 10 节、含"改坏它"实验与验收清单） | 已完成 |
| [交付清单.md](交付清单.md) | 每个文件的用途与状态（含"未生成/未验证"项） | 已完成 |
| [工程规格.md](工程规格.md) | 项目规格（数据口径、接口、验收标准）；开发时的单一事实源 | 已实现 |
| [docs/publish-github.md](docs/publish-github.md) | 如何把这个仓库发布到 GitHub（含排除项与自检命令） | 已完成 |
| [publish.ps1](publish.ps1) | 一键发布脚本（`-DryRun` 可先预览；需本机装 git + gh 并已登录） | 已完成（未执行验证） |

> **这个仓库不包含**（`.gitignore` 已排除，仅保留在本地）：`Academy课程/`（LangChain Academy 第三方课程 notebook，版权与体积都不适合公开）、`briefs/`（内部任务书）、`MISSION.md` / `NOTES.md` / `RESOURCES.md`（个人学习笔记）。
> 若你要改成私有仓库并完整保留它们，删掉 `.gitignore` 顶部对应几行即可，步骤见 [docs/publish-github.md](docs/publish-github.md)。

`ops-assistant/` 内部结构：

```text
ops-assistant/
├── pyproject.toml            # 依赖锁定（langchain 1.4.6 / langgraph 1.2.16）
├── .env.example              # 环境变量示例（全部可留空）
├── scripts/                  # bootstrap / run_cli / check_model / compute_ground_truth
├── src/ops_assistant/
│   ├── config.py             # Settings + load_settings（.env 不覆盖已有环境变量）
│   ├── domain/               # question（规则解析）+ metrics（口径计算）
│   ├── data/                 # schema.sql / db.py / queries.py / generate.py
│   ├── knowledge/            # loaders / splitter / embeddings / index / retrieve / qa
│   ├── workflow/             # state / routing / graph / nodes / persistence
│   ├── report/               # format（8 段模板）+ export（原子落盘）
│   ├── agent/                # llm / tools（6 个 @tool）/ simple_agent（create_agent）
│   ├── api/                  # main（FastAPI 路由）+ schemas（Pydantic v2）
│   └── ui/                   # streamlit_app（只走 HTTP，不碰数据库）
└── data/                     # 运行时生成：ops.db / knowledge_index.json / exports / logs
```

---

## 已实现 / 仅静态检查 / 未来计划

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| 10 节点 LangGraph 工作流 + 3 条条件边 | **已实现** | `src/ops_assistant/workflow/{graph,routing,nodes}.py` |
| SQLite 取数、口径计算、8 类边界数据 | **已实现** | `data/queries.py`、`domain/metrics.py`、`data/generate.py` |
| 知识库检索（哈希/关键词/混合）+ 引用校验 | **已实现** | `knowledge/retrieve.py`、`knowledge/qa.py` |
| 人工审批（`interrupt` + `resume`）与原子导出 | **已实现** | `workflow/nodes.py`（review/export）、`report/export.py` |
| FastAPI 11 个路由 + Streamlit 前端 + CLI | **已实现** | `api/main.py`、`ui/streamlit_app.py`、`scripts/run_cli.py` |
| 环境变量与 `.env` 加载、密钥脱敏 | **已实现** | `config.py`、`logging_utils.py` |
| 代码风格（ruff）与类型注解 | **已实测通过** | `pyproject.toml` 的 `[tool.ruff]` |
| 单元/集成测试 | **已实测通过** | `tests/`：220 项测试，详见验证环境 |
| 评测集与评分脚本 | **已实测通过** | `eval/cases.jsonl`、`eval/run_eval.py` |
| 真实 embedding 模型端到端效果 | **未验证**（默认为哈希向量） | `.env.example` 说明与 `knowledge/embeddings.py` 的回退逻辑 |
| 多实例/多 worker 部署 | **未来计划**（需把检查点与偏好迁到共享后端） | `api/main.py` docstring |
| 用户级鉴权、审计合规、成本核算 | **未来计划** | [docs/limitations.md](docs/limitations.md) |

---

## 常用命令速查

```bash
cd ops-assistant

uv sync --locked                                     # 安装
uv run python scripts/bootstrap.py                   # 初始化（可加 --skip-ground-truth）
uv run python scripts/bootstrap.py --skip-ground-truth

uv run python scripts/run_cli.py --health            # 运行时健康（模型/知识库/DB/预算/检查点）
uv run python scripts/run_cli.py --list-threads
uv run python scripts/run_cli.py --read-thread <thread_id>
uv run python scripts/run_cli.py -q "..." --json     # 机器可读

uv run python scripts/check_model.py                 # 有 Key 时才需要

uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
uv run streamlit run src/ops_assistant/ui/streamlit_app.py

uv run pytest                                        # 需要 tests/ 已存在
uv run ruff check .                                  # 需要开发依赖已安装
```

需要"看一遍就知道怎么演示"的话，直接读 [docs/demo-script.md](docs/demo-script.md)；需要"这个项目哪里不行"的话，直接读 [docs/limitations.md](docs/limitations.md)——两份文档都不美化结论。
