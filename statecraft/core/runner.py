"""AgentRunner（自写，替代 OpenClaw 的位置）。

职责：加载三层 prompt（GLOBAL → 角色文件）→ 注入上下文 → LLM 工具循环 → 返回文本。
Agent 对系统的全部影响必须经工具（受控接口），越权会被拒绝并回填结果。
"""
import inspect
import json
from pathlib import Path

from .tools import AGENT_POLICY, ToolRegistry, call_tool, default_registry

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def load_prompt(name: str, prompts_dir: Path | None = None) -> str:
    """三层 prompt 继承（源自 Edict）：GLOBAL.md → {name}.md。"""
    base = Path(prompts_dir) if prompts_dir else _PROMPTS_DIR
    parts: list[str] = []
    g = base / "GLOBAL.md"
    if g.exists():
        parts.append(g.read_text(encoding="utf-8"))
    r = base / f"{name}.md"
    if r.exists():
        parts.append(r.read_text(encoding="utf-8"))
    return "\n\n".join(parts) or f"你是 {name}。"


class Agent:
    def __init__(self, name: str, llm, role: str | None = None,
                 system_prompt: str | None = None,
                 registry: ToolRegistry | None = None,
                 max_tool_iters: int = 4):
        self.name = name
        self.role = role or name
        self.llm = llm
        self.registry = registry or default_registry()
        self.system_prompt = (system_prompt if system_prompt is not None
                              else load_prompt(name))
        self.max_tool_iters = max_tool_iters
        self.ctx: dict = {}   # 工具执行时的上下文（task/agent），run 时注入

    async def run(self, task, context_text: str = "") -> str:
        # orchestrator/saga 由编排器在 run 前挂到 agent 属性上（getattr 兜底 None，
        # 保证 Agent 脱离编排器单独使用也不报错）
        self.ctx = {"task": task, "agent": self,
                    "orchestrator": getattr(self, "orchestrator", None),
                    "saga": getattr(self, "saga", None)}
        messages = [
            {"role": "system",
             "content": f"[role:{self.name}]\n{self.system_prompt}"},
            {"role": "user",
             "content": (f"任务：{task.description}\n"
                         f"当前状态：{task.state}\n\n"
                         f"相关记忆/上下文：\n{context_text or '（无）'}")},
        ]
        # 只把白名单内的工具暴露给 LLM
        policy = AGENT_POLICY.get(self.role)
        schemas = self.registry.schemas(policy["tools"]) if policy else self.registry.schemas()

        for _ in range(self.max_tool_iters):
            reply = await self.llm.chat(messages, tools=schemas)
            if not reply.tool_calls:
                return reply.content

            # 回填 assistant 消息（OpenAI 格式），再逐个执行工具
            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": tc.name,
                                  "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                    for i, tc in enumerate(reply.tool_calls)
                ],
            })
            for i, tc in enumerate(reply.tool_calls):
                try:
                    result = await call_tool(self, self.registry, tc.name, tc.arguments)
                except Exception as e:            # 权限拒绝/参数错误都回填，让模型知道边界
                    result = f"工具调用失败：{type(e).__name__}: {e}"
                messages.append({"role": "tool", "tool_call_id": f"call_{i}",
                                 "name": tc.name, "content": str(result)})
        return "（达到工具循环上限，未给出最终输出）"

    # ---- 回滚钩子（路线图决策四，阶段 6 实现 Saga/undo 后启用）----
    async def undo(self) -> None:
        """撤销本 Agent 已产生的副作用（受控工具自带 undo 时由编排器在打回时调用）。"""
        return None
