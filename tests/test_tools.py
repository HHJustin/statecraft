"""命令级权限白名单测试（源自 Edict AGENT_POLICY）。"""
import unittest

from statecraft.core.tools import (AUDIT_LOG, PermissionDenied, ToolRegistry,
                                  call_tool, check_permission)


class _DummyAgent:
    def __init__(self, name, role):
        self.name = name
        self.role = role
        self.ctx = {}


class TestPermission(unittest.TestCase):
    def test_allowed_tool_passes(self):
        check_permission("planner", "planner", "report_progress")   # 不抛即通过

    def test_unknown_tool_denied(self):
        with self.assertRaises(PermissionDenied):
            check_permission("executor", "executor", "deploy_prod")

    def test_audit_log_records_denied(self):
        before = len(AUDIT_LOG)
        try:
            check_permission("critic", "critic", "deploy_prod")
        except PermissionDenied:
            pass
        self.assertEqual(len(AUDIT_LOG), before + 1)
        self.assertEqual(AUDIT_LOG[-1]["verdict"], "DENIED")

    def test_call_tool_executes_handler(self):
        from statecraft.core.state_machine import Task
        from statecraft.core.tools import default_registry
        agent = _DummyAgent("executor", "executor")
        task = Task("1", "demo")
        agent.ctx = {"task": task, "agent": agent}
        import asyncio
        result = asyncio.run(call_tool(agent, default_registry(),
                                       "report_progress", {"text": "进展1"}))
        self.assertEqual(result, "progress recorded")
        self.assertEqual(task.progress_log[-1]["text"], "进展1")


if __name__ == "__main__":
    unittest.main()
