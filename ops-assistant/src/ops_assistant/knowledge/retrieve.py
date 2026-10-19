"""知识库门面：构建/加载索引，并提供关键词、向量与混合检索。
关键词基线 = 中文 2-gram + 英文/数字 token，标题命中加权 1.5，完全无命中返回空。
索引保存 chunks/vectors/meta 且不含密钥；向量构建失败时自动回退确定性哈希并记录原因。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from ops_assistant.config import Settings

from .embeddings import (
    DEFAULT_EMBEDDING_DIM,
    HASHING_NOTE,
    HashingEmbeddings,
    create_embeddings,
    redact_secret,
    tokenize,
)
from .index import VectorIndex
from .loaders import load_documents
from .models import Chunk, SearchHit
from .splitter import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    split_documents,
)

logger = logging.getLogger(__name__)

# 标题 token 命中相对于正文命中的权重（规格 5.4）。
KEYWORD_TITLE_WEIGHT = 1.5

# 默认返回条数。
DEFAULT_TOP_K = 4

# 支持的检索模式。
VALID_SEARCH_MODES: tuple[str, ...] = ("vector", "keyword", "hybrid")


class KnowledgeBase:
    """本地知识库：持有索引与向量实现，对外提供检索接口。

    使用方式（推荐）：
        kb = KnowledgeBase.load_or_build(settings)
        hits = kb.search("退款是否计入当月销售额", k=4, mode="hybrid")
    """

    def __init__(
        self,
        index: VectorIndex,
        embeddings: Any,
        *,
        index_path: Path | None = None,
    ) -> None:
        """初始化知识库并预计算关键词检索所需的 token 集合。

        参数：
            index: 已构建或已加载的向量索引。
            embeddings: 与索引匹配的向量实现。
            index_path: 索引文件路径，仅用于展示。
        """
        self.index = index
        self.embeddings = embeddings
        self.index_path = Path(index_path) if index_path is not None else None
        self._body_tokens: list[set[str]] = [
            set(tokenize(chunk.text)) for chunk in index.chunks
        ]
        self._title_tokens: list[set[str]] = [
            set(tokenize(chunk.title)) for chunk in index.chunks
        ]

    @classmethod
    def build(cls, settings: Settings) -> KnowledgeBase:
        """从知识目录重建索引并保存到 settings.index_path。

        流程：读取文档 -> 切分（400/60）-> 向量化 -> 构建索引 -> 原子写盘。
        如果远端 embedding 在向量化阶段失败，会回退到 HashingEmbeddings 重建并记录原因。

        参数：
            settings: 项目配置，需提供 knowledge_dir 与 index_path。

        返回：
            KnowledgeBase：新构建的知识库实例。
        """
        docs = load_documents(Path(settings.knowledge_dir))
        if not docs:
            logger.warning(
                "知识目录为空或不存在：%s；将生成空索引，检索会返回无结果",
                settings.knowledge_dir,
            )
        chunks = split_documents(
            docs,
            chunk_size=DEFAULT_CHUNK_SIZE,
            chunk_overlap=DEFAULT_CHUNK_OVERLAP,
        )
        embeddings, embedding_meta = create_embeddings(settings)

        meta: dict[str, Any] = {
            "embedding_type": embedding_meta.get("type"),
            "embedding_dim": embedding_meta.get("dim"),
            "embedding_model": embedding_meta.get("model"),
            "embedding_fallback_reason": embedding_meta.get("fallback_reason"),
            "embedding_note": embedding_meta.get("note"),
            "chunk_size": DEFAULT_CHUNK_SIZE,
            "chunk_overlap": DEFAULT_CHUNK_OVERLAP,
            "doc_count": len(docs),
        }

        try:
            index = VectorIndex.build(chunks, embeddings, meta=meta)
        except Exception as exc:
            reason = redact_secret(
                f"{type(exc).__name__}: {exc}",
                settings.embedding_api_key,
                settings.api_key,
            )
            logger.warning("向量化失败，回退 HashingEmbeddings 重建索引：%s", reason)
            fallback_dim = (
                settings.embedding_dim
                if (
                    isinstance(settings.embedding_dim, int)
                    and not isinstance(settings.embedding_dim, bool)
                    and settings.embedding_dim > 0
                )
                else DEFAULT_EMBEDDING_DIM
            )
            embeddings = HashingEmbeddings(fallback_dim)
            # 保留配置中的模型名：load_or_build 据此判断“同一模型已回退过”，避免每次都重试远端。
            configured_model = (settings.embedding_model or "").strip() or None
            meta.update(
                {
                    "embedding_type": "hashing",
                    "embedding_dim": fallback_dim,
                    "embedding_model": configured_model,
                    "embedding_fallback_reason": reason,
                    "embedding_note": HASHING_NOTE,
                }
            )
            index = VectorIndex.build(chunks, embeddings, meta=meta)

        if index.vectors:
            meta["embedding_dim"] = len(index.vectors[0])
        index.meta = meta

        index_path = Path(settings.index_path)
        try:
            index.save(index_path)
        except OSError as exc:
            logger.warning("索引写入失败（将继续使用内存索引）：%s", redact_secret(str(exc)))
        logger.info(
            "知识索引就绪：docs=%d chunks=%d embedding=%s dim=%s path=%s",
            len(docs),
            len(index.chunks),
            meta.get("embedding_type"),
            meta.get("embedding_dim"),
            index_path,
        )
        return cls(index, embeddings, index_path=index_path)

    @classmethod
    def load_or_build(cls, settings: Settings) -> KnowledgeBase:
        """优先加载已有索引；文件缺失、损坏或与当前 embedding 配置不一致时重建。

        参数：
            settings: 项目配置。

        返回：
            KnowledgeBase：可直接检索的知识库实例。
        """
        index_path = Path(settings.index_path)
        embeddings, embedding_meta = create_embeddings(settings)

        if not index_path.exists():
            return cls.build(settings)

        try:
            index = VectorIndex.load(index_path)
        except (OSError, ValueError) as exc:
            logger.warning("索引加载失败，将重建：%s", redact_secret(str(exc)))
            return cls.build(settings)

        fallback_embeddings = cls._reuse_fallback_embeddings(index, settings, embedding_meta)
        if fallback_embeddings is not None:
            logger.info("复用上次回退生成的哈希索引（不重复请求远端 embedding）")
            return cls(index, fallback_embeddings, index_path=index_path)

        if not cls._is_compatible(index, embedding_meta):
            logger.info("索引与当前 embedding 配置不一致，重建索引")
            return cls.build(settings)

        return cls(index, embeddings, index_path=index_path)

    @staticmethod
    def _reuse_fallback_embeddings(
        index: VectorIndex,
        settings: Settings,
        embedding_meta: dict[str, Any],
    ) -> HashingEmbeddings | None:
        """如果索引是“远端失败后回退哈希”的产物，重建一个同维哈希向量器并复用索引。

        这样可以避免每次 load_or_build 都重新尝试远端 embedding 再失败回退。
        需要显式重建索引时请运行 bootstrap.py（它会直接调用 build）。

        参数：
            index: 已加载的索引。
            settings: 项目配置（用于缺省维度）。
            embedding_meta: 当前配置期望的 embedding 元信息。

        返回：
            HashingEmbeddings | None：可复用时返回哈希向量器，否则返回 None。
        """
        stored = index.meta
        if stored.get("embedding_type") != "hashing":
            return None
        if not stored.get("embedding_fallback_reason"):
            return None
        if embedding_meta.get("type") != "openai_compatible":
            return None
        if stored.get("embedding_model") != embedding_meta.get("model"):
            return None

        dim: int | None = None
        stored_dim = stored.get("embedding_dim")
        if isinstance(stored_dim, int) and not isinstance(stored_dim, bool) and stored_dim > 0:
            dim = stored_dim
        elif index.vectors:
            dim = len(index.vectors[0])
        if dim is None:
            candidate = settings.embedding_dim
            dim = (
                candidate
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate > 0
                )
                else DEFAULT_EMBEDDING_DIM
            )
        return HashingEmbeddings(dim)

    @staticmethod
    def _is_compatible(index: VectorIndex, embedding_meta: dict[str, Any]) -> bool:
        """判断已存索引是否可复用当前 embedding 配置。

        规则：
            - 索引 meta 必须记录 embedding_type；
            - 类型必须一致；
            - 远端模型还必须模型名一致（维度由服务端决定，加载时不探测）；
            - 哈希向量要求维度一致。
        """
        stored = index.meta
        if not stored or not stored.get("embedding_type"):
            return False
        desired_type = embedding_meta.get("type")
        if stored.get("embedding_type") != desired_type:
            return False
        if desired_type == "openai_compatible":
            return stored.get("embedding_model") == embedding_meta.get("model")
        stored_dim = stored.get("embedding_dim")
        desired_dim = embedding_meta.get("dim")
        if stored_dim is None and index.vectors:
            stored_dim = len(index.vectors[0])
        if stored_dim is not None and desired_dim is not None:
            return int(stored_dim) == int(desired_dim)
        return True

    def search(
        self,
        query: str,
        *,
        k: int = DEFAULT_TOP_K,
        mode: str = "vector",
    ) -> list[SearchHit]:
        """按指定模式检索知识块。

        参数：
            query: 查询文本；空白查询返回空列表。
            k: 返回条数，必须为正整数。
            mode: "vector"（向量余弦）、"keyword"（关键词基线）或 "hybrid"（各取 k 后融合）。

        返回：
            list[SearchHit]：按分数降序、同分按 chunk_id 升序排列的结果。

        异常：
            ValueError: mode 或 k 非法。

        说明：
            向量检索运行期失败（例如远端服务不可用）时降级为关键词检索并记录 WARNING。
        """
        if mode not in VALID_SEARCH_MODES:
            raise ValueError(f"不支持的检索模式：{mode!r}；可选：{', '.join(VALID_SEARCH_MODES)}")
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ValueError("k 必须为正整数")

        text = (query or "").strip()
        if not text:
            return []
        if mode == "keyword":
            return self.keyword_search(text, k=k)

        try:
            vector_hits = self.index.search(text, k, self.embeddings)
        except Exception as exc:
            logger.warning("向量检索失败，降级为关键词检索：%s", redact_secret(str(exc)))
            return self.keyword_search(text, k=k)

        if mode == "vector":
            return vector_hits
        keyword_hits = self.keyword_search(text, k=k)
        return _fuse_hits(keyword_hits, vector_hits, k)

    def keyword_search(self, query: str, *, k: int = DEFAULT_TOP_K) -> list[SearchHit]:
        """关键词基线检索。

        分数公式：
            score = (正文命中数 + 1.5 * 标题命中数) / sqrt(查询 token 数 * 正文 token 数)

        参数：
            query: 查询文本。
            k: 返回条数，必须为正整数。

        返回：
            list[SearchHit]：完全无命中时返回空列表。
        """
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ValueError("k 必须为正整数")
        text = (query or "").strip()
        if not text:
            return []

        query_tokens = set(tokenize(text))
        if not query_tokens:
            return []

        query_size = len(query_tokens)
        scored: list[SearchHit] = []
        for position, chunk in enumerate(self.index.chunks):
            body = self._body_tokens[position]
            title = self._title_tokens[position]
            body_overlap = len(query_tokens & body)
            title_overlap = len(query_tokens & title)
            if body_overlap == 0 and title_overlap == 0:
                continue
            numerator = body_overlap + KEYWORD_TITLE_WEIGHT * title_overlap
            denominator = math.sqrt(query_size * max(1, len(body)))
            score = numerator / denominator
            scored.append(SearchHit(chunk=chunk, score=score, method="keyword"))

        scored.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        return scored[:k]

    def stats(self) -> dict[str, Any]:
        """返回索引统计信息，可直接 JSON 序列化。

        返回：
            dict：文档数、块数、embedding 信息与文档清单。
        """
        documents: dict[str, dict[str, Any]] = {}
        for chunk in self.index.chunks:
            entry = documents.get(chunk.doc_id)
            if entry is None:
                entry = {
                    "doc_id": chunk.doc_id,
                    "title": chunk.title,
                    "chunk_count": 0,
                }
                documents[chunk.doc_id] = entry
            entry["chunk_count"] = int(entry["chunk_count"]) + 1

        meta = self.index.meta
        embedding_dim = meta.get("embedding_dim")
        if embedding_dim is None and self.index.vectors:
            embedding_dim = len(self.index.vectors[0])
        return {
            "doc_count": len(documents),
            "chunk_count": len(self.index.chunks),
            "embedding_type": meta.get("embedding_type"),
            "embedding_dim": embedding_dim,
            "embedding_model": meta.get("embedding_model"),
            "embedding_fallback_reason": meta.get("embedding_fallback_reason"),
            "embedding_note": meta.get("embedding_note"),
            "chunk_size": meta.get("chunk_size"),
            "chunk_overlap": meta.get("chunk_overlap"),
            "index_path": str(self.index_path) if self.index_path is not None else None,
            "documents": sorted(documents.values(), key=lambda item: str(item["doc_id"])),
            "note": meta.get("embedding_note")
            or "HashingEmbeddings 为确定性教学向量，不是语义模型；同义改写不一定命中。",
        }

    def citation_ids(self) -> set[str]:
        """返回索引中全部合法的 chunk_id 集合，用于引用校验。"""
        return {chunk.chunk_id for chunk in self.index.chunks}

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """按 chunk_id 查找知识块；不存在时返回 None。"""
        for chunk in self.index.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None


def _fuse_hits(
    keyword_hits: list[SearchHit],
    vector_hits: list[SearchHit],
    k: int,
) -> list[SearchHit]:
    """融合关键词与向量两路结果。

    规则（可解释的教学简化）：
        - 关键词分数本身已是 0~1 量级的重合度，直接截断到 [0, 1] 后乘 0.5；
        - 向量余弦分数直接乘 0.5；
        - 同一 chunk_id 的分数据相加，去重后按总分排序。
        这里不使用“按本次最高分归一化”，否则任何一次关键词命中都会得到 0.5，
        会让 0.15 的证据阈值失去意义。

    参数：
        keyword_hits: 关键词检索结果。
        vector_hits: 向量检索结果。
        k: 返回条数。

    返回：
        list[SearchHit]：method 统一标记为 "hybrid" 的融合结果。
    """
    if not keyword_hits and not vector_hits:
        return []

    merged: dict[str, SearchHit] = {}

    for hit in keyword_hits:
        normalized = min(1.0, max(0.0, hit.score))
        merged[hit.chunk.chunk_id] = SearchHit(
            chunk=hit.chunk,
            score=0.5 * normalized,
            method="hybrid",
        )

    for hit in vector_hits:
        contribution = 0.5 * max(0.0, hit.score)
        existing = merged.get(hit.chunk.chunk_id)
        if existing is None:
            merged[hit.chunk.chunk_id] = SearchHit(
                chunk=hit.chunk,
                score=contribution,
                method="hybrid",
            )
        else:
            merged[hit.chunk.chunk_id] = SearchHit(
                chunk=existing.chunk,
                score=existing.score + contribution,
                method="hybrid",
            )

    ranked = sorted(merged.values(), key=lambda hit: (-hit.score, hit.chunk.chunk_id))
    return ranked[:k]
