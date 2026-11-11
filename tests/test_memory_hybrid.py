"""混合检索测试：dense+词面双路融合 / 同义词改述召回 / 工厂注册。"""
import unittest

from statecraft.memory.embedding import HashEmbedding, cosine, tokenize
from statecraft.memory.factory import MemoryFactory
from statecraft.memory.hybrid import HybridMemory
from statecraft.memory.local import InMemoryMemory

# 与 eval.runner 一致的离线同义词表
SYNONYMS = {"数据库": ["db"], "线上": ["生产环境"],
            "连不上": ["连接超时"], "发布": ["部署"]}


def make_hybrid():
    return HybridMemory(embedder=HashEmbedding(synonyms=SYNONYMS))


class TestHashEmbedding(unittest.TestCase):
    def test_deterministic(self):
        e = HashEmbedding()
        self.assertEqual(e.embed("数据库连接"), e.embed("数据库连接"))

    def test_normalized_and_dim(self):
        e = HashEmbedding(dim=64)
        v = e.embed("hello world 你好")
        self.assertEqual(len(v), 64)
        self.assertAlmostEqual(sum(x * x for x in v), 1.0, places=6)

    def test_synonyms_close_vectors(self):
        e = HashEmbedding(synonyms={"数据库": ["db"]})
        import math
        def dot(a, b):
            return sum(x * y for x, y in zip(a, b))
        self.assertGreater(dot(e.embed("数据库故障"), e.embed("DB 故障")),
                           dot(e.embed("数据库故障"), e.embed("天气不错")))


class TestHybridMemory(unittest.TestCase):
    CORPUS = ["DB 连接池耗尽的处理步骤：先看配置再看重试",
              "今天团建吃火锅", "周报模板已更新"]

    def test_paraphrase_retrieved_by_dense_only(self):
        """改述样本（数据库 vs DB、连不上 vs 连接池耗尽）：词面召回不了，dense 可以。"""
        lex = InMemoryMemory()
        hyb = make_hybrid()
        for t in self.CORPUS:
            lex.add(t)
            hyb.add(t)
        query = "数据库连不上怎么排查"          # 与语料零词面重叠
        lex_hits = [h.text for h in lex.search(query, limit=3)]
        hyb_hits = [h.text for h in hyb.search(query, limit=3)]
        self.assertNotIn(self.CORPUS[0], lex_hits)    # 词面：改述召回失败
        self.assertEqual(hyb_hits[0], self.CORPUS[0])  # 混合：dense 补上

    def test_explain_shows_both_scores(self):
        hyb = make_hybrid()
        hyb.add("周报必须包含数据来源")
        h = hyb.search("周报格式要求", limit=1, explain=True)[0]
        self.assertIn("lexical", h.score_details)
        self.assertIn("dense", h.score_details)
        self.assertIn("score", h.score_details)

    def test_degrades_without_embedder(self):
        # 无 embedder → 退化为纯词面（优雅降级，不报错）
        hyb = HybridMemory()
        hyb.add("周报必须包含数据来源")
        hits = hyb.search("周报", limit=1)
        self.assertEqual(len(hits), 1)

    def test_update_refreshes_vector(self):
        hyb = make_hybrid()
        mid = hyb.add("旧内容：无关信息")
        hyb.update(mid, "数据库连接池配置说明")
        hits = hyb.search("数据库连接", limit=1, explain=True)
        self.assertEqual(hits[0].id, mid)
        self.assertGreater(hits[0].score_details["dense"], 0.0)

    def test_delete_skips_search(self):
        hyb = make_hybrid()
        mid = hyb.add("数据库配置")
        hyb.delete(mid)
        self.assertEqual(hyb.search("数据库", limit=5), [])
        self.assertEqual(len(hyb.history(mid)), 2)    # ADD + DELETE 留痕


class TestMemoryFactory(unittest.TestCase):
    def test_create_local_and_hybrid(self):
        self.assertIsInstance(MemoryFactory.create("local"), InMemoryMemory)
        self.assertIsInstance(MemoryFactory.create("hybrid"), HybridMemory)

    def test_create_with_kwargs_config(self):
        mem = MemoryFactory.create("hybrid", {"dense_weight": 0.9})
        self.assertEqual(mem.dense_weight, 0.9)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(KeyError):
            MemoryFactory.create("redis")


if __name__ == "__main__":
    unittest.main()
