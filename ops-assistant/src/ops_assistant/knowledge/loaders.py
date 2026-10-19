"""知识文档加载：从知识目录读取 Markdown 与纯文本文件。
只读取 .md/.txt、使用 UTF-8、按文件名排序，保证构建结果可复现。
本模块不修改文件内容，也不递归子目录（知识目录约定为扁平结构）。
"""

from __future__ import annotations

from pathlib import Path

from .models import LoadedDoc

# 允许的文件扩展名（小写比较）。
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".txt"})

# 标题兜底长度，避免把超长首行整段塞进 title。
_FALLBACK_TITLE_MAX_CHARS = 80


def _extract_title(text: str, fallback: str) -> str:
    """提取文档标题：优先第一个 Markdown 标题，其次首个非空行。

    参数：
        text: 文档全文。
        fallback: 没有可用标题时使用的兜底值（通常是文件名）。

    返回：
        str：非空标题文本。
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_FALLBACK_TITLE_MAX_CHARS]
    return fallback


def load_documents(docs_dir: Path) -> list[LoadedDoc]:
    """加载目录下的全部 Markdown/文本文档。

    参数：
        docs_dir: 知识文档目录。允许不存在（返回空列表，由上层决定是否告警）。

    返回：
        list[LoadedDoc]：按文件名升序排列的文档列表。

    异常：
        NotADirectoryError: 路径存在但不是目录。
        OSError: 读取文件失败（例如权限不足）。
        UnicodeDecodeError: 文件不是合法 UTF-8。
    """
    directory = Path(docs_dir)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise NotADirectoryError(f"知识目录不是文件夹：{directory}")

    files = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ),
        key=lambda path: path.name,
    )

    documents: list[LoadedDoc] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        doc_id = path.stem
        documents.append(
            LoadedDoc(
                doc_id=doc_id,
                title=_extract_title(text, doc_id),
                path=str(path),
                text=text,
            )
        )
    return documents
