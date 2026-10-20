"""单元14：隔离数据，完整问答、版本审批、追问与纯知识问答；无模型请求。"""

import dataclasses
import tempfile
from pathlib import Path

from ops_assistant.config import ensure_dirs, load_settings
from ops_assistant.data.generate import generate_database
from ops_assistant.workflow.persistence import AppRuntime

project = Path(__file__).resolve().parents[2] / "ops-assistant"
with tempfile.TemporaryDirectory(prefix="ops-course-14-") as directory:
    settings = dataclasses.replace(
        load_settings(Path(directory)),
        knowledge_dir=project / "data/knowledge",
        model="",
        api_key="",
        embedding_model="",
    )
    ensure_dirs(settings)
    generate_database(settings.db_path)
    runtime = AppRuntime.create(settings)
    try:
        turn = runtime.invoke_turn(
            "2026年9月销售额比8月变化多少？按地区分解，并说明退款口径。"
        )
        assert turn["status"] == "awaiting_approval", turn
        pending = turn["interrupt"]
        print("审批版本：", turn["report_id"], "中断：", pending["id"])
        approved = runtime.resume_turn(
            turn["thread_id"],
            "approve",
            report_id=turn["report_id"],
            interrupt_id=pending["id"],
            content_hash=pending["payload"]["content_hash"],
        )
        assert approved["status"] == "approved"
        assert Path(approved["export_path"]).is_file()
        followup = runtime.invoke_turn("华东地区呢？", thread_id=turn["thread_id"])
        assert followup["status"] == "awaiting_approval"
        assert followup["report_id"] != turn["report_id"]
        runtime.resume_turn(followup["thread_id"], "reject")
        knowledge = runtime.invoke_turn("净销售额的定义是什么？")
        assert knowledge["status"] == "answered"
        print("完整链路通过；新版本与旧版本隔离；纯知识问答：", knowledge["status"])
    finally:
        runtime.close()
