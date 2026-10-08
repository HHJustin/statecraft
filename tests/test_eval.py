"""评估体系测试（P9）：指标数学正确性 + 三层跑分器离线可跑。"""
import unittest

from statecraft.eval.metrics import confusion, hit_at_k, mrr, recall_at_k
from statecraft.eval.runner import run_l1, run_l2, run_l3


class TestMetrics(unittest.TestCase):
    def test_recall_mrr_hit(self):
        ranked = ["a", "b", "c"]
        self.assertEqual(recall_at_k(ranked, {"a"}, 1), 1.0)
        self.assertEqual(recall_at_k(ranked, {"a", "b"}, 1), 0.5)
        self.assertEqual(recall_at_k(ranked, {"c"}, 2), 0.0)
        self.assertEqual(mrr(ranked, {"b"}), 0.5)          # 第 2 位 → 1/2
        self.assertEqual(mrr(ranked, {"z"}), 0.0)
        self.assertEqual(hit_at_k(ranked, {"c"}, 2), 0.0)
        self.assertEqual(hit_at_k(ranked, {"c"}, 3), 1.0)

    def test_confusion_math(self):
        # 正类 = reject；1 tp, 1 fp(误杀), 1 fn(漏放), 1 tn
        r = confusion(["reject", "approve", "reject", "approve"],
                      ["reject", "reject", "approve", "approve"])
        self.assertEqual(r["tp"], 1)
        self.assertEqual(r["fp"], 1)
        self.assertEqual(r["fn"], 1)
        self.assertEqual(r["tn"], 1)
        self.assertAlmostEqual(r["precision"], 0.5)
        self.assertAlmostEqual(r["recall"], 0.5)
        self.assertAlmostEqual(r["f1"], 0.5)
        self.assertAlmostEqual(r["false_kill_rate"], 0.5)
        self.assertAlmostEqual(r["false_pass_rate"], 0.5)


class TestRunners(unittest.TestCase):
    def test_l1_golden_all_retrieved(self):
        r = run_l1()
        self.assertEqual(r["cases"], 3)
        self.assertEqual(r["recall_at_k"], 1.0)   # 种子集应全召回
        self.assertGreaterEqual(r["mrr"], 0.5)

    def test_l2_rule_reviewer_perfect(self):
        r = run_l2()                               # 规则审核器与种子集对齐
        self.assertEqual(r["f1"], 1.0)
        self.assertEqual(r["false_pass_rate"], 0.0)

    def test_l2_lenient_reviewer_detected(self):
        r = run_l2(reviewer=lambda text: "approve")   # 全放行的坏审核器
        self.assertEqual(r["recall"], 0.0)            # 漏放全部该打回的
        self.assertEqual(r["false_pass_rate"], 1.0)

    def test_l3_end_to_end(self):
        r = run_l3()
        self.assertEqual(r["cases"], 3)
        self.assertEqual(r["completion_rate"], 1.0)   # 三个场景终态全部命中
        self.assertEqual(r["must_visit_rate"], 1.0)   # Reviewing 关卡全经过


if __name__ == "__main__":
    unittest.main()
