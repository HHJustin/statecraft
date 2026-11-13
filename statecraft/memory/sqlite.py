"""MemoryBase 的 SQLite 实现 —— 记忆从「进程内」变成「跨进程」。

与 InMemoryMemory 的差别只有一处是本质的：**进程重启后记忆还在**。
除此之外刻意保持一致（同样的 md5 去重、同样的阈值前置、同样的软删、
同样的 history 只增不改），这样切换后端不会悄悄改变系统行为。

三个实现细节：

1. **元数据过滤必须下推到 SQL**（`json_extract`），不能先捞全表再 Python
   过滤 —— 那是「先打分再筛」，正是 Mem0 明确反对的顺序，而且随着库变大
   会越来越慢。

2. **打分仍在 Python 里做**，用与 local.py 完全相同的分词/余弦实现。
   这不是偷懒：SQLite 没有内建分词器能把中文 bigram 和英文符号统一处理，
   而「换后端后打分规则变了」会让已有测试和黄金集失去意义。代价是候选集
   要全扫，所以用 `candidate_limit` 兜住最坏情况，并在 docstring 里说清
   这只是单机万级条目的量级。

3. **id 不依赖自增**。用 `m` + uuid 前缀：自增 id 在多进程/多库合并时
   会撞，而记忆条目是要跟着任务走的。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone

from ..storage.db import DEFAULT_DB, connect
from .base import MemoryBase, MemoryItem
from .local import _cosine_tokens, _tokens


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _where_sql(where: dict | None) -> tuple[str, list]:
    """把 where 编译成 SQL 片段（前置过滤，在打分之前生效）。"""
    clauses: list[str] = []
    args: list = []
    for key, expected in (where or {}).items():
        path = f'$."{key}"'
        values = list(expected) if isinstance(expected, (list, tuple, set)) else [expected]
        if not values:
            clauses.append("0")            # 空列表 = 谁都匹配不上
            continue
        placeholders = ",".join("?" for _ in values)
        clauses.append(f"json_extract(metadata, ?) IN ({placeholders})")
        args.append(path)
        args.extend(values)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), args


class SqliteMemory(MemoryBase):
    """把记忆条目落到 SQLite。与 InMemoryMemory 评分规则完全一致。"""

    SIMILARITY_THRESHOLD = 0.05

    def __init__(self, db_path=DEFAULT_DB, candidate_limit: int = 5000):
        self.conn = connect(db_path)
        self.candidate_limit = candidate_limit

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- 写
    def add(self, text, scope="shared", task_id=None, metadata=None) -> str:
        h = hashlib.md5(str(text).encode("utf-8")).hexdigest()
        row = self.conn.execute(
            "SELECT id FROM memory_items WHERE hash=? AND deleted=0", (h,)
        ).fetchone()
        if row:                                # 确定性去重，不调 LLM
            return row["id"]
        mid = "m" + uuid.uuid4().hex[:12]
        now = _now()
        self.conn.execute(
            "INSERT INTO memory_items (id, text, scope, task_id, metadata, hash, "
            "deleted, created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?)",
            (mid, str(text), scope, task_id,
             json.dumps(dict(metadata or {}), ensure_ascii=False), h, now, now))
        self.conn.execute(
            "INSERT INTO memory_history (memory_id, event, old, new, at) "
            "VALUES (?, 'ADD', NULL, ?, ?)", (mid, str(text), now))
        self.conn.commit()
        return mid

    # ---------------------------------------------------------------- 查
    def search(self, query, limit=5, explain=False, where=None) -> list[MemoryItem]:
        clause, wargs = _where_sql(where)
        rows = self.conn.execute(
            "SELECT * FROM memory_items WHERE deleted=0" + clause +
            " ORDER BY updated_at DESC LIMIT ?",
            (*wargs, self.candidate_limit)).fetchall()

        qt = Counter(_tokens(query))
        results: list[MemoryItem] = []
        for it in rows:
            tt = Counter(_tokens(it["text"]))
            score = _cosine_tokens(qt, tt)
            if score < self.SIMILARITY_THRESHOLD:
                continue                       # 阈值前置，不被其他信号救回
            details = ({"score": round(score, 4),
                        "matched_terms": sorted(set(qt) & set(tt))}
                       if explain else {})
            results.append(MemoryItem(it["id"], it["text"], it["scope"],
                                      it["task_id"], score, details,
                                      self._meta(it)))
        results.sort(key=lambda r: -r.score)
        return results[:limit]

    # ---------------------------------------------------------------- 改 / 删 / 史
    def update(self, memory_id, text) -> None:
        row = self.conn.execute(
            "SELECT text FROM memory_items WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        now = _now()
        self.conn.execute(
            "UPDATE memory_items SET text=?, hash=?, updated_at=?, deleted=0 "
            "WHERE id=?",
            (str(text), hashlib.md5(str(text).encode("utf-8")).hexdigest(), now,
             memory_id))
        self.conn.execute(
            "INSERT INTO memory_history (memory_id, event, old, new, at) "
            "VALUES (?, 'UPDATE', ?, ?, ?)", (memory_id, row["text"], str(text), now))
        self.conn.commit()

    def delete(self, memory_id) -> None:
        row = self.conn.execute(
            "SELECT text FROM memory_items WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        self.conn.execute("UPDATE memory_items SET deleted=1, updated_at=? WHERE id=?",
                          (_now(), memory_id))
        self.conn.execute(
            "INSERT INTO memory_history (memory_id, event, old, new, at) "
            "VALUES (?, 'DELETE', ?, NULL, ?)", (memory_id, row["text"], _now()))
        self.conn.commit()

    def history(self, memory_id) -> list[dict]:
        rows = self.conn.execute(
            "SELECT event, old, new, at FROM memory_history WHERE memory_id=? "
            "ORDER BY rowid", (memory_id,)).fetchall()
        return [{"event": r["event"], "old": r["old"], "new": r["new"], "at": r["at"]}
                for r in rows]

    # ---------------------------------------------------------------- 辅助
    @staticmethod
    def _meta(row) -> dict:
        try:
            return json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            return {}

    def __len__(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE deleted=0").fetchone()[0]
