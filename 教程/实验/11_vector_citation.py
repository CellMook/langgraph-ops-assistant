"""单元 11 实验：向量检索、引用与“资料不足”。

用法（在 ops-assistant 目录执行）：
    uv run python ../教程/实验/11_vector_citation.py

实验做的事：
    1) 打印索引统计（embedding 类型/维度/模型/切分参数）；
    2) 用三个同义/近义查询对比 keyword / vector / hybrid；
    3) 用模板答案演示引用结构；
    4) 用"教学替身模型"验证：合法引用被接受、伪造引用被打回；
    5) 用一个离题问题触发"现有资料不足以回答"。

本实验不调用真实模型、不联网。所有输出都是"预期（未在本环境执行）"。
"""

from __future__ import annotations

import sys
from typing import Any

from ops_assistant.config import Settings, load_settings
from ops_assistant.knowledge.embeddings import HashingEmbeddings
from ops_assistant.knowledge.qa import (
    MAX_HITS,
    SCORE_THRESHOLD,
    answer_from_knowledge,
)
from ops_assistant.knowledge.retrieve import VALID_SEARCH_MODES, KnowledgeBase

GOOD_QUESTIONS = (
    "退款算在哪个月",
    "净销售额怎么算",
    "华东包含哪些省份",
)
OUT_OF_SCOPE_QUESTION = "公司食堂几点开饭"


class StaticRewriteLLM:
    """教学替身：冒充“模型”，输出完全由脚本控制，便于验证引用校验。"""

    enabled = True

    def __init__(self, text: str) -> None:
        self.text = text

    def invoke_text(self, messages: list[Any], temperature: float = 0) -> str:
        return self.text


def demo_semantic_gap() -> None:
    """第一段：哈希向量只看字面重合。"""
    print("\n-- 哈希向量不是语义模型 --")
    model = HashingEmbeddings(dim=512)
    pairs = (("退款", "退钱"), ("退款", "退款额"), ("销售额", "销售金额"))
    for left, right in pairs:
        a = model.embed_query(left)
        b = model.embed_query(right)
        cosine = sum(x * y for x, y in zip(a, b))
        print(f"  cos({left!r}, {right!r}) = {round(cosine, 4)}")


def show_index_stats(kb: KnowledgeBase) -> None:
    """第二段：索引统计。"""
    stats = kb.stats()
    print("\n== 索引统计 ==")
    print("文档数：", stats["doc_count"], "块数：", stats["chunk_count"])
    print(
        "embedding：",
        stats["embedding_type"],
        "维度：",
        stats["embedding_dim"],
        "模型：",
        stats["embedding_model"],
    )
    print("切分：", stats["chunk_size"], "/", stats["chunk_overlap"])
    print("索引文件：", stats["index_path"])
    for item in stats["documents"]:
        print(f"  - {item['doc_id']} | {item['title']} | 块数 {item['chunk_count']}")
    print("说明：", stats["note"])


def compare_modes(kb: KnowledgeBase, question: str) -> None:
    """第三段：三种模式对比。"""
    print(f"\n查询：{question}")
    for mode in VALID_SEARCH_MODES:
        hits = kb.search(question, k=MAX_HITS, mode=mode)
        top = hits[0] if hits else None
        top_score = round(top.score, 4) if top is not None else 0.0
        top_chunk = top.chunk.chunk_id if top is not None else "（无命中）"
        print(
            f"  mode={mode:<7} 命中={len(hits)} top_score={top_score} top={top_chunk}"
        )


def show_answer(question: str, kb: KnowledgeBase, llm: Any | None = None) -> None:
    """打印一次问答结果的完整结构。"""
    answer = answer_from_knowledge(question, kb, llm)
    print(f"\n问题：{question}")
    print("insufficient：", answer.insufficient, " mode：", answer.mode)
    print("答案：")
    for line in answer.answer.splitlines():
        print("  ", line)
    if answer.citations:
        print("引用：")
        for item in answer.citations:
            print(f"   - {item['chunk_id']} | {item['title']} | {item['snippet']}")


def pick_demo_question(kb: KnowledgeBase) -> str | None:
    """挑一个“有依据”的问题用来演示；全都不足时返回 None。"""
    for question in GOOD_QUESTIONS:
        answer = answer_from_knowledge(question, kb)
        if not answer.insufficient:
            return question
    return None


def main() -> int:
    settings: Settings = load_settings()
    kb = KnowledgeBase.load_or_build(settings)
    print("== 单元 11 实验：向量检索、引用与“资料不足” ==")
    print("证据阈值 SCORE_THRESHOLD =", SCORE_THRESHOLD, "引用条数 MAX_HITS =", MAX_HITS)
    show_index_stats(kb)
    demo_semantic_gap()

    print("\n== 三种检索模式对比 ==")
    for question in GOOD_QUESTIONS + (OUT_OF_SCOPE_QUESTION,):
        compare_modes(kb, question)

    demo_question = pick_demo_question(kb)
    if demo_question is None:
        print("\n[提示] 三个示例问题在你这套索引上都判为“资料不足”。")
        print("       请先确认 data/knowledge/ 文档内容，再重跑本脚本；或换更贴近原文的问法。")
        return 0

    print("\n== 模板答案与引用 ==")
    show_answer(demo_question, kb)

    print("\n== 模型改写与引用校验（教学替身，不调用真实模型） ==")
    hits = kb.search(demo_question, k=1, mode="hybrid")
    allowed_id = hits[0].chunk.chunk_id
    good_llm = StaticRewriteLLM(f"退款按退款发生月份归属，不按订单月份 [{allowed_id}]")
    show_answer(demo_question, kb, good_llm)

    forged_llm = StaticRewriteLLM("净销售额 = 毛销售额 - 退款额 [99_不存在的文档#0]")
    show_answer(demo_question, kb, forged_llm)

    print("\n== 资料不足 ==")
    show_answer(OUT_OF_SCOPE_QUESTION, kb)
    for question in GOOD_QUESTIONS + (OUT_OF_SCOPE_QUESTION,):
        hits = kb.search(question, k=MAX_HITS, mode="hybrid")
        top_score = max((hit.score for hit in hits), default=0.0)
        verdict = "资料不足" if top_score < SCORE_THRESHOLD else "有依据"
        print(f"  {question}：top_score={round(top_score, 4)} → {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
