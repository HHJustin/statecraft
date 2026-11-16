"""函数级审查缓存 —— 「同样的代码不审第二遍」（P14）。

三个必须说清的口径：

1. **键是 (file, qualname)，哈希是「必须一并比对的列」，不是主键。**
   函数改回旧版本时，旧哈希的结论已经对不上了（行号可能变了），
   按哈希当主键会翻出一条过期结论，命中即错误。所以只留最新一行。

2. **只承认「已完成的批次」的结论。**
   一次 Blocked 的审查可能只审了半个函数，它的 `clean` 不是结论而是
   中断现场。`get()` 会 join reviews 把这类挡掉 —— 缓存不能比真相源乐观。

3. **命中「审过且有问题」时要把问题带回来，不能只是跳过。**
   否则全仓重跑会得到一份「上次报的问题这次全消失」的报告，那不是省事，
   是数据丢失。带回时按 `offset`（行相对函数起点的偏移）重新定位，
   并**逐条核对证据是否真在那行** —— 哈希相同说明函数体没变，
   但整份文件可能上下挪过，偏移重算错位时会露出马脚。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .units import CodeUnit

# 只信这两个终态批次；draft/executing/blocked 的结论是「中断现场」而非结论
_TRUSTED = ("done", "reviewed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


@dataclass
class CachedUnit:
    file: str
    qualname: str
    body_hash: str
    review_id: str
    status: str                                   # clean / findings / error
    payload: list[dict]
    updated_at: str
    trusted: bool = True                          # 来源批次是否已完成

    @property
    def reusable(self) -> bool:
        """可复用的条件：来源批次可信 + 上次确实给了结论。"""
        return self.trusted and self.status in ("clean", "findings")


class UnitCache:
    """`unit_cache` 表的读写门面。拿 ReviewStore 的连接用，不另开库。"""

    def __init__(self, store):
        self.store = store
        self.conn = getattr(store, "conn", store)

    # ------------------------------------------------------------ 读
    def get(self, file: str, qualname: str) -> CachedUnit | None:
        row = self.conn.execute(
            "SELECT u.*, COALESCE(r.status, '') AS review_status "
            "FROM unit_cache u LEFT JOIN reviews r ON r.id = u.review_id "
            "WHERE u.file=? AND u.qualname=?", (file, qualname)).fetchone()
        if row is None:
            return None
        payload = []
        try:
            payload = json.loads(row["payload"] or "[]")
        except (TypeError, ValueError):
            payload = []
        return CachedUnit(file=row["file"], qualname=row["qualname"],
                          body_hash=row["body_hash"], review_id=row["review_id"],
                          status=row["status"], payload=payload,
                          updated_at=row["updated_at"],
                          trusted=row["review_status"] in _TRUSTED)

    def hit(self, unit: CodeUnit) -> CachedUnit | None:
        """哈希一致且可复用才算命中 —— 这是「跳过」的唯一依据。"""
        c = self.get(unit.file, unit.qualname)
        if c is None or c.body_hash != unit.body_hash or not c.reusable:
            return None
        return c

    def replay(self, unit: CodeUnit, cached: CachedUnit,
               source_lines: list[str] | None = None) -> tuple[list[dict], list[str]]:
        """把缓存里的 findings 重定位到当前单位上。

        返回 (可用的 findings, 被丢弃的原因列表)。丢弃而不是硬塞：
        一条对不上原文的 finding 比少一条更危险。
        """
        kept: list[dict] = []
        dropped: list[str] = []
        for item in cached.payload:
            offset = item.get("offset")
            if not isinstance(offset, int):
                dropped.append(f"{cached.qualname}: 缓存条目缺 offset，无法定位")
                continue
            line = unit.start + offset
            ev = item.get("evidence") or ""
            if source_lines is not None and ev:
                actual = source_lines[line - 1] if 0 < line <= len(source_lines) else ""
                if _norm(ev) and _norm(ev) not in _norm(actual) and \
                        _norm(actual) not in _norm(ev):
                    dropped.append(
                        f"{cached.qualname} @{line}: 证据与当前源码不符（缓存已过期）")
                    continue
            kept.append({**item, "line": line, "cached_from": cached.review_id})
        return kept, dropped

    # ------------------------------------------------------------ 写
    def put(self, file: str, qualname: str, body_hash: str, review_id: str,
            status: str, findings: list[dict] | None = None,
            unit_start: int = 1) -> None:
        if status not in ("clean", "findings", "error"):
            raise ValueError("status 须为 clean/findings/error")
        payload = [{**f, "offset": int(f.get("line", unit_start)) - unit_start}
                   for f in (findings or [])]
        self.conn.execute(
            "INSERT INTO unit_cache (file, qualname, body_hash, review_id, status, "
            "n_findings, payload, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file, qualname) DO UPDATE SET "
            "  body_hash=excluded.body_hash, review_id=excluded.review_id, "
            "  status=excluded.status, n_findings=excluded.n_findings, "
            "  payload=excluded.payload, updated_at=excluded.updated_at",
            (file, qualname, body_hash, review_id, status, len(payload),
             json.dumps(payload, ensure_ascii=False), _now()))
        self.conn.commit()

    def record_verdicts(self, review_id: str, units: dict[str, list[CodeUnit]],
                        verdicts: dict[tuple[str, str], str],
                        findings: list[dict]) -> int:
        """把本次审查的函数级结论落缓存，返回写入条数。

        只写**有结论**的单位：既没被报问题、也没有显式清白声明的单位等于
        「没审到」，写进缓存会让下一次直接跳过它 —— 那才是真正危险的 bug。
        """
        n = 0
        for file, items in units.items():
            for u in items:
                verdict = verdicts.get((file, u.qualname))
                if verdict is None:
                    continue
                mine = [f for f in findings
                        if f.get("file") == file and u.start <= int(f.get("line", 0)) <= u.end]
                if verdict == "clean" and mine:
                    verdict = "findings"           # 声明清白却又报了问题：以问题为准
                self.put(file, u.qualname, u.body_hash, review_id, verdict,
                         mine if verdict == "findings" else [], unit_start=u.start)
                n += 1
        return n

    # ------------------------------------------------------------ 统计
    def stats(self, review_id: str | None = None) -> dict:
        sql = ("SELECT status, COUNT(*) AS n FROM unit_cache"
               + (" WHERE review_id=?" if review_id else "") + " GROUP BY status")
        rows = self.conn.execute(sql, (review_id,) if review_id else ()).fetchall()
        out = {"clean": 0, "findings": 0, "error": 0}
        for r in rows:
            out[r["status"]] = r["n"]
        return out
