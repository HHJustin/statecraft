"""Provider 错误分类与重试（P12 真机实测被 429 打断后补的）。

这次不是「加个功能」，是修一个会让 P12 落库说谎的缺陷：
限流时异常打穿主循环 → 任务既不 Done 也不 Blocked → 批次永远停在 draft，
库里躺着一个「正在跑」的审查，实际早就死了。

两个层次都要测：
1. 分类正确（429 可重试 / 401 不可重试，各自带人话）；
2. 编排器把不可恢复的错误降级成 Blocked，而不是抛出去。
"""
import asyncio
import unittest

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply, ProviderError
from statecraft.providers.mock import MockLLM
from statecraft.providers.openai_compat import _classify


def make_orch(handler, **cfg):
    llm = MockLLM(handler=handler)
    agents = {name: Agent(name, llm) for name in ("planner", "critic", "executor")}
    defaults = dict(state_timeouts={"Planning": 5.0, "Reviewing": 5.0,
                                    "Executing": 5.0},
                    retry_backoff=[0.0], max_retries=1, auto_confirm=True)
    defaults.update(cfg)
    return Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(**defaults))


class TestClassify(unittest.TestCase):
    def test_rate_limit_is_retryable(self):
        err = _classify(429, '{"error":"rate limit exceeded"}')
        self.assertTrue(err.retryable)
        self.assertEqual(err.status, 429)
        self.assertIn("限流", str(err))

    def test_auth_error_is_not_retryable(self):
        err = _classify(401, "invalid api key")
        self.assertFalse(err.retryable)
        self.assertIn("OPENAI_API_KEY", str(err))

    def test_bad_model_name_hints_base_url(self):
        self.assertIn("OPENAI_MODEL", str(_classify(404)))

    def test_quota_exhausted_429_is_not_retryable(self):
        """apigoto 的 42906：状态码是 429，但「额度已用完」重试毫无意义。"""
        err = _classify(429, '{"code":42906,"message":"您的额度已用完"}')
        self.assertEqual(err.status, 429)
        self.assertFalse(err.retryable)
        self.assertIn("额度", str(err))

    def test_plain_429_still_retryable(self):
        """没有额度字样的 429 仍然当限流处理，别把真限流也判死。"""
        self.assertTrue(_classify(429, "rate limit exceeded, retry later").retryable)

    def test_server_errors_retryable(self):
        for code in (500, 502, 503):
            self.assertTrue(_classify(code).retryable, code)


class TestOrchestratorDegrades(unittest.TestCase):
    def test_persistent_rate_limit_ends_blocked_not_raised(self):
        """限流到底也要给一个终态，不能让调用方「什么都没收到」。"""
        async def always_429(messages, tools):
            raise _classify(429, "too many requests")

        orch = make_orch(always_429)
        task = orch.create_task("会被限流的任务")
        result = asyncio.run(orch.run(task))          # 不应抛异常
        self.assertEqual(result.state, "Blocked")
        self.assertTrue(result.blocked_reason)

    def test_transient_rate_limit_recovers(self):
        """429 一次之后成功：重试必须把流程救回来，且不留 Blocked。"""
        state = {"n": 0}

        async def flaky(messages, tools):
            state["n"] += 1
            if state["n"] == 1:
                raise _classify(429, "slow down")
            text = "审核通过" if "critic" in messages[0].get("content", "") else "完成 DONE"
            return LLMReply(content=text)

        orch = make_orch(flaky)
        task = orch.create_task("先被限流再成功的任务")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "Done")
        self.assertGreaterEqual(state["n"], 2)        # 确实重试过

    def test_auth_error_blocks_immediately(self):
        calls = {"n": 0}

        async def bad_key(messages, tools):
            calls["n"] += 1
            raise _classify(401, "invalid key")

        orch = make_orch(bad_key, max_retries=3,
                         retry_backoff=[0.0, 0.0, 0.0])
        task = orch.create_task("key 写错的任务")
        result = asyncio.run(orch.run(task))
        self.assertEqual(result.state, "Blocked")
        # 不可重试 → 只尝试一次，不该按 max_retries 反复烧配额
        self.assertEqual(calls["n"], 1)


class TestProviderErrorType(unittest.TestCase):
    def test_default_not_retryable(self):
        self.assertFalse(ProviderError("boom").retryable)
        self.assertIsNone(ProviderError("boom").status)


if __name__ == "__main__":
    unittest.main()
