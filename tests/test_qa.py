"""问答层（P13）测试：路由 / 槽位 / SQL 聚合 / 引用校验 / 降级。

这里刻意测的都是「看起来对、其实会错」的地方：

1. **计数不能被 limit 截断** —— 造 25 条问题（> MAX_ROWS），问「多少条」，
   答案必须是 25 而不是 20。用 `len(rows)` 实现就会错，且错得很难发现。
2. **默认只看最新批次** —— 同一处问题在 2 个批次都存在，问「有多少」不能答 2。
3. **空结果不许调模型** —— 库是空的时候让模型「根据上下文回答」，它一定会
   倒出上下文之外的东西。用 call_count 断言它一次都没被调用。
4. **引用必须能落地** —— 模型编的 `[F7]` 要进 `unknown_citations`；正常引用
   的 finding_id 必须真的能在库里查到（这条是可断言的事实，不是承诺）。
5. **词表冲突** —— 「空列表」的「列表」曾经被判成「列个清单」，回归守住。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "examples" / "code-review")):
    if p not in sys.path:
        sys.path.insert(0, p)

from statecraft.providers.base import LLMReply, ProviderError            # noqa: E402
from statecraft.providers.mock import MockLLM                            # noqa: E402
from statecraft.qa import router                                        # noqa: E402
from statecraft.qa.answer import CodebaseQA, build_context               # noqa: E402
from statecraft.qa.query import QueryEngine, extract_slots, keywords     # noqa: E402
from statecraft.storage import ReviewStore                               # noqa: E402


class _DB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name) / "qa.db")
        self.store = ReviewStore(self.path)
        self.addCleanup(self.store.close)

    def seed(self, review_id=None, finish=True, task_id="t"):
        rid = self.store.start_review(repo_root="E:/p", scope=["sample.py", "src/auth.py"],
                                      task_id=task_id, review_id=review_id)
        rows = [
            dict(file="sample.py", line=23, severity="high", category="boundary",
                 message="average() 对空列表除零", evidence="return sum(v) / len(v)",
                 suggestion="先判空", status="confirmed"),
            dict(file="sample.py", line=12, severity="medium", category="exception",
                 message="宽泛 except 吞掉异常", evidence="except Exception:"),
            dict(file="src/auth.py", line=88, severity="high", category="security",
                 message="会话令牌未校验过期时间", evidence="if token == expected:"),
            dict(file="src/auth.py", line=40, severity="low", category="style",
                 message="参数命名 d/t 含义不明", evidence="def f(d, t):"),
        ]
        for r in rows:
            self.store.record(rid, form_ok=True, evidence_ok=True, audit_ok=True, **r)
        # 形式审驳回的编造条目：留档但不计数（口径与 P12 一致）
        self.store.record(rid, file="sample.py", line=30, severity="high",
                          category="boundary", message="编造的一条",
                          evidence="placeholder", form_ok=False)
        if finish:
            self.store.finish_review(rid, "done")
        return rid

    def qa(self, llm=None, memory=None) -> CodebaseQA:
        return CodebaseQA(self.store, llm=llm, memory=memory)


# ---------------------------------------------------------------- 路由
class TestRouter(unittest.TestCase):
    def test_filter_question_is_structured(self):
        self.assertEqual(router.route("sample.py 有哪些高危问题？"), router.STRUCTURED)

    def test_aggregate_question_is_structured(self):
        self.assertEqual(router.route("哪个文件问题最多？"), router.STRUCTURED)
        self.assertEqual(router.route("一共查出多少问题？"), router.STRUCTURED)

    def test_history_question_is_semantic(self):
        self.assertEqual(router.route("这个 bug 我们以前修过吗？"), router.SEMANTIC)
        self.assertEqual(router.route("有没有处理过类似的除零问题？"), router.SEMANTIC)

    def test_tie_with_anchor_prefers_structured(self):
        """「哪些…类似」平局时，有文件名/严重度锚点说明用户想过滤。"""
        d = router.decide("sample.py 和这个类似的问题有哪些？")
        self.assertEqual(d["route"], router.STRUCTURED)
        self.assertEqual(d["anchors"], ["file"])

    def test_tie_without_anchor_prefers_semantic(self):
        d = router.decide("哪些问题跟这个类似？")
        self.assertEqual(d["route"], router.SEMANTIC)
        self.assertEqual(d["anchors"], [])

    def test_list_word_is_not_a_cue(self):
        """回归：「空列表」的「列表」曾把问题判成「列个清单」。"""
        self.assertEqual(router.route("空列表直接相除会怎样？"), router.SEMANTIC)
        self.assertEqual(router.route("列出所有高危问题"), router.STRUCTURED)

    def test_past_tense_question_is_semantic(self):
        """回归：「内存泄漏处理过吗」曾无信号命中 → 返回全库清单。"""
        self.assertEqual(router.route("内存泄漏处理过吗？"), router.SEMANTIC)

    def test_empty_input_is_structured(self):
        self.assertEqual(router.route(""), router.STRUCTURED)


# ---------------------------------------------------------------- 槽位
class TestSlots(unittest.TestCase):
    def test_file_and_severity(self):
        s = extract_slots("sample.py 有哪些高危问题？")
        self.assertEqual(s.files, ["sample.py"])
        self.assertEqual(s.severity, "high")
        self.assertIsNone(s.aggregate)

    def test_severity_distribution_is_not_a_filter(self):
        """「严重度分布」里的「严重」不能当 severity=high 的过滤条件。"""
        s = extract_slots("严重度分布是怎样的？")
        self.assertEqual(s.aggregate, "by_severity")
        self.assertIsNone(s.severity)

    def test_aggregate_kinds(self):
        self.assertEqual(extract_slots("哪个文件问题最多？").aggregate, "by_file")
        self.assertEqual(extract_slots("一共多少问题").aggregate, "count")

    def test_aggregate_keeps_severity_when_asked(self):
        """「哪个文件高危最多」要保留 severity —— 问的是高危的分布。"""
        s = extract_slots("哪个文件高危问题最多？")
        self.assertEqual(s.aggregate, "by_file")
        self.assertEqual(s.severity, "high")

    def test_keywords_strip_question_words(self):
        self.assertEqual(keywords("有没有处理过类似的除零问题"), ["除零"])
        self.assertEqual(keywords("average 空列表"), ["average", "空列表"])

    def test_keywords_survive_pure_greeting(self):
        self.assertEqual(keywords("你好"), ["你好"])


# ---------------------------------------------------------------- 结构化查询
class TestStructured(_DB):
    def test_file_filter_returns_only_that_file(self):
        rid = self.seed()
        res = QueryEngine(self.store).structured("sample.py 有哪些高危问题？", review_id=rid)
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0]["file"], "sample.py")
        self.assertEqual(res.rows[0]["severity"], "high")

    def test_bare_filename_maps_to_real_path(self):
        """用户说 `auth.py`，库里是 `src/auth.py` —— 必须映射成功。"""
        rid = self.seed()
        res = QueryEngine(self.store).structured("auth.py 有哪些问题？", review_id=rid)
        self.assertEqual({r["file"] for r in res.rows}, {"src/auth.py"})
        self.assertEqual(len(res.rows), 2)

    def test_count_is_not_truncated_by_limit(self):
        """造 25 条（> MAX_ROWS=20），计数必须是 25。用 len(rows) 会答成 20。"""
        rid = self.store.start_review(repo_root="E:/p", scope=["big.py"], task_id="t")
        for i in range(25):
            self.store.record(rid, file="big.py", line=i + 1, severity="medium",
                              category="style", message=f"问题{i}", evidence="x = 1",
                              form_ok=True)
        self.store.finish_review(rid, "done")
        ans = self.qa().ask("一共查出多少问题？", review_id=rid)
        self.assertIn("25", ans.text)
        self.assertTrue(ans.deterministic)

    def test_form_failed_rows_are_not_counted(self):
        rid = self.seed()
        ans = self.qa().ask("一共查出多少问题？", review_id=rid)
        self.assertIn("4", ans.text)          # 5 条里那条编造的 form_ok=0 不算
        self.assertNotIn("5 条问题", ans.text)

    def test_default_scope_is_latest_review_only(self):
        """同一处问题出现在两个批次，问「多少」不能算两遍。"""
        r1 = self.seed(review_id="old")
        r2 = self.seed(review_id="new", task_id="t2")
        self.assertNotEqual(r1, r2)
        ans = self.qa().ask("一共查出多少问题？")
        self.assertIn("4", ans.text)
        self.assertTrue(any("最新批次" in n for n in ans.notes))

    def test_aggregate_by_file_numbers(self):
        rid = self.seed()
        ans = self.qa().ask("哪个文件问题最多？", review_id=rid)
        self.assertIn("sample.py", ans.text)
        self.assertIn("2 条", ans.text)       # sample.py 与 src/auth.py 各 2 条
        self.assertEqual(ans.source, "sql")
        self.assertEqual(ans.citations, [])


# ---------------------------------------------------------------- 语义查询
class TestSemantic(_DB):
    def test_chinese_keyword_recall(self):
        """回归：整句 LIKE 匹配不到任何东西，必须先把问句切成关键词。"""
        self.seed()
        ans = self.qa().ask("有没有处理过类似的除零问题？")
        self.assertFalse(ans.empty)
        self.assertEqual(ans.citations[0].file, "sample.py")
        self.assertTrue(any("查询改写" in n for n in ans.notes))

    def test_empty_result_does_not_call_llm(self):
        """库里有数据但和不相关的问题无关时，模型一次都不该被调。"""
        self.seed()
        llm = MockLLM(handler=lambda m, t: self.fail("空结果不该调用模型"))
        ans = self.qa(llm=llm).ask("有没有处理过内存泄漏问题？")
        self.assertTrue(ans.empty)
        self.assertEqual(llm.call_count, 0)
        self.assertIn("没有相关结论", ans.text)

    def test_empty_database(self):
        ans = self.qa().ask("sample.py 有哪些高危问题？")
        self.assertTrue(ans.empty)
        self.assertEqual(ans.citations, [])


# ---------------------------------------------------------------- 引用
class TestCitations(_DB):
    def test_citation_resolves_to_real_row(self):
        self.seed()
        llm = MockLLM(handler=lambda m, t: LLMReply(content="空列表会除零 [F1]。"))
        ans = self.qa(llm=llm).ask("sample.py 有哪些高危问题？")
        self.assertEqual(len(ans.citations), 1)
        c = ans.citations[0]
        self.assertEqual(c.label, "F1")
        self.assertIsNotNone(self.store.finding(c.finding_id))   # 引用可溯源
        self.assertEqual(ans.source, "llm")

    def test_fabricated_citation_is_flagged(self):
        self.seed()
        llm = MockLLM(handler=lambda m, t: LLMReply(
            content="存在内存泄漏 [F7]，另外有除零 [F1]。"))
        ans = self.qa(llm=llm).ask("sample.py 有哪些高危问题？")
        self.assertEqual(ans.unknown_citations, ["[F7]"])
        self.assertIn("查无此记录", ans.text)
        self.assertEqual([c.label for c in ans.citations], ["F1"])

    def test_answer_without_citation_is_marked_unsourced(self):
        self.seed()
        llm = MockLLM(handler=lambda m, t: LLMReply(content="这个文件没什么大问题。"))
        ans = self.qa(llm=llm).ask("sample.py 有哪些高危问题？")
        self.assertTrue(ans.unsourced)
        self.assertIn("以下为原始记录，供核对", ans.text)

    def test_full_width_brackets_are_accepted(self):
        self.seed()
        llm = MockLLM(handler=lambda m, t: LLMReply(content="空列表除零【F1】"))
        ans = self.qa(llm=llm).ask("sample.py 有哪些高危问题？")
        self.assertEqual(len(ans.citations), 1)

    def test_context_carries_evidence_and_status(self):
        rid = self.seed()
        rows = self.store.findings(rid, file="sample.py")
        ctx = build_context(rows)
        self.assertIn("[F1]", ctx)
        self.assertIn("证据：", ctx)
        self.assertIn("复核状态：", ctx)


# ---------------------------------------------------------------- 降级与记忆
class TestFallback(_DB):
    def test_llm_error_degrades_to_record_list(self):
        rid = self.seed()

        def boom(messages, tools):
            raise ProviderError("额度已用尽", status=429, retryable=False)

        ans = self.qa(llm=MockLLM(handler=boom)).ask("sample.py 有哪些高危问题？",
                                                     review_id=rid)
        self.assertEqual(ans.source, "records")
        self.assertEqual(len(ans.citations), 1)
        self.assertTrue(any("已降级" in n for n in ans.notes))

    def test_no_llm_still_answers(self):
        rid = self.seed()
        ans = self.qa(llm=None).ask("sample.py 有哪些高危问题？", review_id=rid)
        self.assertFalse(ans.text.startswith("现有审查记录中没有"))
        self.assertEqual(len(ans.citations), 1)

    def test_greeting_does_not_query(self):
        self.seed()
        ans = self.qa().ask("你好")
        self.assertTrue(ans.empty)
        self.assertEqual(ans.source, "none")
        self.assertIn("查询助手", ans.text)

    def test_memory_recall_must_exist_in_db(self):
        """记忆里有、库里没有的条目必须被丢弃 —— 索引不是真相源。"""
        rid = self.seed()

        class StaleMemory:
            """假装索引里有一条已经不在库里的 finding。"""

            def search(self, query, limit=5, explain=False, where=None):
                from statecraft.memory.base import MemoryItem
                return [MemoryItem(id="ghost", text="空列表除零",
                                   metadata={"finding_id": "deadbeef" * 2})]

        eng = QueryEngine(self.store, memory=StaleMemory())
        res = eng.semantic("空列表除零")
        self.assertTrue(all(r["review_id"] == rid for r in res.rows))
        self.assertTrue(any("查无实据" in n for n in res.notes))

    def test_index_findings_tags_metadata(self):
        self.seed()
        from statecraft.memory.sqlite import SqliteMemory

        mem = SqliteMemory(self.path)
        self.addCleanup(mem.close)
        n = self.qa(memory=mem).index_findings()
        self.assertEqual(n, 4)
        hits = mem.search("除零", where={"type": "finding"})
        self.assertTrue(hits)
        self.assertIn("finding_id", hits[0].metadata)
        self.assertIsNotNone(self.store.finding(hits[0].metadata["finding_id"]))


if __name__ == "__main__":
    unittest.main()
