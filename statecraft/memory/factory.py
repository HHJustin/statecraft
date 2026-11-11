"""记忆后端工厂（与 LLMFactory 同构，源自 Mem0 决策一）。

- memory_backend: "local"（词面）| "hybrid"（dense+词面混合）
- register_backend() 开放第三方注册
- 配置三态归一化：None / dict / 已实例化
"""
import importlib


class MemoryFactory:
    _backends = {
        "local": "statecraft.memory.local.InMemoryMemory",
        "hybrid": "statecraft.memory.hybrid.HybridMemory",
    }

    @classmethod
    def register_backend(cls, name: str, class_path: str) -> None:
        cls._backends[name] = class_path

    @classmethod
    def create(cls, backend: str, config=None):
        if backend not in cls._backends:
            raise KeyError(
                f"未知 memory backend: {backend!r}，可用: {sorted(cls._backends)}"
                f"（或用 register_backend 注册）"
            )
        mod_path, cls_name = cls._backends[backend].rsplit(".", 1)
        klass = getattr(importlib.import_module(mod_path), cls_name)

        # ---- 配置三态归一化 ----
        if config is None:
            return klass()
        if isinstance(config, dict):
            return klass(**config)
        return config  # 已经是实例，直接用
