"""环境自检脚本：只读检查本机是否具备继续学习的条件（单元 01 安装环节的验收步骤）。

用法（完成单元 01 §5.1–5.2 后，在 ops-assistant 目录执行）：uv run python ../教程/实验/01_diagnose.py
说明：不安装依赖、不联网、不调用模型；密钥只打印掩码；退出码 0 表示关键项通过。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from importlib.metadata import PackageNotFoundError, version as dist_version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = REPO_ROOT / "ops-assistant"


def mask_secret(value: str | None) -> str:
    """把密钥变成可安全贴到聊天窗口的掩码。"""
    if not value:
        return "未设置"
    if len(value) <= 8:
        return "已设置（长度过短，可能是占位符）"
    return f"已设置（{value[:4]}…{value[-2:]}，长度 {len(value)}）"


def read_env_file(path: Path) -> dict[str, str]:
    """不依赖第三方库解析 .env：按 KEY=VALUE 逐行读取，忽略注释与空行。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check_python() -> tuple[bool, str]:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    return ok, f"Python {major}.{minor}.{sys.version_info[2]}（要求 >= 3.11）"


def check_uv() -> tuple[bool, str]:
    found = shutil.which("uv")
    if found is None:
        return False, "PATH 中找不到 uv（见本单元 5.1 安装步骤）"
    return True, f"找到 uv：{found}"


def check_skeleton() -> tuple[bool, str]:
    required = [
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "src" / "ops_assistant" / "config.py",
        PROJECT_ROOT / "src" / "ops_assistant" / "workflow" / "state.py",
    ]
    missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.is_file()]
    if missing:
        return False, "缺少项目文件：" + "、".join(missing)
    return True, "项目骨架文件齐全"


def check_env_file() -> tuple[bool, str]:
    env_path = PROJECT_ROOT / ".env"
    example_path = PROJECT_ROOT / ".env.example"
    if not env_path.is_file():
        hint = "先复制 .env.example 为 .env" if example_path.is_file() else "项目里也没有 .env.example"
        return False, f"未找到 {env_path.name}；{hint}"
    values = read_env_file(env_path)
    model = values.get("OPS_MODEL", "")
    key = values.get("OPS_API_KEY", "")
    summary = f"OPS_MODEL={model or '空'}；OPS_API_KEY={mask_secret(key)}"
    return True, summary


def check_database() -> tuple[bool, str]:
    db_path = PROJECT_ROOT / "data" / "ops.db"
    if not db_path.is_file():
        return False, "尚未生成 data/ops.db（运行 scripts/bootstrap.py 之后才会出现）"
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            months = conn.execute("SELECT COUNT(DISTINCT substr(order_date, 1, 7)) FROM orders").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"数据库存在但读取失败：{exc}"
    return True, f"订单 {count} 笔，覆盖 {months} 个月"


def check_imports() -> tuple[bool, str]:
    problems: list[str] = []
    for module_name in ("langchain", "langchain_core", "langgraph"):
        try:
            __import__(module_name)
        except ImportError:
            problems.append(module_name)
            continue
        # 各包版本属性习惯不一（langchain 有 __version__、pydantic 用 VERSION、
        # langgraph 两者都没有），用标准库 importlib.metadata 按发行版名取版本才可靠。
        try:
            ver = dist_version(module_name.replace("_", "-"))
        except PackageNotFoundError:
            ver = "未知版本"
        print(f"    - {module_name} {ver}")
    if problems:
        return False, "无法导入：" + "、".join(problems) + "（先执行 uv sync）"
    return True, "核心依赖可导入"


def main() -> int:
    print("== 环境自检（只读检查，不联网）==")
    print(f"仓库根目录：{REPO_ROOT}")
    print(f"项目目录：{PROJECT_ROOT}")

    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python 版本", *check_python()))
    checks.append(("uv 工具", *check_uv()))
    checks.append(("项目骨架", *check_skeleton()))
    checks.append((".env 配置", *check_env_file()))
    checks.append(("数据库", *check_database()))
    checks.append(("依赖导入", *check_imports()))

    print("\n== 检查结果 ==")
    for name, ok, detail in checks:
        flag = "通过" if ok else "未通过"
        print(f"[{flag}] {name}：{detail}")

    key_checks = [item for item in checks if item[0] in {"Python 版本", "项目骨架"}]
    failed_key = [name for name, ok, _ in key_checks if not ok]
    if failed_key:
        print(f"\n结论：关键项未通过（{'、'.join(failed_key)}），先补环境再开始单元 01。")
        return 1
    print("\n结论：关键项通过。其余项不通过也不影响开始学习（见 5.4 的无密钥路径）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
