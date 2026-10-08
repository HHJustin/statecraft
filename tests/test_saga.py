"""Saga 补偿 + 工具 undo 测试（离线，覆盖：记账 / 逆序补偿 / 失败不中断 / 编排器接线）。"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.saga import SagaLog
from statecraft.core.state_machine import reject
from statecraft.core.tools import (AGENT_POLICY, Tool, ToolRegistry, call_tool)
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM

# 测试专用角色：白名单含 kv_set（真实项目里领域工具按角色挂白名单）
AGENT_POLICY["kv_writer"] = {"role": "execution", "tools": {"kv_set", "boom"}}


class FakeAgent:
    """绕开 LLM，直接测 call_tool 的权限/记账路径。"""

    def __init__(self, name: str, role: str, ctx: dict):
        self.name = name
        self.role = role
        self.ctx = ctx


def make_kv_tool(store: dict) -> Tool:
    def handler(ctx, key, value, **_):
        old = store.get(key)
        store[key] = value
        return f"old={old}"

    def undo(ctx, result, key, value, **_):
        old = result.split("=", 1)[1]
        if old == "None":
            store.pop(key, None)
        else:
            store[key] = old
        return "restored"

    return Tool("kv_set", "写入键值（可补偿）",
                {"type": "object",
                 "properties": {"key": {"type": "string"},
                                "value": {"type": "string"}},
                 "required": ["key", "value"]},
                handler, undo=undo)


class TestSagaUnit(unittest.TestCase):
    def test_record_and_compensate_lifo(self):
        store, saga = {}, SagaLog()
        registry = ToolRegistry()
        registry.register(make_kv_tool(store))
        agent = FakeAgent("writer", "kv_writer",
                          {"task": type("T", (), {"id": "t1"})(), "saga": saga})

        asyncio.run(call_tool(agent, registry, "kv_set", {"key": "k", "value": "v1"}))
        asyncio.run(call_tool(agent, registry, "kv_set", {"key": "k", "value": "v2"}))
        self.assertEqual(store["k"], "v2")
        self.assertEqual(saga.pending("t1"), 2)

        records = saga.compensate("t1", reason="test")
        # 逆序：先撤销 v2 回到 v1，再撤销 v1 回到空
        self.assertEqual(store.get("k"), None)
        self.assertEqual([r["status"] for r in records], ["ok", "ok"])
        self.assertEqual(saga.pending("t1"), 0)

    def test_failed_undo_does_not_block_others(self):
        store, saga = {"keep": "x"}, SagaLog()
        registry = ToolRegistry()
        registry.register(make_kv_tool(store))

        def bad_undo(ctx, result, **_):
            raise RuntimeError("补偿失败")

        registry.register(Tool("boom", "炸", {"type": "object", "properties": {}},
                               lambda ctx: "ok", undo=bad_undo))
        agent = FakeAgent("writer", "kv_writer",
                          {"task": type("T", (), {"id": "t1"})(), "saga": saga})
        asyncio.run(call_tool(agent, registry, "boom", {}))
        asyncio.run(call_tool(agent, registry, "kv_set", {"key": "k", "value": "v"}))

        records = saga.compensate("t1")
        self.assertEqual([r["status"] for r in records], ["ok", "failed"])
        self.assertIn("RuntimeError", records[1]["detail"])

    def test_no_saga_in_ctx_no_recording(self):
        store, saga = {}, SagaLog()
        registry = ToolRegistry()
        registry.register(make_kv_tool(store))
        agent = FakeAgent("writer", "kv_writer",
                          {"task": type("T", (), {"id": "t1"})()})  # 无 saga
        asyncio.run(call_tool(agent, registry, "kv_set", {"key": "k", "value": "v"}))
        self.assertEqual(store["k"], "v")
        self.assertEqual(saga.pending("t1"), 0)


def _detect_role(messages):
    for m in messages:
        if m.get("role") == "system":
            return m["content"].split("[role:", 1)[1].split("]", 1)[0].strip()
    return None


class TestOrchestratorCompensation(unittest.TestCase):
    class SpySaga(SagaLog):
        def __init__(self):
            super().__init__()
            self.calls = []

        def compensate(self, task_id, reason=""):
            self.calls.append((task_id, reason))
            return super().compensate(task_id, reason)

    def _make_orch(self, script, saga=None, **cfg):
        llm = MockLLM(script=script)
        agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
        defaults = dict(state_timeouts={"Planning": 5.0, "Reviewing": 5.0,
                                        "Executing": 5.0},
                        retry_backoff=[0.01], max_retries=2, auto_confirm=True)
        defaults.update(cfg)
        return Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(**defaults),
                            saga=saga)

    def test_reject_triggers_compensate_hook(self):
        saga = self.SpySaga()
        orch = self._make_orch({
            "planner": ["计划A", "计划B"],
            "critic": ["打回：不行。", "审核通过"],
            "executor": ["完成。DONE"],
        }, saga=saga)
        task = orch.create_task("测试打回补偿钩子")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "Done")
        # 打回发生过一次 → 补偿钩子被调（此时无记账副作用，返回空列表）
        self.assertIn(("t1", "review_rejected"), saga.calls)

    def test_cancel_compensates_recorded_side_effects(self):
        # 端到端：executor 经 LLM 工具循环真实写入 kv → 停在人工确认 → 拒绝 → 取消 → 补偿
        from statecraft.providers.base import LLMReply, ToolCall

        store = {}
        registry = ToolRegistry()
        registry.register(make_kv_tool(store))

        calls = {"critic": 0}

        def handler(messages, tools):
            role = _detect_role(messages)
            if role == "planner":
                return LLMReply(content="计划：写入数据")
            if role == "critic":
                calls["critic"] += 1
                return LLMReply(content="审核通过")
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if not has_tool_result:
                return LLMReply(tool_calls=[ToolCall(
                    name="kv_set", arguments={"key": "report", "value": "v1"})])
            return LLMReply(content="写入完成。DONE")

        llm = MockLLM(handler=handler)
        agents = {n: Agent(n, llm, registry=registry)
                  for n in ("planner", "critic", "executor")}
        # executor 换成带 kv_set 权限的角色（权限挂在角色上，不挂在名字上）
        agents["executor"].role = "kv_writer"
        orch = Orchestrator(agents, InMemoryMemory(),
                            AgentCoreConfig(auto_confirm=False,
                                            state_timeouts={"Planning": 5.0,
                                                            "Reviewing": 5.0,
                                                            "Executing": 5.0},
                                            retry_backoff=[0.01], max_retries=2))
        task = orch.create_task("写入报告数据")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "PendingConfirm")   # 等人工
        self.assertEqual(store["report"], "v1")

        reject(result)                                     # 人工拒绝 → Executing
        asyncio.run(orch.cancel(result))                   # 取消 → Saga 逆序补偿
        self.assertEqual(result.state, "Cancelled")
        self.assertNotIn("report", store)                  # 副作用已撤销
        self.assertEqual(orch.saga.history[0]["reason"], "task_cancelled")


if __name__ == "__main__":
    unittest.main()
