"""OpenAI 兼容 LLM provider（DeepSeek / Qwen / GLM / Moonshot / Ollama ...）。

换厂商 = 改 base_url + model，零代码改动（路线图第 9 节选型）。
降级策略（源自 Mem0 决策三）：优先 httpx；未安装则用 stdlib urllib 在线程里同步调，
不抛 ImportError 中断主流程。
"""
import asyncio
import json
import os

from .base import LLMBase, LLMReply, ToolCall


def _parse_message(msg: dict) -> LLMReply:
    tcs = []
    for t in msg.get("tool_calls") or []:
        fn = t.get("function", {})
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            args = {}
        tcs.append(ToolCall(name=fn.get("name", ""), arguments=args))
    return LLMReply(content=msg.get("content") or "", tool_calls=tcs)


class OpenAICompatLLM(LLMBase):
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str = "gpt-4o-mini", temperature: float = 0.3):
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL",
                         "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.temperature = temperature

    # ---- 对外协议 ----
    async def chat(self, messages: list[dict], tools=None) -> LLMReply:
        try:
            import httpx  # noqa: F401
            return await self._chat_httpx(messages, tools)
        except ImportError:
            return await asyncio.to_thread(self._chat_urllib, messages, tools)

    # ---- 实现 ----
    def _payload(self, messages, tools) -> dict:
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature}
        if tools:
            payload["tools"] = tools
        return payload

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"}

    def _chat_urllib(self, messages, tools) -> LLMReply:
        import urllib.request
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(self._payload(messages, tools)).encode("utf-8"),
            headers=self._headers(), method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_message(data["choices"][0]["message"])

    async def _chat_httpx(self, messages, tools) -> LLMReply:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(self.base_url + "/chat/completions",
                                  json=self._payload(messages, tools),
                                  headers=self._headers())
            r.raise_for_status()
            return _parse_message(r.json()["choices"][0]["message"])
