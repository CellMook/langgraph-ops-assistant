"""知识层数据模型：文档、切块与检索命中。
本模块只定义不可变数据结构与引用格式，不包含文件 I/O、切分或检索逻辑。
所有字段使用英文标识符，docstring 使用中文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 引用片段的最大字符数，避免把整块文本塞进报告来源区。
SNIPPET_MAX_CHARS = 160


@dataclass(frozen=True)
class LoadedDoc:
    """从知识目录读入的一篇原始文档。

    属性：
        doc_id: 文件名（不含扩展名），例如 "01_指标口径"。
        title: 文档一级标题；没有标题时回退为文件名。
        path: 文件的字符串路径，便于来源展示。
        text: 文件全文（UTF-8 解码后的原始文本）。
    """

    doc_id: str
    title: str
    path: str
    text: str


@dataclass(frozen=True)
class Chunk:
    """切分后的知识块，是检索与引用的最小单位。

    属性：
        chunk_id: 形如 "<doc_id>#<序号>" 的稳定标识。
        doc_id: 所属文档 id。
        title: 所属文档标题。
        text: 知识块正文。
        start: 该块在原文中的起始字符偏移（0 起）。
        metadata: 附加元信息（path/start 等），值统一为字符串。
    """

    chunk_id: str
    doc_id: str
    title: str
    text: str
    start: int
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    """一次检索命中的结果。

    属性：
        chunk: 命中的知识块。
        score: 检索分数（关键词基线分数或余弦相似度，均在 0~1 量级）。
        method: 命中方式，取值 "keyword"、"vector" 或 "hybrid"。
    """

    chunk: Chunk
    score: float
    method: str

    def citation(self) -> dict[str, Any]:
        """返回可直接 JSON 序列化的引用信息。

        返回：
            dict：包含 doc_id、title、chunk_id、snippet 四个键。
        """
        return {
            "doc_id": self.chunk.doc_id,
            "title": self.chunk.title,
            "chunk_id": self.chunk.chunk_id,
            "snippet": make_snippet(self.chunk.text),
        }


def make_snippet(text: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """把知识块正文压缩成单行摘要，用于报告与工具返回。

    参数：
        text: 原始文本。
        max_chars: 摘要最大字符数，必须为正整数。

    返回：
        str：空白归一化后的单行文本；超长时截断并以省略号结尾。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正整数")
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"
