"""记忆层协议（源自 Mem0：对外只有 5 个像数据库一样简单的方法）。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MemoryItem:
    id: str
    text: str
    scope: str = "shared"            # shared / agent / task（三级记忆注入）
    task_id: str | None = None
    score: float = 0.0
    score_details: dict = field(default_factory=dict)   # explain=True 时可审计


class MemoryBase(ABC):
    @abstractmethod
    def add(self, text: str, scope: str = "shared", task_id: str | None = None,
            metadata: dict | None = None) -> str:
        """写入一条记忆，返回 memory_id。"""

    @abstractmethod
    def search(self, query: str, limit: int = 5, explain: bool = False) -> list[MemoryItem]:
        """检索：低于相关性阈值的直接丢弃（阈值前置，源自 Mem0 score_and_rank）。"""

    @abstractmethod
    def update(self, memory_id: str, text: str) -> None:
        """更新：保留 created_at，只改 updated_at（append-only 语义）。"""

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        """软删除：不物理删除，检索时跳过。"""

    @abstractmethod
    def history(self, memory_id: str) -> list[dict]:
        """版本化历史：只增不改（old/new/event），支持审计与回滚。"""
