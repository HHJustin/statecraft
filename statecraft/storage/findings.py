"""审查结果的持久化（P12）—— CRUD + 统计 + 派生索引维护。

三个设计决定，每个都对应一个会真实发生的问题：

1. **主键 = sha1(review_id|file|line)[:16]，不是自增 id。**
   路线图原本写的是 sha1(review_id+file+line+message)，但真机实测把
   `Finding.ensure_id()` 从「文件+行+类别+message」一路收紧到「文件+行」：
   模型换个措辞就把同一处缺陷报成三条。message 进哈希会让去重失效，
   所以这里同样只留「批次+文件+行」。

2. **冲突时 UPDATE 而不是 INSERT OR IGNORE。**
   形式审驳回后用新证据重新提交是正常流程（「证据写错了」不是死结）。
   OR IGNORE 会让那条永远停在旧的驳回状态上。

3. **FTS 文本由一条 SQL 表达式生成，重建和增量走同一个表达式。**
   如果增量写 Python、重建写另一段 SQL，「重建结果一致」就只是个愿望。
   见 `SEARCH_TEXT_SQL`。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .db import DEFAULT_DB, connect

# 派生检索文本：增量索引和全量重建共用，保证两条路径产出一致。
SEARCH_TEXT_SQL = (
    "file || ':' || line || ' [' || severity || '/' || category || '] ' || "
    "message || ' 证据：' || evidence || "
    "CASE WHEN suggestion IS NULL OR suggestion = '' THEN '' "
    "ELSE ' 建议：' || suggestion END"
)

REVIEW_STATUSES = ("draft", "executing", "reviewed", "done", "blocked")
FINDING_STATUSES = ("proposed", "confirmed", "disputed", "dismissed")

_TERM_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def finding_id(review_id: str, file: str, line: int) -> str:
    """幂等主键：同一批次里「文件+行」只可能有一条问题。"""
    return hashlib.sha1(f"{review_id}|{file}|{line}".encode("utf-8")).hexdigest()[:16]


def _row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


class ReviewStore:
    """一次审查批次的读写门面。同步、串行、无连接池 —— 审查本来就是串行的。"""

    def __init__(self, db_path=DEFAULT_DB, conn: sqlite3.Connection | None = None):
        self._owns = conn is None
        self.conn = conn or connect(db_path)

    def close(self) -> None:
        if self._owns:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 批次
    def start_review(self, *, repo_root, scope, task_id: str = "",
                     plan: str | None = None, review_id: str | None = None,
                     model: str | None = None) -> str:
        """开一批审查。

        不传 review_id 时每次都是新批次（保留历史）；传同一个 review_id
        重跑则覆盖同批次 —— 同一处问题不会因为跑了两遍变成两条。CI 里
        应该固定 review_id，人在本地看历史就让它自动生成。
        """
        rid = review_id or hashlib.sha1(
            f"{repo_root}|{','.join(sorted(map(str, scope)))}|{_now()}".encode("utf-8")
        ).hexdigest()[:12]
        self.conn.execute(
            "INSERT INTO reviews (id, task_id, repo_root, scope, plan, status, "
            "review_model, started_at) VALUES (?,?,?,?,?,'draft',?,?) "
            "ON CONFLICT(id) DO UPDATE SET status='draft', finished_at=NULL",
            (rid, task_id, str(repo_root), json.dumps(list(map(str, scope)),
                                                      ensure_ascii=False),
             plan, model, _now()),
        )
        self.conn.commit()
        return rid

    def set_plan(self, review_id: str, plan: str) -> None:
        self.conn.execute("UPDATE reviews SET plan=? WHERE id=?", (plan, review_id))
        self.conn.commit()

    def set_review_status(self, review_id: str, status: str) -> None:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"status 须为 {REVIEW_STATUSES}")
        self.conn.execute("UPDATE reviews SET status=? WHERE id=?", (status, review_id))
        self.conn.commit()

    def finish_review(self, review_id: str, status: str = "done",
                      plan: str | None = None) -> dict:
        """收尾：写终态 + 按 findings 重算统计。重复调用幂等。"""
        if status not in REVIEW_STATUSES:
            raise ValueError(f"status 须为 {REVIEW_STATUSES}")
        sets = "status=?, finished_at=?"
        args: list = [status, _now()]
        if plan is not None:
            sets += ", plan=?"
            args.append(plan)
        args.append(review_id)
        self.conn.execute(f"UPDATE reviews SET {sets} WHERE id=?", args)
        self.conn.commit()
        self.recount(review_id)
        return self.get_review(review_id)

    def recount(self, review_id: str) -> dict:
        """把三个统计列按 findings 重算。派生值必须有唯一的重算入口。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "       COALESCE(SUM(status='confirmed'), 0) AS confirmed, "
            "       COALESCE(SUM(status='disputed'), 0)  AS disputed "
            "FROM findings WHERE review_id=? AND form_ok=1", (review_id,)
        ).fetchone()
        vals = (row["total"], row["confirmed"], row["disputed"])
        self.conn.execute(
            "UPDATE reviews SET findings_total=?, findings_confirmed=?, "
            "findings_disputed=? WHERE id=?", (*vals, review_id))
        self.conn.commit()
        return {"total": vals[0], "confirmed": vals[1], "disputed": vals[2]}

    def get_review(self, review_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if r is None:
            return None
        d = _row(r)
        try:
            d["scope"] = json.loads(d["scope"])
        except (TypeError, ValueError):
            pass
        return d

    def list_reviews(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM reviews ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = _row(r)
            try:
                d["scope"] = json.loads(d["scope"])
            except (TypeError, ValueError):
                pass
            out.append(d)
        return out

    # ------------------------------------------------------------ findings
    def record(self, review_id: str, *, file: str, line: int, severity: str,
               category: str, message: str, evidence: str, suggestion: str = "",
               status: str = "proposed", form_ok: bool = False,
               evidence_ok: bool = False, audit_ok: bool = False,
               critic_note: str = "") -> str:
        """写入/更新一条 finding（幂等）。

        重复写入同一个 (review_id, file, line) 只会更新内容，不新增行 ——
        模型并行发多次 tool_call 是常态，靠内容哈希天然去重。
        """
        if status not in FINDING_STATUSES:
            raise ValueError(f"status 须为 {FINDING_STATUSES}")
        fid = finding_id(review_id, file, line)
        now = _now()
        self.conn.execute(
            "INSERT INTO findings (id, review_id, file, line, severity, category, "
            "message, evidence, suggestion, status, form_ok, evidence_ok, audit_ok, "
            "critic_note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  severity=excluded.severity, category=excluded.category, "
            "  message=excluded.message, evidence=excluded.evidence, "
            "  suggestion=excluded.suggestion, status=excluded.status, "
            "  form_ok=excluded.form_ok, evidence_ok=excluded.evidence_ok, "
            "  audit_ok=excluded.audit_ok, critic_note=excluded.critic_note, "
            "  updated_at=excluded.updated_at",
            (fid, review_id, str(file), int(line), severity, category, message,
             evidence, suggestion or "", status, int(bool(form_ok)),
             int(bool(evidence_ok)), int(bool(audit_ok)), critic_note, now, now),
        )
        self._index_one(fid)
        self.conn.commit()
        return fid

    def set_status(self, fid: str, status: str, note: str = "") -> None:
        if status not in FINDING_STATUSES:
            raise ValueError(f"status 须为 {FINDING_STATUSES}")
        self.conn.execute(
            "UPDATE findings SET status=?, critic_note=?, updated_at=? WHERE id=?",
            (status, note, _now(), fid))
        self.conn.commit()
        # status 不进检索文本，无需重建索引

    def finding(self, fid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
        return _row(r) if r else None

    def findings(self, review_id: str | None = None, *, severity: str | None = None,
                 status: str | None = None, file: str | None = None,
                 form_ok: bool | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM findings WHERE 1=1"
        args: list = []
        for col, val in (("review_id", review_id), ("severity", severity),
                         ("status", status), ("file", file)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        if form_ok is not None:
            sql += " AND form_ok=?"
            args.append(int(form_ok))
        sql += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 " \
               "ELSE 2 END, file, line LIMIT ?"
        args.append(limit)
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def files(self, review_id: str | None = None,
              form_ok: bool | None = True) -> list[str]:
        """本批次（或全库）出现过的文件路径。

        问答层要回答「auth.py 有哪些问题」，就得先把用户嘴里的 `auth.py`
        映射到库里真实的 `src/auth.py` —— 猜测路径前缀是错的，查一遍才对。
        """
        sql = "SELECT DISTINCT file FROM findings WHERE 1=1"
        args: list = []
        if review_id:
            sql += " AND review_id=?"
            args.append(review_id)
        if form_ok is not None:
            sql += " AND form_ok=?"
            args.append(int(form_ok))
        sql += " ORDER BY file"
        return [r["file"] for r in self.conn.execute(sql, args).fetchall()]

    def latest_review(self, only_finished: bool = True) -> dict | None:
        """最新批次。结构化查询默认只查它，理由见 qa/query.py 的说明。"""
        sql = "SELECT * FROM reviews"
        if only_finished:
            sql += " WHERE status IN ('done','reviewed')"
        sql += " ORDER BY started_at DESC LIMIT 1"
        r = self.conn.execute(sql).fetchone()
        return self.get_review(r["id"]) if r else None

    def count(self, review_id: str | None = None, *, severity: str | None = None,
              file: str | None = None, form_ok: bool | None = True) -> int:
        """精确计数（不用 len(list)：列表有 limit，计数不该被截断）。"""
        sql = "SELECT COUNT(*) AS n FROM findings WHERE 1=1"
        args: list = []
        if review_id:
            sql += " AND review_id=?"
            args.append(review_id)
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        if file:
            sql += " AND file=?"
            args.append(file)
        if form_ok is not None:
            sql += " AND form_ok=?"
            args.append(int(form_ok))
        return self.conn.execute(sql, args).fetchone()["n"]

    def counts_by_severity(self, review_id: str | None = None, *,
                           severity: str | None = None, file: str | None = None,
                           form_ok: bool | None = True) -> dict[str, int]:
        """严重度分布。

        `form_ok=True`（默认）只统计通过形式审的条目 —— 否则报告说「5 条」，
        分布加起来却是 6（那条被驳回的编造条目也算进去了），两个数字对不上
        比数字不准更糟。要看幻觉率就显式传 form_ok=False。
        """
        sql = "SELECT severity, COUNT(*) AS n FROM findings WHERE 1=1"
        args: list = []
        if review_id:
            sql += " AND review_id=?"
            args.append(review_id)
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        if file:
            sql += " AND file=?"
            args.append(file)
        if form_ok is not None:
            sql += " AND form_ok=?"
            args.append(int(form_ok))
        sql += " GROUP BY severity"
        return {r["severity"]: r["n"] for r in self.conn.execute(sql, args).fetchall()}

    def counts_by_file(self, review_id: str | None = None,
                       severity: str | None = None,
                       form_ok: bool | None = True) -> list[dict]:
        sql = "SELECT file, COUNT(*) AS n FROM findings WHERE 1=1"
        args: list = []
        if review_id:
            sql += " AND review_id=?"
            args.append(review_id)
        if severity:
            sql += " AND severity=?"
            args.append(severity)
        if form_ok is not None:
            sql += " AND form_ok=?"
            args.append(int(form_ok))
        sql += " GROUP BY file ORDER BY n DESC, file"
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ 检索（派生视图）
    def _index_one(self, fid: str) -> None:
        self.conn.execute("DELETE FROM findings_fts WHERE finding_id=?", (fid,))
        self.conn.execute(
            f"INSERT INTO findings_fts(finding_id, text) "
            f"SELECT id, {SEARCH_TEXT_SQL} FROM findings WHERE id=?", (fid,))

    def rebuild_index(self) -> int:
        from .db import rebuild_fts
        return rebuild_fts(self.conn)

    def search(self, query: str, limit: int = 5,
               review_id: str | None = None) -> list[dict]:
        """按符号名/关键词找历史 finding。

        FTS5 处理英文符号很准（`average`、`Session`、`pass`），中文不分词
        命中率低，所以 FTS 无结果时退回 LIKE 兜底 —— 宁慢不空。
        """
        q = (query or "").strip()
        if not q:
            return []
        terms = [t for t in _TERM_RE.findall(q) if t.isascii()]
        rows: list = []
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            sql = ("SELECT f.*, bm25(findings_fts) AS rank FROM findings_fts "
                   "JOIN findings f ON f.id = findings_fts.finding_id "
                   "WHERE findings_fts MATCH ?")
            args: list = [match]
            if review_id:
                sql += " AND f.review_id=?"
                args.append(review_id)
            sql += " ORDER BY rank LIMIT ?"
            args.append(limit)
            try:
                rows = [_row(r) for r in self.conn.execute(sql, args).fetchall()]
            except sqlite3.OperationalError:      # 查询串被 FTS 语法拒了，走兜底
                rows = []
        if not rows:
            like = f"%{q}%"
            sql = ("SELECT * FROM findings WHERE (message LIKE ? OR evidence LIKE ? "
                   "OR file LIKE ?)")
            args = [like, like, like]
            if review_id:
                sql += " AND review_id=?"
                args.append(review_id)
            sql += " ORDER BY line LIMIT ?"
            args.append(limit)
            rows = [_row(r) for r in self.conn.execute(sql, args).fetchall()]
        return rows

    # ------------------------------------------------------------ 清白声明
    def add_clean_claim(self, review_id: str, file: str, agent: str,
                        reason: str) -> None:
        self.conn.execute(
            "INSERT INTO clean_claims (review_id, file, agent, reason, claimed_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(review_id, file) DO UPDATE SET "
            "agent=excluded.agent, reason=excluded.reason, claimed_at=excluded.claimed_at",
            (review_id, str(file), agent, reason, _now()))
        self.conn.commit()

    def clean_claims(self, review_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM clean_claims"
        args: list = []
        if review_id:
            sql += " WHERE review_id=?"
            args.append(review_id)
        sql += " ORDER BY file"
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]
