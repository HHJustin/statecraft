"""代码符号图测试：AST 抽取 / 入边回填 / 符号边界匹配 / 第三路图加成。"""
import unittest

from statecraft.memory.codegraph import CodeGraph, SymbolIndex
from statecraft.memory.embedding import HashEmbedding
from statecraft.memory.hybrid import HybridMemory

SRC_ORDER = '''\
def create_order(user, items, coupon=None):
    validate_stock(items)
    total = apply_coupon(10, coupon)
    return total


def validate_stock(items):
    if not items:
        raise ValueError("empty")
    return True


def apply_coupon(total, coupon):
    return total * 0.8
'''

SRC_NOTIFY = '''\
def send_receipt(user):
    print(user)
'''

NOISE = ["周报模板已更新", "今天团建吃火锅", "发布流程需要灰度",
         "数据库连接池配置", "接口超时重试策略", "日志采集规范"]


def make_graph() -> CodeGraph:
    return CodeGraph().add_source(SRC_ORDER, module="order")


class TestCodeGraph(unittest.TestCase):
    def setUp(self):
        self.graph = make_graph()

    def test_extracts_symbols_with_args(self):
        names = {n.name for n in self.graph.symbols.values()}
        self.assertEqual(names, {"create_order", "validate_stock", "apply_coupon"})
        node = self.graph.get("create_order")
        self.assertEqual(node.args, ["user", "items", "coupon"])
        self.assertEqual(node.kind, "function")
        self.assertFalse(node.has_doc)

    def test_extracts_raises(self):
        self.assertEqual(self.graph.get("validate_stock").raises, ["ValueError"])

    def test_callees_filtered_to_graph_symbols(self):
        # print/sum 这类图外调用被滤掉，只留图内边
        self.assertEqual(self.graph.get("create_order").callees,
                         ["apply_coupon", "validate_stock"])

    def test_callers_backfilled(self):
        self.assertEqual(self.graph.get("validate_stock").callers, ["create_order"])
        self.assertEqual(self.graph.get("apply_coupon").callers, ["create_order"])
        self.assertEqual(self.graph.get("create_order").callers, [])

    def test_multi_file_graph(self):
        self.graph.add_source(SRC_NOTIFY, module="notify")
        self.assertEqual(len(self.graph.modules), 2)
        self.assertIn("send_receipt", {n.name for n in self.graph.symbols.values()})

    def test_text_excludes_edges_by_default(self):
        node = self.graph.get("create_order")
        plain = node.to_text()
        self.assertIn("create_order", plain)
        self.assertIn("无 docstring", plain)
        self.assertNotIn("调用", plain)          # 关系不入文本（职责分离）
        self.assertIn("调用", node.to_text(with_edges=True))

    def test_missing_doc_by_module(self):
        self.graph.add_source(SRC_NOTIFY, module="notify")
        self.assertEqual(len(self.graph.missing_doc()), 4)
        self.assertEqual(len(self.graph.missing_doc(module="notify")), 1)

    def test_neighbors_both_directions(self):
        self.assertEqual(self.graph.neighbors("validate_stock"), {"create_order"})
        self.assertIn("apply_coupon", self.graph.neighbors("create_order"))

    def test_memory_entries_carry_symbol_metadata(self):
        entries = dict((md["symbol"], text)
                       for text, md in self.graph.to_memory_entries())
        self.assertIn("create_order", entries)
        self.assertEqual(len(entries), 3)


class TestSymbolIndex(unittest.TestCase):
    def setUp(self):
        self.graph = make_graph()
        self.index = SymbolIndex(self.graph)

    def test_query_symbols_respects_ascii_boundary(self):
        # 子串不算命中：apply_coupon_x / myapply_coupon 都不能算
        self.assertEqual(self.index.query_symbols("看一下 apply_coupon 的实现"),
                         {"apply_coupon"})
        self.assertEqual(self.index.query_symbols("apply_coupon_v2 怎么写"), set())
        self.assertEqual(self.index.query_symbols("完全没有符号名的一句话"), set())

    def test_query_symbols_chinese_adjacent(self):
        # 中文紧邻标识符时 \b 会失效，这里用 ASCII 边界正则兜住
        self.assertEqual(self.index.query_symbols("谁调用了validate_stock"),
                         {"validate_stock"})

    def test_score_direct_neighbor_unrelated(self):
        self.index.link("m1", ["create_order"])
        self.index.link("m2", ["apply_coupon"])
        self.index.link("m3", ["unrelated"])
        qsyms = self.index.query_symbols("create_order 依赖什么")
        self.assertEqual(self.index.score(qsyms, "m1"), 1.0)     # 直接命中
        self.assertEqual(self.index.score(qsyms, "m2"), 0.5)     # 一跳邻居
        self.assertEqual(self.index.score(qsyms, "m3"), 0.0)     # 无关

    def test_link_from_metadata(self):
        self.index.link_from_metadata("m9", {"symbol": "apply_coupon"})
        self.index.link_from_metadata("m9", {"symbols": ["create_order"]})
        qsyms = self.index.query_symbols("apply_coupon")
        self.assertEqual(self.index.score(qsyms, "m9"), 1.0)


class TestThirdPathScoring(unittest.TestCase):
    """第三路的核心价值：关系型查询能召回「文本里完全没有关键词」的条目。"""

    def setUp(self):
        self.graph = make_graph()
        self.entries = self.graph.to_memory_entries()

    def _mem(self, with_graph: bool):
        mem = (HybridMemory(graph_index=SymbolIndex(self.graph))
               if with_graph else HybridMemory())
        for text, md in self.entries:
            mem.add(text, metadata=md)
        for t in NOISE:
            mem.add(t)
        return mem

    def test_two_path_cannot_reach_subfunctions(self):
        mem = self._mem(with_graph=False)
        hits = mem.search("create_order 依赖哪些子函数", limit=10, explain=True)
        names = [h.text.split("｜", 1)[0].split("(")[0] for h in hits]
        self.assertNotIn("apply_coupon", names)      # 无 embedder 时纯词面：无重叠即 0
        self.assertNotIn("validate_stock", names)

    def test_third_path_brings_neighbors_back(self):
        mem = self._mem(with_graph=True)
        hits = mem.search("create_order 依赖哪些子函数", limit=10, explain=True)
        by_name = {h.text.split("｜", 1)[0].split("(")[0]: h for h in hits}
        self.assertEqual(by_name["create_order"].score_details["graph"], 1.0)
        self.assertEqual(by_name["apply_coupon"].score_details["graph"], 0.5)
        self.assertEqual(by_name["validate_stock"].score_details["graph"], 0.5)
        # 图分让邻居越过阈值被召回（词面分是 0）
        self.assertEqual(by_name["apply_coupon"].score_details["lexical"], 0.0)

    def test_unrelated_noise_not_boosted(self):
        mem = self._mem(with_graph=True)
        hits = mem.search("create_order 依赖哪些子函数", limit=10, explain=True)
        graphs = {h.text: h.score_details["graph"] for h in hits}
        self.assertTrue(all(g == 0.0 for t, g in graphs.items() if "｜" not in t))

    def test_explain_exposes_query_symbols(self):
        mem = self._mem(with_graph=True)
        hit = mem.search("create_order 依赖哪些子函数", limit=1, explain=True)[0]
        self.assertIn("create_order", hit.score_details["query_symbols"])


class TestMetadataFilter(unittest.TestCase):
    """元数据前置过滤（路线图决策六「先过滤再打分」）。"""

    def setUp(self):
        self.mem = HybridMemory(embedder=HashEmbedding())
        self.mem.add("注释规范：必须说明异常行为", metadata={"type": "style"})
        self.mem.add("术语约定：折扣指会员折扣", metadata={"type": "terminology"})
        self.mem.add("calc_discount(price)｜sample.py:8", metadata={"type": "code_symbol"})

    def test_where_single_value(self):
        hits = self.mem.search("注释 术语 折扣", limit=5, where={"type": "style"})
        self.assertEqual([h.text for h in hits],
                         ["注释规范：必须说明异常行为"])

    def test_where_list_means_any(self):
        hits = self.mem.search("注释 术语 折扣", limit=5,
                               where={"type": ["style", "terminology"]})
        self.assertEqual(len(hits), 2)
        self.assertNotIn("calc_discount(price)｜sample.py:8", [h.text for h in hits])

    def test_where_mismatch_returns_empty(self):
        self.assertEqual(self.mem.search("注释", where={"type": "unknown"}), [])

    def test_item_carries_metadata(self):
        hit = self.mem.search("注释规范", limit=1, where={"type": "style"})[0]
        self.assertEqual(hit.metadata["type"], "style")


if __name__ == "__main__":
    unittest.main()
