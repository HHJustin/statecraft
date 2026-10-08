"""看板测试（渲染内容 + 落盘；产物按约定写 E:\\Study\\探针，用 uuid 文件名）。"""
import unittest
import uuid
from pathlib import Path

from statecraft.core.observability import SessionLogger
from statecraft.core.state_machine import Task
from statecraft.dashboard import render_dashboard, write_dashboard

_PROBE_DIR = Path("E:/Study/探针")


def _sample_tasks():
    t1 = Task(id="t1", description="写一份本周项目周报")
    t1.parent_id = None
    t1.flow_log = [{"from": "Created", "to": "Planning", "reason": "transition"},
                   {"from": "Planning", "to": "Reviewing", "reason": "transition"}]
    t1.progress_log = [{"agent": "planner", "text": "计划已产出", "at": "2026-01-01"}]
    t2 = Task(id="t2", description="补充数据来源", state="Blocked",
              parent_id="t1", blocked_reason="缺少权限")
    return [t1, t2]


def _sample_logger():
    lg = SessionLogger()
    lg.log("flow", task_id="t1", **_sample_tasks()[0].flow_log[0])
    lg.log("injection_warning", task_id="t1", patterns=["ignore.*instructions"])
    lg.log("saga_compensate", task_id="t1", tool="kv_set", status="ok",
           detail="", reason="review_rejected")
    return lg


class TestDashboard(unittest.TestCase):
    def test_render_contains_key_sections(self):
        html = render_dashboard(_sample_tasks(), _sample_logger())
        self.assertIn("<!DOCTYPE html>", html)
        for fragment in ("AgentCore 看板", "t1", "t2", "Blocked",
                         "委派自", "流转时间线", "进展上报",
                         "injection_warning", "kv_set", "review_rejected"):
            self.assertIn(fragment, html)

    def test_render_escapes_html(self):
        t = Task(id="t<1>", description="<script>alert(1)</script>")
        html = render_dashboard([t])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_write_dashboard_creates_file(self):
        out = write_dashboard(str(_PROBE_DIR / f"board_{uuid.uuid4().hex}.html"),
                              _sample_tasks(), _sample_logger())
        p = Path(out)
        self.assertTrue(p.exists())
        self.assertTrue(p.read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))


if __name__ == "__main__":
    unittest.main()
