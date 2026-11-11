"""真实模型冒烟暴露的四个缺陷的回归测试。

来源：2026-11-11 用 DeepSeek 跑 codedoc 场景时暴露，MockLLM 此前测不出来
（脚本化 LLM 不会真的去调越权工具，也不会挑刺打回）：

1. 暴露层与执行层的权限解析不一致 → role="review" 时 fail-open 暴露全部工具
2. 审核打回没有上限 → Planning ↔ Reviewing 无限循环（真金白银在烧）
3. 审核者拿不到「上一步产出」→ 只能反复打回「未提供待审内容」
4. 契约标记大小写敏感 → 模型输出 "reject" / "done" 时漏判
"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.state_machine import Task
from statecraft.core.tools import (PermissionDenied, Tool, ToolRegistry,
                                   check_permission, resolve_policy)
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply
from statecraft.providers.mock import MockLLM


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool("read_file", "读文件", {"type": "object", "properties": {}},
                      lambda ctx, **_: "content"))
    reg.register(Tool("edit_file", "改文件", {"type": "object", "properties": {}},
                      lambda ctx, **_: "edited"))
    return reg


def _agents(llm) -> dict:
    return {n: Agent(n, llm) for n in ("planner", "critic", "executor")}


class PolicyResolutionTest(unittest.TestCase):
    """缺陷 1：两套解析逻辑 → fail-open。"""

    def test_resolve_falls_back_to_agent_name(self):
        # role 是角色分类 review，策略表 key 是 agent 名 critic
        policy = resolve_policy("critic", "review")
        self.assertIsNotNone(policy)
        self.assertNotIn("edit_file", policy["tools"])

    def test_execution_layer_denies(self):
        with self.assertRaises(PermissionDenied):
            check_permission("critic", "review", "edit_file")

    def test_exposure_layer_does_not_leak_all_tools(self):
        """role 查不到策略时必须回落到 agent 名，而不是「暴露全部工具」。"""
        seen = {}
        llm = MockLLM(script={"critic": ["审核通过"]})
        orig = llm.chat

        async def spy(messages, tools=None):
            seen["tools"] = [t["function"]["name"] for t in (tools or [])]
            return await orig(messages, tools)

        llm.chat = spy
        agent = Agent("critic", llm, role="review", registry=_registry())
        asyncio.run(agent.run(Task(id="t1", description="审一下", state="Reviewing")))
        # critic 的白名单只有 report_progress/request_block，registry 里没有 → 空
        self.assertEqual(seen["tools"], [])


class RejectLoopGuardTest(unittest.TestCase):
    """缺陷 2：打回无上限 → 无限循环。"""

    def test_blocked_after_max_rejections(self):
        llm = MockLLM(script={"planner": [f"计划{i}" for i in range(9)],
                              "critic": ["打回：不行"] * 9,
                              "executor": ["DONE"]})
        cfg = AgentCoreConfig(max_rejections=1)
        orch = Orchestrator(_agents(llm), InMemoryMemory(), cfg)
        task = asyncio.run(orch.run(orch.create_task("写个东西")))

        self.assertEqual(task.state, "Blocked")
        self.assertEqual(task.flow_log[-1]["reason"], "review_rejected_too_many")
        self.assertEqual(task.reject_count, 2)          # 上限 1 → 第 2 次打回即停
        self.assertLess(len(task.flow_log), 12)         # 有限步内收敛，没有空转

    def test_normal_reject_then_pass_is_unaffected(self):
        llm = MockLLM(script={"planner": ["计划A", "计划B"],
                              "critic": ["打回：缺少数据来源", "审核通过"],
                              "executor": ["DONE"]})
        orch = Orchestrator(_agents(llm), InMemoryMemory(), AgentCoreConfig())
        task = asyncio.run(orch.run(orch.create_task("写个报告")))
        self.assertEqual(task.state, "Done")
        self.assertEqual(task.reject_count, 1)


class PriorOutputInjectionTest(unittest.TestCase):
    """缺陷 3：审核者看不到被审对象。"""

    def test_critic_sees_planner_output(self):
        seen: list[tuple[str, str]] = []

        def handler(messages, tools):
            role = "?"
            for m in messages:
                if m.get("role") == "system" and "[role:" in m["content"]:
                    role = m["content"].split("[role:", 1)[1].split("]", 1)[0]
                    break
            seen.append((role, messages[1]["content"]))
            if role == "planner":
                return LLMReply(content="第一步读文件，第二步补 docstring")
            if role == "critic":
                return LLMReply(content="审核通过")
            return LLMReply(content="做完了 DONE")

        orch = Orchestrator(_agents(MockLLM(handler=handler)),
                            InMemoryMemory(), AgentCoreConfig())
        asyncio.run(orch.run(orch.create_task("给代码补注释")))

        critic_ctx = [u for r, u in seen if r == "critic"][0]
        self.assertIn("本任务上一步产出", critic_ctx)
        self.assertIn("第一步读文件，第二步补 docstring", critic_ctx)

    def test_first_agent_has_no_prior_output(self):
        seen: list[str] = []

        def handler(messages, tools):
            seen.append(messages[1]["content"])
            return LLMReply(content="计划" if len(seen) == 1 else "DONE")

        orch = Orchestrator(_agents(MockLLM(handler=handler)),
                            InMemoryMemory(), AgentCoreConfig())
        asyncio.run(orch.run(orch.create_task("写个计划")))
        self.assertNotIn("本任务上一步产出", seen[0])


class ContractMarkCaseTest(unittest.TestCase):
    """缺陷 4：契约标记大小写敏感。"""

    def test_lowercase_reject_triggers_rollback(self):
        llm = MockLLM(script={"planner": ["计划A", "计划B"],
                              "critic": ["reject: 缺少数据来源", "审核通过"],
                              "executor": ["DONE"]})
        orch = Orchestrator(_agents(llm), InMemoryMemory(), AgentCoreConfig())
        task = asyncio.run(orch.run(orch.create_task("写个报告")))
        self.assertEqual(task.reject_count, 1)
        self.assertEqual(task.state, "Done")

    def test_lowercase_done_finishes(self):
        llm = MockLLM(script={"planner": ["计划A"], "critic": ["审核通过"],
                              "executor": ["做完了 done"]})
        orch = Orchestrator(_agents(llm), InMemoryMemory(), AgentCoreConfig())
        task = asyncio.run(orch.run(orch.create_task("写个报告")))
        self.assertEqual(task.state, "Done")


class ReviewFocusTest(unittest.TestCase):
    """缺陷 5：Reviewing 同时承担「审计划」和「审产出」，审核者不知道在审什么。"""

    def test_review_focus_differs_by_source_state(self):
        seen: list[tuple[str, str]] = []

        def handler(messages, tools):
            role = "?"
            for m in messages:
                if m.get("role") == "system" and "[role:" in m["content"]:
                    role = m["content"].split("[role:", 1)[1].split("]", 1)[0]
                    break
            seen.append((role, messages[1]["content"]))
            if role == "planner":
                return LLMReply(content="计划：先读文件再补注释")
            if role == "critic":
                # 第一次审计划 → 通过；第二次审产出 → 通过收尾
                if sum(1 for r, _ in seen if r == "critic") == 1:
                    return LLMReply(content="审核通过")
                return LLMReply(content="审核通过 DONE")
            return LLMReply(content="注释已写入 REQUEST_REVIEW")

        orch = Orchestrator(_agents(MockLLM(handler=handler)),
                            InMemoryMemory(), AgentCoreConfig())
        asyncio.run(orch.run(orch.create_task("给代码补注释")))

        critic_ctxs = [u for r, u in seen if r == "critic"]
        self.assertIn("[审核对象｜来自 Planning]", critic_ctxs[0])
        self.assertIn("计划（未执行", critic_ctxs[0])
        self.assertIn("[审核对象｜来自 Executing]", critic_ctxs[1])
        self.assertIn("执行产出", critic_ctxs[1])


class ReviewOutcomeBySourceTest(unittest.TestCase):
    """缺陷 6：「审核通过」的去向不能靠模型措辞，必须靠进入 Reviewing 前的状态。"""

    def test_output_review_pass_finishes_without_re_executing(self):
        llm = MockLLM(script={"planner": ["计划：补三条注释"],
                              "critic": ["审核通过", "审核通过"],   # 只说通过，不带 DONE
                              "executor": ["注释已写入 REQUEST_REVIEW"]})
        orch = Orchestrator(_agents(llm), InMemoryMemory(), AgentCoreConfig())
        task = asyncio.run(orch.run(orch.create_task("给代码补注释")))

        self.assertEqual(task.state, "Done")
        executes = [f for f in task.flow_log if f["to"] == "Executing"]
        self.assertEqual(len(executes), 1, "审产出通过后不应再次进入执行")

    def test_plan_review_pass_leads_to_execute(self):
        llm = MockLLM(script={"planner": ["计划：补三条注释"],
                              "critic": ["审核通过", "审核通过"],
                              "executor": ["注释已写入 REQUEST_REVIEW"]})
        orch = Orchestrator(_agents(llm), InMemoryMemory(), AgentCoreConfig())
        task = asyncio.run(orch.run(orch.create_task("给代码补注释")))
        states = [f["to"] for f in task.flow_log]
        self.assertEqual(states[:3], ["Planning", "Reviewing", "Executing"])


if __name__ == "__main__":
    unittest.main()
