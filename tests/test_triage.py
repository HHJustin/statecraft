"""意图识别 / 分拣节点测试：规则分拣 + 编排器直通 + 可插拔分拣器。"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.triage import RuleTriage, TriageBase
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM


class TestRuleTriage(unittest.TestCase):
    def setUp(self):
        self.triage = RuleTriage()

    def test_chitchat_detected(self):
        for text in ("你好", "在吗？", "谢谢啦", "哈哈 666", "再见拜拜", "  "):
            self.assertEqual(self.triage.classify(text)["intent"],
                             "chitchat", msg=text)

    def test_task_detected(self):
        for text in ("写一份本周项目周报", "整理调研报告", "修复登录 bug",
                     "为代码文件 a.py 补充 docstring", "执行不完整的任务"):
            self.assertEqual(self.triage.classify(text)["intent"],
                             "task", msg=text)

    def test_mixed_input_not_killed(self):
        # 混合输入：寒暄开头 + 行动请求 → 必须按任务处理（防误杀）
        self.assertEqual(self.triage.classify("你好，帮我写个爬虫")["intent"],
                         "task")

    def test_conservative_fallback_is_task(self):
        # 拿不准 → 默认任务（漏放代价 < 误杀代价）
        self.assertEqual(self.triage.classify("这句话没有明显动作")["intent"],
                         "task")


class TestTriageInPipeline(unittest.TestCase):
    def _orch(self, **cfg):
        llm = MockLLM(script={
            "planner": ["计划"],
            "critic": ["审核通过"],
            "executor": ["完成。DONE"],
        })
        agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
        defaults = dict(
            state_timeouts={"Planning": 5.0, "Reviewing": 5.0, "Executing": 5.0},
            retry_backoff=[0.01], max_retries=1, auto_confirm=True)
        defaults.update(cfg)
        return Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(**defaults))

    def test_chitchat_short_circuits_pipeline(self):
        orch = self._orch()
        task = orch.create_task("在吗")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.intent, "chitchat")
        self.assertEqual(result.state, "Done")
        # 分拣直通：不进流水线，从未去过 Planning
        self.assertNotIn("Planning", {f["to"] for f in result.flow_log})
        self.assertIn("triage_chitchat", [f["reason"] for f in result.flow_log])

    def test_task_goes_through_pipeline(self):
        orch = self._orch()
        task = orch.create_task("写一份本周项目周报")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.intent, "task")
        self.assertEqual(result.state, "Done")
        self.assertIn("Planning", {f["to"] for f in result.flow_log})

    def test_triage_can_be_disabled(self):
        orch = self._orch(triage_enabled=False)
        task = orch.create_task("在吗")
        result = asyncio.run(orch.run(task))
        self.assertIsNone(result.intent)              # 未分拣
        self.assertIn("Planning", {f["to"] for f in result.flow_log})  # 照常进流水线

    def test_pluggable_llm_triage(self):
        # TriageBase 协议可插拔：模拟「LLM 分拣器」把一切都判为闲聊
        class FakeLLMTriage(TriageBase):
            def classify(self, text):
                return {"intent": "chitchat", "reason": "fake-llm"}

        orch = self._orch(triage_classifier=FakeLLMTriage())
        task = orch.create_task("写一份周报")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.intent, "chitchat")
        self.assertEqual(result.intent_reason, "fake-llm")
        self.assertEqual(result.state, "Done")


if __name__ == "__main__":
    unittest.main()
