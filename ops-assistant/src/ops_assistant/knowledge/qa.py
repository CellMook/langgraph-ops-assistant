"""知识问答：检索 + 模板答案 + 可选模型改写与引用校验。
无命中或最高分低于 0.15 时明确回答“现有资料不足以回答”，不做推断。
有模型时只允许基于给定知识块改写；程序逐条校验引用 id，失败即回退模板。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .embeddings import redact_secret
from .models import SearchHit, make_snippet
from .retrieve import KnowledgeBase

logger = logging.getLogger(__name__)

# 证据阈值：最高检索分低于该值视为资料不足（规格 5.4）。
SCORE_THRESHOLD = 0.15

# 默认引用的知识块数量。
MAX_HITS = 4

# 引用格式：[doc_id#序号]，例如 [01_指标口径#0]。
_CITATION_PATTERN = re.compile(r"\[([^\[\]\s]+#[0-9]+)\]")


@dataclass(frozen=True)
class KnowledgeAnswer:
    """知识问答结果。

    属性：
        answer: 面向用户的回答文本。
        citations: 引用信息元组（doc_id/title/chunk_id/snippet）。
        hits: 原始检索命中（便于调试与报告来源）。
        insufficient: 是否资料不足。
        mode: "template"（模板）或 "llm"（模型改写且引用校验通过）。
    """

    answer: str
    citations: tuple[dict[str, Any], ...]
    hits: tuple[SearchHit, ...]
    insufficient: bool
    mode: str


def build_template_answer(hits: list[SearchHit]) -> str:
    """用检索命中的知识块拼出可核对的模板答案。

    参数：
        hits: 检索命中列表（至少一条）。

    返回：
        str：逐条带 [chunk_id] 引用的中文回答。
    """
    lines = ["根据本地知识库检索结果（仅基于下列资料，未做额外推断）："]
    for position, hit in enumerate(hits, start=1):
        snippet = make_snippet(hit.chunk.text, max_chars=180)
        lines.append(f"{position}. [{hit.chunk.chunk_id}] {snippet}")
    lines.append("以上引用来自本地知识库文档，完整口径请查看对应原文。")
    return "\n".join(lines)


def _insufficient_answer(question: str, kb: KnowledgeBase) -> str:
    """生成“资料不足”的明确回答，并列出已检索的文档。

    参数：
        question: 用户问题。
        kb: 知识库实例，用于列出文档清单。

    返回：
        str：中文说明，包含“现有资料不足以回答”。
    """
    stats = kb.stats()
    documents = stats.get("documents") or []
    doc_items = [f"{item.get('doc_id')}（{item.get('title')}）" for item in documents]
    document_text = "、".join(doc_items) if doc_items else "（知识库为空或索引尚未构建）"
    short_question = question if len(question) <= 80 else question[:77] + "…"
    return (
        f"现有资料不足以回答“{short_question}”。\n"
        f"已检索的知识文档：{document_text}。\n"
        "检索最高分低于阈值 0.15 或没有任何命中，因此不作推断。"
        "建议改用更具体的业务词（例如“退款归属”“净销售额口径”“地区说明”），"
        "或补充知识文档后重新运行 bootstrap.py 重建索引。"
    )


def _format_context(hits: list[SearchHit]) -> str:
    """把命中知识块格式化为模型可引用的上下文。"""
    blocks: list[str] = []
    for hit in hits:
        blocks.append(
            f"[{hit.chunk.chunk_id}] 标题：{hit.chunk.title}\n{hit.chunk.text}"
        )
    return "\n\n".join(blocks)


def _validate_citations(text: str, hits: list[SearchHit]) -> bool:
    """校验模型输出中的引用。

    规则：
        - 至少要有一个 [chunk_id] 形式的引用；
        - 每个引用 id 都必须真实存在于索引，且必须是本次提供的知识块；
        - 不允许出现索引中不存在的伪造 id。

    参数：
        text: 模型输出文本。
        hits: 本次提供给模型的命中列表。

    返回：
        bool：通过返回 True，否则 False。
    """
    cited = set(_CITATION_PATTERN.findall(text))
    if not cited:
        return False
    allowed = {hit.chunk.chunk_id for hit in hits}
    return cited.issubset(allowed)


def _llm_rewrite(
    question: str,
    hits: list[SearchHit],
    llm: Any,
    fallback: str,
) -> tuple[str, str]:
    """尝试让模型基于给定知识块改写答案，失败时回退模板。

    参数：
        question: 用户问题。
        hits: 已通过阈值筛选的命中列表。
        llm: LLMClient 或任意具备 invoke_text/invoke 的聊天模型；可为 None。
        fallback: 模板答案。

    返回：
        tuple[str, str]：(最终答案, 模式 "llm" 或 "template")。
    """
    if llm is None:
        return fallback, "template"
    if not bool(getattr(llm, "enabled", True)):
        return fallback, "template"

    context = _format_context(hits)
    system_prompt = (
        "你是企业知识库问答助手。只能依据用户消息中给出的知识块作答，"
        "不得补充知识块之外的事实、数字或推断。"
        "每个结论后面必须用方括号引用来源块，格式为 [块编号]，例如 [01_指标口径#0]。"
        "如果知识块不足以回答，直接回答“现有资料不足以回答”，并说明缺少什么。"
        "用简体中文回答，保持简洁。"
    )
    human_prompt = (
        f"问题：{question}\n\n"
        f"可引用的知识块：\n{context}\n\n"
        "请基于上述知识块回答，并逐条给出引用。"
    )
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]

    try:
        if hasattr(llm, "invoke_text"):
            raw_text = llm.invoke_text(messages, temperature=0)
        elif hasattr(llm, "invoke"):
            response = llm.invoke(messages)
            raw_text = getattr(response, "content", None) or str(response)
        else:
            logger.warning("传入的 llm 既不支持 invoke_text 也不支持 invoke，回退模板")
            return fallback, "template"
    except Exception as exc:
        logger.warning("模型改写失败，回退模板：%s", redact_secret(str(exc)))
        return fallback, "template"

    text = str(raw_text or "").strip()
    if not text:
        logger.warning("模型返回空文本，回退模板")
        return fallback, "template"
    if not _validate_citations(text, hits):
        logger.warning("模型输出引用校验失败，回退模板；输出片段：%s", text[:120])
        return fallback, "template"
    return text, "llm"


def answer_from_knowledge(
    question: str,
    kb: KnowledgeBase,
    llm: Any | None = None,
) -> KnowledgeAnswer:
    """基于本地知识库回答问题。

    流程：
        1. 用 hybrid 模式检索最多 4 个知识块；
        2. 无命中或最高分 < 0.15 -> insufficient=True，明确写“现有资料不足以回答”；
        3. 有证据 -> 先用模板答案兜底，再尝试模型改写；
        4. 模型改写必须通过引用校验（id 必须真实存在且在本次给定块内），否则回退模板。

    参数：
        question: 用户问题。
        kb: 知识库实例。
        llm: 可选模型（LLMClient 或聊天模型）；为 None 或未启用时只输出模板。

    返回：
        KnowledgeAnswer：包含答案、引用、命中、是否资料不足与模式。
    """
    text = (question or "").strip()
    if not text:
        return KnowledgeAnswer(
            answer=(
                "现有资料不足以回答：问题为空。"
                "请提供具体业务问题，例如“退款是否计入当月销售额？”。"
            ),
            citations=(),
            hits=(),
            insufficient=True,
            mode="template",
        )

    hits = kb.search(text, k=MAX_HITS, mode="hybrid")
    top_score = max((hit.score for hit in hits), default=0.0)
    if not hits or top_score < SCORE_THRESHOLD:
        return KnowledgeAnswer(
            answer=_insufficient_answer(text, kb),
            citations=(),
            hits=tuple(hits),
            insufficient=True,
            mode="template",
        )

    fallback = build_template_answer(hits)
    answer, mode = _llm_rewrite(text, hits, llm, fallback)
    citations = tuple(hit.citation() for hit in hits)
    return KnowledgeAnswer(
        answer=answer,
        citations=citations,
        hits=tuple(hits),
        insufficient=False,
        mode=mode,
    )
