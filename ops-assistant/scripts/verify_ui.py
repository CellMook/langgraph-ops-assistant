"""Isolated live-API + Streamlit AppTest interaction probes (not browser screenshots)."""

import dataclasses
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path.cwd().resolve()
OUT = PROJECT / "data/verification"
OUT.mkdir(parents=True, exist_ok=True)
for key in list(os.environ):
    if key.startswith("OPS_") or key.endswith("_PROXY") or key.endswith("_proxy"):
        os.environ.pop(key, None)
os.environ["OPS_MODEL"] = ""
os.environ["OPS_API_KEY"] = ""
os.environ["OPS_EMBEDDING_MODEL"] = ""
os.environ["LANGSMITH_TRACING"] = "false"
from ops_assistant.config import ensure_dirs, load_settings  # noqa: E402
from ops_assistant.data.generate import generate_database  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="ops-ui-audit-"))
settings = dataclasses.replace(load_settings(tmp), knowledge_dir=PROJECT / "data/knowledge")
ensure_dirs(settings)
generate_database(settings.db_path)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base = f"http://127.0.0.1:{port}"
os.environ["OPS_API_BASE_URL"] = base
server_code = """
import sys,dataclasses
from pathlib import Path
import uvicorn
from ops_assistant.config import load_settings
from ops_assistant.api import main
s=dataclasses.replace(load_settings(Path(sys.argv[1])), knowledge_dir=Path(sys.argv[2]))
main.load_settings=lambda:s
uvicorn.run(main.app,host='127.0.0.1',port=int(sys.argv[3]),log_level='warning')
"""
process = subprocess.Popen([sys.executable, "-c", server_code, str(tmp), str(PROJECT / "data/knowledge"), str(port)])
records = []
try:
    import httpx
    from streamlit.testing.v1 import AppTest

    with httpx.Client(trust_env=False, timeout=10) as client:
        for _ in range(200):
            try:
                if client.get(base + "/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)

        def snap(label, app):
            turn = app.session_state["turn"] or {}
            result = {
                "label": label,
                "thread_id": app.session_state["thread_id"],
                "status": turn.get("status"),
                "question": turn.get("spec", {}).get("raw"),
                "exceptions": [x.message for x in app.exception],
                "errors": [x.value for x in app.error],
            }
            records.append(result)
            return turn

        app = AppTest.from_file(str(PROJECT / "src/ops_assistant/ui/streamlit_app.py"), default_timeout=20).run()
        snap("empty", app)
        app.chat_input[0].set_value("2026年9月销售额比8月变化多少？").run()
        first = snap("first_question", app)
        first_editor = app.text_area[0].value
        next(b for b in app.button if "通过并导出" in b.label).click().run()
        approved = snap("first_approve", app)
        records.append(
            {
                "label": "first_approve_completed",
                "passed": approved.get("status") == "approved" and len(app.get("download_button")) == 1,
                "download_button_count": len(app.get("download_button")),
            }
        )
        next(b for b in app.button if "新建会话" in b.label).click().run()
        snap("new_chat_after_history_exists", app)
        records.append({"label": "new_chat_is_empty", "passed": app.session_state["thread_id"] is None})
        app.chat_input[0].set_value("2026年7月销售额比6月变化多少？").run()
        second = snap("second_question", app)
        assert second["thread_id"] != first["thread_id"]
        before_edit = app.text_area[0].value if app.text_area else None
        current_draft = second.get("interrupt", {}).get("payload", {}).get("report_markdown")
        records.append(
            {
                "label": "editor_matches_current_draft",
                "passed": before_edit == current_draft,
                "editor_still_first_draft": before_edit == first_editor,
            }
        )
        next(b for b in app.button if "通过并导出" in b.label).click().run()
        snap("second_approve", app)
        # A second tab advances a different thread, changing sidebar ordering.
        client.post(base + "/api/chat", json={"question": "2026年5月销售额是多少？"})
        app.run()
        snap("rerun_after_other_thread_created", app)
        # A fresh UI test session loads existing history and then asks/approves.
        fresh = AppTest.from_file(str(PROJECT / "src/ops_assistant/ui/streamlit_app.py"), default_timeout=20).run()
        fresh.chat_input[0].set_value("2026年9月销售额是多少？").run()
        pending = snap("fresh_with_history_question", fresh)
        next(b for b in fresh.button if "通过并导出" in b.label).click().run()
        result = snap("fresh_with_history_approve", fresh)
        records.append(
            {
                "label": "history_approval_keeps_target",
                "passed": result.get("thread_id") == pending["thread_id"] and result.get("status") == "approved",
            }
        )
finally:
    process.terminate()
    process.wait(timeout=5)
    (OUT / "ui-probe.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(records, ensure_ascii=False, indent=2))

if any(record.get("passed") is False or record.get("exceptions") or record.get("errors") for record in records):
    raise SystemExit(1)
