"""MockLLM —— 离线可跑的假 LLM。

两种用法：
1. script={"planner": ["输出1", "输出2"], ...}：按 agent 角色依次弹出脚本化输出；
   耗尽后返回兜底文本 "OK"。
2. handler(messages, tools) -> LLMReply：完全自定义（测试超时/异常场景用）。

角色识别：AgentRunner 会在系统提示头部注入 `[role:name]` 标记，Mock 据此路由。
"""
import inspect

from .base import LLMBase, LLMReply


def _detect_role(messages: list[dict]) -> str | None:
    sys_text = ""
    for m in messages:
        if m.get("role") == "system":
            sys_text = m.get("content", "")
            break
    if "[role:" in sys_text:
        return sys_text.split("[role:", 1)[1].split("]", 1)[0].strip()
    return None


class MockLLM(LLMBase):
    def __init__(self, script: dict | None = None, handler=None, fallback: str = "OK"):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.handler = handler
        self.fallback = fallback
        self.call_count = 0

    async def chat(self, messages: list[dict], tools=None) -> LLMReply:
        self.call_count += 1
        if self.handler is not None:
            result = self.handler(messages, tools)
            if inspect.isawaitable(result):   # 支持异步 handler（测试超时场景）
                result = await result
            return result
        role = _detect_role(messages)
        queue = self.script.get(role)
        if queue:
            return LLMReply(content=queue.pop(0))
        return LLMReply(content=self.fallback)
