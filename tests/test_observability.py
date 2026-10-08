"""可观测性测试（P7）：JSONL 落盘 + 活动流过滤。

注：本沙箱环境禁止删除文件，故用唯一文件名（uuid），不做清理；
产物按约定落在 E:\Study\探针。
"""
import json
import os
import unittest
import uuid

from statecraft.core.observability import SessionLogger


class TestObservability(unittest.TestCase):
    def test_jsonl_write_and_stream(self):
        path = rf"E:\Study\探针\test_session_{uuid.uuid4().hex[:8]}.jsonl"
        lg = SessionLogger(path=path)
        try:
            lg.log("flow", task_id="t1", state="Planning")
            lg.log("progress", task_id="t1", text="进展1")

            with open(path, encoding="utf-8") as f:
                lines = [json.loads(x) for x in f.read().strip().splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["kind"], "flow")
            self.assertEqual(lines[1]["text"], "进展1")
            self.assertEqual(len(lg.activity_stream()), 2)
            self.assertEqual(len(lg.activity_stream(kinds=["progress"])), 1)
        finally:
            self.assertTrue(os.path.exists(path))   # 产物留在探针目录

    def test_memory_only_mode(self):
        lg = SessionLogger()   # path=None：不落盘
        lg.log("flow", state="Done")
        self.assertEqual(len(lg.records), 1)


if __name__ == "__main__":
    unittest.main()
