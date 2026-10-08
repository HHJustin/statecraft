"""综合演示：委派 + Saga 补偿 + 看板（离线，MockLLM，零依赖）。

场景：
  t1 根任务（周报）—— 正常闭环到 Done
  t2 委派子任务（补充数据）—— executor 写入 kv 副作用 → 停在人工确认 → 拒绝 →
     取消 → Saga 逆序补偿，副作用被撤销
  t3 子任务 —— 执行不完整 → Blocked
最后把全部任务 + 会话日志渲染成静态 HTML 看板。
"""
import asyncio

from statecraft.config import AgentCoreConfig
from statecraft.core.observability import SessionLogger
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.state_machine import reject
from statecraft.core.tools import AGENT_POLICY, Tool, ToolRegistry
from statecraft.dashboard import write_dashboard
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply, ToolCall
from statecraft.providers.mock import MockLLM

AGENT_POLICY["kv_writer"] = {"role": "execution", "tools": {"kv_set"}}

STORE: dict = {}


def _detect_role(messages):
    for m in messages:
        if m.get("role") == "system":
            return m["content"].split("[role:", 1)[1].split("]", 1)[0].strip()
    return None


def make_kv_tool() -> Tool:
    def handler(ctx, key, value, **_):
        old = STORE.get(key)
        STORE[key] = value
        return f"old={old}"

    def undo(ctx, result, key, value, **_):
        old = result.split("=", 1)[1]
        if old == "None":
            STORE.pop(key, None)
        else:
            STORE[key] = old
        return "restored"

    return Tool("kv_set", "写入键值（带 undo 的可补偿工具）",
                {"type": "object",
                 "properties": {"key": {"type": "string"},
                                "value": {"type": "string"}},
                 "required": ["key", "value"]},
                handler, undo=undo)


async def main():
    logger = SessionLogger()
    registry = ToolRegistry()
    registry.register(make_kv_tool())

    def handler(messages, tools):
        """按角色/任务分发的脚本化 LLM：t2 的 executor 真实发起一次 kv_set 工具调用。"""
        role = _detect_role(messages)
        user = next((m.get("content", "") for m in messages
                     if m.get("role") == "user"), "")
        if role == "planner":
            return LLMReply(content=f"计划：{user.splitlines()[0].replace('任务：', '')}")
        if role == "critic":
            return LLMReply(content="审核通过")
        # executor
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if "补充数据来源" in user:
            if not has_tool_result:
                return LLMReply(tool_calls=[ToolCall(
                    name="kv_set",
                    arguments={"key": "data_source", "value": "v1"})])
            return LLMReply(content="写入完成。DONE")
        if "周报" in user:
            return LLMReply(content="周报已生成。DONE")
        return LLMReply(content="做了一半")

    llm = MockLLM(handler=handler)
    agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
    agents["executor"].role = "kv_writer"          # executor 具备 kv_set 权限
    agents["executor"].registry = registry

    orch = Orchestrator(agents, InMemoryMemory(),
                        AgentCoreConfig(auto_confirm=False),  # 停在人工确认，演示 Saga
                        logger=logger)

    # --- t1 根任务：正常闭环 ---
    t1 = orch.create_task("写一份本周项目周报")
    await orch.run(t1)

    # --- t2 委派子任务：有副作用，人工拒绝后取消 → Saga 补偿 ---
    t2 = orch.create_task("补充数据来源", parent=t1)     # 走委派闸门
    await orch.run(t2)                                  # executor 已写入 kv，停在 PendingConfirm
    print(f"[demo] t2 写入副作用：{STORE}")
    reject(t2)                                          # 人工拒绝 → Executing
    await orch.cancel(t2)                               # 取消 → Saga 逆序补偿
    print(f"[demo] t2 取消补偿后：{STORE}（副作用已撤销）")
    print(f"[demo] Saga 审计：{orch.saga.history}")

    # --- t3 子任务：执行不完整 → Blocked ---
    t3 = orch.create_task("整理归档", parent=t1)
    await orch.run(t3)

    # --- 委派链一览 ---
    print(f"[demo] 委派边：{orch.delegations.edges}，t3 深度={orch.delegations.depth('t3')}")

    # --- 看板 ---
    out = write_dashboard("dashboard_demo.html", [t1, t2, t3], logger,
                          title="AgentCore 看板 · 委派与补偿演示")
    print(f"[demo] 看板已生成：{out}")


if __name__ == "__main__":
    asyncio.run(main())
