"""向量化实现：确定性哈希向量与可选 OpenAI 兼容远端向量。
HashingEmbeddings 是教学用的词袋哈希向量，可离线、可复现，但不理解语义。
OpenAICompatEmbeddings 仅在配置了 embedding 模型时启用；失败由上层回退哈希并记录原因。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any

from ops_assistant.config import Settings

logger = logging.getLogger(__name__)

# 中文连续片段与英文/数字 token 的切分规则。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOKEN = re.compile(r"[a-z0-9]+")

# 默认哈希维度（与规格一致）。
DEFAULT_EMBEDDING_DIM = 512

HASHING_NOTE = (
    "HashingEmbeddings 是确定性教学向量：对中文 2-gram 与英文/数字 token 做 blake2b 哈希映射后累加并 L2 归一化。"
    "它只反映词形重合，不理解同义改写与上下文，因此不能当作语义模型；生产环境应换成真实 embedding 模型。"
)

REMOTE_NOTE = (
    "OpenAICompatEmbeddings 通过 langchain_openai.OpenAIEmbeddings 调用 OpenAI 兼容端点；"
    "向量维度由服务端决定，索引中记录实际维度。"
)


def tokenize(text: str) -> list[str]:
    """把文本切分为检索 token。

    规则：
        - 中文连续片段切成所有相邻 2-gram；单字片段保留该单字。
        - 英文与数字转小写后按连续字母/数字切分（如 "2026-09" -> "2026"、"09"）。
        - 保留重复 token（哈希向量按出现次数累加；关键词检索会自行去重）。

    参数：
        text: 任意文本。

    返回：
        list[str]：token 列表；空文本返回空列表。
    """
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_ASCII_TOKEN.findall(lowered))
    return tokens


def redact_secret(text: str, *secrets: str | None) -> str:
    """对日志/错误摘要做脱敏，避免泄漏 API key 等敏感值。

    参数：
        text: 原始文本。
        *secrets: 需要被替换成 "***" 的敏感字符串（长度小于 4 的忽略，避免误伤）。

    返回：
        str：脱敏并截断到 300 字符以内的文本。
    """
    result = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, "***")
    result = re.sub(r"(?i)(authorization|api[-_ ]?key|token)\s*[:=]\s*\S+", r"\1=***", result)
    result = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", result)
    if len(result) > 300:
        result = result[:299] + "…"
    return result


def _hash_index(token: str, dim: int) -> int:
    """把 token 稳定映射到 [0, dim) 的维度下标。

    使用 blake2b 摘要的整数值取模；同一 token 在任何进程、任何机器上结果一致。
    """
    digest = hashlib.blake2b(token.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % dim


class HashingEmbeddings:
    """确定性哈希向量（教学用，不是语义模型）。

    实现方式：token -> blake2b 哈希 -> 维度下标，逐 token 累加计数，
    最后做 L2 归一化，使余弦相似度等价于点积。
    """

    def __init__(self, dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        """初始化哈希向量器。

        参数：
            dim: 向量维度，必须为正整数。

        异常：
            ValueError: dim 非法。
        """
        if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
            raise ValueError("dim 必须为正整数")
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文档文本。

        参数：
            texts: 文本列表。

        返回：
            list[list[float]]：与输入等长的归一化向量列表。
        """
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """向量化单条查询。

        参数：
            text: 查询文本。

        返回：
            list[float]：长度为 dim 的归一化向量；空文本返回全零向量。
        """
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        """内部实现：词袋计数后做 L2 归一化。"""
        vector = [0.0] * self.dim
        for token in tokenize(text or ""):
            vector[_hash_index(token, self.dim)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0.0:
            return [value / norm for value in vector]
        return vector


class OpenAICompatEmbeddings:
    """OpenAI 兼容端点的向量封装（可选，按需惰性创建客户端）。

    支持通过 base_url 指向 DeepSeek、DashScope、Ollama 等兼容服务；
    任何初始化或调用失败都由上层捕获并回退到 HashingEmbeddings。
    """

    def __init__(self, model: str, *, base_url: str | None, api_key: str | None, timeout: float = 60.0) -> None:
        """记录配置，延迟到首次调用才创建 langchain_openai 客户端。

        参数：
            model: embedding 模型名，例如 "text-embedding-3-small"。
            base_url: OpenAI 兼容端点；None 表示使用官方默认地址。
            api_key: API key；不会写入日志或索引。

        异常：
            ValueError: 模型名为空。
        """
        if not model or not model.strip():
            raise ValueError("embedding 模型名不能为空")
        self.model = model.strip()
        self.base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """惰性创建并缓存 OpenAIEmbeddings 客户端。"""
        if self._client is None:
            from langchain_openai import OpenAIEmbeddings

            kwargs: dict[str, Any] = {"model": self.model, "request_timeout": self._timeout, "max_retries": 1}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = OpenAIEmbeddings(**kwargs)
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文档文本。

        参数：
            texts: 文本列表。

        返回：
            list[list[float]]：服务端返回的向量列表。
        """
        client = self._get_client()
        vectors = client.embed_documents([str(text) for text in texts])
        return [[float(value) for value in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        """向量化单条查询。

        参数：
            text: 查询文本。

        返回：
            list[float]：服务端返回的查询向量。
        """
        client = self._get_client()
        vector = client.embed_query(str(text))
        return [float(value) for value in vector]


def _resolve_dim(dim: int) -> int:
    """校验并返回哈希维度；非法值回退默认维度并记录告警。"""
    if isinstance(dim, int) and not isinstance(dim, bool) and dim > 0:
        return dim
    logger.warning("embedding_dim 非法（%r），回退为 %d", dim, DEFAULT_EMBEDDING_DIM)
    return DEFAULT_EMBEDDING_DIM


def create_embeddings(settings: Settings) -> tuple[Any, dict[str, Any]]:
    """按配置选择向量实现，并在构造失败时回退到哈希向量。

    规则：
        - settings.embedding_model 为空 -> HashingEmbeddings（离线、确定性）。
        - 配置了远端模型且客户端可构造 -> OpenAICompatEmbeddings。
        - 构造失败（如缺少 key、依赖缺失）-> HashingEmbeddings，并写入 fallback_reason。
    说明：本函数不发起网络请求；真正的远端调用发生在 embed_documents/embed_query，
    因此构建索引时还会由 KnowledgeBase.build 捕获运行期失败并再次回退。

    参数：
        settings: 项目配置（含 embedding_model/base_url/api_key/embedding_dim）。

    返回：
        tuple[Any, dict]：(向量实现, 元信息)。元信息可安全写入索引，不含密钥。
    """
    dim = _resolve_dim(settings.embedding_dim)
    hashing_meta: dict[str, Any] = {
        "type": "hashing",
        "dim": dim,
        "model": None,
        "fallback_reason": None,
        "note": HASHING_NOTE,
    }

    model_name = (settings.embedding_model or "").strip()
    if not model_name:
        return HashingEmbeddings(dim), hashing_meta

    try:
        embeddings: Any = OpenAICompatEmbeddings(
            model=model_name,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            timeout=settings.request_timeout_s,
        )
    except Exception as exc:
        reason = redact_secret(
            f"{type(exc).__name__}: {exc}",
            settings.embedding_api_key,
            settings.api_key,
        )
        logger.warning("远端 embedding 初始化失败，回退 HashingEmbeddings：%s", reason)
        fallback_meta = dict(hashing_meta)
        fallback_meta["model"] = model_name
        fallback_meta["fallback_reason"] = reason
        return HashingEmbeddings(dim), fallback_meta

    return embeddings, {
        "type": "openai_compatible",
        "dim": None,
        "model": model_name,
        "fallback_reason": None,
        "note": REMOTE_NOTE,
    }
