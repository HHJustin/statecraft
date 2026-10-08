"""全局配置（数据类，零依赖）。

对应路线图：超时预算 / 退避序列 / 记忆注入条数 / 高风险自动确认开关。
"""
from dataclasses import dataclass, field


@dataclass
class AgentCoreConfig:
    # Provider 底座（源自 Mem0 决策一）：换厂商只改这两个字段
    llm_provider: str = "mock"
    llm_config: dict = field(default_factory=dict)

    # 记忆层
    memory_backend: str = "local"
    memory_config: dict = field(default_factory=dict)
    inject_memory_topk: int = 5          # 编排时注入的记忆条数

    # 调度可靠性三件套（源自 Edict 决策四）
    state_timeouts: dict = field(default_factory=lambda: {
        "Planning": 300.0,               # 时钟超时预算（秒），与心跳停滞检测互补
        "Reviewing": 300.0,
        "Executing": 1200.0,
    })
    retry_backoff: list = field(default_factory=lambda: [30.0, 60.0, 120.0])
    max_retries: int = 2

    # 高风险流转二次确认（源自 Edict 决策八）：
    # True  = 单机演示自动确认（仍走 PendingConfirm 态，留痕）
    # False = 停在 PendingConfirm 等人工 approve/reject
    auto_confirm: bool = True

    # 委派防死锁（源自 Edict 决策八）：委派链深度上限，超限拒绝
    max_delegation_depth: int = 3
