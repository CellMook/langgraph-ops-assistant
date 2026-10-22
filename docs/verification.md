# 验证记录

## 当前复测：2026-10-20（第四轮）

Linux x86_64，Python 3.11.2、uv 0.13.3；使用仓库 `uv.lock`：LangChain 1.4.6、LangGraph 1.2.16、langchain-openai 1.7.2、langchain-text-splitters 1.1.3、langgraph-checkpoint-sqlite 3.1.2；ruff 0.17.1、pytest 8.4.2。本轮做完第三轮留下的三件遗留项（方向语义、流式输出、术语表），明细见 [修复记录](audit-fixes-2026-10-20.md) 第四轮章节。

| 验证项 | 本轮结果 |
| --- | --- |
| `uv sync --locked` | 成功 |
| `uv run python scripts/bootstrap.py` | 成功 |
| `uv run pytest -q` | **294 passed**（第三轮 259）：`test_audit_regressions.py` 100 项 + 新增 `test_streaming.py` 18 项 |
| `uv run ruff check .` / compileall | 通过（All checks passed） |
| `uv run python eval/run_eval.py` | 20/20 状态匹配，异常 0 |
| `uv run python scripts/verify_recovery.py` | 退出码 0 |
| `uv run python scripts/verify_ui.py` | 退出码 0 |
| `教程/实验/*.py` | 23 个脚本全部退出码 0 |
| CLI `--stream` | 逐节点打印进度后输出与非流式一致的结果；`--stream --json` 输出 JSONL |
| SSE 真实 TCP | uvicorn 起在 127.0.0.1:8123，`httpx` 流式读取：8 帧按序到达，`content-type: text/event-stream; charset=utf-8` |
| 逐帧送达（决定性） | 把 `nodes.get_month_sales` 放慢 500ms×2 后：`start` 于 15.6ms、前两个 `node` 帧于 26.8/30.1ms 到达，其余帧在 2434ms 之后——证明不是一次性缓冲 |
| 真实模型 / embedding、Windows 本地、浏览器 `EventSource`、CI 远端结果 | 本轮未验证；远端 Actions 结果以 PR 页面为准 |

方向语义复验（均为实测解析结果，`today=2026-10-20`）：

| 问题 | 修复前 | 修复后 |
| --- | --- | --- |
| `去年 9 月和今年 9 月对比` | 本期 **2025-09** / 上期 2026-09（基期比本期新，差额符号相反） | 本期 **2026-09** / 上期 2025-09 |
| `2026 年 9 月与 2025 年 9 月对比` | 本期 2026-09（提及顺序恰好正确） | 本期 2026-09（不再依赖提及顺序） |
| `2026 年 8 月比 9 月变化多少` | 本期 2026-08 | 本期 2026-08（**非对称比较词不改**，无回归） |
| `2026 年 9 月和 9 月对比` | 出报告，差额恒为 0 | `clarification`：本期与基期相同 |

未验证项如实保留：浏览器原生 `EventSource` 只做了 HTTP 层验证（`httpx` + `TestClient`），没做浏览器内消费；SSE 断线重连与 `Last-Event-ID` 续传未实现也未测试；Streamlit 仍走非流式路径。

复现命令（从 `ops-assistant/` 执行）：

```bash
uv sync --locked
uv run python scripts/bootstrap.py
uv run python -m compileall -q src scripts tests eval ../教程/实验
uv run pytest -q
uv run ruff check .
uv run python eval/run_eval.py
uv run python scripts/verify_recovery.py
uv run python scripts/verify_ui.py
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？" --stream
```

---

## 上一轮复测：2026-10-20（第三轮，历史）

Linux x86_64，Python 3.11.2、uv 0.13.3；使用仓库 `uv.lock`：LangChain 1.4.6、LangGraph 1.2.16、langchain-openai 1.7.2、langchain-text-splitters 1.1.3、langgraph-checkpoint-sqlite 3.1.2；ruff 0.17.1、pytest 8.4.2。以下结果来自本轮真实执行。本轮起点是"全套验证已经全绿"，因此缺陷不是靠跑既有命令发现的，而是靠对解析层与导出层做对抗性输入探测发现的，明细见 [修复记录](audit-fixes-2026-10-20.md) 第三轮章节。

| 验证项 | 本轮结果 |
| --- | --- |
| `uv sync --locked` | 成功 |
| `uv run python scripts/bootstrap.py` | 成功：regions=5、products=12、orders=956、refunds=94；索引 chunk_count=23 |
| `uv run pytest -q` | **259 passed**，含 **83 项**审查回归断言（上轮 220 / 44） |
| `uv run ruff check .` / compileall | 通过（All checks passed） |
| `uv run python eval/run_eval.py` | 20/20 状态匹配，异常 0 |
| `uv run python scripts/verify_recovery.py` | 通过：三个独立进程的待审批恢复、导出、重复审批同路径 |
| `uv run python scripts/verify_ui.py` | 通过：真实本地 API + Streamlit AppTest |
| `uv run python scripts/check_model.py` | 退出码 0；未配置密钥时 `enabled=false` 是预期行为 |
| `教程/实验/*.py` | 23 个脚本全部退出码 0 |
| CLI 端到端 | `2026 年 9 月各地区销售额是多少？` 正常出报告：本期 2026-09 为 11,107.99 元、上期 2026-08 为 16,846.83 元、差额 -5,738.84 元（修复前该问题被 `未找到地区「月各」` 拦下） |
| 真实模型 / embedding、Windows 本地、浏览器渲染、CI 远端结果 | 本轮未验证；远端 Actions 结果以 PR 页面为准，不能从配置文件推断 |

本轮修复后的关键复验（均为实测输出，非推演）：

| 复验点 | 修复前 | 修复后 |
| --- | --- | --- |
| `今年 9 月比去年 9 月变化多少` | 本期 2026-09 对比 **上期 2026-09**，差额恒为 0 | 本期 2026-09 对比 **上期 2025-09**（11,107.99 vs 17,010.70） |
| `2026 年 9 月各地区销售额` | `clarification`：未找到地区「月各」 | `awaiting_approval`，dimension=region，正常出报告 |
| `2026 年 9 月按地区分解` | `clarification`：未找到地区「月按」 | 正常解析，dimension=region |
| `exported_report_path("t-123", dir)` | `report-t-123.md`（**永不存在**） | `report-t-123-r-9.md`（与 `export_report` 实际产物一致且存在） |
| `parse_question(123)` | `AttributeError: 'int' object has no attribute 'strip'` | 收敛为澄清，不抛异常 |

复现命令与上一轮相同（见下）。`data/ground_truth.json` 的 `generated_at_utc` 会随 bootstrap 变化，本轮已还原，不纳入提交。

---

## 上一轮复测：2026-10-20（第二轮，历史）

Linux x86_64，Python 3.12.14、uv 0.12.23；使用仓库 `uv.lock`：LangChain 1.4.6、LangGraph 1.2.16、langchain-openai 1.7.2。以下结果来自该轮真实执行；其计数与接口不代表当前版本，后面的 2026-10-19 章节同样保留为历史记录。

| 验证项 | 本轮结果 |
| --- | --- |
| `uv sync --locked` | 成功 |
| `uv run pytest -q` | 220 passed，含 44 项审查回归断言 |
| `uv run ruff check .` / compileall | 通过 |
| `uv run python eval/run_eval.py --verbose` | 20/20，异常、轨迹和数值断言失配均为 0；失败退出非零 |
| `uv run python scripts/verify_ui.py` | 真实本地 API + Streamlit AppTest：审批、下载按钮、新建会话、当前编辑器、历史会话目标通过 |
| `uv run python scripts/verify_recovery.py` | 三个独立 Python 进程：待审批恢复、导出、重复审批同内容同路径通过 |
| `教程/实验/*.py` | 23 个脚本全部退出码 0；无 key 探测分支明确跳过 |
| 真实模型 / embedding、Windows、GitHub CI | 本轮本地未验证；CI 新增矩阵不等于已在远端通过 |

UI 验证是实际 Streamlit Python 交互与本地 HTTP 验证，未做浏览器像素或跨浏览器兼容测试。20 条评估为确定性验收，`model_behavior_evaluated=false`，不能据此宣称模型准确率。

从 `ops-assistant/` 执行：

```bash
uv sync --locked
uv run python scripts/bootstrap.py
uv run python -m compileall -q src scripts tests eval ../教程/实验
uv run pytest -q
uv run ruff check .
uv run python eval/run_eval.py --verbose
uv run python scripts/verify_recovery.py
uv run python scripts/verify_ui.py
uv run python ../教程/实验/23_framework_contracts.py
uv run python ../教程/实验/23_agent_budget.py
```

复测输出可从 `data/verification/` 查看 UI 与跨进程 JSON。评估会复制演示数据库到临时目录，隔离检查点、偏好、索引、导出并强制关闭外部模型。CI 覆盖 Linux/Windows × Python 3.11/3.12；首次远端运行结果以 Actions 页面为准。

回归问题与验收映射见 [本轮修复记录](audit-fixes-2026-10-20.md)。自己的机器仍须复跑，不能把本环境结果当成本人机器结果。

---

# 历史记录：2026-10-19（旧版本）

## 1. 实测环境与总体结果（2026-10-19）

| 项目 | 值 |
| --- | --- |
| 环境 | Linux 沙箱（Debian 系），x86_64 |
| Python | 3.11.2 |
| 包管理 | uv 0.13.1（`uv sync` 自动创建 `.venv`） |
| langgraph | 1.2.16（与 2026-10-19 PyPI 最新版一致） |
| langchain | 1.4.6（与 2026-10-19 PyPI 最新版一致） |
| langchain-openai / langchain-text-splitters / langgraph-checkpoint-sqlite | 1.7.2 / 1.1.3 / 3.1.2（均为当日最新） |
| 模型密钥 | 未配置（纯规则模式验证；模型路径未评估，见第 6 节） |

**总体结果**：

| 验证项 | 结果 |
| --- | --- |
| `uv sync`（依赖解析与安装） | ✅ 成功，生成 `uv.lock` |
| `uv run python -m compileall -q src scripts tests eval` | ✅ 退出码 0 |
| `uv run pytest -q` | ✅ **176 passed, 0 failed, 0 error**（初测为 149 passed / 17 failed / 9 error，修复后全绿，见第 3 节） |
| `uv run ruff check .` | ✅ All checks passed（初测 156 项告警，修复后清零） |
| `uv run python eval/run_eval.py` | ✅ 14 条用例、状态匹配 14、异常 0 |
| `uv run python scripts/bootstrap.py` | ✅ 建库（orders=956 / refunds=94）、索引（23 块）、标准答案（585 行 JSON） |
| CLI 主问题 → `awaiting_approval` → `--resume approve` | ✅ 导出 `data/exports/report-<thread_id>-<report_id>.md`；重复 approve 返回同一路径（幂等） |
| FastAPI（uvicorn 单 worker） | ✅ `/api/health` 200；`/api/chat` 返回 awaiting_approval；`resume` approve 导出；追问沿用 thread；`PUT /api/preferences` 写入后 `GET` 读回一致 |
| Streamlit（headless） | ✅ 页面 200、`/_stcore/health` = ok、经 API 完成一轮问答 |
| 教程 00–18 单元的 19 个实验脚本（从正文提取） | ✅ 全部退出码 0（清单见第 2 节） |
| 单元 14 的 `scratch/exp14_invoke_turn.py` | ✅ 退出码 0，输出与第 6 节预期一致（修复 interrupt 字段读取后） |
| 真实模型调用（`check_model.py` 探测） | ❌ 未验证（无 API Key；这是唯一保留"未验证"的大项） |

## 2. 教程实验脚本实测清单（2026-10-19，均在 `ops-assistant/` 目录执行）

脚本从各单元第 5 节正文提取，存放于 `教程/实验/`（提取规则：单元内"保存路径：教程/实验/NN_*.py"
之后的第一个 Python 代码块）。

| 脚本 | 单元 | 退出码 | 备注 |
| --- | --- | --- | --- |
| `00_diagnose.py` | 00 | 0 | 关键项全通过（该脚本现更名 `01_diagnose.py` 并移入单元 01，作为装好环境后的验收步骤） |
| `01_hello_model.py` | 01 | 0 | 规则模式分支输出完整报告 |
| `01_probe_model.py` | 01 | 0 | 无密钥时显式跳过，不联网 |
| `02_context_growth.py` | 02 | 0 | 规则模式模拟回复 |
| `03_validate_spec.py` | 03 | 0 | Pydantic + 业务校验两段都过 |
| `04_tool_loop.py` | 04 | 0 | 假模型走完工具循环，预算耗尽路径可复现 |
| `05_simple_agent.py` | 05 | 0 | 无密钥回退路径（`used_fallback=True`） |
| `06_state_graph.py` | 06 | 0 | 默认 reducer 覆盖现象可观察 |
| `07_reducers.py` | 07 | 0 | `operator.add` 与 `add_messages` 行为符合预期 |
| `08_routing.py` | 08 | 0 | `GraphRecursionError` 如预期抛出（limit=2） |
| `09_checkpoint.py` | 09 | 0 | `--resume` 变体：无检查点时打印跳过提示，退出码 0 |
| `10_retrieval_baseline.py` | 10 | 0 | 关键词基线命中预期文档 |
| `11_vector_citation.py` | 11 | 0 | "资料不足"分支（食堂问题 top_score=0.1062 < 0.15）可复现 |
| `12_sql_basics.py` | 12 | 0 | `EXPLAIN QUERY PLAN` 显示 `SEARCH orders USING INDEX idx_orders_date`；LIKE 走 `SCAN` |
| `13_metrics_check.py` | 13 | 0 | 整数分自洽校验全部通过 |
| `15_memory_store.py` | 15 | 0 | Store 注入 compile 成功；无 checkpointer 追问失忆可复现 |
| `16_interrupt_resume.py` | 16 | 0 | resume 无待审批会话按预期抛 `ValueError` |
| `17_idempotent_restart.py` | 17 | 0 | 重启恢复 + 重复审批同路径 |
| `18_parallel_subgraph_budget.py` | 18 | 0 | 并行 fan-out/fan-in、子图、预算受限路径全部符合预期 |
| `scratch/exp14_invoke_turn.py` | 14 | 0 | 修复 `interrupt` 字段读取（`payload` 嵌套）后通过 |

## 3. 审查发现并修复的缺陷（2026-10-19）

初测 `pytest` 为 **149 passed / 17 failed / 9 error**，`ruff` 156 项告警。逐条根因分析与修复如下：

| # | 缺陷 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | `PUT /api/preferences` 后整轮工作流崩溃：`TypeError: unhashable type: 'dict'`（连带 9 个 HITL 用例 ERROR、多个工作流用例失败） | `PreferenceStore.set_many/put` 落盘统一包成 `{"value": X}`，但 `all_for_user` 返回时**没有解包**，`effective_preferences` 拿 dict 做集合成员判断 | `all_for_user` 解包 `{"value": X}` 包装（`_unwrap_preference_value`）；`effective_preferences` 对非字符串取值回退默认值（双保险） |
| 2 | `max_steps=2` 时预算用尽场景返回 `failed` 而非 `budget_exceeded` | `recursion_limit = max(4, max_steps*2)` 过小：最坏路径 4 超级步 + 1 次框架计数 > 4，先触发 `GraphRecursionError` | `recursion_limit = max(8, max_steps * 2)`，并在 `_config` docstring 记录推导 |
| 3 | 拒绝审批后 `TurnResult.report` 仍返回整份草稿 | `_turn_result` 的 `final_report or draft_report` 回退对 `rejected` 也生效 | `rejected` 状态不回退草稿（review 节点已把 `final_report` 置空） |
| 4 | 追问用例断言 `calculation["regions"]` 报 KeyError | `compute_node` 只写 `region_deltas`，测试/规格/API 用例读 `regions` | `calculation` 增加 `regions` 别名（保留 `region_deltas` 兼容） |
| 5 | `T20 预算` 用例断言 `metrics["tool_calls"] <= 8` 失败（实测 24） | `metrics` 是**进程级累计值**，跨用例共享 runtime 后必然超单轮上限 | 按规格 5.10"usage 合并图状态计数"：`TurnResult.usage` 增加 `steps/tool_calls/max_*`（本轮值）；`TurnUsage` schema 同步；测试改断言 `usage` |
| 6 | `T01` 两个用例单跑报 `no such table: orders` | 用例只依赖 `settings` 夹具（只建目录），数据库由 `db_summary` 生成 | 用例签名补 `db_summary` 依赖（`test_data`×2、`test_hitl`×2、`test_workflow`×1） |
| 7 | `T06 东北 2025-09` 断言 `net_cents == 0` 失败（实测 -13447） | 种子数据里有一笔 2025-08 订单的 approved 退款落在 2025-09（跨月退款按退款月归属，符合口径设计）；测试与规格表把它误写成"金额为 0" | 测试改为断言真实语义（`gross=0`、`net=-refund`），全零缺月改用 2026-08 验证；`工程规格.md` T06 行同步修正 |
| 8 | `T18 关键词检索` 期望"退款是否计入当月销售额？"命中 `01_指标口径` 失败（top4 全是 `02_退款与归属`） | 该问题语义上就是退款归属问题，最相关文档是 02（IDF 加权实验也无法改变排序） | 用例期望改为 02，另补"净销售额的定义是什么？"覆盖 01 |
| 9 | `eval/run_eval.py` 状态匹配只有 7/14 | `expect_status` 描述的是**提问阶段**状态，脚本却在执行 approve/reject flow **之后**比较终态 | 记录 `status_at_question` 与 `final_status` 双值；用例新增 `expect_status_at`（默认 `question`，E13/E14 为 `final`） |
| 10 | `ruff check .` 156 项告警（E501×120、UP×31、B×3、I×2） | 行宽 100 按显示宽度计，中文 docstring 普遍超限；另有弃用 import 等 | 行宽调整为 120（CJK 显示宽度说明写入 pyproject）；`--fix` 自动修复 52 项；超 120 列的 9 行手动换行；`zip` 补 `strict=`；`pytest.raises(Exception)` 收窄为 `ThreadNotFoundError` |
| 11 | 单元 14 实验脚本打印 `interrupt: None None` | 脚本读 `interrupt["type"]`，实际结构是 `{"id", "payload": {...}}`（`_extract_interrupt` 的封装） | 教程脚本改为读 `payload`；6.5 节勘误同步 |
| 12 | 源码含非中文字符笔误"教学용"（韩文） | 笔误 | `persistence.py` 改"教学用"，单元 15 引文与勘误说明同步 |

修复后复测：`pytest` 176/176、`ruff` 清零、评估 14/14、上述清单全部通过。

## 4. 学习者机器上的完整验证命令

在仓库根目录下（Windows PowerShell 去掉 `./`、把 `/` 换成 `\`）：

```bash
# 1) 进入项目
cd ops-assistant

# 2) 安装依赖（Python 3.11+；首次会自动创建 .venv 并生成 uv.lock）
uv sync

# 3) 语法检查（不起服务，先确认没有低级语法错误）
uv run python -m compileall -q src scripts tests eval

# 4) 生成演示库 + 知识索引 + 标准答案
uv run python scripts/bootstrap.py

# 5) 确定性测试（不依赖模型）——通过标准：0 failed / 0 error（当前 176 项）
uv run pytest -q

# 6) 静态检查——通过标准：All checks passed
uv run ruff check .

# 7) 确定性评估——通过标准：14 条用例、状态匹配 14、异常 0
uv run python eval/run_eval.py --verbose

# 8) 命令行跑一轮（不启 Web）
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"

# 9) 起 API（另开一个终端）
uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000/docs

# 10) 起 UI（再开一个终端）
uv run streamlit run src/ops_assistant/ui/streamlit_app.py

# 11)（可选）教程实验脚本——从教程正文提取，存放于 教程/实验/
uv run python ../教程/实验/06_state_graph.py
```

每一步的**通过标准**：

| 步骤 | 通过标准 | 常见失败与排查 |
| --- | --- | --- |
| `uv sync` | 退出码 0，`.venv` 出现 | 网络/镜像问题：设置 `UV_INDEX_URL`；Python 版本过低：`uv python install 3.12` |
| `compileall` | 退出码 0，无 SyntaxError | 按报错文件行号修；注意中文路径不影响，但文件编码必须是 UTF-8 |
| `bootstrap.py` | 打印数据库、索引、标准答案三处路径与行数摘要 | `知识目录为空`：检查 `data/knowledge/*.md`；`ModuleNotFoundError: ops_assistant`：确认在 `ops-assistant` 目录且已 `uv sync` |
| `pytest` | 0 failed / 0 error（2026-10-19 基线：176 passed） | 见第 5 节排查顺序 |
| `ruff check .` | All checks passed | 行宽按显示宽度计（中文占 2 列），阈值 120 |
| `run_eval.py` | 14/14 匹配、exception_count=0 | 先看 `cases[i].errors` 与 `trace_nodes` |
| `run_cli.py` | 打印 `awaiting_approval` 与报告 | `THREAD_NOT_FOUND`：先 `--list-threads` 拿 thread_id |
| uvicorn / streamlit | `/docs` 200；页面能选会话并提问 | 端口占用换端口；UI 报连接失败检查 API 是否已启动 |

## 5. 测试失败时的排查顺序

1. **先跑 `uv run python -m compileall`**：语法错会伪装成 import 失败。
2. **单个文件先跑**：`uv run pytest tests/test_queries.py -q`。
3. **看是"环境问题"还是"口径问题"**：
   - `ModuleNotFoundError` / `sqlite3.OperationalError: no such table` → 环境/初始化问题，回第 4 节第 4 步；
   - `AssertionError` 且发生在 `test_ground_truth.py` → 说明生产实现与独立标准答案不一致，这是**最有价值**的失败，先别改测试。
4. **口径相关的三个高频坑**：
   - 退款归属：只统计 `status='approved'`，且按 `refunds.refund_date` 归月，不是按订单日期；
   - 零基数：上期为 0 时 `pct_change` 必须是 `None`，不是 `0` 或 `inf`；
   - 空月：`2025-01` 是"无记录"，`has_orders=False`，不能当"销售额为 0"；另注意"无订单月"可能仍有跨月退款（净额为负），这是口径设计的自然结果。
5. **工作流类失败**：先看 `TurnResult.trace` 停在哪；`budget_exceeded` 检查 `OPS_MAX_STEPS`；`failed` 看 `errors` 前缀（`INVALID_MONTH` / `GRAPH_ERROR` / `REVIEW_ERROR`）。
6. **HITL 类失败**：确认用的是 `SqliteSaver`（重启恢复用例）还是 `InMemorySaver`（内存用例）；`test_hitl.py` 专门对比这两者，不要混用。
7. **偏好类失败**：`TypeError: unhashable type: 'dict'` 说明偏好读回来的值没解包——检查 `PreferenceStore.all_for_user` 是否走了 `_unwrap_preference_value`。

## 6. 关于 `uv.lock` 与模型验证

- `uv.lock` **已于 2026-10-19 实测生成**（`uv sync` 产出）；建议提交入库，保证他人复现同一套依赖。
- 四个 LangChain/LangGraph 相关包钉精确版本（`langgraph==1.2.16` 等），经与 2026-10-19 PyPI 元数据核对均为当日最新版。
- **模型行为仍未评估**：本环境无 API Key。要声称"支持真实模型"，必须附 `scripts/check_model.py` 的实际 JSON 输出，并说明模型名与端点；`eval` 结果里的 `model_behavior_evaluated: false` 不要删——它证明你没有把确定性结果冒充模型效果。

## 7. 本次审查做过的静态核对（可复核）

- `config.Settings` 字段与 `工程规格.md` 5.1 一致；`load_settings` 不覆盖已存在的环境变量；
- `data/queries.py` 的 `MonthSales`（9 字段）/`RegionSales`（6 字段）与规格 3.4 一致；
  月份校验正则拒绝 `2026-13`、`2026-9`；
- `domain/metrics.py` 的 `ComparisonResult` / `RegionDelta` 字段与规格 5.3 一致；
  `compare_months` 在 `dimension="channel"` 时返回 `unsupported_dimension` 而不抛异常；
- `workflow/graph.py` 注册 10 个节点、3 处 `add_conditional_edges`、`export/clarify/fail → END`；
- `workflow/persistence.py`：`SqliteSaver(sqlite3.connect(..., check_same_thread=False))` + `setup()`；
  `resume_turn` 对非法 decision 抛 `ValueError`；重复审批返回同一 `export_path`（实测确认）；
- 中断载荷 `{"type": "report_approval", "options": [...], "report_markdown": ...}` 为 JSON 可序列化（实测经 SQLite 检查点往返）；
- 导出文件名为 `report-<thread_id>-<report_id>.md`，写入方式是临时文件 + `os.replace`（幂等实测确认）；
- 教程各单元引用的源码片段与 `src/ops_assistant/**` 当前实现逐块比对（差异仅为标注过的教学压缩）。

## 8. 交付前必须补的证据（如果你要拿它去面试）

1. 在**自己的机器**上按第 4 节完整跑一遍，保存终端输出（截图或日志文件）；
2. 把 `pytest` 的失败项如实贴出来（如果有），并说明是环境问题还是代码问题；
3. 把 `eval/results/deterministic-*.json` 的 `summary` 贴出来，同时保留
   `"model_behavior_evaluated": false` 这一行——它证明你没有把确定性结果冒充模型效果；
4. 如果要声称"支持真实模型"，必须附 `scripts/check_model.py` 的实际 JSON 输出，并说明模型名与端点。

**不要做的事**：不要把本文档的沙箱实测结果冒充你自己机器的结果；不要在 README 里写
"已通过全部测试"却不附执行环境；不要引用仓库里不存在的测试报告。
