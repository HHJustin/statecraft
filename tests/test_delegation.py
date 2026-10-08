"""委派 + 防死锁测试（深度限制 / 成环检测 / 工具路径 / 编排器接线）。"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.delegation import DelegationDenied, DelegationTracker
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.tools import call_tool
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM


class FakeAgent:
    def __init__(self, name: str, role: str, ctx: dict):
        self.name = name
        self.role = role
        self.ctx = ctx


def make_orch(**cfg):
    llm = MockLLM(script={})
    agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
    defaults = dict(max_delegation_depth=3)
    defaults.update(cfg)
    return Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(**defaults))


class TestTracker(unittest.TestCase):
    def test_depth_limit(self):
        tr = DelegationTracker(max_depth=3)
        tr.register("t1", "t2")
        tr.register("t2", "t3")
        tr.register("t3", "t4")            # t4 深度 3，等于上限，放行
        self.assertEqual(tr.depth("t4"), 3)
        with self.assertRaises(DelegationDenied):
            tr.register("t4", "t5")        # 深度 4 > 3，拒绝

    def test_cycle_denied(self):
        tr = DelegationTracker(max_depth=3)
        tr.register("t1", "t2")
        tr.register("t2", "t3")
        with self.assertRaises(DelegationDenied):
            tr.register("t3", "t1")        # 委派给祖先 → 成环
        with self.assertRaises(DelegationDenied):
            tr.register("t2", "t2")        # 自己委派给自己

    def test_chain_and_subtree(self):
        tr = DelegationTracker(max_depth=3)
        tr.register("t1", "t2")
        tr.register("t1", "t3")
        tr.register("t2", "t4")
        self.assertEqual(tr.chain("t4"), ["t1", "t2", "t4"])
        self.assertEqual(tr.subtree("t1"), {"t1", "t2", "t3", "t4"})


class TestOrchestratorDelegation(unittest.TestCase):
    def test_create_child_sets_parent(self):
        orch = make_orch()
        parent = orch.create_task("父任务")
        child = orch.create_task("子任务", parent=parent)
        self.assertEqual(child.parent_id, "t1")
        self.assertEqual(orch.delegations.depth("t2"), 1)
        self.assertIn(("t1", "t2"), orch.delegations.edges)

    def test_depth_exceeded_across_orchestrator(self):
        orch = make_orch(max_delegation_depth=2)
        t1 = orch.create_task("L0")
        t2 = orch.create_task("L1", parent=t1)
        t3 = orch.create_task("L2", parent=t2)
        with self.assertRaises(DelegationDenied):
            orch.create_task("L3", parent=t3)
        self.assertEqual(t3.parent_id, "t2")

    def test_delegate_subtask_tool(self):
        orch = make_orch()
        parent = orch.create_task("父任务")
        agent = FakeAgent("planner", "planner",
                          {"task": parent, "orchestrator": orch})
        msg = asyncio.run(call_tool(agent, _registry(), "delegate_subtask",
                                    {"description": "帮我查资料"}))
        self.assertIn("t2", msg)
        child = orch.delegations.parent_of
        self.assertEqual(child["t2"], "t1")

    def test_delegate_tool_denied_at_depth(self):
        orch = make_orch(max_delegation_depth=1)
        t1 = orch.create_task("L0")
        t2 = orch.create_task("L1", parent=t1)
        agent = FakeAgent("planner", "planner",
                          {"task": t2, "orchestrator": orch})
        with self.assertRaises(DelegationDenied):
            asyncio.run(call_tool(agent, _registry(), "delegate_subtask",
                                  {"description": "再委派一层"}))

    def test_executor_cannot_delegate(self):
        orch = make_orch()
        task = orch.create_task("任务")
        agent = FakeAgent("executor", "executor",
                          {"task": task, "orchestrator": orch})
        from statecraft.core.tools import PermissionDenied
        with self.assertRaises(PermissionDenied):
            asyncio.run(call_tool(agent, _registry(), "delegate_subtask",
                                  {"description": "越权委派"}))


def _registry():
    from statecraft.core.tools import default_registry
    return default_registry()


if __name__ == "__main__":
    unittest.main()
