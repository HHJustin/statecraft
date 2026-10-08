"""状态机白名单 / 二次确认 / 升级路径 测试（全离线）。"""
import unittest

from statecraft.core.state_machine import (Task, TransitionDenied, approve,
                                          can_transition, escalate, reject,
                                          transition)


class TestStateMachine(unittest.TestCase):
    def test_happy_path(self):
        t = Task("1", "demo")
        for nxt in ("Planning", "Reviewing", "Executing"):
            transition(t, nxt)
        self.assertEqual(t.state, "Executing")
        transition(t, "Done", confirm=True)
        self.assertEqual(t.state, "Done")
        self.assertFalse(can_transition("Done", "Planning"))  # 终态不可逆

    def test_illegal_transition_denied(self):
        t = Task("1", "demo")
        transition(t, "Planning")
        with self.assertRaises(TransitionDenied):
            transition(t, "Done")          # 跳过审核 → 拒绝
        self.assertEqual(t.state, "Planning")

    def test_high_risk_goes_pending_confirm_then_approve(self):
        t = Task("1", "demo")
        for nxt in ("Planning", "Reviewing", "Executing"):
            transition(t, nxt)
        transition(t, "Done")              # 高风险且未确认
        self.assertEqual(t.state, "PendingConfirm")
        self.assertEqual(t.pending_target, "Done")
        self.assertEqual(t.pending_source, "Executing")
        approve(t)
        self.assertEqual(t.state, "Done")

    def test_high_risk_reject_returns_to_source(self):
        t = Task("1", "demo")
        for nxt in ("Planning", "Reviewing", "Executing"):
            transition(t, nxt)
        transition(t, "Done")
        reject(t)
        self.assertEqual(t.state, "Executing")

    def test_escalation_chain_then_blocked(self):
        t = Task("1", "demo")
        t._set("Executing", "test")
        escalate(t)
        self.assertEqual(t.state, "Reviewing")
        escalate(t)
        self.assertEqual(t.state, "Planning")
        escalate(t)
        self.assertEqual(t.state, "Created")
        escalate(t)                         # 到顶 → Blocked
        self.assertEqual(t.state, "Blocked")
        self.assertIsNotNone(t.blocked_reason)

    def test_snapshot_rollback(self):
        t = Task("1", "demo")
        t.snapshot()
        t._set("Planning", "test")
        t.rollback()
        self.assertEqual(t.state, "Created")


if __name__ == "__main__":
    unittest.main()
