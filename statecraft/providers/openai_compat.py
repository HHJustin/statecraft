"""OpenAI 兼容 LLM provider（DeepSeek / Qwen / GLM / Moonshot / Ollama ...）。

换厂商 = 改 base_url + model，零代码改动（路线图第 9 节选型）。
降级策略（源自 Mem0 决策三）：优先 httpx；未安装则用 stdlib urllib 在线程里同步调，
不抛 ImportError 中断主流程。
"""
import asyncio
import json
import os

from .base import LLMBase, LLMReply, ProviderError, ToolCall


# 这些状态码重试有意义：限流按分钟恢复、网关抖动是暂时的
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}

# 同样是 429，含义可能相反：限流（等一分钟就好）vs 额度耗尽（等到明天也不会好）。
# apigoto 的 42906「您的额度已用完」实测就是后者，重试只会白等 90 秒。
_EXHAUSTED_HINTS = ("额度", "余额", "欠费", "quota", "insufficient", "billing",
                    "exceeded your current quota")

_HINTS = {
    401: "鉴权失败，检查 OPENAI_API_KEY",
    402: "余额或配额不足",
    403: "无权访问该模型",
    404: "接口或模型不存在，检查 OPENAI_BASE_URL / OPENAI_MODEL",
    429: "触发限流，稍后重试或降低调用频率",
}


def _classify(status: int, detail: str = "") -> ProviderError:
    """把 HTTP 状态码翻译成「能不能重试」+ 一句人话。"""
    low = (detail or "").lower()
    exhausted = any(k in low for k in _EXHAUSTED_HINTS)
    msg = f"模型服务返回 {status}"
    if exhausted:
        msg += "：额度/余额已用尽，充值或换 key 后重试（重试当前请求没有意义）"
    elif status in _HINTS:
        msg += f"：{_HINTS[status]}"
    if detail:
        msg += f"（{detail.strip()[:200]}）"
    return ProviderError(msg, status=status,
                         retryable=status in _RETRYABLE and not exhausted)


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
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:          # 429/401/5xx...
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:                        # noqa: BLE001 读不到正文不影响分类
                pass
            raise _classify(e.code, detail) from None
        except urllib.error.URLError as e:           # DNS / 连接超时 / 代理不通
            raise ProviderError(f"连不上模型服务：{e.reason}",
                                retryable=True) from None
        return _parse_message(data["choices"][0]["message"])

    async def _chat_httpx(self, messages, tools) -> LLMReply:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                r = await client.post(self.base_url + "/chat/completions",
                                      json=self._payload(messages, tools),
                                      headers=self._headers())
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise _classify(e.response.status_code, e.response.text) from None
            except httpx.HTTPError as e:
                raise ProviderError(f"连不上模型服务：{e}", retryable=True) from None
            return _parse_message(r.json()["choices"][0]["message"])
