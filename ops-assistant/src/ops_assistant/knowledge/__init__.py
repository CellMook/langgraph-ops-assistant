"""知识层公共入口：文档加载、切分、向量化、索引、检索与问答。
外部代码只需从这里导入 KnowledgeBase 与 answer_from_knowledge。
具体实现分别位于 models/loaders/splitter/embeddings/index/retrieve/qa 模块。
"""

from __future__ import annotations

from .embeddings import (
    DEFAULT_EMBEDDING_DIM,
    HashingEmbeddings,
    OpenAICompatEmbeddings,
    create_embeddings,
    redact_secret,
    tokenize,
)
from .index import INDEX_SCHEMA_VERSION, VectorIndex
from .loaders import SUPPORTED_SUFFIXES, load_documents
from .models import Chunk, LoadedDoc, SearchHit, make_snippet
from .qa import (
    MAX_HITS,
    SCORE_THRESHOLD,
    KnowledgeAnswer,
    answer_from_knowledge,
    build_template_answer,
)
from .retrieve import (
    DEFAULT_TOP_K,
    KEYWORD_TITLE_WEIGHT,
    VALID_SEARCH_MODES,
    KnowledgeBase,
)
from .splitter import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    SEPARATORS,
    split_documents,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_TOP_K",
    "HashingEmbeddings",
    "INDEX_SCHEMA_VERSION",
    "KEYWORD_TITLE_WEIGHT",
    "KnowledgeAnswer",
    "KnowledgeBase",
    "LoadedDoc",
    "Chunk",
    "MAX_HITS",
    "OpenAICompatEmbeddings",
    "SCORE_THRESHOLD",
    "SEPARATORS",
    "SUPPORTED_SUFFIXES",
    "SearchHit",
    "VALID_SEARCH_MODES",
    "VectorIndex",
    "answer_from_knowledge",
    "build_template_answer",
    "create_embeddings",
    "load_documents",
    "make_snippet",
    "redact_secret",
    "split_documents",
    "tokenize",
]
