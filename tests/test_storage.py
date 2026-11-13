"""持久层（P12）测试：schema / 幂等 / 统计 / 检索 / 记忆落盘。

这里刻意测四类容易「看起来对、其实不对」的东西：
1. **幂等**：同一批次的同一处问题写两次，行数不能翻倍；
2. **口径一致**：`findings_total` 与严重度分布必须能对上（被驳回的条目不进分布）；
3. **索引可重建**：删掉 `findings_fts` 重建后，检索结果必须一模一样；
4. **跨进程**：换一个连接对象，数据里还在。
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "examples" / "code-review"
for p in (str(ROOT), str(REVIEW_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from statecraft.memory.factory import MemoryFactory                    # noqa: E402
from statecraft.memory.sqlite import SqliteMemory                       # noqa: E402
from statecraft.storage import ReviewStore, finding_id                  # noqa: E402
from statecraft.storage import db as sdb                                # noqa: E402


class _DB(unittest.TestCase):
    """每个用例一个独立库文件（不用 :memory:，要验证真的落了盘）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "t.db")
        self.store = ReviewStore(self.path)
        self.addCleanup(self.store.close)

    def memory(self) -> SqliteMemory:
        """建一个 SqliteMemory 并登记关闭（Windows 上不关会锁住临时文件）。"""
        mem = SqliteMemory(self.path)
        self.addCleanup(mem.close)
        return mem

    def _finding(self, rid, **kw):
        base = dict(file="a.py", line=23, severity="high", category="boundary",
                    message="空列表除零", evidence="return sum(v) / len(v)",
                    form_ok=True, evidence_ok=True, audit_ok=True)
        base.update(kw)
        return self.store.record(rid, **base)


class TestSchema(_DB):
    def test_tables_and_version(self):
        conn = self.store.conn
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for t in ("reviews", "findings", "findings_fts", "clean_claims",
                  "memory_items", "memory_history"):
            self.assertIn(t, names)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         sdb.SCHEMA_VERSION)

    def test_init_is_idempotent(self):
        for _ in range(3):
            self.assertEqual(sdb.initialize(self.store.conn), sdb.SCHEMA_VERSION)

    def test_newer_schema_is_rejected(self):
        """库比代码新时必须报错，而不是带着缺列继续跑。"""
        tmp = str(Path(self._tmp.name) / "future.db")
        conn = sdb.connect(tmp)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        fresh = sdb.connect(tmp, init=False)      # 绕过进程内「已建表」缓存
        with self.assertRaises(RuntimeError):
            sdb.initialize(fresh)
        fresh.close()


class TestReviewBatch(_DB):
    def test_start_and_read_back(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py", "b.py"],
                                      task_id="t1", plan="先看边界", model="mock")
        rv = self.store.get_review(rid)
        self.assertEqual(rv["scope"], ["a.py", "b.py"])     # JSON 往返
        self.assertEqual(rv["status"], "draft")
        self.assertEqual(rv["plan"], "先看边界")
        self.assertIsNone(rv["finished_at"])

    def test_same_review_id_reuses_row(self):
        rid1 = self.store.start_review(repo_root="/repo", scope=["a.py"],
                                       review_id="fixed")
        rid2 = self.store.start_review(repo_root="/repo", scope=["a.py"],
                                       review_id="fixed")
        self.assertEqual(rid1, rid2)
        self.assertEqual(len(self.store.list_reviews()), 1)

    def test_finish_computes_stats(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid, line=10)
        self._finding(rid, line=20)
        self._finding(rid, line=30, severity="low")
        self.store.set_status(finding_id(rid, "a.py", 10), "confirmed")
        self.store.set_status(finding_id(rid, "a.py", 20), "disputed", "证据不符")
        rv = self.store.finish_review(rid, "done")
        self.assertEqual(rv["status"], "done")
        self.assertEqual(rv["findings_total"], 3)
        self.assertEqual(rv["findings_confirmed"], 1)
        self.assertEqual(rv["findings_disputed"], 1)
        self.assertIsNotNone(rv["finished_at"])

    def test_blocked_keeps_findings(self):
        """Blocked 的产出恰恰最该留档——真机实测里它们曾因重跑被冲掉。"""
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid)
        rv = self.store.finish_review(rid, "blocked")
        self.assertEqual(rv["status"], "blocked")
        self.assertEqual(rv["findings_total"], 1)

    def test_bad_status_rejected(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        with self.assertRaises(ValueError):
            self.store.finish_review(rid, "finish")


class TestFindings(_DB):
    def test_record_and_read(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        fid = self._finding(rid)
        row = self.store.finding(fid)
        self.assertEqual(row["file"], "a.py")
        self.assertEqual(row["line"], 23)
        self.assertEqual(row["status"], "proposed")
        self.assertEqual(row["form_ok"], 1)

    def test_duplicate_does_not_double(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        a = self._finding(rid)
        b = self._finding(rid, message="换个说法，还是同一行")
        self.assertEqual(a, b)
        self.assertEqual(len(self.store.findings(rid)), 1)

    def test_resubmit_updates_content(self):
        """形式审驳回后带着正确证据重新提交，必须能覆盖（不能是死结）。"""
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        fid = self._finding(rid, evidence="编造的一行", form_ok=False, evidence_ok=False)
        self._finding(rid, evidence="return sum(v) / len(v)", form_ok=True)
        row = self.store.finding(fid)
        self.assertEqual(row["evidence"], "return sum(v) / len(v)")
        self.assertEqual(row["form_ok"], 1)

    def test_rejected_rows_kept_for_hallucination_rate(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid, line=99, form_ok=False, evidence_ok=False)
        self.assertEqual(len(self.store.findings(rid, form_ok=False)), 1)

    def test_constraints(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        with self.assertRaises(sqlite3.IntegrityError):        # 严重度枚举
            self._finding(rid, severity="critical")
        with self.assertRaises(sqlite3.IntegrityError):        # 行号约束
            self._finding(rid, line=0)
        with self.assertRaises(sqlite3.IntegrityError):        # 外键：批次不存在
            self._finding("no-such-review")

    def test_counts_consistent_with_total(self):
        """报告说 5 条、分布加起来 6 —— 这种不一致比数字不准更糟。"""
        rid = self.store.start_review(repo_root="/repo", scope=["a.py", "b.py"])
        self._finding(rid, line=1, severity="high")
        self._finding(rid, line=2, severity="high")
        self._finding(rid, line=3, severity="medium", file="b.py")
        self._finding(rid, line=4, severity="low", form_ok=False, evidence_ok=False)
        rv = self.store.finish_review(rid)
        dist = self.store.counts_by_severity(rid)
        self.assertEqual(sum(dist.values()), rv["findings_total"])
        self.assertEqual(dist, {"high": 2, "medium": 1})
        # 显式要全部时才是 4 条
        self.assertEqual(sum(self.store.counts_by_severity(rid, form_ok=None).values()), 4)
        self.assertEqual(self.store.counts_by_file(rid, severity="high"),
                         [{"file": "a.py", "n": 2}])


class TestSearch(_DB):
    def test_fts_hits_symbol(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid, message="average() 对空列表直接除零")
        self._finding(rid, line=40, message="open() 未关闭文件句柄")
        hits = self.store.search("average")
        self.assertEqual(len(hits), 1)
        self.assertIn("average", hits[0]["message"])

    def test_chinese_falls_back_to_like(self):
        """FTS5 不分中文，纯中文查询要能不空手而归。"""
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid, message="异常被静默吞掉")
        self.assertTrue(self.store.search("静默"))

    def test_search_scoped_by_review(self):
        r1 = self.store.start_review(repo_root="/repo", scope=["a.py"], review_id="r1")
        r2 = self.store.start_review(repo_root="/repo", scope=["a.py"], review_id="r2")
        self._finding(r1, message="average 除零")
        self._finding(r2, message="average 除零")
        self.assertEqual(len(self.store.search("average", review_id=r1)), 1)

    def test_rebuild_fts_gives_same_result(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid, message="average 除零")
        self._finding(rid, line=40, message="session 未失效")
        before = [r["id"] for r in self.store.search("average OR session", limit=10)]
        self.assertEqual(self.store.rebuild_index(), 2)
        after = [r["id"] for r in self.store.search("average OR session", limit=10)]
        self.assertEqual(before, after)

    def test_rebuild_after_dropping_index(self):
        """索引坏了不修，直接重建。"""
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self._finding(rid, message="average 除零")
        self.store.conn.execute("DROP TABLE findings_fts")
        self.store.conn.execute(
            "CREATE VIRTUAL TABLE findings_fts USING fts5(finding_id UNINDEXED, text)")
        self.assertEqual(len(self.store.search("average")), 1)   # 空索引 → LIKE 兜底
        self.assertEqual(self.store.rebuild_index(), 1)
        self.assertEqual(len(self.store.search("average")), 1)


class TestCleanClaims(_DB):
    def test_claim_upsert(self):
        rid = self.store.start_review(repo_root="/repo", scope=["a.py"])
        self.store.add_clean_claim(rid, "a.py", "reviewer", "查了五类问题，均不构成缺陷")
        self.store.add_clean_claim(rid, "a.py", "reviewer", "复看一遍，确认无问题")
        claims = self.store.clean_claims(rid)
        self.assertEqual(len(claims), 1)
        self.assertIn("复看", claims[0]["reason"])


class TestSqliteMemory(_DB):
    def test_add_dedup_and_search(self):
        mem = self.memory()
        a = mem.add("函数 average 对空列表会除零，需先判空")
        b = mem.add("函数 average 对空列表会除零，需先判空")
        self.assertEqual(a, b)
        hits = mem.search("average 空列表", explain=True)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].score_details)

    def test_where_filter_is_applied_before_scoring(self):
        mem = self.memory()
        mem.add("命名用 snake_case", metadata={"type": "terminology"})
        mem.add("命名用 snake_case 的反例说明", metadata={"type": "style"})
        only_term = mem.search("命名 snake_case", where={"type": "terminology"})
        self.assertEqual(len(only_term), 1)
        self.assertEqual(only_term[0].metadata["type"], "terminology")
        both = mem.search("命名 snake_case", where={"type": ["style", "terminology"]})
        self.assertEqual(len(both), 2)
        self.assertEqual(mem.search("命名", where={"type": "missing"}), [])

    def test_update_delete_history(self):
        mem = self.memory()
        mid = mem.add("旧文本 about average")
        created = mem.conn.execute("SELECT created_at FROM memory_items WHERE id=?",
                                   (mid,)).fetchone()[0]
        mem.update(mid, "新文本 about average")
        row = mem.conn.execute("SELECT * FROM memory_items WHERE id=?", (mid,)).fetchone()
        self.assertEqual(row["created_at"], created)      # 创建时间不改
        self.assertNotEqual(row["updated_at"], created)
        events = [h["event"] for h in mem.history(mid)]
        self.assertEqual(events, ["ADD", "UPDATE"])
        mem.delete(mid)
        self.assertEqual(self.mem_search_len(mem, "average"), 0)   # 软删后检索跳过
        self.assertEqual([h["event"] for h in mem.history(mid)][-1], "DELETE")
        self.assertEqual(len(mem), 0)

    @staticmethod
    def mem_search_len(mem, q):
        return len(mem.search(q))

    def test_survives_new_instance(self):
        mem = self.memory()
        mid = mem.add("跨进程要还在", metadata={"k": "v"})
        mem.close()
        again = self.memory()
        hits = again.search("跨进程")
        self.assertEqual([h.id for h in hits], [mid])
        self.assertEqual(hits[0].metadata, {"k": "v"})
        again.close()

    def test_factory_registration(self):
        import statecraft.storage  # noqa: F401  导入即注册
        mem = MemoryFactory.create("sqlite", {"db_path": self.path})
        self.assertIsInstance(mem, SqliteMemory)
        mem.close()


    def test_from_config_switches_backend(self):
        """memory_backend 配了就得生效，不能是个死字段。"""
        from statecraft.config import AgentCoreConfig
        from statecraft.memory import from_config

        mem = from_config(AgentCoreConfig(memory_backend="sqlite",
                                          memory_config={"db_path": self.path}))
        if hasattr(mem, "close"):
            self.addCleanup(mem.close)
        self.assertIsInstance(mem, SqliteMemory)
        self.assertEqual(type(from_config(AgentCoreConfig())).__name__,
                         "InMemoryMemory")


class TestSessionIntegration(_DB):
    """审查会话 → 落库：三条结论路径都要能查到。"""

    def setUp(self):
        super().setUp()
        import findings as F
        self.F = F
        self.session = F.ReviewSession(REVIEW_DIR, ["target.py"])
        rid = self.store.start_review(repo_root=REVIEW_DIR, scope=["target.py"])
        self.session.attach(self.store, rid)
        self.rid = rid

    def test_finding_lands_with_form_checks(self):
        f = self.F.Finding(file="target.py", line=23, severity="high",
                           category="boundary", message="空列表除零",
                           evidence="return sum(values) / len(values)")
        self.session.read("reviewer", "target.py")
        self.session.check(f, "reviewer")
        self.session.add(f)
        rows = self.store.findings(self.rid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["form_ok"], 1)
        self.assertEqual(rows[0]["evidence_ok"], 1)
        self.assertEqual(rows[0]["audit_ok"], 1)
        self.assertTrue(f.db_id)                     # 会话侧也拿得到库内主键

    def test_rejected_finding_still_stored(self):
        f = self.F.Finding(file="target.py", line=30, severity="low",
                           category="perf", message="编造的",
                           evidence="这行源码里根本没有")
        self.session.read("reviewer", "target.py")
        self.session.check(f, "reviewer")
        self.session.add(f)
        row = self.store.findings(self.rid)[0]
        self.assertEqual(row["form_ok"], 0)          # 不进报告，但幻觉率算得到
        self.assertEqual(row["evidence_ok"], 0)

    def test_clean_claim_and_confirm(self):
        self.session.read("reviewer", "target.py")
        self.session.declare_clean("reviewer", "target.py",
                                   "逐个函数检查五类问题，均不构成缺陷")
        self.assertEqual([c["file"] for c in self.store.clean_claims(self.rid)],
                         ["target.py"])
        f = self.F.Finding(file="target.py", line=23, severity="high",
                           category="boundary", message="除零",
                           evidence="return sum(values) / len(values)")
        self.session.check(f, "reviewer")
        self.session.add(f)
        self.session.confirm(f.id, "dispute", "该函数上游已判空")
        self.assertEqual(self.store.finding(f.db_id)["status"], "disputed")
        self.assertEqual(self.store.finding(f.db_id)["critic_note"], "该函数上游已判空")


if __name__ == "__main__":
    unittest.main()
