"""实验 17：验证导出幂等、重启恢复与时间旅行。"""

from __future__ import annotations

from pathlib import Path

from ops_assistant.config import load_settings
from ops_assistant.workflow.persistence import AppRuntime

QUESTION = "2026 年九月销售额比八月变化多少？按地区分解差额，并说明销售额是否扣除了退款。"


def main() -> None:
    settings = load_settings()

    # ---- 1) 第一次运行：跑到审批中断 ----
    first = AppRuntime.create(settings)  # 默认用 SqliteSaver
    try:
        turn = first.invoke_turn(QUESTION)
        thread_id = turn["thread_id"]
        print("状态：", turn["status"])
        print("报告字符数：", len(turn["report"]))
        approved_once = first.resume_turn(thread_id, "approve")
        approved_twice = first.resume_turn(thread_id, "approve")
        print("第一次导出：", approved_once["export_path"])
        print("第二次导出：", approved_twice["export_path"])
        print("路径相同：", approved_once["export_path"] == approved_twice["export_path"])

        # 导出目录里这个 thread 只应有一个文件
        export_dir = Path(settings.export_dir)
        files = sorted(path.name for path in export_dir.glob(f"report-{thread_id}*.md"))
        print("导出文件列表：", files)

        # ---- 2) 时间旅行：看历史快照 ----
        config = {"configurable": {"thread_id": thread_id}}
        history = list(first.graph.get_state_history(config))
        print("历史快照数量：", len(history))
        for snapshot in history[:5]:
            # next 表示"如果继续执行，下一个要跑的节点"
            print("  checkpoint:", snapshot.config["configurable"].get("checkpoint_id"), "next:", snapshot.next)
    finally:
        first.close()

    # ---- 3) 模拟重启：新的进程实例读同一个检查点 ----
    second = AppRuntime.create(settings)
    try:
        reloaded = second.read_thread(thread_id)
        print("重启后状态：", reloaded["status"])
        print("重启后导出路径：", reloaded["export_path"])
        again = second.resume_turn(thread_id, "approve")
        print("重启后重复审批路径相同：", again["export_path"] == reloaded["export_path"])
    finally:
        second.close()


if __name__ == "__main__":
    main()
