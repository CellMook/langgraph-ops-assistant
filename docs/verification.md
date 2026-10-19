# 验证记录（verification）

> **先读结论**：本次交付的代码**没有在任何真实 Python 环境中执行过**。
> 原因是编写环境无法启动进程：`pwsh` 的任何调用（包括 `Write-Output`）都返回
> `[exit code: 3221225794]`（`0xC0000142`，`STATUS_DLL_INIT_FAILED`），
> `python`、`uv`、`pytest` 自然也无法启动。
> 因此本文档只提供两类内容：**可执行的验证命令清单** 与 **每种失败怎么排查**。
> 仓库里不存在本次生成的测试报告、覆盖率、通过率或性能数据——**也不应该有**。

## 1. 本次交付"验证到了什么、没验证什么"

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| 语法能否通过 `python -m compileall` | ❌ 未验证 | 环境不能启动 Python |
| `uv sync` / `uv lock` 能否解析依赖 | ❌ 未验证 | 未生成 `uv.lock`（见第 4 节） |
| `pytest` 能否全部通过 | ❌ 未验证 | 测试代码是静态编写 |
| `bootstrap.py` 能否建库/建索引 | ❌ 未验证 | 未生成 `data/ops.db`、`data/knowledge_index.json` |
| API 能否启动、`/docs` 是否可访问 | ❌ 未验证 | 未启动 uvicorn |
| Streamlit 页面能否渲染 | ❌ 未验证 | 未启动 streamlit |
| 真实模型调用（`check_model.py`） | ❌ 未验证 | 无 API Key，且不能起进程 |
| 接口签名/字段名与规格一致 | ✅ 静态核对 | 通过阅读 `src/ops_assistant/**` 逐项比对（第 3 节） |
| 数据生成器的 8 类边界事实在代码中被构造 | ✅ 静态核对 | `generate.py` 有显式 `_construct_*` 与运行前自查断言 |
| 文档/教程中"预期输出"是否被标成"未执行" | ✅ 静态核对 | 抽查各文件，均标注"预期（未在本环境执行）" |

一句话：**代码是"写出来并互相核对过"的，不是"跑通过"的。** 下面的命令必须由学习者在自己的机器上跑一遍，才算验证完成。

## 2. 学习者机器上的完整验证命令（Windows PowerShell）

在仓库根目录 `D:\MySpace\LangChain学习路线` 下：

```powershell
# 1) 进入项目
cd .\ops-assistant

# 2) 安装依赖（Python 3.11+；首次会自动创建 .venv）
uv sync

# 3) 语法检查（不起服务，先确认没有低级语法错误）
uv run python -m compileall -q src scripts tests eval

# 4) 生成演示库 + 知识索引 + 标准答案
uv run python scripts/bootstrap.py

# 5) 确定性测试（不依赖模型）
uv run pytest -q

# 6) 确定性评估（输出到 eval/results/）
uv run python eval/run_eval.py --verbose

# 7) 命令行跑一轮（不启 Web）
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"

# 8) 起 API（另开一个终端）
uv run uvicorn ops_assistant.api.main:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000/docs

# 9) 起 UI（再开一个终端）
uv run streamlit run src/ops_assistant/ui/streamlit_app.py
```

每一步的**通过标准**：

| 步骤 | 通过标准 | 常见失败与排查 |
| --- | --- | --- |
| `uv sync` | 退出码 0，`.venv` 出现 | 网络/镜像问题：设置 `UV_INDEX_URL`；Python 版本过低：`uv python install 3.12` |
| `compileall` | 退出码 0，无 SyntaxError | 按报错文件行号修；注意中文路径不影响，但文件编码必须是 UTF-8 |
| `bootstrap.py` | 打印数据库、索引、标准答案三处路径与行数摘要 | `知识目录为空`：检查 `data/knowledge/*.md` 是否存在；`ModuleNotFoundError: ops_assistant`：确认在 `ops-assistant` 目录且已 `uv sync` |
| `pytest` | 无 failed/error（数量以实际为准） | 见第 3 节"测试失败排查顺序" |
| `run_eval.py` | 生成 `eval/results/deterministic-*.json`，`exception_count=0` | 先看 `cases[i].errors` 与 `trace_nodes` |
| `run_cli.py` | 打印 `awaiting_approval` 与报告 | `THREAD_NOT_FOUND`：先 `--list-threads` 拿 thread_id |
| uvicorn / streamlit | `/docs` 200；页面能选会话并提问 | 端口占用换端口；UI 报连接失败检查 API 是否已启动 |

## 3. 测试失败时的排查顺序

1. **先跑 `uv run python -m compileall`**：语法错会伪装成 import 失败。
2. **单个文件先跑**：`uv run pytest tests/test_queries.py -q`。
3. **看是"环境问题"还是"口径问题"**：
   - `ModuleNotFoundError` / `sqlite3.OperationalError: no such table` → 环境/初始化问题，回第 2 节第 4 步；
   - `AssertionError` 且发生在 `test_ground_truth.py` → 说明生产实现与独立标准答案不一致，这是**最有价值**的失败，先别改测试。
4. **口径相关的三个高频坑**：
   - 退款归属：只统计 `status='approved'`，且按 `refunds.refund_date` 归月，不是按订单日期；
   - 零基数：上期为 0 时 `pct_change` 必须是 `None`，不是 `0` 或 `inf`；
   - 空月：`2025-01` 是"无记录"，`has_orders=False`，不能当"销售额为 0"。
5. **工作流类失败**：先看 `TurnResult.trace` 停在哪；`budget_exceeded` 检查 `OPS_MAX_STEPS`；`failed` 看 `errors` 前缀（`INVALID_MONTH` / `GRAPH_ERROR` / `REVIEW_ERROR`）。
6. **HITL 类失败**：确认用的是 `SqliteSaver`（重启恢复用例）还是 `InMemorySaver`（内存用例）；`test_hitl.py` 专门对比这两者，不要混用。

## 4. 关于 `uv.lock`

- 本次**没有**生成 `uv.lock`，因为生成它必须执行 `uv lock`，而本环境无法启动进程。
- 仓库里的版本策略是：`pyproject.toml` 固定四个 LangChain/LangGraph 相关包的精确版本，其余给范围；
  学习者首次 `uv sync` 时会解析出具体版本并写 `uv.lock`。
- `uv.lock` 生成后**应当提交入库**（`.gitignore` 已不再忽略它），这样别人拿到仓库能复现同一套依赖。
- 面试时如果被问"你的依赖可复现吗"，正确回答是："精确版本已钉死，锁文件由首次安装生成并入库；本次交付环境不能执行 uv，所以我不会假装锁文件已经存在。"

## 5. 本次交付实际做过的静态核对（可复核）

这些是"读代码/读文档"就能复核的，不依赖运行：

- `config.Settings` 的 23 个字段与 `工程规格.md` 5.1 一致；`load_settings` 不覆盖已存在的环境变量；
- `data/queries.py` 的 `MonthSales`（9 字段）/`RegionSales`（6 字段）与规格 3.4 一致；
  月份校验正则 `^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$` 拒绝 `2026-13`、`2026-9`；
- `domain/metrics.py` 的 `ComparisonResult` / `RegionDelta` 字段与规格 5.3 一致；
  `compare_months` 在 `dimension="channel"` 时返回 `unsupported_dimension` 而不抛异常；
- `workflow/graph.py` 注册 10 个节点、3 处 `add_conditional_edges`、`export/clarify/fail → END`；
- `workflow/persistence.py`：`SqliteSaver(sqlite3.connect(..., check_same_thread=False))` + `setup()`；
  `resume_turn` 对非法 decision 抛 `ValueError`；重复审批返回同一 `export_path`；
- 中断载荷 `{"type": "report_approval", "options": [...], "report_markdown": ...}` 为 JSON 可序列化；
- 导出文件名为 `report-<thread_id>.md`，写入方式是临时文件 + `os.replace`。

## 6. 交付前必须补的证据（如果你要拿它去面试）

1. 在**自己的机器**上按第 2 节完整跑一遍，保存终端输出（截图或日志文件）；
2. 把 `pytest` 的失败项如实贴出来（如果有），并说明是环境问题还是代码问题；
3. 把 `eval/results/deterministic-*.json` 的 `summary` 贴出来，同时保留
   `"model_behavior_evaluated": false` 这一行——它证明你没有把确定性结果冒充模型效果；
4. 如果要声称"支持真实模型"，必须附 `scripts/check_model.py` 的实际 JSON 输出，并说明模型名与端点。

**不要做的事**：不要把本文档当成"已验证"；不要在 README 里写"已通过全部测试"；
不要引用仓库里不存在的测试报告。
