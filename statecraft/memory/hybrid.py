"""混合检索记忆（路线图决策五：dense + 词面 + 图的「三路混合检索」）。

    score = lexical_weight × 词面余弦
          + dense_weight   × 向量余弦
          + graph_weight   × 结构关系加成（仅代码场景，接入 SymbolIndex 时生效）

- 词面：英文按词、中文按 bigram 的词袋余弦（复用 local.py）
- dense：EmbeddingBase 伪/真向量余弦（离线 mock 用 HashEmbedding）
- graph：代码符号图跳数加成（codegraph.SymbolIndex：直接命中 1.0 / 邻居 0.5）
  —— 这是 Mem0 第三路（实体图）在代码场景的对应物，但实体来自 AST 而非 NLP
- explain.score_details 同时给出三路分值 —— 检索从黑盒变白盒

两路 vs 三路：
    不传 graph_index → 权重默认 0.5 / 0.5（向后兼容原有双路行为）
    传入 graph_index → 权重默认 0.4 / 0.4 / 0.2（自动让位给第三路）

用法：
    mem = HybridMemory(embedder=HashEmbedding(synonyms={"db": ["数据库"]}))
    mem = HybridMemory(embedder=..., graph_index=SymbolIndex(code_graph))
"""
from collections import Counter

from .base import MemoryItem
from .embedding import cosine
from .local import InMemoryMemory, _cosine_tokens, _match_metadata, _tokens


class HybridMemory(InMemoryMemory):
    """dense + lexical (+ graph) 混合检索；无 embedder 时退化为纯词面（优雅降级）。"""

    # 混合分值多路各归一化，阈值相应放低
    SIMILARITY_THRESHOLD = 0.02

    def __init__(self, embedder=None, dense_weight: float | None = None,
                 lexical_weight: float | None = None,
                 graph_index=None, graph_weight: float | None = None):
        super().__init__()
        self.embedder = embedder
        self.graph_index = graph_index
        # 权重默认值随「是否接入第三路」自动切换，显式传入则一律以传入为准
        if graph_index is not None:
            self.dense_weight = 0.4 if dense_weight is None else dense_weight
            self.lexical_weight = 0.4 if lexical_weight is None else lexical_weight
            self.graph_weight = 0.2 if graph_weight is None else graph_weight
        else:
            self.dense_weight = 0.5 if dense_weight is None else dense_weight
            self.lexical_weight = 0.5 if lexical_weight is None else lexical_weight
            self.graph_weight = 0.0 if graph_weight is None else graph_weight

    # ---- 写：入向量缓存 + 符号反查链（各只算一次）----
    def add(self, text, scope="shared", task_id=None, metadata=None) -> str:
        mid = super().add(text, scope=scope, task_id=task_id, metadata=metadata)
        self._items[mid]["vec"] = (self.embedder.embed(text)
                                   if self.embedder else None)
        if self.graph_index is not None:
            self.graph_index.link_from_metadata(mid, metadata)
        return mid

    # ---- 改：文本变了，向量跟着重算 ----
    def update(self, memory_id, text) -> None:
        super().update(memory_id, text)
        if self.embedder:
            self._items[memory_id]["vec"] = self.embedder.embed(text)

    # ---- 查：多路打分融合 ----
    def search(self, query, limit=5, explain=False, where=None) -> list[MemoryItem]:
        qt = Counter(_tokens(query))
        qv = self.embedder.embed(query) if self.embedder else None
        qsyms = (self.graph_index.query_symbols(query)
                 if self.graph_index is not None else set())
        results: list[MemoryItem] = []
        for it in self._items.values():
            if it["deleted"]:
                continue
            if not _match_metadata(it.get("metadata"), where):
                continue                          # 前置过滤：不参与打分
            lex = _cosine_tokens(qt, Counter(_tokens(it["text"])))
            vec = it.get("vec")
            dense = cosine(qv, vec) if (qv and vec) else 0.0
            graph = (self.graph_index.score(qsyms, it["id"])
                     if self.graph_index is not None else 0.0)
            if qv:
                score = (self.lexical_weight * lex
                         + self.dense_weight * dense
                         + self.graph_weight * graph)
            else:                                  # 无 embedder：退化纯词面 + 图
                score = lex + self.graph_weight * graph
            if score < self.SIMILARITY_THRESHOLD:
                continue
            details = ({"lexical": round(lex, 4), "dense": round(dense, 4),
                        "graph": round(graph, 4), "score": round(score, 4),
                        "query_symbols": sorted(qsyms)}
                       if explain else {})
            results.append(MemoryItem(it["id"], it["text"], it["scope"],
                                      it["task_id"], score, details,
                                      dict(it.get("metadata") or {})))
        results.sort(key=lambda r: -r.score)
        return results[:limit]
