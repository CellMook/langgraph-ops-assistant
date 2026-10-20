"""报告包：固定 8 段 Markdown 渲染与原子导出。

对外只暴露两件事：
``render_analysis_report`` 负责把程序计算结果渲染成报告文本，
``export_report`` 负责把它原子写入 ``settings.export_dir`` 下的
``report-<thread_id>-<report_id>.md``，重复导出返回同一路径。
"""

from ops_assistant.report.export import export_report, exported_report_path, report_filename, sanitize_thread_id
from ops_assistant.report.format import (
    format_cents,
    format_int,
    format_pct,
    format_share,
    markdown_table,
    render_analysis_report,
)

__all__ = [
    "export_report",
    "exported_report_path",
    "report_filename",
    "sanitize_thread_id",
    "format_cents",
    "format_int",
    "format_pct",
    "format_share",
    "markdown_table",
    "render_analysis_report",
]
