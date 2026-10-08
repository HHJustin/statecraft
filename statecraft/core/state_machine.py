"""状态机白名单 + 任务模型（源自 Edict 决策一/四/八）。

核心纪律：
- 改状态必须走 transition()，白名单外直接拒绝 —— 「Agent 自治」到「流程可控」的分水岭
- 高风险流转（完结/取消）先入 PendingConfirm 二次确认
- 升级路径 ESCALATION_PATH：卡住逐级退回上级，到顶 Blocked 人工兜底
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


class TransitionDenied(Exception):
    """白名单外的状态流转。"""


class TaskStalled(Exception):
    """超时重试耗尽，交由编排器升级处理。"""


# ---- 白名单：只允许这些跳转（单向递进，不可绕过）----
STATE_TRANSITIONS = {
    "Created":        {"Planning", "Cancelled"},
    "Planning":       {"Reviewing", "Blocked", "Cancelled"},
    "Reviewing":      {"Executing", "Planning", "Blocked", "Cancelled"},  # Planning=打回
    "Executing":      {"Done", "Reviewing", "Blocked", "Cancelled"},
    "PendingConfirm": {"Done", "Cancelled"},                              # 二次确认中转态
    "Blocked":        {"Planning", "Reviewing", "Executing", "Cancelled"},
    "Done":           set(),
    "Cancelled":      set(),
}

# 停滞升级路径（源自 Edict）：卡住 → 退回上一级重新决策
ESCALATION_PATH = {"Executing": "Reviewing", "Reviewing": "Planning", "Planning": "Created"}

# 状态 → 该唤醒谁
STATE_AGENT_MAP = {"Planning": "planner", "Reviewing": "critic", "Executing": "executor"}

# 高风险流转：需二次确认（源自 Edict HIGH_RISK_TRANSITIONS）
HIGH_RISK_TRANSITIONS = {("Executing", "Done"), ("Reviewing", "Cancelled")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def can_transition(cur: str, to: str) -> bool:
    return to in STATE_TRANSITIONS.get(cur, set())


@dataclass
class Task:
    id: str
    description: str
    state: str = "Created"
    checkpoint: str | None = None      # 回滚快照（回滚手段 b：检查点）
    retries: int = 0
    pending_target: str | None = None  # PendingConfirm 的目标态
    pending_source: str | None = None  # 进入确认前的来源态（reject 回退用）
    blocked_reason: str | None = None
    parent_id: str | None = None       # 委派来源（None = 根任务）
    flow_log: list = field(default_factory=list)      # 状态流转日志（可观测）
    progress_log: list = field(default_factory=list)  # Agent 强制上报的进展

    # ---- 回滚两件套（路线图决策四）----
    def snapshot(self) -> None:
        self.checkpoint = self.state

    def rollback(self) -> None:
        if self.checkpoint is None:
            raise TransitionDenied("无快照可回滚")
        self._set(self.checkpoint, "rollback")

    # ---- 内部 ----
    def _set(self, state: str, reason: str) -> None:
        self.flow_log.append({"from": self.state, "to": state,
                              "reason": reason, "at": _now()})
        self.state = state


def transition(task: Task, to: str, *, confirm: bool = False) -> Task:
    """唯一合法的状态变更入口。

    高风险流转未确认时，不直接执行：先入 PendingConfirm，
    由 approve()/reject() 完成二次确认。
    """
    if not can_transition(task.state, to):
        raise TransitionDenied(f"非法状态流转 {task.state} -> {to}（白名单外）")
    if (task.state, to) in HIGH_RISK_TRANSITIONS and not confirm:
        task.pending_source = task.state
        task.pending_target = to
        task._set("PendingConfirm", f"high-risk:{to}")
        return task
    task._set(to, "transition")
    task.pending_target = None
    task.pending_source = None
    return task


def approve(task: Task) -> Task:
    """二次确认通过 → 执行 pending_target。"""
    if task.state != "PendingConfirm":
        raise TransitionDenied("任务不在 PendingConfirm 态")
    target = task.pending_target
    task.pending_target = None
    task.pending_source = None
    return transition(task, target, confirm=True)


def reject(task: Task) -> Task:
    """二次确认拒绝 → 退回来源态。"""
    if task.state != "PendingConfirm":
        raise TransitionDenied("任务不在 PendingConfirm 态")
    source = task.pending_source or "Executing"
    task.pending_target = None
    task.pending_source = None
    task._set(source, "reject")
    return task


def escalate(task: Task) -> Task:
    """停滞升级（源自 Edict）：沿路径退回上级；到顶 → Blocked 人工兜底。"""
    up = ESCALATION_PATH.get(task.state)
    if up is None:
        task.blocked_reason = "升级耗尽，需人工介入"
        task._set("Blocked", "escalation_exhausted")
    else:
        task._set(up, "escalate")
    return task
