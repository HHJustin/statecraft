"""AgentRunner（自写，替代 OpenClaw 的位置）。

职责：加载三层 prompt（GLOBAL → 角色文件）→ 注入上下文 → LLM 工具循环 → 返回文本。
Agent 对系统的全部影响必须经工具（受控接口），越权会被拒绝并回填结果。
"""
import inspect
import json
from pathlib import Path

from .tools import (ToolRegistry, call_tool, default_registry,
                    resolve_policy)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents"


class PromptResourceMissing(RuntimeError):
    """包内 agents/ 缺文件 —— 这是打包问题，不是运行时问题。"""


def load_prompt(name: str, prompts_dir: Path | None = None) -> str:
    """三层 prompt 继承（源自 Edict）：GLOBAL.md → {name}.md。

    默认目录是**包内** `statecraft/agents/`，这些 md 靠 pyproject 的
    `package-data` 带进 wheel。缺文件时不许静默降级：原来的写法返回
    「你是 executor。」，装出来的包照样跑、测试照样全绿，但多 agent 的
    全部行为约束凭空消失 —— 没有异常、没有日志，只有输出质量整体掉档。
    """
    base = Path(prompts_dir) if prompts_dir else _PROMPTS_DIR
    parts: list[str] = []
    g = base / "GLOBAL.md"
    if g.exists():
        parts.append(g.read_text(encoding="utf-8"))
    r = base / f"{name}.md"
    # name="GLOBAL" 时 r 就是 g：再拼一次等于把规范读两遍（长度翻倍，
    # 而且模型看到两份一模一样的规则）。实际调用不会传 GLOBAL，但没理由留坑。
    if r.exists() and r.resolve() != g.resolve():
        parts.append(r.read_text(encoding="utf-8"))
    if prompts_dir is None:
        missing = [x.name for x in (g, r) if not x.exists()]
        if missing:
            raise PromptResourceMissing(
                f"包内 agents 目录缺 {missing}（{base}）。多半是 wheel 漏了 "
                f"package-data —— 检查 pyproject 的 [tool.setuptools.package-data]。")
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
        self.mutations = 0    # 本轮 run 内的「有副作用工具调用」次数，由 call_tool 累加
        self.ctx: dict = {}   # 工具执行时的上下文（task/agent），run 时注入

    async def run(self, task, context_text: str = "") -> str:
        # orchestrator/saga 由编排器在 run 前挂到 agent 属性上（getattr 兜底 None，
        # 保证 Agent 脱离编排器单独使用也不报错）
        self.mutations = 0
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
        # 只把白名单内的工具暴露给 LLM（与执行层共用 resolve_policy，避免 fail-open）
        policy = resolve_policy(self.name, self.role)
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
        # 工具轮次耗尽 ≠ 任务失败。给一次「只输出结论」的收尾机会：
        # 没有这一步，模型报完 N 条问题后连终止标记都来不及写，
        # 编排器就会把「干完了但没写 REQUEST_REVIEW」当成产出不达标（真机踩过）。
        messages.append({"role": "user", "content": (
            "工具调用次数已达本轮上限。现在**不要再调用任何工具**，"
            "直接输出最终结论（必须包含此前约定的终止标记）。")})
        reply = await self.llm.chat(messages, tools=None)
        return reply.content or "（达到工具循环上限，未给出最终输出）"

    # ---- 回滚钩子（路线图决策四，阶段 6 实现 Saga/undo 后启用）----
    async def undo(self) -> None:
        """撤销本 Agent 已产生的副作用（受控工具自带 undo 时由编排器在打回时调用）。"""
        return None
