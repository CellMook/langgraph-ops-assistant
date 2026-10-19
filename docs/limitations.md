# 已知限制、未验证项与改进清单

这份文档存在的目的：**让"我不知道/没验证/做不到"变成可检索的信息**，而不是藏在心里。
面试时主动讲限制，远比被追问出漏洞好。

## 1. 先说最关键的：哪些结论没有实测

**2026-10-19 更新**：初稿编写环境无法启动任何进程（`pwsh` 返回 `0xC0000142`），当时下表全部未做。
审查阶段已在 Linux 沙箱补齐大部分实测（`uv sync`/`uv.lock`、`compileall`、`pytest` 176/176、
`bootstrap.py`、uvicorn/streamlit 均已实际运行，详见 `verification.md`）。**当前仍未验证的**：

| 仍未做的事 | 后果 |
| --- | --- |
| `check_model.py`（真实模型） | 未验证任何真实模型的工具调用/结构化输出能力（无 API Key） |
| 有密钥时的模型润色分支 | `LLM_POLISH_*` 降级路径未在真实模型下观察过 |
| 真实 embedding 召回质量 | 索引仍用确定性哈希向量，语义检索效果未评估 |
| 多用户并发 / 部署 | 单 worker 进程内单例，未做压测 |

**唯一做过的是**：读代码逐项核对接口与字段（见 `verification.md` 第 5 节）、以及所有文档里的"预期输出"都标注为未执行。

> 谁要说"这个项目测试通过"，必须先在自己机器上跑 `uv run pytest -q` 并贴输出。

## 2. 已实现（代码在，逻辑闭环）

- 规则问题解析：月份/年份/相对时间、地区匹配、维度与指标识别、澄清话术；
- SQLite 数据层：四表 schema、固定种子生成、8 类边界事实、参数化查询；
- 指标层：gross/refund/net、退款按 `refund_date` 归属、零基数、地区差额分解与合计校验；
- 知识层：Markdown 加载、400/60 切分、关键词基线、哈希向量检索、引用校验、"资料不足"；
- 工作流：10 节点、3 处条件路由、预算、repair 重试、统一 fail 出口；
- 人工审批：`interrupt` + `Command(resume=)` + approve/reject/edit + 幂等导出；
- 持久化：`SqliteSaver` 检查点（重启可恢复）、`PreferenceStore` 偏好存储；
- 接口：FastAPI 9 个端点 + 统一错误结构；Streamlit 页面；
- 可观测性：结构化日志（脱敏）、指标快照、`TurnResult.trace`；
- 测试与评估：`tests/`（T01–T23 覆盖）、`eval/cases.jsonl` + `run_eval.py`。

## 3. 已知设计权衡（不是 bug，但有代价）

| 权衡 | 现状 | 代价 | 如果重做 |
| --- | --- | --- | --- |
| 主链路用固定工作流而非 Agent 自由选工具 | `workflow/graph.py` | 灵活性低，新维度要改代码 | 保留固定链路，Agent 只做"探索型问题"的旁路 |
| 检索默认用确定性哈希向量 | `HashingEmbeddings` | **不是语义模型**，同义改写会漏召回 | 接真实 embedding + 小规模召回评估集 |
| `sources` 没有 reducer | 各节点 `list(state.get("sources") or [])` 后整体返回 | 任一节点漏读旧值就**静默丢来源**，测试不易发现 | 加 `merge_sources` reducer 并按 `(type, doc_id/sql)` 去重 |
| 展示用 SQL 与执行 SQL 是两份维护源 | `nodes.py` 里的 `MONTH_SALES_SQL` 等常量只作审计说明 | 改口径只改一处会造成报告与实现漂移 | 从 `queries.py` 反向生成展示 SQL，或只展示参数化模板 |
| Store 用 SQLite 鸭子类型适配器 | `PreferenceStore` | 与官方 Store 语义不完全一致 | 用官方 Store 实现或明确抽象接口 |
| 审批只校验"原数字是否保留 + 是否新增数字" | `_edited_report_or_draft` | 无法识别同义改写级别的措辞篡改 | 加 diff 展示 + 人工二次确认 |
| 单 worker、进程内锁 | `AppRuntime.lock` | 跨进程并发不安全 | 引入任务队列或数据库行级锁 |
| 报告与导出在本地 | `data/exports/` | 无权限控制、无审计留痕 | 加操作审计表与访问控制 |

## 4. 明确未实现 / 未来计划

按"风险 × 收益"排序：

1. **真实模型行为评估**：现在只探测能力（`check_model.py`），没有"工具序列正确率、答案忠实度"的评估集；
2. **接入真实 embedding 并量化召回**：需要一个标注了"问题 → 应命中块"的小数据集；
3. **并发与多用户隔离**：单 worker 之外的部署形态未设计；
4. **权限与审计**：谁能看哪个地区的数据、谁批准了什么，目前没有记录；
5. **流式输出**：`stream()` 已在教程里讲，但 API 未提供 SSE；
6. **时间旅行 UI**：`get_state_history` 可用，但没有界面；
7. **数据接入层**：目前是生成数据，接真实数仓需要新的 `queries` 实现与口径对齐流程。

## 5. 演示数据与合成数据的边界

- 数据由 `SEED=20260901` 固定生成，**不代表任何真实业务**；
- 8 类边界（空月、地区缺月、真零、纯 pending、跨月退款、负净额、商品零记录、channel 存在）是**刻意构造**的教学样本；
- 负净额场景中有一笔退款金额大于其挂靠订单金额（`毛额 + 50000` 分），用于演示"退款大于销售"的极端情况，**不符合真实财务流程**；
- 因此：任何"分析结论"只对本演示库成立，不能当作业务洞察。

## 6. 安全与隐私

- 无真实用户数据；`.env` 不入库，`.env.example` 只有占位；
- 日志对 `api_key`/`Authorization`/`sk-*` 做脱敏（`logging_utils.sanitize_fields`、`embeddings.redact_secret`）；
- 但：SQLite 文件本身未加密；导出报告未做访问控制；`/api/*` 无鉴权（仅本地演示）。

## 7. 如何验证上面这些"限制"是真的

```powershell
cd .\ops-assistant
# 1) sources 丢来源风险：grep 所有读取 sources 的位置
Select-String -Path .\src\ops_assistant\workflow\nodes.py -Pattern "sources"
# 2) 展示 SQL 与 queries 的双源
Select-String -Path .\src\ops_assistant\workflow\nodes.py -Pattern "_SQL"
# 3) 哈希向量不是语义模型
Select-String -Path .\src\ops_assistant\knowledge\embeddings.py -Pattern "HASHING_NOTE"
# 4) 没有 uv.lock
Test-Path .\uv.lock    # 预期 False
# 5) 没有运行生成物
Test-Path .\data\ops.db # 预期 False（除非你已跑过 bootstrap）
```

## 8. 面试时怎么说（一句话版）

> 这个项目的核心是"把不确定的模型放进确定的工程约束"：数字全部程序算、报告可追溯、审批可中断可恢复。
> 它的限制我也写清楚了——检索是词面向量、没做真实模型评估、单 worker、数据是合成的。
> 我没有把"没跑过"说成"跑通了"，验证记录在 `docs/verification.md`。
