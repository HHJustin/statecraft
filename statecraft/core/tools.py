"""工具契约 + 命令级权限白名单（源自 Edict 决策八 AGENT_POLICY）。

三条铁律：
1. Agent 只能调用白名单内的工具，越权 → PermissionDenied + 审计日志
2. 改状态（update_state）不对 Agent 开放 —— 那是编排器专属的受控接口
3. 每次 tool 调用都进 AUDIT_LOG，事后可审计
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


class PermissionDenied(Exception):
    """Agent 调用了权限白名单外的工具。"""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                       # JSON Schema
    handler: Callable                      # handler(ctx, **arguments) -> str
    undo: Callable | None = None           # undo(ctx, result, **arguments) -> str（同步）
                                           # 声明了 undo 的工具，执行成功后自动入 SagaLog


def tool_schema(t: Tool) -> dict:
    """转 OpenAI function-calling 格式。"""
    return {"type": "function",
            "function": {"name": t.name, "description": t.description,
                         "parameters": t.parameters}}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未注册的工具: {name}")
        return self._tools[name]

    def schemas(self, names: set[str] | None = None) -> list[dict]:
        return [tool_schema(t) for n, t in self._tools.items()
                if names is None or n in names]


# ---- 命令级权限白名单（声明式策略表）----
AGENT_POLICY = {
    "planner":  {"role": "coordination", "tools": {"report_progress", "request_block",
                                                   "delegate_subtask"}},
    "critic":   {"role": "review",       "tools": {"report_progress", "request_block"}},
    "executor": {"role": "execution",    "tools": {"report_progress", "request_block"}},
}

AUDIT_LOG: list[dict] = []   # 模块级审计日志（阶段 7 换 JSONL 落盘）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_permission(agent_name: str, role: str, tool_name: str) -> None:
    policy = AGENT_POLICY.get(role) or AGENT_POLICY.get(agent_name)
    allowed = policy["tools"] if policy else set()
    verdict = "OK" if tool_name in allowed else "DENIED"
    AUDIT_LOG.append({"agent": agent_name, "role": role, "tool": tool_name,
                      "verdict": verdict, "at": _now()})
    if verdict == "DENIED":
        raise PermissionDenied(
            f"Agent[{agent_name}/{role}] 无权调用工具 {tool_name}（白名单外）")


async def call_tool(agent, registry: ToolRegistry, name: str, arguments: dict):
    """先权限校验，再执行 —— 路线图「先校验再执行」纪律。

    带 undo 的工具执行成功后，自动记账进 SagaLog（ctx 里要有 saga + task），
    供编排器在打回/取消时逆序补偿。
    """
    check_permission(agent.name, agent.role, name)
    tool = registry.get(name)
    result = tool.handler(agent.ctx, **(arguments or {}))
    import inspect
    if inspect.isawaitable(result):
        result = await result
    saga = agent.ctx.get("saga")
    if tool.undo is not None and saga is not None and "task" in agent.ctx:
        saga.record(agent.ctx["task"].id, name, arguments or {},
                    result, tool.undo, agent.ctx)
    return result


# ---- 内置工具（受控接口：Agent 改变系统的唯一途径）----
def _report_progress(ctx: dict, text: str, **_) -> str:
    ctx["task"].progress_log.append(
        {"agent": ctx["agent"].name, "text": text, "at": _now()})
    return "progress recorded"


def _request_block(ctx: dict, reason: str, **_) -> str:
    ctx["task"].blocked_reason = reason
    ctx["request_block"] = True
    return "block requested"


def _delegate_subtask(ctx: dict, description: str, **_) -> str:
    """委派子任务：经编排器创建，深度/成环闸门在 DelegationTracker。

    DelegationDenied 会沿调用链抛回 AgentRunner，回填给 LLM 让它知道边界。
    """
    orch = ctx.get("orchestrator")
    if orch is None:
        return "委派失败：当前无编排器上下文"
    child = orch.create_task(description, parent=ctx["task"])
    return f"已委派子任务 {child.id}（描述：{description}），等待编排调度"


BUILTIN_TOOLS = [
    Tool("report_progress", "汇报任务进展（强制上报义务）",
         {"type": "object", "properties": {"text": {"type": "string"}},
          "required": ["text"]}, _report_progress),
    Tool("request_block", "遇到无法解决的问题时申请阻塞任务，等待人工介入",
         {"type": "object", "properties": {"reason": {"type": "string"}},
          "required": ["reason"]}, _request_block),
    Tool("delegate_subtask", "把一块可独立完成的工作委派为子任务"
         "（受委派深度与成环检测约束，超限会被拒绝）",
         {"type": "object", "properties": {"description": {"type": "string"}},
          "required": ["description"]}, _delegate_subtask),
]

_default_registry: ToolRegistry | None = None


def default_registry() -> ToolRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
        for t in BUILTIN_TOOLS:
            _default_registry.register(t)
    return _default_registry
