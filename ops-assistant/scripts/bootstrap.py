"""初始化脚本：一次生成演示数据库、知识索引与标准答案。
执行顺序固定为 ensure_dirs → 生成数据 → 构建知识索引 → 计算标准答案，任一步失败都给中文提示。
本脚本只做编排与摘要输出，不实现数据生成、检索、标准答案算法，也不调用任何模型。
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

# 允许在未安装（uv sync 之前）时直接从源码运行：把 src 加入模块搜索路径。
# 仅当 ops_assistant 确实不可导入时才插入，避免影响已安装环境。
_PROJECT_DIR = Path(__file__).resolve().parents[1]
_SRC_DIR = _PROJECT_DIR / "src"
if importlib.util.find_spec("ops_assistant") is None and _SRC_DIR.is_dir():
    sys.path.insert(0, str(_SRC_DIR))

if TYPE_CHECKING:  # 仅用于类型标注；运行时按需延迟导入，便于给出中文错误提示
    from ops_assistant.config import Settings
    from ops_assistant.knowledge.retrieve import KnowledgeBase

LOGGER = logging.getLogger("ops_assistant.bootstrap")

# 标准答案文件名（与工程规格 3.3 一致）。
GROUND_TRUTH_FILENAME = "ground_truth.json"

# 摘要中展示行数的表（顺序固定，便于人工核对）。
SUMMARY_TABLES: tuple[str, ...] = ("regions", "products", "orders", "refunds")

# 知识文档允许的扩展名（与知识层加载器一致）。
KNOWLEDGE_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    参数：
        argv: 参数列表；None 时使用 sys.argv[1:]。

    返回：
        argparse.Namespace：包含 skip_ground_truth 字段。
    """
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="初始化 ops-assistant 演示项目：生成 SQLite 数据库、知识索引与标准答案。",
    )
    parser.add_argument(
        "--skip-ground-truth",
        action="store_true",
        help="跳过标准答案计算（scripts/compute_ground_truth.py），用于只想快速建库与索引的场景。",
    )
    return parser.parse_args(argv)


def _load_settings() -> "Settings":
    """延迟导入并读取设置。

    返回：
        Settings：以 ops-assistant 项目根为基准的设置对象。

    异常：
        ImportError: 项目包或其依赖不可导入。
        ValueError: OPS_* 数值变量非法。
    """
    from ops_assistant.config import load_settings

    return load_settings()


def _ensure_dirs(settings: "Settings") -> None:
    """创建运行目录（幂等）。

    参数：
        settings: 设置对象。

    异常：
        OSError: 目录创建失败。
    """
    from ops_assistant.config import ensure_dirs

    ensure_dirs(settings)


def _setup_logging(settings: "Settings") -> Path | None:
    """配置控制台与文件日志。

    参数：
        settings: 设置对象，提供 log_level 与 log_dir。

    返回：
        Path | None：日志文件路径。
    """
    from ops_assistant.logging_utils import setup_logging

    return setup_logging(settings.log_level, log_dir=Path(settings.log_dir))


def _generate_database(settings: "Settings") -> dict[str, Any]:
    """调用数据层生成演示数据库。

    参数：
        settings: 设置对象，使用其中的 db_path。

    返回：
        dict[str, Any]：生成器给出的统计摘要（结构由数据层定义，本脚本只做展示）。

    异常：
        ImportError: ops_assistant.data.generate 不存在。
        Exception: 建库失败（sqlite3.Error、OSError 等），由 main 统一转成中文提示。
    """
    from ops_assistant.data.generate import generate_database

    result = generate_database(db_path=Path(settings.db_path))
    if isinstance(result, dict):
        return dict(result)
    return {"raw": result}


def _collect_db_counts(settings: "Settings") -> dict[str, int]:
    """统计数据库中关键表的行数。

    参数：
        settings: 设置对象，使用其中的 db_path。

    返回：
        dict[str, int]：表名到行数；数据库缺失或表不存在时跳过对应项。
    """
    counts: dict[str, int] = {}
    db_path = Path(settings.db_path)
    if not db_path.is_file():
        return counts
    connection = sqlite3.connect(db_path)
    try:
        for table in SUMMARY_TABLES:
            # 表名来自模块内常量白名单，不来自用户输入，可安全拼接。
            try:
                row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            except sqlite3.Error:
                continue
            counts[table] = int(row[0]) if row is not None else 0
    finally:
        connection.close()
    return counts


def _build_knowledge(settings: "Settings") -> "KnowledgeBase":
    """构建知识索引并返回知识库对象。

    参数：
        settings: 设置对象，提供 knowledge_dir 与 index_path。

    返回：
        KnowledgeBase：已构建（并已保存索引）的知识库。

    异常：
        ImportError: 知识层模块不存在。
        ValueError: 知识目录为空或文档不可解析。
        OSError: 文档读取或索引写入失败。
    """
    from ops_assistant.knowledge.retrieve import KnowledgeBase

    return KnowledgeBase.build(settings)


def _knowledge_stats(knowledge_base: "KnowledgeBase") -> dict[str, Any]:
    """读取知识库统计信息，失败时返回空字典（摘要失败不应中断初始化）。

    参数：
        knowledge_base: 已构建的知识库。

    返回：
        dict[str, Any]：知识库自报的统计信息。
    """
    try:
        stats = knowledge_base.stats()
    except Exception as exc:  # noqa: BLE001 - 统计只是摘要信息，不应让初始化失败
        LOGGER.warning("读取知识库统计信息失败：%s", exc)
        return {}
    return dict(stats) if isinstance(stats, dict) else {}


def _count_knowledge_docs(knowledge_dir: Path) -> int:
    """统计知识目录中会被加载的文档数量。

    参数：
        knowledge_dir: 知识文档目录。

    返回：
        int：.md/.txt 文件个数；目录不存在时为 0。
    """
    directory = Path(knowledge_dir)
    if not directory.is_dir():
        return 0
    return sum(
        1
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in KNOWLEDGE_SUFFIXES
    )


def _run_ground_truth(settings: "Settings") -> Path | None:
    """用当前 Python 解释器执行 scripts/compute_ground_truth.py。

    参数：
        settings: 设置对象，用于定位输出文件所在的数据目录。

    返回：
        Path | None：生成的标准答案文件；脚本成功但未产出文件时返回 None。

    异常：
        FileNotFoundError: 脚本不存在。
        RuntimeError: 脚本以非零退出码结束。
    """
    script = _PROJECT_DIR / "scripts" / "compute_ground_truth.py"
    if not script.is_file():
        raise FileNotFoundError(f"未找到标准答案脚本：{script}")

    command = [sys.executable, str(script)]
    LOGGER.info("执行标准答案脚本：%s", " ".join(command))
    completed = subprocess.run(command, cwd=str(_PROJECT_DIR), check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"标准答案脚本退出码为 {completed.returncode}，脚本路径：{script}")

    output_path = Path(settings.data_dir) / GROUND_TRUTH_FILENAME
    if not output_path.is_file():
        LOGGER.warning("标准答案脚本执行成功，但未找到输出文件：%s", output_path)
        return None
    return output_path


def _display_path(path: Path, base_dir: Path) -> str:
    """把绝对路径尽量显示为相对项目根的 POSIX 风格路径。

    参数：
        path: 目标路径。
        base_dir: 项目根目录。

    返回：
        str：相对路径（无法相对化时回退为绝对路径）。
    """
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(Path(base_dir).resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def _format_size(size_bytes: int) -> str:
    """把字节数格式化为人类可读大小。

    参数：
        size_bytes: 字节数。

    返回：
        str：例如 "512 B"、"12.3 KB"、"1.4 MB"。
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _count_text_lines(path: Path) -> int:
    """统计 UTF-8 文本文件的行数。

    参数：
        path: 文件路径。

    返回：
        int：行数；文件缺失或编码非法时返回 0。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return len(text.splitlines())


def _format_mapping(data: Mapping[str, Any], *, limit: int = 4) -> str:
    """把字典压缩成一行 "k=v" 摘要（只保留标量，最多 limit 项）。

    参数：
        data: 待展示的字典。
        limit: 最多展示的键数量。

    返回：
        str：形如 "orders=336；refunds=27"；没有可展示项时返回 "无"。
    """
    parts: list[str] = []
    for key, value in data.items():
        if len(parts) >= limit:
            break
        if isinstance(value, bool):
            parts.append(f"{key}={value}")
        elif isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif isinstance(value, str):
            text = value.replace("\n", " ")
            if len(text) > 40:
                text = text[:40] + "…"
            parts.append(f"{key}={text}")
    return "；".join(parts) if parts else "无"


def _print_summary(
    settings: "Settings",
    *,
    generator_summary: Mapping[str, Any],
    db_counts: Mapping[str, int],
    knowledge_stats: Mapping[str, Any],
    knowledge_doc_count: int,
    ground_truth_path: Path | None,
    ground_truth_skipped: bool,
    log_path: Path | None,
) -> None:
    """打印初始化结果摘要与下一步命令。

    参数：
        settings: 设置对象。
        generator_summary: 数据生成器返回的摘要。
        db_counts: 关键表行数。
        knowledge_stats: 知识库统计信息。
        knowledge_doc_count: 知识文档数量。
        ground_truth_path: 标准答案文件路径；未生成时为 None。
        ground_truth_skipped: 是否因 --skip-ground-truth 跳过。
        log_path: 日志文件路径。
    """
    base_dir = Path(settings.base_dir)
    separator = "=" * 64
    print(separator)
    print("初始化完成：ops-assistant 演示项目")
    print(separator)

    knowledge_dir = Path(settings.knowledge_dir)
    knowledge_lines = sum(
        _count_text_lines(path)
        for path in sorted(knowledge_dir.glob("*"))
        if path.is_file() and path.suffix.lower() in KNOWLEDGE_SUFFIXES
    )
    print(
        f"- 知识文档：{_display_path(knowledge_dir, base_dir)}"
        f"（{knowledge_doc_count} 篇，共 {knowledge_lines} 行）"
    )

    db_path = Path(settings.db_path)
    if db_path.is_file():
        counts_text = _format_mapping(db_counts, limit=len(SUMMARY_TABLES)) or "无"
        print(
            f"- 数据库：{_display_path(db_path, base_dir)}"
            f"（{_format_size(db_path.stat().st_size)}；{counts_text}）"
        )
    else:
        print(f"- 数据库：未生成（预期路径 {_display_path(db_path, base_dir)}）")

    if generator_summary:
        print(f"- 生成器摘要：{_format_mapping(generator_summary)}")

    index_path = Path(settings.index_path)
    index_stats = _format_mapping(knowledge_stats, limit=6)
    if index_path.is_file():
        print(
            f"- 知识索引：{_display_path(index_path, base_dir)}"
            f"（{_format_size(index_path.stat().st_size)}；{index_stats}）"
        )
    else:
        print(f"- 知识索引：未生成（预期路径 {_display_path(index_path, base_dir)}）")

    if ground_truth_skipped:
        print("- 标准答案：已按 --skip-ground-truth 跳过")
    elif ground_truth_path is not None and Path(ground_truth_path).is_file():
        ground_truth = Path(ground_truth_path)
        print(
            f"- 标准答案：{_display_path(ground_truth, base_dir)}"
            f"（{_format_size(ground_truth.stat().st_size)}；"
            f"{_count_text_lines(ground_truth)} 行）"
        )
    else:
        print("- 标准答案：未生成（可重新运行本脚本，省略 --skip-ground-truth）")

    if log_path is not None:
        print(f"- 日志文件：{_display_path(log_path, base_dir)}")

    print(separator)
    print("下一步（在 ops-assistant 目录执行）：")
    print("  uv run pytest                                             # 运行测试")
    print("  uv run uvicorn ops_assistant.api.main:app --reload        # 启动 API")
    print("  uv run streamlit run src/ops_assistant/ui/streamlit_app.py # 启动界面")
    print("说明：本脚本不调用任何模型；未配置 OPS_API_KEY 时项目以纯规则模式运行。")


def _fail(message: str, hint: str | None = None) -> int:
    """打印中文错误与建议，返回失败退出码。

    参数：
        message: 错误描述。
        hint: 可操作的修复建议。

    返回：
        int：固定为 1。
    """
    print(f"错误：{message}", file=sys.stderr)
    if hint:
        print(f"建议：{hint}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """命令行入口：按固定顺序完成初始化。

    参数：
        argv: 命令行参数；None 时使用 sys.argv[1:]。

    返回：
        int：0 表示全部步骤成功；1 表示有步骤失败（已打印中文原因）。
    """
    args = _parse_args(argv)
    print("开始初始化 ops-assistant 演示项目……")

    # 1) 读取配置（含 .env，不覆盖已存在环境变量）。
    try:
        settings = _load_settings()
    except ImportError as exc:
        return _fail(
            f"无法导入 ops_assistant 包：{exc}",
            "请先在 ops-assistant 目录执行 `uv sync`，再用 `uv run python scripts/bootstrap.py` 运行。",
        )
    except ValueError as exc:
        return _fail(
            f"配置读取失败：{exc}",
            "请检查 .env 中的 OPS_* 数值变量（如 OPS_MAX_STEPS、OPS_EMBEDDING_DIM）是否为合法整数或数字。",
        )

    # 2) 创建目录，再初始化日志（日志文件需要 log_dir 已存在）。
    try:
        _ensure_dirs(settings)
    except OSError as exc:
        return _fail(
            f"创建目录失败：{exc}",
            f"请确认项目目录 {settings.base_dir} 可写，或手动创建 data/、data/knowledge/、logs/ 目录。",
        )

    try:
        log_path = _setup_logging(settings)
    except ImportError as exc:
        return _fail(
            f"无法导入日志工具：{exc}",
            "请确认 src/ops_assistant/logging_utils.py 存在，并已执行 `uv sync`。",
        )
    except OSError as exc:
        return _fail(
            f"初始化日志文件失败：{exc}",
            f"请确认日志目录 {settings.log_dir} 可写，或检查磁盘空间。",
        )

    LOGGER.info("项目根：%s", settings.base_dir)
    print(f"项目根：{settings.base_dir}")
    print(f"数据目录：{_display_path(Path(settings.data_dir), settings.base_dir)}")

    # 3) 生成演示数据库（固定种子，可重复生成）。
    try:
        generator_summary = _generate_database(settings)
    except ImportError as exc:
        return _fail(
            f"无法导入数据生成模块：{exc}",
            "请确认 ops-assistant/src/ops_assistant/data/generate.py 存在；"
            "该模块缺失时无法建库，请先补齐数据层代码。",
        )
    except Exception as exc:  # noqa: BLE001 - 命令行编排需要把任意失败转成中文提示
        return _fail(
            f"生成演示数据库失败（{type(exc).__name__}）：{exc}",
            f"可先删除旧库 {_display_path(Path(settings.db_path), settings.base_dir)} 后重试；"
            "若仍失败，请保留完整报错并检查 ops_assistant/data/generate.py 与 schema.sql。",
        )
    LOGGER.info("数据库已生成：%s", settings.db_path)

    # 4) 构建知识索引（读取 data/knowledge/ 下的文档并保存索引文件）。
    try:
        knowledge_base = _build_knowledge(settings)
    except ImportError as exc:
        return _fail(
            f"无法导入知识检索模块：{exc}",
            "请确认 ops-assistant/src/ops_assistant/knowledge/ 完整，并已执行 `uv sync`。",
        )
    except ValueError as exc:
        return _fail(
            f"构建知识索引失败：{exc}",
            "请确认 data/knowledge/ 下至少有 1 篇 .md/.txt 文档，且文件为 UTF-8 编码。",
        )
    except Exception as exc:  # noqa: BLE001 - 同上，统一中文提示
        return _fail(
            f"构建知识索引失败（{type(exc).__name__}）：{exc}",
            f"可删除旧索引 {_display_path(Path(settings.index_path), settings.base_dir)} 后重试；"
            "若仍失败，请保留完整报错并检查 knowledge/retrieve.py。",
        )

    # 5) 计算标准答案（独立子进程，避免与查询层互相依赖）。
    ground_truth_path: Path | None = None
    ground_truth_skipped = bool(args.skip_ground_truth)
    if ground_truth_skipped:
        print("已按 --skip-ground-truth 跳过标准答案计算。")
    else:
        try:
            ground_truth_path = _run_ground_truth(settings)
        except FileNotFoundError as exc:
            return _fail(
                f"标准答案脚本缺失：{exc}",
                "请确认 ops-assistant/scripts/compute_ground_truth.py 存在，"
                "或使用 --skip-ground-truth 先跳过该步骤。",
            )
        except Exception as exc:  # noqa: BLE001 - 子进程失败需要中文提示
            return _fail(
                f"计算标准答案失败（{type(exc).__name__}）：{exc}",
                "请单独运行 `uv run python scripts/compute_ground_truth.py` 查看完整报错；"
                "或使用 --skip-ground-truth 先跳过该步骤。",
            )

    # 6) 打印摘要。
    _print_summary(
        settings,
        generator_summary=generator_summary,
        db_counts=_collect_db_counts(settings),
        knowledge_stats=_knowledge_stats(knowledge_base),
        knowledge_doc_count=_count_knowledge_docs(Path(settings.knowledge_dir)),
        ground_truth_path=ground_truth_path,
        ground_truth_skipped=ground_truth_skipped,
        log_path=log_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
