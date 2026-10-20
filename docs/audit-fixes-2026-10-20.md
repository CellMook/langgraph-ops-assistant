# 对抗审查修复记录（2026-10-20）

本轮以原审查反例为起点修改实现，再把它们转成回归断言。原有 176 项测试不足以证明需求边界：审查反例 28 项中原先 24 项失败，另 4 项为正常路径对照；修复后这 28 项均通过，再增加 16 项覆盖版本下载、错误重置、导出恢复等边界，共 220 项测试。该数字不是线上错误率或模型准确率。

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

## 实测结果与复现

Linux x86_64 / Python 3.12.14 / uv 0.12.23，锁文件中的 LangChain 1.4.6、LangGraph 1.2.16。锁定安装、compileall、ruff、220 项测试、20 条确定性评估和 23 个实验脚本通过；真实本地 API + Streamlit AppTest 与三个独立进程的审批恢复通过。复现命令见 [verification](verification.md)。

新增 GitHub Actions：Linux/Windows × Python 3.11/3.12，执行锁定安装、bootstrap、lint、测试、评估和恢复。CI 的实际远端运行结果另看 Actions，不能从配置文件推断通过。

仍未验证真实 LLM/embedding、Windows 本地执行、浏览器渲染和多用户压测。主产品链路是确定性工作流，可选 Agent 的工具预算使用假模型验证；这些结果不能替代真实模型效果评估。user_id 由客户端提供，所有 HTTP 接口仍无认证，只适合本机单用户演示。文件原子写不提供跨 worker 的 exactly-once，展示 SQL 双源等权衡仍列在 [limitations](limitations.md)。

## 官方核对来源

- [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel)
- [INVALID_CONCURRENT_GRAPH_UPDATE](https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE)
- [middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

官方事实与本项目设计选择分别解释，示例不使用已弃用的旧版 Agent 构造方式。核对日期 2026-10-20，升级 lock 后须重新验收。
