# 评估（eval）说明

这个目录回答一个问题：**"你怎么知道它是对的？"**——不是"跑通了一次"，而是"有固定用例、可重复、能指出哪一步不对"。

## 两层评估，不要混在一起

| 层 | 评什么 | 怎么做 | 结果可信度 |
| --- | --- | --- | --- |
| 确定性层（本项目主线） | 解析、SQL 口径、指标计算、工作流状态机、审批语义 | `pytest tests/` 与 `python eval/run_eval.py` | 可重复：同样输入必得同样结果 |
| 模型行为层（可选增强） | 模型是否选对工具、答案措辞是否忠实、token 成本 | 需配置真实模型后另行记录 | 受模型版本、提示词、温度影响，**不能**用本目录结果代替 |

`run_eval.py` **只跑确定性层**。它的输出里专门写了：

```json
"model_behavior_evaluated": false
```

意思是"这次没有评估模型"。如果哪个项目文档拿 `status_matched` 当"准确率"，那是偷换概念。

## 用例集 `cases.jsonl`

每行一个 JSON 对象，字段如下：

| 字段 | 含义 |
| --- | --- |
| `id` | 用例编号（E01…），报告与日志里引用它 |
| `category` | 分类：normal / follow_up / clarification / boundary / unsupported_dimension / invalid_input / knowledge / budget / approval |
| `question` | 用户问题原文 |
| `expect_status` | 预期的工作流状态：`awaiting_approval` / `clarification` / `approved` / `rejected` / `budget_exceeded` |
| `expect_trace_contains` | 轨迹里应出现的节点（只检查"出现"，不检查顺序） |
| `flow` | 收到审批中断后做什么：`none` / `approve` / `reject` / `edit` |
| `requires_thread` | 是否先跑一轮主问题以便追问能继承上下文 |
| `settings_override` | 临时覆盖配置，例如 `{"max_steps": 2}` 用来制造预算耗尽 |
| `note` | 这条用例在防什么回归 |

覆盖的场景：

- 正常对比问答（E01）与追问继承（E02）；
- 无上下文的追问、缺年份、越界月份必须澄清（E03–E05、E10）；
- 真零、pending 退款、零基数、不支持维度（E06–E09）；
- 口径类问题走知识检索并带引用（E11）；
- 预算耗尽走统一失败出口（E12）；
- 审批三态：编辑后导出、拒绝不落盘（E13–E14）。

## 运行

前提：已执行过 `uv run python scripts/bootstrap.py`（需要 `data/ops.db` 与知识索引）。

```bash
cd ops-assistant
uv run python eval/run_eval.py --verbose
```

输出写到 `eval/results/deterministic-<UTC时间戳>.json`（该目录被 `.gitignore` 忽略）。

## 怎么读结果

先看 `summary`：

```json
"summary": {
  "total": 14,
  "status_matched": 14,
  "status_mismatched": 0,
  "exception_count": 0,
  "note": "status_matched 仅统计工作流状态是否与用例预期一致，不是准确率。"
}
```

再看单条 `cases[i]`：

- `actual_status` 与 `expected_status` 不一致 → 先查 `trace_nodes` 停在哪一步；
- `missing_expected_nodes` 非空 → 说明该走的节点没走（通常是路由条件变了）；
- `errors` 非空 → 看错误码（`INVALID_MONTH` / `BUDGET_EXCEEDED` / `GRAPH_ERROR` 等）；
- `usage.model_calls` 应为 0（没配模型时）；若不为 0，说明有代码在偷偷调模型。

## 诚实的边界

- 本目录**没有**任何随仓库分发的"评估结果文件"。`eval/results/` 是运行生成物。
- 编写代码的环境无法启动 Python 进程，因此仓库里不存在"实测 14/14 通过"这类数字；
  谁要引用通过率，必须在自己的机器上跑出来并附上 `generated_at_utc`。
- 用例集覆盖的是**我们已经想到的失败模式**，不是穷举；新增 bug 时应该先补一条用例再修。
