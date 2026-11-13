"""记忆层：协议 + 三个后端（local / hybrid / sqlite）。

`from_config()` 是配置字段 `memory_backend` / `memory_config` 的**唯一消费点**。
加它的原因是：这两个字段在 config 里躺了很久，却没人读——配置项写了不生效，
比没有这个配置项更坑（改了半天以为切了后端，其实一直跑 local）。
"""
from .base import MemoryBase, MemoryItem

__all__ = ["MemoryBase", "MemoryItem", "from_config"]


def from_config(config) -> MemoryBase:
    """按配置造记忆后端。

    用法：
        mem = from_config(AgentCoreConfig(memory_backend="sqlite",
                                          memory_config={"db_path": "reviews.db"}))
    """
    from .factory import MemoryFactory

    backend = getattr(config, "memory_backend", "local") or "local"
    options = dict(getattr(config, "memory_config", None) or {})
    if backend == "sqlite":
        # 导入 storage 才会把 sqlite 后端注册进工厂（factory.py 保持零改动）
        import statecraft.storage  # noqa: F401
    return MemoryFactory.create(backend, options or None)
