"""混合检索记忆（路线图决策五：dense + 词面双路打分，图记忆留插槽）。

对应「三路混合检索」的离线简化版：
    score = lexical_weight × 词面余弦 + dense_weight × 向量余弦
- 词面：英文按词、中文按 bigram 的词袋余弦（复用 local.py）
- dense：EmbeddingBase 伪/真向量余弦（离线 mock 用 HashEmbedding）
- explain.score_details 同时给出两路分值 —— 检索从黑盒变白盒

用法：
    mem = HybridMemory(embedder=HashEmbedding(synonyms={"db": ["数据库"]}))
"""
from collections import Counter

from .embedding import cosine, tokenize
from .base import MemoryItem
from .local import InMemoryMemory, _cosine_tokens, _tokens


class HybridMemory(InMemoryMemory):
    """dense + lexical 双路混合检索；无 embedder 时退化为纯词面（优雅降级）。"""

    # 混合分值两路各归一化，阈值相应放低
    SIMILARITY_THRESHOLD = 0.02

    def __init__(self, embedder=None, dense_weight: float = 0.5,
                 lexical_weight: float = 0.5):
        super().__init__()
        self.embedder = embedder
        self.dense_weight = dense_weight
        self.lexical_weight = lexical_weight

    # ---- 写：入向量缓存（每条只算一次）----
    def add(self, text, scope="shared", task_id=None, metadata=None) -> str:
        mid = super().add(text, scope=scope, task_id=task_id, metadata=metadata)
        self._items[mid]["vec"] = (self.embedder.embed(text)
                                   if self.embedder else None)
        return mid

    # ---- 改：文本变了，向量跟着重算 ----
    def update(self, memory_id, text) -> None:
        super().update(memory_id, text)
        if self.embedder:
            self._items[memory_id]["vec"] = self.embedder.embed(text)

    # ---- 查：双路打分融合 ----
    def search(self, query, limit=5, explain=False) -> list[MemoryItem]:
        qt = Counter(_tokens(query))
        qv = self.embedder.embed(query) if self.embedder else None
        results: list[MemoryItem] = []
        for it in self._items.values():
            if it["deleted"]:
                continue
            lex = _cosine_tokens(qt, Counter(_tokens(it["text"])))
            vec = it.get("vec")
            dense = cosine(qv, vec) if (qv and vec) else 0.0
            score = (self.lexical_weight * lex
                     + (self.dense_weight * dense if qv else lex))
            if score < self.SIMILARITY_THRESHOLD:
                continue
            details = ({"lexical": round(lex, 4), "dense": round(dense, 4),
                        "score": round(score, 4)}
                       if explain else {})
            results.append(MemoryItem(it["id"], it["text"], it["scope"],
                                      it["task_id"], score, details))
        results.sort(key=lambda r: -r.score)
        return results[:limit]
