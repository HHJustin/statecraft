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
    metadata: dict = field(default_factory=dict)        # 入库时带的标签（可按类型过滤）


class MemoryBase(ABC):
    @abstractmethod
    def add(self, text: str, scope: str = "shared", task_id: str | None = None,
            metadata: dict | None = None) -> str:
        """写入一条记忆，返回 memory_id。"""

    @abstractmethod
    def search(self, query: str, limit: int = 5, explain: bool = False,
               where: dict | None = None) -> list[MemoryItem]:
        """检索：低于相关性阈值的直接丢弃（阈值前置，源自 Mem0 score_and_rank）。

        where：元数据前置过滤（路线图决策六「先过滤再打分」）。
            等值匹配，值可给单值或列表（列表 = 命中任一即可）：
                where={"type": "style"}
                where={"type": ["style", "terminology"]}
            前置过滤而非结果后筛：噪声条目根本不参与打分，省算力也更准。
        """

    @abstractmethod
    def update(self, memory_id: str, text: str) -> None:
        """更新：保留 created_at，只改 updated_at（append-only 语义）。"""

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        """软删除：不物理删除，检索时跳过。"""

    @abstractmethod
    def history(self, memory_id: str) -> list[dict]:
        """版本化历史：只增不改（old/new/event），支持审计与回滚。"""
