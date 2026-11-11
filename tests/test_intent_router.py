"""意图路由测试：四分类规则打分 / LLM 兜底与降级 / 与编排器的分拣直通。"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.triage import IntentRouter
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM

ROUTES = {
    "doc": {"补充注释": 3, "写注释": 3, "docstring": 2, "注释": 1},
    "review": {"审查": 3, "代码审查": 3, "review": 3, "质量": 1},
    "explain": {"解释": 3, "什么意思": 3, "是干什么": 3, "干什么": 2},
}


def fake_llm(route="explain", confidence=0.72):
    def _call(text, labels):
        return {"route": route, "confidence": confidence, "reason": "fake-llm"}
    return _call


class TestRuleRouting(unittest.TestCase):
    def setUp(self):
        self.router = IntentRouter(ROUTES, default_route="doc")

    def test_four_routes(self):
        cases = {
            "给 sample.py 补充 docstring": "doc",
            "审查一下这段代码的质量": "review",
            "解释一下这个函数是干什么的": "explain",
        }
        for text, expected in cases.items():
            v = self.router.classify(text)
            self.assertEqual(v["route"], expected, msg=text)
            self.assertEqual(v["level"], "rule", msg=text)

    def test_weight_beats_shared_noun(self):
        # 「审查注释质量」同时命中 review(3+1) 和 doc(1) → review 胜（动词权重高）
        v = self.router.classify("审查注释质量")
        self.assertEqual(v["route"], "review")

    def test_chitchat_short_circuits(self):
        for text in ("你好", "在吗？", "谢谢啦", "哈哈 666", "   "):
            v = self.router.classify(text)
            self.assertEqual(v["route"], "chitchat", msg=text)
            self.assertEqual(v["intent"], "chitchat", msg=text)

    def test_intent_field_compatible_with_orchestrator(self):
        # 非闲聊一律 intent=task（编排器只认 task/chitchat）
        self.assertEqual(self.router.classify("解释这个函数")["intent"], "task")
        self.assertEqual(self.router.classify("审查代码")["intent"], "task")

    def test_fallback_goes_to_default_route(self):
        v = self.router.classify("这段东西帮我看看")
        self.assertEqual(v["route"], "doc")        # 无关键词命中 → 默认路由
        self.assertEqual(v["level"], "fallback")
        self.assertLess(v["confidence"], 0.5)      # 置信度低，便于上层人工兜底

    def test_default_route_must_exist(self):
        with self.assertRaises(ValueError):
            IntentRouter(ROUTES, default_route="unknown")


class TestLLMFallback(unittest.TestCase):
    def test_gray_zone_asked_to_llm(self):
        router = IntentRouter(ROUTES, default_route="doc",
                             llm_classifier=fake_llm("explain", 0.72))
        v = router.classify("这段东西帮我看看")
        self.assertEqual(v["route"], "explain")
        self.assertEqual(v["level"], "llm")
        self.assertAlmostEqual(v["confidence"], 0.72)

    def test_rule_hit_skips_llm(self):
        calls = []

        def counting(text, labels):
            calls.append(text)
            return {"route": "review"}

        router = IntentRouter(ROUTES, llm_classifier=counting)
        router.classify("审查代码质量")           # 规则命中，不该调 LLM
        self.assertEqual(calls, [])

    def test_llm_exception_degrades_gracefully(self):
        def boom(text, labels):
            raise RuntimeError("provider down")

        router = IntentRouter(ROUTES, default_route="doc", llm_classifier=boom)
        v = router.classify("这段东西帮我看看")
        self.assertEqual(v["route"], "doc")        # 降级到默认路由，不崩
        self.assertEqual(v["level"], "fallback")
        self.assertIn("provider down", router.last_error)

    def test_llm_unknown_route_rejected(self):
        router = IntentRouter(ROUTES, default_route="doc",
                             llm_classifier=fake_llm("nonexistent"))
        v = router.classify("这段东西帮我看看")
        self.assertEqual(v["route"], "doc")
        self.assertEqual(v["level"], "fallback")
        self.assertIn("未知路由", router.last_error)

    def test_llm_string_return_accepted(self):
        router = IntentRouter(ROUTES, default_route="doc",
                             llm_classifier=lambda text, labels: "review")
        v = router.classify("这段东西帮我看看")
        self.assertEqual(v["route"], "review")
        self.assertEqual(v["level"], "llm")

    def test_deadlocked_routes_use_llm(self):
        # 两路同分 → share 0.5 < 0.6，交给 LLM 裁决
        routes = {"a": {"代码": 1}, "b": {"质量": 1}}
        router = IntentRouter(routes, default_route="a",
                             llm_classifier=fake_llm("b", 0.8))
        v = router.classify("代码 质量")
        self.assertEqual(v["route"], "b")
        self.assertEqual(v["level"], "llm")

    def test_deadlocked_without_llm_picks_top_score(self):
        routes = {"a": {"代码": 1}, "b": {"质量": 1}}
        router = IntentRouter(routes, default_route="a")
        v = router.classify("代码 质量")
        self.assertEqual(v["route"], "a")          # 同分按注册顺序
        self.assertEqual(v["level"], "rule")


class TestRouterInPipeline(unittest.TestCase):
    def _orch(self, router):
        llm = MockLLM(script={"planner": ["计划"], "critic": ["审核通过"],
                              "executor": ["完成。DONE"]})
        agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
        cfg = AgentCoreConfig(
            state_timeouts={"Planning": 5.0, "Reviewing": 5.0, "Executing": 5.0},
            retry_backoff=[0.01], max_retries=1, auto_confirm=True,
            triage_classifier=router)
        return Orchestrator(agents, InMemoryMemory(), cfg)

    def test_router_drives_chitchat_shortcut(self):
        orch = self._orch(IntentRouter(ROUTES, default_route="doc"))
        task = orch.create_task("在吗？")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.intent, "chitchat")
        self.assertEqual(result.state, "Done")
        self.assertNotIn("Planning", {f["to"] for f in result.flow_log})

    def test_router_routes_task_into_pipeline(self):
        orch = self._orch(IntentRouter(ROUTES, default_route="doc"))
        task = orch.create_task("解释一下这个函数")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.intent, "task")
        self.assertIn("Planning", {f["to"] for f in result.flow_log})


if __name__ == "__main__":
    unittest.main()
