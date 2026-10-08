"""内存版记忆实现（阶段 4 基础版）。

照抄 Mem0 的四个精髓：
1. MD5 确定性去重 —— 去重靠哈希，不靠 LLM
2. 阈值前置 —— 低于相关性阈值直接丢弃，不给加权机会
3. 打分可解释 —— explain=True 返回 score_details
4. history 版本化 —— 只增不改（old/new/event），软删除
"""
import hashlib
import itertools
import math
import re
from collections import Counter
from datetime import datetime, timezone

from .base import MemoryBase, MemoryItem

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+")


def _tokens(text: str) -> list[str]:
    """英文按词，中文按 bigram（单字命中噪声太大）。"""
    out: list[str] = []
    for chunk in _TOKEN_RE.findall(text.lower()):
        if chunk.isascii():
            out.append(chunk)
        elif len(chunk) == 1:
            out.append(chunk)
        else:
            out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryMemory(MemoryBase):
    SIMILARITY_THRESHOLD = 0.05   # 阈值前置：语义分不过线直接淘汰

    def __init__(self):
        self._items: dict[str, dict] = {}
        self._hist: dict[str, list[dict]] = {}
        self._seq = itertools.count(1)

    # ---- 写 ----
    def add(self, text, scope="shared", task_id=None, metadata=None) -> str:
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        for it in self._items.values():          # 确定性去重
            if it["hash"] == h and not it["deleted"]:
                return it["id"]
        mid = f"m{next(self._seq)}"
        self._items[mid] = {
            "id": mid, "text": text, "scope": scope, "task_id": task_id,
            "metadata": dict(metadata or {}), "hash": h, "deleted": False,
            "created_at": _now(), "updated_at": _now(),
        }
        self._hist[mid] = [{"event": "ADD", "old": None, "new": text, "at": _now()}]
        return mid

    # ---- 查 ----
    def search(self, query, limit=5, explain=False) -> list[MemoryItem]:
        qt = Counter(_tokens(query))
        results: list[MemoryItem] = []
        for it in self._items.values():
            if it["deleted"]:
                continue
            tt = Counter(_tokens(it["text"]))
            common = set(qt) & set(tt)
            score = 0.0
            if common:
                dot = sum(qt[w] * tt[w] for w in common)
                denom = math.sqrt(sum(qt.values()) * sum(tt.values())) or 1.0
                score = dot / denom
            if score < self.SIMILARITY_THRESHOLD:
                continue                          # 一票否决，不被其他信号救回
            details = ({"score": round(score, 4),
                        "matched_terms": sorted(common)}
                       if explain else {})
            results.append(MemoryItem(it["id"], it["text"], it["scope"],
                                      it["task_id"], score, details))
        results.sort(key=lambda r: -r.score)
        return results[:limit]

    # ---- 改 / 删 / 史 ----
    def update(self, memory_id, text) -> None:
        it = self._items[memory_id]
        old = it["text"]
        it["text"] = text
        it["hash"] = hashlib.md5(text.encode("utf-8")).hexdigest()
        it["updated_at"] = _now()                 # created_at 保留不动
        self._hist[memory_id].append({"event": "UPDATE", "old": old,
                                      "new": text, "at": _now()})

    def delete(self, memory_id) -> None:
        it = self._items[memory_id]
        it["deleted"] = True                      # 软删，物理数据仍在
        self._hist[memory_id].append({"event": "DELETE", "old": it["text"],
                                      "new": None, "at": _now()})

    def history(self, memory_id) -> list[dict]:
        return list(self._hist[memory_id])

    # ---- 调试辅助 ----
    def __len__(self) -> int:
        return sum(1 for it in self._items.values() if not it["deleted"])
