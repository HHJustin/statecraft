"""最小闭环演示：任务 Created → Planning → Reviewing → Executing → Done。

运行：python demo.py
"""
import asyncio

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM


async def main():
    # 1. MockLLM：脚本化三个角色的输出（换成真实 LLM 只需改 provider 配置）
    llm = MockLLM(script={
        "planner": ["1. 收集本周项目进展数据；2. 提炼三个亮点与风险；3. 按模板输出周报。"],
        "critic": ["审核通过：计划覆盖数据、亮点与产出物，有明确验收方式。"],
        "executor": ["周报已生成：含 3 个亮点、2 个风险及数据来源。DONE"],
    })
    agents = {name: Agent(name, llm) for name in ("planner", "critic", "executor")}

    # 2. 记忆层：预置一条共享记忆，演示「记忆注入」
    mem = InMemoryMemory()
    mem.add("团队约定：周报必须包含数据来源与风险提示", scope="shared")

    # 3. 编排器（超时/退避改为演示用的小值）
    config = AgentCoreConfig(
        retry_backoff=[0.05, 0.1],
        state_timeouts={"Planning": 10.0, "Reviewing": 10.0, "Executing": 10.0},
    )
    orch = Orchestrator(agents, mem, config)

    # 4. 跑一个任务
    task = orch.create_task("写一份本周项目周报")
    task = await orch.run(task)

    print("=" * 60)
    print("最终状态:", task.state)
    print("状态流转:", " → ".join(["Created"] + [f["to"] for f in task.flow_log]))
    print("进展上报:", len(task.progress_log), "条")
    print("记忆条数:", len(mem))
    print("-" * 60)
    for f in task.flow_log:
        print(f"  {f['from']:>14} → {f['to']:<14} ({f['reason']})")
    print("-" * 60)
    for h in mem.search("周报", limit=10):
        print(f"  [{h.scope}] {h.text}")


if __name__ == "__main__":
    asyncio.run(main())
