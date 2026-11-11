"""Embedding Provider 协议 + 离线实现（源自 Mem0 决策一：抽象基类只定最小协议）。

设计要点：
- EmbeddingBase 与 LLMBase 同构：治理层/记忆层只认协议，不认具体厂商
- HashEmbedding：确定性哈希伪向量（hashing trick），零依赖、离线可测、结果可复现
  —— mock 定位明确写死：真实场景把 EmbeddingBase 换成 API 调用（openai/bge 等）
- 可选同义词扩展表：给伪向量一点「伪语义」能力，用于离线演示 dense 召回的价值
"""
import hashlib
import re
from abc import ABC, abstractmethod

# 与 local.py 相同的分词口径：英文按词，中文按 bigram
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for chunk in _TOKEN_RE.findall(text.lower()):
        if chunk.isascii():
            out.append(chunk)
        elif len(chunk) == 1:
            out.append(chunk)
        else:
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return out


def cosine(a: list[float], b: list[float]) -> float:
    """两已归一化向量的余弦（未归一化也能算，代价是分母兜底 1.0）。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(y * y for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


class EmbeddingBase(ABC):
    dim: int

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """文本 → 定长向量（调用方约定 L2 归一化）。"""
        raise NotImplementedError


class HashEmbedding(EmbeddingBase):
    """哈希伪向量（离线 mock）：token/bigram → 多桶哈希累加 → L2 归一化。

    - 确定性：同一文本永远得到同一向量（测试可断言）
    - synonyms：同义词扩展表（双向注入），让「数据库」与「DB」向量接近
      —— 这是 mock 的伪语义；真语义请接真实 embedding API
    """

    def __init__(self, dim: int = 128,
                 synonyms: dict[str, list[str]] | None = None):
        self.dim = dim
        self.synonyms = {k: v for k, v in (synonyms or {}).items()}
        # 同义词表双向化：db→[数据库] 自动补 数据库→[db]
        for k, vs in list(self.synonyms.items()):
            for v in vs:
                self.synonyms.setdefault(v, [])
                if k not in self.synonyms[v]:
                    self.synonyms[v].append(k)

    def embed(self, text: str) -> list[float]:
        low = (text or "").lower()
        # 同义词扩展在原始文本层做（中文 bigram 切分后词边界已破坏，不能按 token 匹配）
        extra: list[str] = []
        for k, vs in self.synonyms.items():
            if k in low:
                extra.extend(vs)
        tokens = tokenize(low) + tokenize(" ".join(extra))
        vec = [0.0] * self.dim
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0                       # 主桶
            vec[(h >> 32) % self.dim] += 0.5               # 副桶（降权）
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [x / norm for x in vec]
