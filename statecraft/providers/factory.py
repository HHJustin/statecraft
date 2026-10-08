"""Provider 工厂（源自 Mem0 决策一）。

- 类路径字符串当配置值，运行时 importlib 动态加载
- register_provider() 开放第三方注册，不 fork 也能扩展
- 配置三态归一化：None / dict / 已实例化，工厂统一兜底
"""
import importlib


class LLMFactory:
    _providers = {
        "mock": "statecraft.providers.mock.MockLLM",
        "openai_compat": "statecraft.providers.openai_compat.OpenAICompatLLM",
    }

    @classmethod
    def register_provider(cls, name: str, class_path: str) -> None:
        """第三方扩展入口：LLMFactory.register_provider("my", "pkg.mod.MyLLM")"""
        cls._providers[name] = class_path

    @classmethod
    def create(cls, provider: str, config=None):
        if provider not in cls._providers:
            raise KeyError(
                f"未知 LLM provider: {provider!r}，可用: {sorted(cls._providers)}"
                f"（或用 register_provider 注册）"
            )
        mod_path, cls_name = cls._providers[provider].rsplit(".", 1)
        klass = getattr(importlib.import_module(mod_path), cls_name)

        # ---- 配置三态归一化 ----
        if config is None:
            return klass()
        if isinstance(config, dict):
            return klass(**config)
        return config  # 已经是实例，直接用
