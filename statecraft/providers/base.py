"""LLM Provider 协议层（源自 Mem0 决策一：抽象基类只定义最小协议）。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """LLM 发起的一次工具调用请求。"""
    name: str
    arguments: dict


@dataclass
class LLMReply:
    """一次 chat 的返回：要么最终文本，要么一批工具调用。"""
    content: str = ""
    tool_calls: list = field(default_factory=list)


class ProviderError(Exception):
    """模型服务返回的错误（限流 / 鉴权 / 地址错 / 5xx）。

    为什么要单独分一类：这三类错误的处理方式完全不同 ——
        429 / 5xx  → 重试有意义（后台抖动、配额按分钟恢复）
        401 / 403 / 404 → 重试一万次也一样，必须立刻停
    以前 provider 直接抛 HTTPError，编排器只认超时，于是「限流」把整个任务
    打崩，连 Blocked 都进不去（P12 真机实测被 429 打断才发现）。
    """

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMBase(ABC):
    """所有 LLM provider 的统一协议（治理层只认这个接口，不认具体厂商）。"""

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        """messages: OpenAI 格式 [{"role": ..., "content": ...}]；
        tools: OpenAI function-calling 格式的 schema 列表。"""
        raise NotImplementedError
