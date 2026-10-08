"""编排器端到端测试（MockLLM 离线跑，覆盖：闭环 / 打回 / 升级 / 二次确认）。"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM


def make_orch(script=None, handler=None, **cfg):
    llm = MockLLM(script=script, handler=handler)
    agents = {name: Agent(name, llm)
              for name in ("planner", "critic", "executor")}
    defaults = dict(
        state_timeouts={"Planning": 5.0, "Reviewing": 5.0, "Executing": 5.0},
        retry_backoff=[0.01, 0.01, 0.01],
        max_retries=2,
        auto_confirm=True,
    )
    defaults.update(cfg)
    return Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(**defaults))


class TestOrchestrator(unittest.TestCase):
    def test_happy_path_end_to_end(self):
        orch = make_orch({
            "planner": ["1. 收集数据；2. 提炼亮点；3. 输出周报。"],
            "critic": ["审核通过：计划覆盖完整，有验收标准。"],
            "executor": ["周报已生成，含数据来源。DONE"],
        })
        task = orch.create_task("写一份本周项目周报")
        result = asyncio.run(orch.run(task))

        self.assertEqual(result.state, "Done")
        states = [f["to"] for f in task.flow_log]
        # 关卡顺序：Planning → Reviewing → Executing →（PendingConfirm）→ Done
        self.assertEqual(states, ["Planning", "Reviewing", "Executing",
                                  "PendingConfirm", "Done"])
        # 记忆沉淀：任务目标与各 Agent 输出都在
        hits = orch.memory.search("周报", limit=10)
        self.assertTrue(any("任务目标" in h.text for h in hits))
        self.assertTrue(any("DONE" in h.text for h in hits))

    def test_reject_loop_then_pass(self):
        orch = make_orch({
            "planner": ["计划A", "计划B（已补充数据来源）"],
            "critic": ["打回：缺少数据来源。", "审核通过：已修复。"],
            "executor": ["产出完成。DONE"],
        })
        task = orch.create_task("整理调研报告")
        result = asyncio.run(orch.run(task))

        self.assertEqual(result.state, "Done")
        states = [f["to"] for f in task.flow_log]
        self.assertEqual(states, ["Planning", "Reviewing", "Planning",
                                  "Reviewing", "Executing",
                                  "PendingConfirm", "Done"])

    def test_stall_escalates_to_blocked(self):
        # 异步慢 handler：超过超时预算 → wait_for 触发 TimeoutError → 升级
        from statecraft.providers.base import LLMReply

        async def slow_handler(messages, tools):
            await asyncio.sleep(0.5)
            return LLMReply(content="卡住了")

        orch = make_orch(handler=slow_handler,
                         state_timeouts={"Planning": 0.05, "Reviewing": 0.05,
                                         "Executing": 0.05},
                         retry_backoff=[0.0], max_retries=1)
        task = orch.create_task("永远跑不完的任务")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "Blocked")
        # 升级链路应出现过 escalate
        reasons = [f["reason"] for f in task.flow_log]
        self.assertIn("escalate", reasons)

    def test_pending_confirm_waits_for_human(self):
        orch = make_orch({
            "planner": ["计划"],
            "critic": ["审核通过"],
            "executor": ["完成。DONE"],
        }, auto_confirm=False)
        task = orch.create_task("需要人工确认的任务")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "PendingConfirm")
        self.assertEqual(task.pending_target, "Done")
        # 人工放行
        from statecraft.core.state_machine import approve
        approve(task)
        self.assertEqual(task.state, "Done")

    def test_executor_without_done_marker_blocks(self):
        orch = make_orch({
            "planner": ["计划"],
            "critic": ["审核通过"],
            "executor": ["做了一半"],
        }, max_retries=1)
        task = orch.create_task("执行不完整的任务")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "Blocked")
        self.assertIn("DONE", task.blocked_reason)


if __name__ == "__main__":
    unittest.main()
