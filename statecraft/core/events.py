"""事件驱动 + Outbox + 死信队列（路线图决策四 / P6，进程内版）。

对应 Edict 的三 Worker 架构，单进程简化：
- Outbox：先记账后投递 —— 事件先入 outbox，relay() 才上总线
  （模拟「同事务写 outbox_events + Relay 轮询投递」，保证业务与事件最终一致）
- EventBus：订阅/发布；消费失败按 retryable 分类：
  可重试 → 重新入队（delivery_count+1，退避由调用方控制）
  超过 MAX_DELIVERIES 或不可重试 → DLQ
- DLQ：死信队列，人工兜底入口（源自 Edict DispatchError(msg, retryable) 设计）
"""
import asyncio
import inspect
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_DELIVERIES = 3   # 同一事件最大投递次数，超限进 DLQ
_seq = itertools.count(1)


class DispatchError(Exception):
    """消费方抛出：retryable=True 可重试（下游抖动），False 直接进 DLQ（配置错误）。"""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    type: str                        # task.created / task.status / task.progress ...
    payload: dict = field(default_factory=dict)
    id: str = ""
    delivery_count: int = 0

    def __post_init__(self):
        if not self.id:
            self.id = f"e{next(_seq)}"


class Outbox:
    """发件箱：业务侧只管 enqueue，relay 统一投递。"""

    def __init__(self):
        self._pending: list[Event] = []

    def enqueue(self, event: Event) -> Event:
        self._pending.append(event)
        return event

    def relay(self) -> list[Event]:
        """一次性取走全部待投递事件（模拟 Relay Worker 轮询批次）。"""
        out, self._pending = self._pending, []
        return out


class EventBus:
    """进程内事件总线（asyncio.Queue）。上规模换 Redis Streams / NATS 时接口不变。"""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._handlers: dict[str, list] = {}
        self.dlq: list[dict] = []    # 死信：{"event": ..., "error": ..., "at": ...}

    def subscribe(self, event_type: str, handler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    def pending(self) -> int:
        return self._queue.qsize()

    async def run_once(self) -> Event | None:
        """消费一条事件（简化 ACK 语义）。队列空时立即返回 None，不阻塞。

        成功 → ACK；DispatchError(retryable) 且未超限 → 重新入队重投；
        超限 / 不可重试 / 未分类异常 → DLQ 人工兜底。

        注意：同一 EventBus 应在同一个事件循环内使用（asyncio.Queue 的约束）。
        """
        if self._queue.empty():
            return None
        event = await self._queue.get()
        try:
            for h in self._handlers.get(event.type, []):
                result = h(event)
                if inspect.isawaitable(result):
                    await result
            return event
        except DispatchError as e:
            event.delivery_count += 1
            if e.retryable and event.delivery_count < MAX_DELIVERIES:
                await self._queue.put(event)          # 重投
            else:
                self.dlq.append({"event": event, "error": str(e), "at": _now()})
        except Exception as e:                        # 未分类异常按不可重试处理
            self.dlq.append({"event": event, "error": f"{type(e).__name__}: {e}",
                             "at": _now()})
        finally:
            self._queue.task_done()
        return None
