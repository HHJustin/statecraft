"""事件总线 / Outbox / DLQ 测试（P6）。

注意：asyncio.Queue 绑定事件循环，整个场景必须在同一个 asyncio.run 内完成。
"""
import asyncio
import unittest

from statecraft.core.events import DispatchError, Event, EventBus, Outbox


class TestEvents(unittest.TestCase):
    def test_outbox_relay(self):
        ob = Outbox()
        ob.enqueue(Event("task.status", {"state": "Planning"}))
        evs = ob.relay()
        self.assertEqual(len(evs), 1)
        self.assertEqual(ob.relay(), [])          # 投递后清空

    def test_event_auto_id(self):
        e1, e2 = Event("t", {}), Event("t", {})
        self.assertTrue(e1.id and e2.id and e1.id != e2.id)

    def test_publish_consume_ok(self):
        bus, got = EventBus(), []

        def h(e):
            got.append(e.payload)
        bus.subscribe("t", h)

        async def main():
            await bus.publish(Event("t", {"x": 1}))
            await bus.run_once()
        asyncio.run(main())
        self.assertEqual(got, [{"x": 1}])
        self.assertEqual(bus.dlq, [])

    def test_retryable_requeued_then_dlq(self):
        bus, calls = EventBus(), {"n": 0}

        def flaky(e):
            calls["n"] += 1
            raise DispatchError("下游抖动", retryable=True)
        bus.subscribe("t", flaky)

        async def main():
            await bus.publish(Event("t", {}))
            while bus.pending():
                await bus.run_once()
        asyncio.run(main())
        self.assertEqual(calls["n"], 3)           # MAX_DELIVERIES=3 次后进 DLQ
        self.assertEqual(len(bus.dlq), 1)

    def test_non_retryable_immediate_dlq(self):
        bus, calls = EventBus(), {"n": 0}

        def broken(e):
            calls["n"] += 1
            raise DispatchError("配置错误", retryable=False)
        bus.subscribe("t", broken)

        async def main():
            await bus.publish(Event("t", {}))
            await bus.run_once()
        asyncio.run(main())
        self.assertEqual(calls["n"], 1)           # 不可重试 → 只调一次
        self.assertEqual(len(bus.dlq), 1)

    def test_unclassified_exception_goes_dlq(self):
        bus = EventBus()

        def bad(e):
            raise ValueError("未分类错误")
        bus.subscribe("t", bad)

        async def main():
            await bus.publish(Event("t", {}))
            await bus.run_once()
        asyncio.run(main())
        self.assertEqual(len(bus.dlq), 1)
        self.assertIn("ValueError", bus.dlq[0]["error"])

    def test_run_once_on_empty_queue_returns_none(self):
        bus = EventBus()
        self.assertIsNone(asyncio.run(bus.run_once()))   # 不阻塞


if __name__ == "__main__":
    unittest.main()
