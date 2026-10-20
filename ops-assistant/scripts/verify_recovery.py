"""Prove pending HITL recovery across distinct Python processes."""

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ops_assistant.config import ensure_dirs, load_settings
from ops_assistant.data.generate import generate_database
from ops_assistant.workflow.persistence import AppRuntime

PROJECT = Path.cwd().resolve()


def settings_at(path):
    return dataclasses.replace(load_settings(Path(path)), knowledge_dir=PROJECT / "data/knowledge")


if len(sys.argv) > 1:
    runtime = AppRuntime.create(settings_at(sys.argv[2]))
    try:
        result = (
            runtime.invoke_turn("2026年9月销售额比8月变化多少？")
            if sys.argv[1] == "first"
            else runtime.resume_turn(sys.argv[3], "approve")
        )
        print(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "thread_id": result["thread_id"],
                    "status": result["status"],
                    "export_path": result["export_path"],
                },
                ensure_ascii=False,
            )
        )
    finally:
        runtime.close()
else:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("OPS_"):
            env.pop(key, None)
    env.update(OPS_MODEL="", OPS_API_KEY="", OPS_EMBEDDING_MODEL="", LANGSMITH_TRACING="false")
    directory = Path(tempfile.mkdtemp(prefix="ops-process-audit-"))
    s = settings_at(directory)
    ensure_dirs(s)
    generate_database(s.db_path)

    def child(step, thread=""):
        out = subprocess.check_output(
            [sys.executable, str(Path(__file__).resolve()), step, str(directory), thread], env=env, text=True
        )
        return json.loads(out.strip().splitlines()[-1])

    first = child("first")
    second = child("resume", first["thread_id"])
    content = Path(second["export_path"]).read_bytes()
    third = child("repeat", first["thread_id"])
    assert first["status"] == "awaiting_approval"
    assert second["status"] == third["status"] == "approved"
    assert len({first["pid"], second["pid"], third["pid"]}) == 3
    assert second["export_path"] == third["export_path"]
    assert Path(third["export_path"]).read_bytes() == content
    (PROJECT / "data/verification").mkdir(parents=True, exist_ok=True)
    record = {"passed": True, "processes": [first, second, third]}
    (PROJECT / "data/verification/process-recovery.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
