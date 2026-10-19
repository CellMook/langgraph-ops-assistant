"""T17–T19：知识库加载、检索与引用（工程规格 6.1）。

覆盖点：
- T17 加载与切分：4 篇文档、chunk_id 唯一且形如 ``<doc_id>#<n>``、索引可保存/加载；
- T18 检索：关键词基线能命中业务词，完全无关的问题返回空；哈希向量检索确定性；
- T19 回答与引用：资料不足要明说；有证据时引用必须真实存在；伪造引用的模型输出被拒绝。

说明：
    本项目默认用确定性 ``HashingEmbeddings``（词面哈希），**不是语义模型**。
    因此这里只断言"能命中贴近文档用词的问题"，不断言同义改写能力——
    那是真实 embedding 才能提供的，属于已知限制（见 docs/limitations.md）。

诚实声明：未在编写环境中执行。
"""

from __future__ import annotations

from typing import Any

import pytest

from ops_assistant.knowledge.embeddings import HashingEmbeddings, tokenize
from ops_assistant.knowledge.index import VectorIndex
from ops_assistant.knowledge.loaders import load_documents
from ops_assistant.knowledge.qa import KnowledgeAnswer, answer_from_knowledge
from ops_assistant.knowledge.retrieve import KnowledgeBase
from ops_assistant.knowledge.splitter import split_documents

# ---------------------------------------------------------------------------
# T17 加载、切分与索引持久化
# ---------------------------------------------------------------------------


def test_t17_loads_all_four_documents(settings) -> None:
    """T17：知识目录里的 4 篇 Markdown 全部被读入。"""
    docs = load_documents(settings.knowledge_dir)
    assert len(docs) == 4
    assert {doc.doc_id for doc in docs} == {
        "01_指标口径",
        "02_退款与归属",
        "03_地区说明",
        "04_数据字典",
    }


def test_t17_every_document_has_title_and_text(settings) -> None:
    """T17：每篇文档都有标题与正文，避免读到空文件也算"加载成功"。"""
    for doc in load_documents(settings.knowledge_dir):
        assert doc.title.strip()
        assert len(doc.text.strip()) > 200, f"{doc.doc_id} 正文过短"


def test_t17_chunks_have_stable_ids(settings) -> None:
    """T17：chunk_id 形如 "<doc_id>#<序号>" 且全局唯一。"""
    docs = load_documents(settings.knowledge_dir)
    chunks = split_documents(docs, chunk_size=400, chunk_overlap=60)
    assert chunks, "切分结果不应为空"
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    for chunk in chunks:
        assert chunk.chunk_id.startswith(f"{chunk.doc_id}#")
        suffix = chunk.chunk_id.split("#", 1)[1]
        assert suffix.isdigit()


def test_t17_chunk_size_is_respected(settings) -> None:
    """T17：切分块长度不超过配置上限（允许少量分隔符误差）。"""
    docs = load_documents(settings.knowledge_dir)
    chunks = split_documents(docs, chunk_size=400, chunk_overlap=60)
    assert all(len(chunk.text) <= 400 for chunk in chunks)


def test_t17_kb_stats_describe_index(kb: KnowledgeBase) -> None:
    """T17：stats() 返回可展示的统计（文档数、块数、向量实现）。"""
    stats = kb.stats()
    assert stats["doc_count"] == 4
    assert stats["chunk_count"] > 0
    assert stats["embedding_type"] in {"hashing", "openai-compatible"}


def test_t17_index_roundtrip(tmp_path, settings) -> None:
    """T17：索引写盘后能重新加载，且检索结果一致（持久化可用）。"""
    kb = KnowledgeBase.build(settings)
    index_path = tmp_path / "index.json"
    kb.index.save(index_path)

    loaded = VectorIndex.load(index_path)
    assert len(loaded.chunks) == len(kb.index.chunks)
    assert loaded.chunks[0].chunk_id == kb.index.chunks[0].chunk_id
    assert loaded.meta.get("doc_count") == 4


# ---------------------------------------------------------------------------
# T18 检索
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected_doc"),
    [
        # "退款是否计入当月销售额"问的是退款归属规则，最相关文档是 02（实测核对）；
        # 01 的对应问题用"净销售额的定义"这类指标口径问法覆盖。
        ("退款是否计入当月销售额？", "02_退款与归属"),
        ("净销售额的定义是什么？", "01_指标口径"),
        ("退款金额算在哪个月？", "02_退款与归属"),
        ("华东大区包含哪些省份？", "03_地区说明"),
        ("orders 表有哪些字段？", "04_数据字典"),
    ],
)
def test_t18_keyword_search_hits_expected_document(
    kb: KnowledgeBase, question: str, expected_doc: str
) -> None:
    """T18：贴近文档用词的问题，关键词基线能召回对应文档。"""
    hits = kb.keyword_search(question, k=4)
    assert hits, f"关键词检索无结果：{question}"
    top_docs = [hit.chunk.doc_id for hit in hits]
    assert expected_doc in top_docs


def test_t18_keyword_search_returns_empty_for_no_overlap(kb: KnowledgeBase) -> None:
    """T18：完全没有词面重叠时返回空列表（不硬凑结果）。"""
    hits = kb.keyword_search("量子纠缠与超导材料能带结构", k=4)
    assert hits == []


def test_t18_vector_search_is_deterministic(kb: KnowledgeBase) -> None:
    """T18：同一问题检索两次，结果 id 与顺序完全一致（可复现）。"""
    first = kb.search("退款归属月份", k=4, mode="vector")
    second = kb.search("退款归属月份", k=4, mode="vector")
    assert [hit.chunk.chunk_id for hit in first] == [
        hit.chunk.chunk_id for hit in second
    ]
    assert [hit.score for hit in first] == [hit.score for hit in second]


def test_t18_scores_are_normalized(kb: KnowledgeBase) -> None:
    """T18：检索分数在 0~1 之间，便于用统一阈值判断资料是否充分。"""
    hits = kb.search("净销售额 退款 口径", k=4, mode="hybrid")
    assert hits
    assert all(0.0 <= hit.score <= 1.0 for hit in hits)


def test_t18_hybrid_mode_returns_hits(kb: KnowledgeBase) -> None:
    """T18：hybrid 模式可用，且 method 标记为 hybrid。"""
    hits = kb.search("退款归属", k=3, mode="hybrid")
    assert hits
    assert {hit.method for hit in hits} == {"hybrid"}


def test_t18_unknown_mode_raises(kb: KnowledgeBase) -> None:
    """T18：非法检索模式要报错，不能静默降级。"""
    with pytest.raises(ValueError):
        kb.search("退款", k=3, mode="semantic")


def test_t18_hashing_embeddings_are_lexical_not_semantic() -> None:
    """T18：哈希向量对完全相同文本给出相同向量；对无关文本不保证相似。

    这条测试是为了把"确定性词面向量"的局限写成可执行的断言，
    避免把 HashingEmbeddings 误当成语义模型。
    """
    embeddings = HashingEmbeddings(dim=256)
    same_a = embeddings.embed_query("退款归属月份")
    same_b = embeddings.embed_query("退款归属月份")
    assert same_a == same_b
    assert len(same_a) == 256
    # 归一化后的向量长度约为 1（全 0 向量除外）
    norm = sum(value * value for value in same_a) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_t18_tokenize_handles_chinese_and_ascii() -> None:
    """T18：分词同时产出一元/二元中文 token 与英文 token。"""
    tokens = tokenize("退款 refund 2026-09")
    assert "退款" in tokens
    assert "refund" in tokens
    # 中文 2-gram 应包含"退款"内部的相邻组合
    assert any(len(token) == 2 for token in tokens)


# ---------------------------------------------------------------------------
# T19 回答、引用与资料不足
# ---------------------------------------------------------------------------


def test_t19_template_answer_contains_citation_ids(kb: KnowledgeBase) -> None:
    """T19：模板答案里每个结论都带 [chunk_id]，可回溯到具体知识块。"""
    answer = answer_from_knowledge("退款是否计入当月销售额？", kb, llm=None)
    assert isinstance(answer, KnowledgeAnswer)
    assert answer.insufficient is False
    assert answer.mode == "template"
    assert answer.citations
    for citation in answer.citations:
        assert citation["chunk_id"]
        assert citation["doc_id"]
        assert citation["snippet"]


def test_t19_citations_refer_to_real_chunks(kb: KnowledgeBase) -> None:
    """T19：引用 id 必须真实存在于索引中（不能是模型编的编号）。"""
    known = {chunk.chunk_id for chunk in kb.index.chunks}
    answer = answer_from_knowledge("净销售额怎么计算？", kb, llm=None)
    assert answer.citations
    for citation in answer.citations:
        assert citation["chunk_id"] in known


def test_t19_insufficient_when_no_overlap(kb: KnowledgeBase) -> None:
    """T19：完全无关的问题必须明确说“资料不足”，而不是编答案。"""
    answer = answer_from_knowledge("量子计算机的退相干时间是多少？", kb, llm=None)
    assert answer.insufficient is True
    assert "现有资料不足以回答" in answer.answer
    assert answer.citations == ()


def test_t19_empty_question_is_insufficient(kb: KnowledgeBase) -> None:
    """T19：空问题直接判为资料不足，并提示正确问法。"""
    answer = answer_from_knowledge("   ", kb, llm=None)
    assert answer.insufficient is True
    assert answer.mode == "template"


class _FakeLLM:
    """测试替身：只实现 invoke_text/enabled，用于验证引用校验逻辑。

    这是唯一允许 mock 的地方——模型是外部网络服务，不是被测业务逻辑。
    """

    enabled = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[Any] = []

    def invoke_text(self, messages: Any, *, temperature: float = 0.0) -> str:
        self.calls.append((messages, temperature))
        return self.text


def test_t19_fake_llm_with_fabricated_citation_falls_back(kb: KnowledgeBase) -> None:
    """T19：模型引用不存在的块编号 -> 判定校验失败 -> 回退模板答案。"""
    fake = _FakeLLM("净销售额等于毛销售额减退款 [03_地区说明#999]。")
    answer = answer_from_knowledge("净销售额怎么计算？", kb, llm=fake)
    assert answer.mode == "template", "伪造引用必须被拒绝"
    assert "现有资料不足以回答" not in answer.answer
    assert fake.calls, "应当真的调用过模型（随后被校验拒绝）"


def test_t19_fake_llm_with_valid_citation_is_accepted(kb: KnowledgeBase) -> None:
    """T19：模型引用了本次命中里真实存在的块 -> 接受模型答案。"""
    probe = answer_from_knowledge("净销售额怎么计算？", kb, llm=None)
    valid_id = probe.citations[0]["chunk_id"]
    fake = _FakeLLM(f"净销售额 = 毛销售额 - 已批准退款 [{valid_id}]。")
    answer = answer_from_knowledge("净销售额怎么计算？", kb, llm=fake)
    assert answer.mode == "llm"
    assert valid_id in answer.answer


def test_t19_llm_failure_falls_back_to_template(kb: KnowledgeBase) -> None:
    """T19：模型调用抛异常时回退模板，整个问答链路不崩。"""

    class _BoomLLM:
        enabled = True

        def invoke_text(self, messages: Any, *, temperature: float = 0.0) -> str:
            raise RuntimeError("模拟网络超时")

    answer = answer_from_knowledge("退款归属月份", kb, llm=_BoomLLM())
    assert answer.mode == "template"
    assert answer.answer
    assert answer.insufficient is False


def test_t19_disabled_llm_uses_template(kb: KnowledgeBase) -> None:
    """T19：没有模型时（llm=None 或 enabled=False）只输出模板答案。"""

    class _Disabled:
        enabled = False

    answer = answer_from_knowledge("退款归属月份", kb, llm=_Disabled())
    assert answer.mode == "template"
    assert answer.insufficient is False
