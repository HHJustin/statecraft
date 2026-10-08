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


class LLMBase(ABC):
    """所有 LLM provider 的统一协议（治理层只认这个接口，不认具体厂商）。"""

    @abstractmethod
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        """messages: OpenAI 格式 [{"role": ..., "content": ...}]；
        tools: OpenAI function-calling 格式的 schema 列表。"""
        raise NotImplementedError
