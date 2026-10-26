#!/usr/bin/env python3
"""练习本对照工具：把你在 `教程/我的实验/` 敲的实验脚本，与 `教程/实验/` 的同名参考实现做**行为对照**。

刻意不做逐字 diff——变量名、注释、函数顺序不同都属正常。对照只看三件事：

1. **结构**：顶层函数 / 类 / 常量的**名字**是否齐全（只报「参考有你没有」的名字，不显示代码，
   防止「对答案」退化成「抄答案」）；
2. **运行**：你的脚本能以退出码 0 跑完（在 `ops-assistant/` 目录下运行，超时 180 秒）；
3. **输出锚点**：关键 banner 是否出现在你的输出里（锚点见本文件 ANCHORS 表）。

用法（在 `ops-assistant/` 目录执行）：

    uv run python ../教程/tools/compare.py ../教程/我的实验/04_tool_loop.py
    uv run python ../教程/tools/compare.py --list

退出码：0 = 全部通过；1 = 对照未通过；2 = 用法/路径问题（没找到文件、脚本不是练习对象等）。
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

# 目录定位（本文件在 教程/tools/ 下）：
TOOLS_DIR = Path(__file__).resolve().parent
TUTORIAL_DIR = TOOLS_DIR.parent
REFERENCE_DIR = TUTORIAL_DIR / "实验"          # 答案库：只读，不编辑
PROJECT_DIR = TUTORIAL_DIR.parent / "ops-assistant"  # 子进程 cwd：data/ops.db 等相对路径在这里成立

TIMEOUT_SECONDS = 180

# ---------------------------------------------------------------------------
# 输出锚点：参考实现在正常运行（无参数、退出码 0）时一定会打印的子串。
# 全部锚点都用参考脚本实测过；改参考实现时须同步更新这张表。
# ---------------------------------------------------------------------------
ANCHORS: dict[str, tuple[str, ...]] = {
    "01_diagnose.py": (
        "== 环境自检（只读检查，不联网）==",
        "== 检查结果 ==",
        "结论：",
    ),
    "01_hello_model.py": (
        "== 单元 01 实验 A：第一条模型调用 ==",
        "----- 结束 -----",
    ),
    "01_probe_model.py": (
        "== 单元 01 实验 B：模型能力探测 ==",
        "[跳过]",
    ),
    "02_context_growth.py": (
        "== 单元 02 实验：上下文增长与截断 ==",
    ),
    "03_validate_spec.py": (
        "== 单元 03 实验：结构化输出与业务校验 ==",
    ),
    "04_tool_loop.py": (
        "== 单元 04 实验：手写工具循环 ==",
        "[结论] 工具执行发生在",
    ),
    "05_simple_agent.py": (
        "== 单元 05 实验：create_agent 第一版问答助手 ==",
    ),
    "06_state_graph.py": (
        "== 单元 06 实验：StateGraph 三节点直线图 ==",
    ),
    "07_reducers.py": (
        "== 单元 07 实验：reducer 与消息状态 ==",
    ),
    "08_routing.py": (
        "== 单元 08 实验：条件路由、循环、预算与失败出口 ==",
    ),
    "09_checkpoint.py": (
        "== 单元 09 实验：checkpointer、thread 与恢复 ==",
        "== 第一段：InMemorySaver 的 thread 隔离 ==",
        "== 第二段：SqliteSaver 写入（本轮进程内） ==",
        "→ 不同 thread_id 完全隔离。",
    ),
    "10_retrieval_baseline.py": (
        "== 第一段：加载 ==",
    ),
    "11_vector_citation.py": (
        "== 单元 11 实验",
        "== 索引统计 ==",
        "== 资料不足 ==",
    ),
    "12_sql_basics.py": (
        "[1] 数据库里真实存在的对象：",
        "[2] 四张表的行数：",
        "[3] 同一个月的两种写法",
    ),
    "13_metrics_check.py": (
        "[1] 手写 SQL 独立计算",
        "[2] 与查询层逐项比对",
        "[5] 单位换算",
    ),
    "15_memory_store.py": (
        "== 单元 15 实验：多轮记忆、Store 与 Runtime Context ==",
    ),
    "16_interrupt_resume.py": (
        "== 单元 16 实验：interrupt 与恢复 ==",
    ),
    "17_idempotent_restart.py": (
        "状态：",
        "路径相同： True",
        "重启后状态：",
    ),
    "18_parallel_subgraph_budget.py": (
        "并行分支写入的 notes：",
        "子图执行后 value =",
        "预算受限时的状态：",
    ),
    "22_streaming.py": (
        "== 1) stream_mode='updates'：节点级进度反馈 ==",
        "== 4) 结论对照 ==",
    ),
}

# 不是「照第 5 节敲一遍」的练习对象：项目级验收/契约脚本，直接运行参考实现即可。
NOT_EXERCISES: dict[str, str] = {
    "14_workflow.py": "单元 14 是项目级整体验收演示，直接运行参考实现即可",
    "23_agent_budget.py": "单元 23 的预算脚本是仓库验收脚本，直接运行参考实现即可",
    "23_framework_contracts.py": "单元 23 的契约脚本是仓库验收脚本，直接运行参考实现即可",
}


def top_level_names(path: Path) -> set[str]:
    """收集一个 .py 文件顶层的函数 / 类 / 常量名（不读内容，只报名字）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def run_learner(path: Path) -> tuple[int, str, str, str | None]:
    """在 ops-assistant/ 目录下用当前解释器运行学习者脚本。

    返回 (returncode, stdout, stderr, timeout_flag)。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 管道输出强制 UTF-8，避免中文锚点被本地编码打散
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr, None
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return -1, out, err, f"超过 {TIMEOUT_SECONDS} 秒未结束"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="练习本对照：教程/我的实验/ 的脚本 ↔ 教程/实验/ 的同名参考实现（行为对照，不逐字比对）",
    )
    parser.add_argument("learner", nargs="?", help="你写的脚本路径，例如 ../教程/我的实验/04_tool_loop.py")
    parser.add_argument("--list", action="store_true", help="列出支持对照的脚本清单")
    args = parser.parse_args()

    if args.list:
        print("支持对照的练习脚本（教程/实验/ 下的同名参考实现）：")
        for name in sorted(ANCHORS):
            print(f"  {name}   （{len(ANCHORS[name])} 个输出锚点）")
        print("\n不是练习对象、直接运行参考实现即可：")
        for name, note in sorted(NOT_EXERCISES.items()):
            print(f"  {name}   {note}")
        return 0

    if not args.learner:
        parser.print_help()
        return 2

    learner = Path(args.learner).resolve()
    if not learner.is_file():
        print(f"找不到你的脚本：{args.learner}")
        print("先确认路径；首次使用请在仓库根目录执行 `mkdir 教程/我的实验`。")
        return 2

    name = learner.name
    if name in NOT_EXERCISES:
        print(f"{name} 不是练习对象：{NOT_EXERCISES[name]}。")
        return 2
    if name not in ANCHORS:
        print(f"没有为 {name} 配置对照锚点。")
        print("请核对文件名与参考实现同名（例如单元 04 是 04_tool_loop.py）；用 --list 查看支持的清单。")
        return 2

    reference = REFERENCE_DIR / name
    if not reference.is_file():
        print(f"答案库里找不到参考实现：{reference}")
        return 2
    if learner.resolve() == reference.resolve():
        print("这是参考实现本身。请把你的版本放在 教程/我的实验/ 下再对照。")
        return 2

    print(f"对照：{learner}")
    print(f"参考：{reference}")
    print()

    failed: list[str] = []

    # ---- [1] 结构：顶层名字 ----
    print("[1] 结构（顶层函数 / 类 / 常量名）")
    try:
        learner_names = top_level_names(learner)
    except SyntaxError as exc:
        print(f"    ✗ 你的脚本有语法错误：第 {exc.lineno} 行 {exc.msg}")
        print("      先修到能 `python 文件名` 不报 SyntaxError，再回来对照。")
        return 1
    reference_names = top_level_names(reference)

    missing = sorted(reference_names - learner_names)
    extra = sorted(learner_names - reference_names)
    if missing:
        failed.append("结构")
        print(f"    ✗ 参考有你没有：{', '.join(missing)}")
        print("      —— 回到本单元第 5 节，按名字找到对应小节补上（对照只报名字、不看代码，别把答案抄回去）。")
    else:
        print(f"    ✓ 名字齐全（参考 {len(reference_names)} 个，你有 {len(learner_names)} 个）")
    if extra:
        print(f"    · 你多出来的名字（仅提示，不影响通过）：{', '.join(extra)}")
    print()

    # ---- [2] 运行 ----
    print(f"[2] 运行（cwd = {PROJECT_DIR.name}/，超时 {TIMEOUT_SECONDS} 秒）")
    code, out, err, timeout_flag = run_learner(learner)
    if timeout_flag is not None:
        failed.append("运行")
        print(f"    ✗ {timeout_flag}")
        print("      检查是否有 input() 等输入等待、死循环或过长的 sleep。")
        return 1
    if code != 0:
        failed.append("运行")
        print(f"    ✗ 退出码 {code}（应为 0）。报错最后几行：")
        for line in (err or out).strip().splitlines()[-8:]:
            print(f"      {line}")
        if "ops.db" in (err or "") or "bootstrap" in (err or ""):
            print("      提示：需要先建库——在 ops-assistant/ 目录执行 `uv run python scripts/bootstrap.py`。")
        if "ModuleNotFoundError" in (err or "") or "ImportError" in (err or ""):
            print("      提示：请用 `uv run python`（项目虚拟环境）运行，而不是系统 python。")
        return 1
    print("    ✓ 退出码 0")
    print()

    # ---- [3] 输出锚点 ----
    print("[3] 输出锚点")
    combined = out + "\n" + (err or "")
    missing_anchors = [a for a in ANCHORS[name] if a not in combined]
    for anchor in ANCHORS[name]:
        mark = "✓" if anchor not in missing_anchors else "✗"
        print(f"    {mark} 「{anchor}」")
    if missing_anchors:
        failed.append("输出锚点")
        print("      —— 没出现的锚点说明对应实验段没跑出预期结果，回到第 5 节核对该段的行为。")
    print()

    if failed:
        print(f"结论：{ '、'.join(failed) } 未通过 ✗ —— 按上面的提示改完再对照一次。")
        return 1
    print("结论：结构、运行、输出锚点全部通过 ✅ —— 行为一致，本单元收工。")
    print("（对照看的是行为不是字：变量名、注释、函数顺序不同都算你自己的实现。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
