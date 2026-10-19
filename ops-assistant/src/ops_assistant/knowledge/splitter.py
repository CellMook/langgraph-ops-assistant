"""知识块切分：使用 langchain_text_splitters 的递归字符切分器。
优先按段落、中文句号/逗号等分隔符切分，默认 chunk_size=400、overlap=60。
切分结果带稳定 chunk_id 与起始偏移，保证引用可定位、多次构建可复现。
"""

from __future__ import annotations

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .models import Chunk, LoadedDoc

# 规格约定：每块 400 字符、相邻块重叠 60 字符。
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 60

# 中文优先的分隔符顺序：段落 → 换行 → 中文句末标点 → 逗号 → 空格。
SEPARATORS: list[str] = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]


def split_documents(
    docs: list[LoadedDoc],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """把原始文档切分为可检索的知识块。

    参数：
        docs: load_documents 返回的文档列表。
        chunk_size: 单块最大字符数，必须为正整数。
        chunk_overlap: 相邻块重叠字符数，必须满足 0 <= overlap < chunk_size。

    返回：
        list[Chunk]：按文档顺序、块顺序排列的知识块；
        chunk_id 形如 "<doc_id>#<序号>"，序号从 0 开始。

    异常：
        ValueError: 切分参数非法。
    """
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    if not isinstance(chunk_overlap, int) or isinstance(chunk_overlap, bool):
        raise ValueError("chunk_overlap 必须为非负整数")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须满足 0 <= chunk_overlap < chunk_size")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=list(SEPARATORS),
        keep_separator=True,
        add_start_index=True,
        length_function=len,
    )

    chunks: list[Chunk] = []
    for doc in docs:
        if not doc.text.strip():
            continue
        source = Document(
            page_content=doc.text,
            metadata={
                "doc_id": doc.doc_id,
                "title": doc.title,
                "path": doc.path,
            },
        )
        pieces = splitter.split_documents([source])
        for index, piece in enumerate(pieces):
            text = piece.page_content
            if not text.strip():
                continue
            start = int(piece.metadata.get("start_index", 0))
            metadata = {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "path": doc.path,
                "start": str(start),
            }
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}#{index}",
                    doc_id=doc.doc_id,
                    title=doc.title,
                    text=text,
                    start=start,
                    metadata=metadata,
                )
            )
    return chunks
