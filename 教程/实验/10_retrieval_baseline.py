"""单元 10 实验：文档加载、切分与关键词基线。

用法（在 ops-assistant 目录执行）：
    uv run python ../教程/实验/10_retrieval_baseline.py

实验做的事：
    1) 读 data/knowledge/，打印文档清单；
    2) 切分并统计块数与长度分布；
    3) 构建索引（写 data/knowledge_index.json）；
    4) 用三个查询演示关键词打分，并手算校验第一条分数。

本实验不调用模型、不需要 API Key。所有输出都是"预期（未在本环境执行）"。
"""

from __future__ import annotations

import math
import sys
from collections import Counter
from pathlib import Path

from ops_assistant.config import Settings, load_settings
from ops_assistant.knowledge.embeddings import tokenize
from ops_assistant.knowledge.loaders import load_documents
from ops_assistant.knowledge.models import Chunk, LoadedDoc, SearchHit, make_snippet
from ops_assistant.knowledge.retrieve import (
    DEFAULT_TOP_K,
    KEYWORD_TITLE_WEIGHT,
    KnowledgeBase,
)
from ops_assistant.knowledge.splitter import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    split_documents,
)

QUERIES = ("退款算在哪个月", "净销售额怎么算", "退钱算哪个月")


def show_documents(settings: Settings) -> list[LoadedDoc]:
    """第一段：加载并打印文档清单。"""
    print("== 第一段：加载 ==")
    print("知识目录：", settings.knowledge_dir)
    docs = load_documents(Path(settings.knowledge_dir))
    print("文档数：", len(docs))
    for doc in docs:
        print(f"  - {doc.doc_id} | {doc.title} | {len(doc.text)} 字符 | {doc.path}")
    return docs


def show_chunks(docs: list[LoadedDoc]) -> list[Chunk]:
    """第二段：切分并统计。"""
    chunks = split_documents(docs)
    print("\n== 第二段：切分（chunk_size=400, chunk_overlap=60）==")
    print("块数：", len(chunks))
    if chunks:
        average = sum(len(chunk.text) for chunk in chunks) / len(chunks)
        print("平均块长：", round(average, 1), "字符")
        sizes = Counter(len(chunk.text) for chunk in chunks)
        print("长度分布（前 8 个长度: 块数）：", dict(sorted(sizes.items())[:8]))
        for chunk in chunks[:3]:
            print(
                f"  {chunk.chunk_id} | start={chunk.start} | 标题={chunk.title}"
                f" | {make_snippet(chunk.text, 48)}"
            )
    return chunks


def show_keyword(kb: KnowledgeBase) -> None:
    """第三段：关键词检索 + 手算校验。"""
    print("\n== 第三段：关键词基线 ==")
    for query in QUERIES:
        hits = kb.keyword_search(query, k=3)
        print(f"\n查询：{query}  命中={len(hits)}")
        for hit in hits:
            print(
                f"  score={hit.score:.4f} method={hit.method} {hit.chunk.chunk_id}"
                f" | {make_snippet(hit.chunk.text, 48)}"
            )

    print("\n-- 手算校验：第一条分数是否等于公式 --")
    hits: list[SearchHit] = kb.keyword_search(QUERIES[0], k=1)
    if not hits:
        print("  查询无命中，无法手算；请确认文档里确实有“退款算在哪个月”这类表述。")
        return
    target = hits[0]
    position = next(
        index
        for index, chunk in enumerate(kb.index.chunks)
        if chunk.chunk_id == target.chunk.chunk_id
    )
    query_tokens = set(tokenize(QUERIES[0]))
    body_tokens = set(tokenize(kb.index.chunks[position].text))
    title_tokens = set(tokenize(kb.index.chunks[position].title))
    numerator = len(query_tokens & body_tokens) + KEYWORD_TITLE_WEIGHT * len(
        query_tokens & title_tokens
    )
    denominator = math.sqrt(len(query_tokens) * max(1, len(body_tokens)))
    print("  手算：", round(numerator / denominator, 4))
    print("  程序：", round(target.score, 4))
    print("  权重：", KEYWORD_TITLE_WEIGHT)


def show_citation(kb: KnowledgeBase) -> None:
    """第四段：引用长什么样。"""
    print("\n== 第四段：引用信息 ==")
    hits = kb.keyword_search("净销售额怎么算", k=1)
    if not hits:
        print("  无命中，跳过。")
        return
    citation = hits[0].citation()
    for key, value in citation.items():
        print(f"  {key}: {value}")


def main() -> int:
    settings = load_settings()
    docs = show_documents(settings)
    if not docs:
        print("[失败] 知识目录为空或不存在：请确认 data/knowledge/ 下有 .md 或 .txt 文档。")
        return 1

    chunks = show_chunks(docs)
    print("\n切分总计：", len(chunks), "块（知识库构建会重新切一次，结果一致）")

    kb = KnowledgeBase.build(settings)
    print("\n== 索引 ==")
    print("索引文件：", settings.index_path)
    print("索引块数：", len(kb.index.chunks), "默认 k：", DEFAULT_TOP_K)
    print("切分参数：", DEFAULT_CHUNK_SIZE, "/", DEFAULT_CHUNK_OVERLAP)

    show_keyword(kb)
    show_citation(kb)

    print("\n提示：把“退钱算哪个月”的 top_score 与“退款算在哪个月”对比，")
    print("      差值就是关键词基线的天花板；单元 11 用向量与混合检索再比一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
