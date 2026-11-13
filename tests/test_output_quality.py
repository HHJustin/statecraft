"""产出质量判定：真机实测暴露的两个缺陷的回归测试。

1. 模型报完问题却忘了写终止标记（REQUEST_REVIEW/DONE）→ 旧逻辑 Blocked，
   前面所有有效产出一起丢掉。现在按「有没有副作用产出」判断，而不是按措辞。
2. 工具轮次耗尽后，模型连输出总结的机会都没有 → 现在补一次「只输出结论」的收尾轮。
"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.tools import AGENT_POLICY, Tool, ToolRegistry
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply, ToolCall
from statecraft.providers.mock import MockLLM, _detect_role


def _make_registry(notes: list[str]) -> ToolRegistry:
    reg = ToolRegistry()

    def _record(ctx, text, **_):
        notes.append(text)
        return "已记录"

    def _read(ctx, path, **_):
        return "文件内容"

    reg.register(Tool("record_note", "记录一条结论", {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"]}, _record, mutates=True))
    reg.register(Tool("read_file", "读文件", {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"]}, _read))
    return reg


class _PolicyPatch:
    """临时给 executor 加工具权限，退场时精确还原（避免污染全局策略表）。"""

    def __init__(self, tools: set[str]):
        self.tools = tools
        self.saved = None

    def __enter__(self):
        self.saved = set(AGENT_POLICY["executor"]["tools"])
        AGENT_POLICY["executor"]["tools"] = self.saved | self.tools

    def __exit__(self, *exc):
        AGENT_POLICY["executor"]["tools"] = self.saved


class TestOutputQuality(unittest.TestCase):
    def _orch(self, llm, registry, max_tool_iters=4, **cfg):
        agents = {
            "planner": Agent("planner", llm),
            "critic": Agent("critic", llm),
            "executor": Agent("executor", llm, registry=registry,
                              max_tool_iters=max_tool_iters),
        }
        defaults = dict(state_timeouts={"Planning": 5, "Reviewing": 5, "Executing": 5},
                        retry_backoff=[0.01], max_retries=2, auto_confirm=True)
        defaults.update(cfg)
        return Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(**defaults))

    def test_mutation_without_marker_goes_to_review_not_blocked(self):
        """有 record_note 落库、只是没写标记 → 进 Reviewing，而不是 Blocked。"""
        notes: list[str] = []
        turn = {"executor": 0}

        def handler(messages, tools):
            role = _detect_role(messages)
            if role == "executor":
                turn["executor"] += 1
                if turn["executor"] == 1:
                    return LLMReply(tool_calls=[ToolCall("record_note",
                                                         {"text": "第 1 条问题"})])
                return LLMReply(content="报告完毕，共 1 条问题。")   # 故意不写 REQUEST_REVIEW
            return LLMReply(content="审核通过")

        with _PolicyPatch({"record_note"}):
            orch = self._orch(MockLLM(handler=handler), _make_registry(notes))
            task = orch.create_task("审查代码")
            result = asyncio.run(orch.run(task))

        self.assertEqual(result.state, "Done")
        self.assertEqual(notes, ["第 1 条问题"])
        reasons = [f["reason"] for f in task.flow_log]
        self.assertIn("no_marker_but_mutated", reasons)

    def test_read_only_without_marker_still_blocks(self):
        """只读了文件、没产出任何副作用 → 仍按无产出处理（不能放宽成无限容忍）。"""
        notes: list[str] = []
        turn = {"executor": 0}

        def handler(messages, tools):
            role = _detect_role(messages)
            if role == "executor":
                turn["executor"] += 1
                return LLMReply(tool_calls=[ToolCall("read_file", {"path": "a.py"})])
            return LLMReply(content="审核通过")

        with _PolicyPatch({"record_note", "read_file"}):
            orch = self._orch(MockLLM(handler=handler), _make_registry(notes),
                              max_tool_iters=2, max_retries=1)
            task = orch.create_task("审查代码")
            result = asyncio.run(orch.run(task))

        self.assertEqual(result.state, "Blocked")
        self.assertIn("DONE", result.blocked_reason)
        self.assertEqual(notes, [])

    def test_tool_loop_exhaustion_still_yields_final_answer(self):
        """工具轮次打满后，还有一次「不许调工具、只输出结论」的收尾调用。"""
        notes: list[str] = []
        seen = {"final_turn": False}

        def handler(messages, tools):
            role = _detect_role(messages)
            if role != "executor":
                return LLMReply(content="审核通过")
            if tools is None:                     # 收尾轮：不再给工具
                seen["final_turn"] = True
                return LLMReply(content="共 2 条问题。REQUEST_REVIEW")
            return LLMReply(tool_calls=[ToolCall("record_note",
                                                 {"text": f"问题 {len(notes) + 1}"})])

        with _PolicyPatch({"record_note"}):
            orch = self._orch(MockLLM(handler=handler), _make_registry(notes),
                              max_tool_iters=2)
            task = orch.create_task("审查代码")
            result = asyncio.run(orch.run(task))

        self.assertTrue(seen["final_turn"], "轮次耗尽后应有一次无工具的收尾调用")
        self.assertEqual(len(notes), 2)
        self.assertEqual(result.state, "Done")


if __name__ == "__main__":
    unittest.main()
