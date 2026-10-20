# 5 分钟演示脚本（含真实失败路径）

目标：5 分钟内让面试官相信三件事——**能跑**、**数字可信**、**边界清楚**。

> 演示前先读 `docs/verification.md`：你在自己机器上跑过的才算"验证"，演示时不要替仓库撒谎。

## 0. 演示前 3 分钟自检（不要省略）

```powershell
cd .\ops-assistant
uv run python -m compileall -q src scripts tests eval
uv run pytest -q
uv run python scripts/run_cli.py --health
```

若 `pytest` 有失败：**照实说**"这条现在失败，原因是我改了 X"，比假装全绿安全。

## 1. 开场 30 秒

> "这是一个经营数据分析助手。用户用中文问'九月比八月销售额变化多少、按地区分解'，系统会解析口径、查知识库、查 SQLite、**用程序算差额**、生成带来源的报告，然后**停下来等人工审批**，批准后才落盘。
> 最关键的一点：**所有数字都是程序算的，模型只做可选润色且不允许出现数字**——所以没有 API Key 也能跑通全链路。"

（可选）打开 `README.md` 首屏，指"30 秒上手：不需要 API Key"。

## 2. 正常路径（约 90 秒）

```powershell
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
```

照着输出讲：

1. `状态：awaiting_approval` —— "没有直接导出，因为这是要给人看的经营报告"；
2. 报告 8 个小节 —— 指出「关键数字」「地区差额分解」「口径与依据」「来源」；
3. `来源` —— "每个数字能指回一条 SQL 或一个知识块"。

```powershell
uv run python scripts/run_cli.py --list-threads
uv run python scripts/run_cli.py --thread-id <thread_id> --resume approve --comment "口径确认"
```

讲：`状态：approved`，`导出文件：data/exports/report-<thread_id>-<report_id>.md`；打开文件，内容与屏幕报告一致。

## 3. 多轮追问（约 40 秒）

```powershell
uv run python scripts/run_cli.py -q "那华东地区呢？" --thread-id <thread_id>
```

讲："它没有重新问'哪个月'，而是沿用上一轮的 2026-09 vs 2026-08，只把地区过滤成华东。这就是 `last_spec` 的作用。"

## 4. 四条失败路径（约 120 秒，**这是加分项**）

### 失败 1：缺年份 → 澄清（不是报错）

```powershell
uv run python scripts/run_cli.py -q "九月销售额是多少？"
```

讲："状态是 `clarification`，它问'哪一年'并给出数据范围。宁可多问一句，也不猜年份。"

### 失败 2：非法月份 → 可读错误

```powershell
uv run python scripts/run_cli.py -q "2026 年 13 月销售额比 12 月变化多少？"
```

讲："月份解析在进入 SQL 之前就被拦住了，不会出现 `2026-13-01` 这种脏参数。"

### 失败 3：拒绝审批 → 不落盘

```powershell
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额。" 
uv run python scripts/run_cli.py --thread-id <新thread_id> --resume reject --comment "数字需复核"
```

讲："`rejected`，`data/exports/` 下没有这个 thread 的文件。审批不是走形式。"

### 失败 4：资料不足 → 明说不知道

```powershell
uv run python scripts/run_cli.py -q "2026 年 9 月销售额口径是什么？量子退火怎么算？"
```

讲："知识库里没有'量子退火'，它说'现有资料不足以回答'并列出已检索文档，而不是编一段。"

### 失败 5（可选）：预算耗尽 → 统一失败出口

```powershell
$env:OPS_MAX_STEPS = "2"
uv run python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？按地区分解差额。"
Remove-Item Env:\OPS_MAX_STEPS
```

讲："不是抛 `GraphRecursionError`，而是 `budget_exceeded` + 可操作建议。"

## 5. 收尾（约 30 秒）

打开 `docs/limitations.md`，指两行：

> "检索默认是确定性哈希向量，不是语义模型；没做真实模型行为评估；单 worker；数据是合成的。
> 这些不是我忘做了，是我知道它们在哪、代价是什么。"

然后说下一步（任选两条，讲清优先级理由）：
1. 接真实 embedding + 召回评估集；
2. 给 `sources` 加 reducer，消除静默丢来源；
3. 并发审批幂等与审计。

## 6. 面试官可能的追问与一句话回答

| 追问 | 回答要点 |
| --- | --- |
| "中断后节点会重跑，你怎么保证不重复写文件？" | 副作用只在 `export` 节点；固定文件名 + 原子写；无待审批时 `resume_turn` 直接返回既有结果 |
| "为什么不用 Agent 自由选工具？" | 口径要可审计、可复现；Agent 路线保留在 `agent/` 做对比，不是主链路 |
| "上期是 0，变化率怎么算？" | `pct_change=None` + `base_zero=True` + 报告文字说明，不编百分比 |
| "无记录和真零怎么区分？" | `has_orders` 标志 + gross/refund 关系；报告「数据边界」小节写明 |
| "模型编造引用怎么办？" | 引用必须是本次命中的 `chunk_id`，否则回退模板（`knowledge/qa.py`） |
| "并发两个请求会怎样？" | 单 worker + `AppRuntime.lock` 串行；跨进程并发是已知限制 |
| "你怎么证明它是对的？" | 确定性测试 + 独立标准答案交叉验证；模型行为未验证，写在 verification |
| "这个项目能上线吗？" | 不能直接上线：无鉴权、无审计、单 worker、合成数据。它是"工程约束"的演示，不是生产系统 |

## 7. 演示事故预案

| 事故 | 处理 |
| --- | --- |
| `ModuleNotFoundError` | 确认在 `ops-assistant` 目录且 `uv sync` 过；`uv run python -c "import ops_assistant"` 自检 |
| `no such table` | 没跑 `bootstrap.py`；现场跑一次（约十几秒）或切到 API 已就绪的终端 |
| 端口占用 | `--port 8001`；UI 改 API 地址 |
| 模型超时 | 忽略即可：主链路不依赖模型；讲"这正是模型可选的意义" |
| 电脑没网 | 全链路离线可用（哈希向量 + 模板答案），正好演示"无 Key 也能跑" |

## 8. 演示后自查

- [ ] 成功路径讲到了"数字由程序算"；
- [ ] 至少 3 条失败路径，每条都解释了"为什么这样设计"；
- [ ] 主动讲了 2 条以上限制；
- [ ] 没有说"已通过全部测试"这类没有证据的话；
- [ ] 时间控制在 5–6 分钟（超时说明你讲太细，砍掉实现细节）。
