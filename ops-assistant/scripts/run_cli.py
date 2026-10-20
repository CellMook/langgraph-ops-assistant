"""命令行入口：不启动浏览器/服务，直接跑一轮问答、审批或查看会话。

用法示例::

    # 跑一轮问答（自动新建会话）
    python scripts/run_cli.py -q "2026 年九月销售额比八月变化多少？"

    # 继续同一个会话追问（沿用上一轮的时间范围/地区，可被新问题覆盖）
    python scripts/run_cli.py --thread-id <thread_id> -q "那华东呢？"

    # 查看会话列表 / 单个会话
    python scripts/run_cli.py --list-threads
    python scripts/run_cli.py --read-thread <thread_id>

    # 审批
    python scripts/run_cli.py --thread-id <thread_id> --resume approve --comment "OK"
    python scripts/run_cli.py --thread-id <thread_id> --resume edit --edited-report-file draft.md

    # 机器可读输出（含完整 report/trace/sources）
    python scripts/run_cli.py -q "..." --json

退出码：0 成功；1 工作流失败/参数或运行时错误；2 argparse 用法错误。
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:  # 支持"不安装直接跑"：优先用仓库内 src 布局
    sys.path.insert(0, str(SRC_DIR))

# noqa: E402 - 上面的 sys.path 调整必须先执行，才能导入 ops_assistant
from ops_assistant.workflow.persistence import AppRuntime, ThreadNotFoundError  # noqa: E402
from ops_assistant.workflow.state import DEFAULT_USER_ID  # noqa: E402

# 节点名 → 中文步骤提示。只用于 CLI 进度展示，不进入任何业务判断。
STEP_LABELS = {
    "understand": "解析问题",
    "clarify": "请求澄清",
    "knowledge": "检索知识库",
    "data": "查询经营数据",
    "repair": "修复查询",
    "compute": "计算指标与差额",
    "draft": "渲染报告草稿",
    "review": "人工审批",
    "export": "导出报告",
    "fail": "统一失败出口",
}


def _run_streaming(runtime: AppRuntime, args: argparse.Namespace) -> dict[str, Any] | None:
    """流式跑一轮：逐事件打印进度，返回终止事件里的 TurnResult。

    输出约定：
        - ``--json``：每个事件打印成**一行** JSON（JSONL），完整 turn 在 ``final`` 事件里；
          此时不再打印人类可读结果，避免一个命令产出两种 JSON 形态。
        - 默认：打印中文步骤进度，最后仍由 ``_print_turn`` 输出与非流式一致的结果。

    流式只改变"进度怎么送达"，不改变审批语义：``interrupt`` 之后仍须 ``--resume``。
    """
    turn: dict[str, Any] | None = None
    for event in runtime.stream_turn(
        str(args.question or ""),
        thread_id=args.thread_id,
        user_id=args.user_id,
    ):
        name = str(event.get("event") or "")
        if args.json:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        elif name == "node":
            node = str(event.get("node") or "")
            print(f"· {STEP_LABELS.get(node, node)}（{node}）", flush=True)
        elif name == "interrupt":
            payload = event.get("payload") or {}
            options = "/".join(str(item) for item in (payload.get("options") or []))
            print(f"! 待人工审批：{payload.get('type') or ''}，可选 {options}", flush=True)
        elif name == "start":
            print(f"· 会话 {event.get('thread_id') or ''}", flush=True)
        if name in {"final", "error"}:
            turn = event.get("turn") if name == "final" else None
            if name == "error":
                print(f"× 流式失败：{event.get('code') or ''} {event.get('message') or ''}", flush=True)
    return turn


def _configure_stdout() -> None:
    """尽量把标准输出切到 UTF-8，避免 Windows 控制台把中文标记成乱码。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # 某些被重定向的流不支持 reconfigure
            pass


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="run_cli.py",
        description="经营数据分析助手命令行：问答、审批、查看会话。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-q", "--question", help="要分析的问题；不带其它动作时必填")
    parser.add_argument("--thread-id", help="会话 id（多轮追问/审批时使用）")
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help=f"用户 id，默认 {DEFAULT_USER_ID}")
    parser.add_argument(
        "--resume",
        choices=["approve", "reject", "edit"],
        help="提交人工审批结论（需要 --thread-id）",
    )
    parser.add_argument("--comment", default="", help="审批备注（可选）")
    parser.add_argument(
        "--edited-report-file",
        type=Path,
        help="decision=edit 时的修改稿文件（UTF-8 Markdown）",
    )
    parser.add_argument("--list-threads", action="store_true", help="列出历史会话后退出")
    parser.add_argument("--read-thread", metavar="THREAD_ID", help="读取指定会话状态后退出")
    parser.add_argument("--health", action="store_true", help="打印运行时健康状态后退出")
    parser.add_argument("--limit", type=int, default=20, help="--list-threads 的最大条数，默认 20")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="流式跑一轮问答：逐节点打印进度（配 --json 时输出 JSONL 事件流）",
    )
    return parser


def _fail(message: str) -> int:
    """把错误写 stderr 并返回非零退出码。"""
    print(message, file=sys.stderr)
    return 1


def _read_edited_report(path: Path) -> str | None:
    """读取修改稿文件；失败返回 None。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"读取修改稿失败：{path}（{type(exc).__name__}: {exc}）")
        return None


def _print_turn(turn: dict[str, Any], *, as_json: bool) -> None:
    """打印一轮结果（人类可读或 JSON）。"""
    if as_json:
        print(json.dumps(turn, ensure_ascii=False, indent=2))
        return

    print(f"会话：{turn.get('thread_id') or ''}")
    print(f"状态：{turn.get('status') or ''}")
    answer = str(turn.get("answer") or "")
    if answer:
        print(f"答复：{answer}")
    if turn.get("clarification"):
        print(f"澄清：{turn.get('clarification')}")

    report = str(turn.get("report") or "")
    if report:
        print("\n—— 报告（Markdown）——")
        print(report)

    for table in turn.get("tables") or []:
        if not isinstance(table, dict):
            continue
        columns = [str(column) for column in (table.get("columns") or [])]
        rows = [row for row in (table.get("rows") or []) if isinstance(row, (list, tuple))]
        if not columns:
            continue
        print(f"\n—— {table.get('title') or '明细表'} ——")
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(cell) for cell in row))

    trace = [item for item in (turn.get("trace") or []) if isinstance(item, dict)]
    if trace:
        print(f"\n—— 执行轨迹（{len(trace)} 条）——")
        for entry in trace:
            tool = f" tool={entry.get('tool')}" if entry.get("tool") else ""
            retryable = " retryable" if entry.get("retryable") else ""
            print(
                f"- {entry.get('node')}/{entry.get('action')} "
                f"ok={entry.get('ok')} {entry.get('duration_ms') or 0}ms{tool}{retryable}"
            )

    sources = [item for item in (turn.get("sources") or []) if isinstance(item, dict)]
    if sources:
        print(f"\n—— 来源（{len(sources)} 条）——")
        for source in sources:
            if source.get("type") == "sql":
                print(f"[SQL] {source.get('note') or ''}")
                print(str(source.get("sql") or ""))
                params = source.get("params")
                if params:
                    print("params: " + json.dumps(params, ensure_ascii=False))
            else:
                ref = source.get("ref") or f"[{source.get('doc_id')}#{source.get('chunk_id')}]"
                print(f"[文档] {ref}《{source.get('title') or ''}》：{source.get('snippet') or ''}")

    usage = turn.get("usage") if isinstance(turn.get("usage"), dict) else {}
    if usage:
        print(
            "\n用量："
            f"prompt_tokens={usage.get('prompt_tokens', 0)} "
            f"completion_tokens={usage.get('completion_tokens', 0)} "
            f"model_calls={usage.get('model_calls', 0)} "
            f"elapsed_ms={usage.get('elapsed_ms', 0)}"
        )

    if turn.get("export_path"):
        print(f"\n导出文件：{turn.get('export_path')}")
    errors = [str(item) for item in (turn.get("errors") or []) if str(item).strip()]
    if errors:
        print("\n错误：")
        for item in errors:
            print(f"- {item}")

    interrupt = turn.get("interrupt")
    if turn.get("status") == "awaiting_approval" and isinstance(interrupt, dict):
        payload = interrupt.get("payload") if isinstance(interrupt.get("payload"), dict) else {}
        summary = payload.get("summary") or "（无摘要）"
        print(f"\n审批摘要：{summary}")
        print(
            "下一步：python scripts/run_cli.py --thread-id "
            f"{turn.get('thread_id')} --resume approve|reject|edit [--comment 备注]"
        )


def _run(runtime: AppRuntime, args: argparse.Namespace) -> int:
    """按参数执行一个动作；返回退出码。"""
    actions = [
        bool(args.health),
        bool(args.list_threads),
        bool(args.read_thread),
        bool(args.resume),
        bool(args.question),
    ]
    selected = sum(1 for item in actions if item)
    if selected == 0:
        return _fail("请至少指定一个动作：--question / --resume / --read-thread / --list-threads / --health")
    if selected > 1:
        return _fail("一次只能执行一个动作（问题、审批、读会话、列会话、健康检查互斥）")
    if args.resume and not args.thread_id:
        return _fail("--resume 需要同时提供 --thread-id")
    if args.edited_report_file and not args.resume:
        return _fail("--edited-report-file 只能和 --resume 一起使用")

    if args.health:
        print(json.dumps(runtime.health(), ensure_ascii=False, indent=2))
        return 0

    if args.list_threads:
        threads = runtime.list_threads(user_id=args.user_id or None, limit=args.limit)
        if args.json:
            print(json.dumps(threads, ensure_ascii=False, indent=2))
            return 0
        if not threads:
            print("（没有历史会话）")
            return 0
        for item in threads:
            print(
                f"- {item.get('thread_id')}  "
                f"status={item.get('status') or '?'}  "
                f"updated_at={item.get('updated_at') or '?'}  "
                f"{item.get('question') or ''}"
            )
        return 0

    if args.read_thread:
        try:
            turn = runtime.read_thread(args.read_thread, user_id=args.user_id)
        except ThreadNotFoundError as exc:
            return _fail(f"读取会话失败：{exc}")
        _print_turn(turn, as_json=args.json)
        return 0

    if args.resume:
        edited = ""
        if args.edited_report_file is not None:
            content = _read_edited_report(args.edited_report_file)
            if content is None:
                return 1
            edited = content
        try:
            current = runtime.read_thread(args.thread_id, user_id=args.user_id)
            turn = runtime.resume_turn(
                args.thread_id,
                args.resume,
                interrupt_id=(current.get("interrupt") or {}).get("id") or current.get("approval_interrupt_id"),
                report_id=current.get("report_id"),
                comment=args.comment,
                edited_report=edited,
                user_id=args.user_id,
            )
        except (ValueError, ThreadNotFoundError) as exc:
            return _fail(f"审批失败：{exc}")
        _print_turn(turn, as_json=args.json)
        return 1 if turn.get("status") in {"failed", "budget_exceeded"} else 0

    try:
        if args.stream:
            turn = _run_streaming(runtime, args)
            if turn is None:
                return _fail("流式执行没有产出可用的终止结果（final/error）。")
            if not args.json:
                # --json 时事件流已含完整 turn，不再叠加第二种 JSON 形态
                _print_turn(turn, as_json=False)
            return 1 if turn.get("status") in {"failed", "budget_exceeded"} else 0
        turn = runtime.invoke_turn(
            str(args.question or ""),
            thread_id=args.thread_id,
            user_id=args.user_id,
        )
    except (ValueError, ThreadNotFoundError) as exc:
        return _fail(str(exc))
    _print_turn(turn, as_json=args.json)
    return 1 if turn.get("status") in {"failed", "budget_exceeded"} else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主函数：装配运行时 → 执行动作 → 释放连接。"""
    _configure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        runtime = AppRuntime.create()
    except Exception as exc:
        return _fail(f"初始化运行时失败：{type(exc).__name__}: {exc}")
    try:
        return _run(runtime, args)
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
