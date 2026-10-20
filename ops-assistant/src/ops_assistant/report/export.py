"""报告导出：把 Markdown 报告原子写入导出目录。

写出流程是"临时文件 → fsync → os.replace"：
同一次导出要么完整成功，要么完全不生效，不会留下半截报告；
同一 thread_id + report_id 复用同内容产物；不同版本分开保存，内容冲突拒绝。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

# 只保留可安全用于文件名的字符，避免路径穿越与跨平台问题
_SAFE_CHARS = re.compile(r"[^0-9A-Za-z_.\-]")
MAX_NAME_LEN = 80


def sanitize_thread_id(thread_id: str) -> str:
    """把 thread_id 变成可安全用做文件名片段的字符串。

    参数：
        thread_id: 任意字符串形式的会话 id。

    返回：
        str：只含字母、数字、下划线、点、短横线的片段；空值回退为 "thread"。

    说明：
        这里不处理 ".." 之外的花样，因为 os.replace 的目标是 export 目录里的
        固定文件名，且目录由配置决定，用户无法通过 thread_id 指定目录。
    """
    raw = str(thread_id or "").strip()
    if not raw:
        return "thread"
    safe = _SAFE_CHARS.sub("_", raw)
    safe = safe.strip("._-") or "thread"
    # 清洗和截断可能把不同原始 ID 映射到相同文件名，附原始值摘要消除碰撞。
    if safe != raw or len(safe) > MAX_NAME_LEN:
        return safe[:48] + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return safe


def report_filename(thread_id: str, report_id: str | None = None) -> str:
    """返回版本化报告文件名；report_id 省略仅作旧调用兼容。"""
    suffix = "-" + sanitize_thread_id(report_id) if report_id else ""
    return f"report-{sanitize_thread_id(thread_id)}{suffix}.md"


def export_report(
    report_markdown: str,
    *,
    thread_id: str,
    export_dir: str | Path,
    report_id: str | None = None,
) -> Path:
    """原子写出一份 Markdown 报告，返回最终文件路径。

    参数：
        report_markdown: 报告全文（最终文本，原样写盘，不追加/删改内容）。
        thread_id: 会话 id，与 report_id 一起决定版本化文件名。
        export_dir: 导出目录；不存在会自动创建。
        report_id: 报告版本，主工作流每轮生成，重试保持不变。

    返回：
        Path：报告文件的绝对路径；重复调用返回同一路径。

    异常：
        ValueError：报告内容为空（拒绝生成空文件）。
        OSError：目录不可创建/不可写、磁盘错误等；临时文件会被清理后重抛。
    """
    text = report_markdown if isinstance(report_markdown, str) else str(report_markdown or "")
    if not text.strip():
        raise ValueError("报告内容为空，拒绝导出空文件")

    directory = Path(export_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / report_filename(thread_id, report_id)
    if target.exists():
        if target.read_text(encoding="utf-8") != text:
            raise ValueError("报告编号已存在且内容不同，禁止覆盖已导出版本。")
        return target.resolve()

    fd = -1
    temp_name = ""
    try:
        # 临时文件与目标同目录，保证 os.replace 是同盘原子替换
        fd, temp_name = tempfile.mkstemp(prefix=".report-", suffix=".tmp", dir=str(directory))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1  # 句柄所有权已交给文件对象，避免重复关闭
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = ""  # 已被 replace 消费，无需再清理
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise
    return target.resolve()


def exported_report_path(thread_id: str, export_dir: str | Path) -> Path:
    """在不写文件的前提下，计算某个会话报告的目标路径（供查询/校验用）。"""
    return (Path(export_dir).expanduser() / report_filename(thread_id)).resolve()


def report_digest(content: str) -> str:
    """审批绑定用户所见的完整草稿内容。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
