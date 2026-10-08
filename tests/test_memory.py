"""记忆层测试：去重 / 打分阈值 / history 版本化 / 软删除。"""
import unittest

from statecraft.memory.local import InMemoryMemory


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.mem = InMemoryMemory()

    def test_add_dedup_by_hash(self):
        id1 = self.mem.add("用户偏好使用中文回复", scope="shared")
        id2 = self.mem.add("用户偏好使用中文回复", scope="shared")
        self.assertEqual(id1, id2)          # 确定性去重

    def test_search_threshold_and_ranking(self):
        self.mem.add("团队约定：周报必须包含数据来源与风险提示")
        self.mem.add("完全无关的记忆条目：今天天气晴朗适合郊游")
        hits = self.mem.search("周报 数据来源 要求", explain=True)
        self.assertTrue(hits)
        self.assertIn("周报", hits[0].text)
        self.assertTrue(hits[0].score_details)   # explain 可审计
        # 无关条目被阈值淘汰
        self.assertTrue(all("郊游" not in h.text for h in hits))

    def test_update_keeps_created_at(self):
        mid = self.mem.add("旧文本")
        created = self.mem._items[mid]["created_at"]
        self.mem.update(mid, "新文本")
        it = self.mem._items[mid]
        self.assertEqual(it["text"], "新文本")
        self.assertEqual(it["created_at"], created)
        hist = self.mem.history(mid)
        self.assertEqual([h["event"] for h in hist], ["ADD", "UPDATE"])

    def test_soft_delete_and_history(self):
        mid = self.mem.add("待删除条目")
        self.mem.delete(mid)
        self.assertEqual(self.mem.search("待删除条目"), [])   # 检索跳过软删
        events = [h["event"] for h in self.mem.history(mid)]
        self.assertEqual(events, ["ADD", "DELETE"])


if __name__ == "__main__":
    unittest.main()
