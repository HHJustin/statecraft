"""事件驱动演示（P6 + P7）：Outbox → EventBus → 订阅者，含重试与 DLQ。

运行：python demo_events.py
"""
import asyncio

from statecraft.config import AgentCoreConfig
from statecraft.core.events import DispatchError, Event, EventBus, Outbox
from statecraft.core.observability import SessionLogger
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM


async def main():
    outbox, bus, logger = Outbox(), EventBus(), SessionLogger()  # logger 传 path 即落盘 JSONL

    # --- 订阅者 1/2：正常记录事件流 ---
    seen = []

    def on_created(e):
        seen.append(f"任务创建 {e.payload['task_id']}: {e.payload['description']}")

    def on_status(e):
        seen.append(f"状态流转 {e.payload['task_id']}: "
                    f"{e.payload['from']} → {e.payload['to']}")

    bus.subscribe("task.created", on_created)
    bus.subscribe("task.status", on_status)

    # --- 订阅者 3：flaky（前 2 次抛可重试异常，第 3 次成功）---
    flaky = {"n": 0}

    def on_progress(e):
        flaky["n"] += 1
        if flaky["n"] < 3:
            raise DispatchError("下游抖动，稍后重试", retryable=True)
        seen.append(f"进展事件处理成功（第 {flaky['n']} 次尝试）")

    bus.subscribe("task.progress", on_progress)

    # --- 编排任务（outbox + logger 接入编排器）---
    llm = MockLLM(script={
        "planner": ["1. 收集数据；2. 输出周报。"],
        "critic": ["审核通过：计划完整。"],
        "executor": ["周报完成。DONE"],
    })
    agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
    orch = Orchestrator(agents, InMemoryMemory(), AgentCoreConfig(),
                        outbox=outbox, logger=logger)
    task = await orch.run(orch.create_task("写一份本周项目周报"))

    # --- Outbox Relay：投递到总线，消费到空 ---
    for ev in outbox.relay():
        await bus.publish(ev)
    while bus.pending():
        await bus.run_once()

    # --- 合成一条进展事件：演示 flaky 订阅者重试（前 2 次失败，第 3 次成功）---
    await bus.publish(Event("task.progress", {"task_id": task.id, "text": "合成进展"}))
    for _ in range(5):
        await bus.run_once()

    # --- 毒丸事件：不可重试 → 直接 DLQ ---
    def poison(e):
        raise DispatchError("配置错误，重试无用", retryable=False)

    bus.subscribe("poison", poison)
    await bus.publish(__import__("statecraft.core.events", fromlist=["Event"])
                      .Event("poison", {"why": "bad config"}))
    await bus.run_once()

    # --- 输出 ---
    print("=" * 62)
    print("任务终态:", task.state)
    print("-" * 62)
    print("事件流（订阅者视角）:")
    for s in seen:
        print("  •", s)
    print("-" * 62)
    print("死信队列 DLQ:")
    for d in bus.dlq:
        print(f"  • [{d['event'].type}] {d['error']}")
    print("-" * 62)
    print("会话日志（活动流，JSONL 落盘可回放）:")
    for r in logger.activity_stream(kinds=["flow", "progress"]):
        print(f"  • [{r['kind']}] {r.get('from', '')}"
              f"{('→' + r['to']) if 'to' in r else r.get('text', '')}")


if __name__ == "__main__":
    asyncio.run(main())
