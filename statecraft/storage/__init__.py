"""持久层（P12）：审查结果落 SQLite。

对外只有两样东西：
    ReviewStore   —— 审查批次 / findings / 清白声明的读写门面
    SqliteMemory  —— MemoryBase 的落盘实现（memory/sqlite.py）

导入本包时会顺手把 `sqlite` 记忆后端注册进 MemoryFactory，这样
`MemoryFactory.create("sqlite", {"db_path": ...})` 直接可用，
而 factory.py 一行都不用改（它早就提供了 register_backend 入口）。
"""
from __future__ import annotations

from .db import DEFAULT_DB, SCHEMA_VERSION, connect, initialize, rebuild_fts
from .findings import ReviewStore, finding_id

__all__ = ["ReviewStore", "finding_id", "connect", "initialize", "rebuild_fts",
           "DEFAULT_DB", "SCHEMA_VERSION", "register_memory_backend"]

_registered = False


def register_memory_backend() -> None:
    """把 sqlite 后端注册进 MemoryFactory（幂等）。"""
    global _registered
    if _registered:
        return
    from statecraft.memory.factory import MemoryFactory
    MemoryFactory.register_backend("sqlite", "statecraft.memory.sqlite.SqliteMemory")
    _registered = True


register_memory_backend()
