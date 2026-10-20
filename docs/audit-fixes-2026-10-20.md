# 对抗审查修复记录（2026-10-20）

本文件记录 2026-10-20 的两轮修复：第二轮（F01–F20，下面第 1–4 节）与第三轮（F21–F24，见文末章节）。**当前实测结果以第三轮章节和 [verification](verification.md) 首节为准。**

第二轮以原审查反例为起点修改实现，再把它们转成回归断言。原有 176 项测试不足以证明需求边界：审查反例 28 项中原先 24 项失败，另 4 项为正常路径对照；修复后这 28 项均通过，再增加 16 项覆盖版本下载、错误重置、导出恢复等边界，共 220 项测试（第三轮后为 259 项）。该数字不是线上错误率或模型准确率。

## 问题与验收映射

| 审查项 | 当前处理 | 主要验收证据 |
| --- | --- | --- |
| F01 时间方向、同比与相对年份 | 局部年份锚定，同比回到上年同期，from/to 当前与基期顺序明确 | period_semantics、period_anchor 测试 |
| F02 未知指标 | 利润/成本/订单数等未实现指标明确澄清 | unknown_metric 测试、E16 |
| F03 追问与地区筛选 | 先应用显式指标/全国/地区，再继承；未知地区包括混合列表要澄清 | gross/national/unknown_region 测试 |
| F04 分解口径 | gross/refund/net 分项、总量和报告公式一致，核对失败不出稿 | breakdown_uses_same_metric 测试、E19 |
| F05 地区过滤 | 金额、订单/退款计数、表格统一过滤 | refund_counts、scoped_detail 测试、E18 |
| F06 陈旧审批 | HTTP 强制 report_id + interrupt_id，可附 content_hash；验证与执行在同一进程锁内 | stale/missing/hash、版本下载测试 |
| F07 编辑篡改 | 正文只读，只追加无数字备注；非法编辑保持待审批，HTTP 422 | sign/unit/assignment 测试、test_hitl |
| F08 导出碰撞与覆盖 | 非 ASCII 清洗附摘要，报告按 thread_id + report_id 保存，冲突内容拒绝 | unicode、report_versions 测试 |
| F09 UI 误选会话 | 新会话显式空选项；当前 thread 与编辑器按版本绑定 | verify_ui.py 实际 HTTP + AppTest |
| F10 执行预算 | create_agent 使用工具/模型限额 middleware；0 不再回退默认值；调用前检查 | agent_tool_budget、zero_tool、E20、23_agent_budget.py |
| F11 纯知识问答 | 不依赖销售月份，knowledge answered 后直接结束 | knowledge_question 测试、E17、14_workflow.py |
| F12 零值与无订单描述 | 有订单且金额零才标真零；无订单仍可有退款 | true_zero/no_orders_with_refund 测试 |
| F13 模型配置 | 对话/embedding 传超时，限制重试，显式兼容 URL 时规范 provider 名 | configured_model_timeout 测试；真实请求未验证 |
| F14 评估可信度 | 运行目录隔离、外部模型关闭、状态/轨迹/数值断言，失败退出非零 | failed_eval 测试、20 条 eval |
| F15 引用 ID 不等于忠实度 | 每条回答要求引用块中的摘录，不合格退模板；不证明知识库本身正确 | citation_id_alone 测试 |
| F16 身份边界 | 校验已存 thread 所属 user_id；保留单用户无认证限制，未宣称生产隔离 | another_users_thread 测试；limitations |
| F17 导出失败恢复 | 批准版本保持不变，允许对 EXPORT_ERROR 重新 approve 仅重试导出 | export_failure_is_retryable 测试 |
| F18 历史错误污染当前轮 | 新轮用 Overwrite 重置 errors/trace，历史仍在 checkpoint | old_errors 测试 |
| F19 未实现维度 | 商品/月/渠道明确澄清，偏好只暴露地区 | unimplemented_dimensions 测试 |
| F20 引用 ID 重复拼接 | 报告使用索引原始 chunk_id，避免 doc_id 再拼一次 | final_report_citation_ids 测试 |

测试位置：`ops-assistant/tests/test_audit_regressions.py`（44 项含参数化），及原有 tests 文件。预期发生变化的原测试已按新业务契约更新：不合法编辑应拒绝，已导出文件按版本命名，HTTP resume 必须传身份标识。

## 教程同步

修正节点 None/末端 END、未知键与并发写入的框架描述；区分读历史与真正 replay/fork；加入不同深度分支的 join 屏障实验；修正副作用失败发生在写入前后的区别。单元 13/14 对齐当前指标与解析实现，16 的迷你编辑策略与主项目一致，19 的 HTTP 示例回传审批标识。新增单元 23 与三个脚本：14_workflow、23_framework_contracts、23_agent_budget；目前共 23 个实验脚本。

教程中的迷你图依然只用于概念实验：单元 16 的固定文件名不用于正式系统的版本隔离；主项目以版本化导出为准。旧日期验证记录保留为历史，当前结果集中在 verification.md 首节。

## 第二轮实测结果与复现（历史）

> 本节记录 F01–F20 那一轮的结果，计数不代表当前版本；最新实测结果见下面的第三轮章节与 [verification](verification.md) 首节。

Linux x86_64 / Python 3.12.14 / uv 0.12.23，锁文件中的 LangChain 1.4.6、LangGraph 1.2.16。锁定安装、compileall、ruff、220 项测试、20 条确定性评估和 23 个实验脚本通过；真实本地 API + Streamlit AppTest 与三个独立进程的审批恢复通过。复现命令见 [verification](verification.md)。

新增 GitHub Actions：Linux/Windows × Python 3.11/3.12，执行锁定安装、bootstrap、lint、测试、评估和恢复。CI 的实际远端运行结果另看 Actions，不能从配置文件推断通过。

仍未验证真实 LLM/embedding、Windows 本地执行、浏览器渲染和多用户压测。主产品链路是确定性工作流，可选 Agent 的工具预算使用假模型验证；这些结果不能替代真实模型效果评估。user_id 由客户端提供，所有 HTTP 接口仍无认证，只适合本机单用户演示。文件原子写不提供跨 worker 的 exactly-once，展示 SQL 双源等权衡仍列在 [limitations](limitations.md)。

## 第三轮实测审查（2026-10-20）

前两轮结束后，全套验证已经全绿（220 项测试、20/20 评估、ruff 清零、23 个实验脚本退出码 0）。本轮先复跑确认全绿，再对解析层与导出层做对抗性输入探测——**缺陷不是靠跑既有命令发现的**，既有命令一条都没报错。发现 4 项，编号续 F21–F24。

| # | 现象 | 根因 | 修复 | 严重度 |
| --- | --- | --- | --- | --- |
| F21 | `今年 9 月比去年 9 月变化多少` 报告输出"本期 2026-09 对比 **上期 2026-09**"，差额恒为 0；无空格写法 `今年9月比去年9月` 却正确 | `_collect_mentions` 用 `prefix.endswith("去年")` 判断相对年份，`prefix` 是 3 个**原文**字符的窗口，"比去年 9 月"里的空格让判断失效，基期退回 `year_context`（今年） | 前缀窗口扩到 8 字符并先剔除空白再比较；年份锚点移出循环只算一次 | 高：报告数字静默错误，属 F01 同族的漏网变体 |
| F22 | `2026 年 9 月各地区销售额` 被回以 `未找到地区「月各」`；同族 `各大区/各区域/每个地区/各个地区/按地区分解/分地区看/这个地区` 全部误报 | `_REGION_PHRASE_RE` 取"地区"前 2–4 个汉字做候选词，会把前面的时间/量词字一起吃进来（`月各`、`月按`、`月分`、`月这个`），挡住 `_REGION_STOPWORDS` 判断——`按/分/这个/每个/各个` 本来就在停用词表里 | 候选词先剥掉前导时间/量词噪声再比对停用词与已知地区名 | 高：拦下合法提问；`按地区分解` 正是项目自己在澄清话术里推荐的示例说法 |
| F23 | `每个地区/各个地区` 修掉误报后维度仍是 `unknown`，静默出一份只有总量的报告 | `_detect_dimension` 的地区关键词表收了 `各地区` 但没收 `每个地区/各个地区/按区域/分区域/各区域` | 补齐同族分布说法的关键词 | 中：不报错但答非所问 |
| F24 | `exported_report_path("t-123", dir)` 返回 `report-t-123.md`，而 `export_report` 永远写 `report-t-123-{report_id}.md` | 版本化文件名（F08）落地后，这个查询用辅助函数没有同步 | 改为版本感知：已知 `report_id` 直接算版本化文件名；省略时定位目录里最近写入的版本；无版本时回退且 `exists()` 为假 | 中：`report/__init__.py` 公开导出的 API 给出的路径必然不存在 |

附带一处契约收敛：`parse_question` 的模块契约写明"解析失败不抛异常"，但传入非字符串会 `AttributeError: 'int' object has no attribute 'strip'`；改为按 `str()` 收敛后走澄清路径。

测试位置：仍集中在 `ops-assistant/tests/test_audit_regressions.py`，新增 39 项（含参数化），该文件由 44 项增至 83 项，全仓 220 → 259 项。新增断言覆盖：空格/无空格同比必须同解、同比不得自比、9 种地区分布说法不得触发未知地区澄清、7 种分布说法必须落到 region 维度、未知地区澄清须点出用户真正说的地区名（`「华中」` 而非 `「月华中」`）、既有追问形态 `那华东地区呢？` 不被误伤、非字符串输入不抛异常、`exported_report_path` 三种情形（带 report_id / 省略 / 无任何版本）。

三条断言最初写得过严并被本轮实测纠正，记录在此以免被当成产品缺陷：`这个地区` 是指示代词，不该断言 dimension=region（停用词表本就收录它，F22 只是让被"月"字挡住的判断重新生效）；`["华东"]`、`{"region": "华东"}` 按 `str()` 收敛后字符串里确实含"华东"，命中地区因而不需澄清，该用例要验的是"不抛异常"而不是"必须澄清"。

未改动：方向语义仍按"提及顺序 + `到/至` 前向标记"决定。`去年 9 月和今年 9 月对比` 解析为本期 2025-09、上期 2026-09——两个期间都真实且不再自比，但顺序是否符合提问意图属另一层语义判断，本轮不扩大范围。

## 官方核对来源

- [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [INVALID_CONCURRENT_GRAPH_UPDATE](https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE)
- [middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

官方事实与本项目设计选择分别解释，示例不使用已弃用的旧版 Agent 构造方式。核对日期 2026-10-20，升级 lock 后须重新验收。
