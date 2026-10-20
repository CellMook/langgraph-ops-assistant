"""单元 22（附录）实验：把LangGraph 的流式输出接到主项目工作流上。

用法（在 ops-assistant 目录执行）：
    uv run python ../教程/实验/22_streaming.py

本实验不需要模型密钥（纯规则模式），验证四件事：
    1) stream_mode="updates"：每个节点跑完立刻吐一条 {节点名: 状态增量}；
    2) 多模式组合 ["updates", "values"]：返回 (模式, 数据) 二元组；
    3) 审批中断在流里长什么样：{"__interrupt__": (Interrupt(...),)}；
    4) resume 之后继续流式收尾（review/export 的增量）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2] / "ops-assistant"
SRC_DIR = PROJECT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ops_assistant.config import load_settings  # noqa: E402
from ops_assistant.workflow.persistence import AppRuntime  # noqa: E402

MAIN_QUESTION = (
    "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"
)


def main() -> None:
    settings = load_settings(PROJECT_DIR)
    runtime = AppRuntime.create(settings, use_memory_checkpointer=True)
    try:
        thread_id = "exp22-stream"
        config = {
            "configurable": {"thread_id": thread_id, "user_id": "demo-user"},
        }
        context = runtime._context("demo-user")  # noqa: SLF001 - 教学实验直接复用装配逻辑

        print("== 1) stream_mode='updates'：节点级进度反馈 ==")
        for chunk in runtime.graph.stream(
            {"question": MAIN_QUESTION, "thread_id": thread_id, "user_id": "demo-user"},
            config=config,
            context=context,
            stream_mode="updates",
        ):
            for node, update in chunk.items():
                keys = sorted(update.keys()) if isinstance(update, dict) else ["<非dict>"]
                print(f"  节点 {node:<12} 写入字段: {keys}")

        print("== 2) 状态快照：应停在 awaiting_approval ==")
        snapshot = runtime.graph.get_state(config)
        print("  status:", snapshot.values.get("status"))
        print("  interrupt:", (snapshot.next and "有" ) or "无")

        print("== 3) 多模式组合 ['updates', 'values']（resume 之后）==")
        from langgraph.types import Command  # noqa: E402

        for mode, chunk in runtime.graph.stream(
            Command(resume={"decision": "approve", "comment": "流式实验"}),
            config=config,
            context=context,
            stream_mode=["updates", "values"],
        ):
            if mode == "updates":
                for node, update in chunk.items():
                    keys = sorted(update.keys()) if isinstance(update, dict) else ["<非dict>"]
                    print(f"  [updates] 节点 {node:<12} 写入字段: {keys}")
            else:
                status = chunk.get("status")
                export_path = chunk.get("export_path")
                print(f"  [values ] 全量状态 status={status} export={export_path}")

        print("== 4) 结论对照 ==")
        final = runtime.graph.get_state(config).values
        print("  最终 status    :", final.get("status"))
        print("  导出文件       :", final.get("export_path"))
        print("  轨迹节点顺序   :", [t.get("node") for t in final.get("trace", [])][:12])
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
