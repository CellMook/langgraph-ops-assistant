"""向量索引：把知识块与向量打包成可保存、可加载的 JSON 索引。
build 负责批量向量化，search 用余弦相似度返回命中，save/load 做原子读写。
索引只保存 chunks/vectors/meta，不保存任何密钥。
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Chunk, SearchHit

logger = logging.getLogger(__name__)

# 索引文件格式版本，便于未来兼容性判断。
INDEX_SCHEMA_VERSION = 1

# 向量保存精度：8 位小数足够稳定，也避免 JSON 体积过大。
_VECTOR_DECIMALS = 8


@dataclass
class VectorIndex:
    """切块与向量的内存索引。

    属性：
        chunks: 知识块列表。
        vectors: 与 chunks 一一对应的向量列表。
        meta: 索引元信息（embedding 类型/维度/模型、切分参数等），不含密钥。
    """

    chunks: list[Chunk]
    vectors: list[list[float]]
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        """返回知识块数量。"""
        return len(self.chunks)

    @classmethod
    def build(
        cls,
        chunks: list[Chunk],
        embeddings: Any,
        *,
        meta: dict[str, Any] | None = None,
    ) -> VectorIndex:
        """向量化全部知识块并构建索引。

        参数：
            chunks: 切分后的知识块列表；空列表允许。
            embeddings: 具备 embed_documents 方法的向量实现。
            meta: 可选元信息。

        返回：
            VectorIndex：索引对象。

        异常：
            ValueError: 向量数量与知识块数量不一致。
        """
        texts = [chunk.text for chunk in chunks]
        vectors = embeddings.embed_documents(texts) if texts else []
        if len(vectors) != len(chunks):
            raise ValueError(
                f"向量数量与知识块数量不一致：{len(vectors)} != {len(chunks)}"
            )
        # 统一按保存精度取整，保证“内存索引”与“落盘后重新加载”的检索分数完全一致。
        normalized_vectors = [
            [round(float(value), _VECTOR_DECIMALS) for value in vector] for vector in vectors
        ]
        if normalized_vectors:
            dimension = len(normalized_vectors[0])
            for position, vector in enumerate(normalized_vectors):
                if len(vector) != dimension:
                    raise ValueError(
                        "向量维度不一致："
                        f"vectors[0] 为 {dimension} 维，vectors[{position}] 为 {len(vector)} 维"
                    )
        return cls(chunks=list(chunks), vectors=normalized_vectors, meta=dict(meta or {}))

    def search(self, query: str, k: int, embeddings: Any) -> list[SearchHit]:
        """按余弦相似度检索最相关的知识块。

        参数：
            query: 查询文本。
            k: 返回条数，必须为正整数。
            embeddings: 具备 embed_query 方法的向量实现。

        返回：
            list[SearchHit]：按分数降序、chunk_id 升序（同分）排列的前 k 条；
            余弦相似度 <= 0 的块会被过滤，因此可能少于 k 条或为空。

        异常：
            ValueError: k 非法或查询向量与索引向量维度不一致。
        """
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ValueError("k 必须为正整数")
        if not self.chunks:
            return []

        query_vector = [float(value) for value in embeddings.embed_query(query)]
        scored: list[SearchHit] = []
        for chunk, vector in zip(self.chunks, self.vectors, strict=False):
            score = _cosine_similarity(query_vector, vector)
            if score > 0.0:
                scored.append(SearchHit(chunk=chunk, score=score, method="vector"))
        scored.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        return scored[:k]

    def save(self, path: Path) -> None:
        """把索引原子写入 JSON 文件。

        参数：
            path: 目标路径；父目录不存在时自动创建。

        说明：
            先写同目录临时文件再 os.replace，避免中途失败留下半个索引。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta)
        meta.setdefault("schema_version", INDEX_SCHEMA_VERSION)
        meta["chunk_count"] = len(self.chunks)
        payload = {
            "meta": meta,
            "chunks": [_chunk_to_dict(chunk) for chunk in self.chunks],
            "vectors": [
                [round(value, _VECTOR_DECIMALS) for value in vector]
                for vector in self.vectors
            ],
        }
        temp_path = target.with_name(target.name + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(str(temp_path), str(target))

    @classmethod
    def load(cls, path: Path) -> VectorIndex:
        """从 JSON 文件加载索引。

        参数：
            path: 索引文件路径。

        返回：
            VectorIndex：加载后的索引。

        异常：
            OSError: 文件不可读。
            ValueError: JSON 结构或字段非法。
        """
        source = Path(path)
        raw_text = source.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"索引文件不是合法 JSON：{source}") from exc
        if not isinstance(payload, dict):
            raise ValueError("索引文件顶层必须是 JSON 对象")

        raw_chunks = payload.get("chunks")
        raw_vectors = payload.get("vectors")
        raw_meta = payload.get("meta", {})
        if not isinstance(raw_chunks, list) or not isinstance(raw_vectors, list):
            raise ValueError("索引文件缺少 chunks 或 vectors 数组")
        if len(raw_chunks) != len(raw_vectors):
            raise ValueError(
                f"索引 chunks 与 vectors 数量不一致：{len(raw_chunks)} != {len(raw_vectors)}"
            )
        if not isinstance(raw_meta, dict):
            raise ValueError("索引 meta 必须是 JSON 对象")

        chunks = [
            _chunk_from_dict(item, position)
            for position, item in enumerate(raw_chunks)
        ]
        vectors = [_vector_from_json(item, position) for position, item in enumerate(raw_vectors)]
        return cls(chunks=chunks, vectors=vectors, meta=dict(raw_meta))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度。

    参数：
        left: 查询向量。
        right: 索引向量。

    返回：
        float：范围约 [-1, 1]；任一向量为零向量时返回 0.0。

    异常：
        ValueError: 维度不一致。
    """
    if len(left) != len(right):
        raise ValueError(
            f"向量维度不一致：查询 {len(left)} 维，索引 {len(right)} 维；"
            "请删除旧索引后重新运行 bootstrap.py 重建"
        )
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=True):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _chunk_to_dict(chunk: Chunk) -> dict[str, Any]:
    """把 Chunk 转为可 JSON 序列化的 dict。"""
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "title": chunk.title,
        "text": chunk.text,
        "start": int(chunk.start),
        "metadata": {str(key): str(value) for key, value in chunk.metadata.items()},
    }


def _chunk_from_dict(data: Any, position: int) -> Chunk:
    """从 JSON dict 还原 Chunk，并做必要字段校验。"""
    if not isinstance(data, dict):
        raise ValueError(f"chunks[{position}] 必须是 JSON 对象")
    for key in ("chunk_id", "doc_id", "title", "text"):
        if not isinstance(data.get(key), str):
            raise ValueError(f"chunks[{position}].{key} 必须是字符串")
    try:
        start = int(data.get("start", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"chunks[{position}].start 必须是整数") from exc

    raw_metadata = data.get("metadata", {})
    if raw_metadata is None:
        raw_metadata = {}
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"chunks[{position}].metadata 必须是 JSON 对象")
    metadata = {str(key): str(value) for key, value in raw_metadata.items()}

    return Chunk(
        chunk_id=data["chunk_id"],
        doc_id=data["doc_id"],
        title=data["title"],
        text=data["text"],
        start=start,
        metadata=metadata,
    )


def _vector_from_json(data: Any, position: int) -> list[float]:
    """从 JSON 数组还原向量，并校验元素为数字。"""
    if not isinstance(data, list):
        raise ValueError(f"vectors[{position}] 必须是数组")
    vector: list[float] = []
    for element in data:
        if isinstance(element, bool) or not isinstance(element, (int, float)):
            raise ValueError(f"vectors[{position}] 含非数字元素")
        vector.append(float(element))
    return vector
