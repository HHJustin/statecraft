"""review —— 代码审查流水线（P11），P14/P15 后从示例升级为可调用模块。

对外四样：
    ReviewSession / Finding / make_tools   领域模型与工具契约（findings.py）
    build / run_review                    装配与一次完整审查（pipeline.py）
    HeuristicMockBrain                    离线假模型（演示流程，不冒充判断力）
    Batch / make_batches                  分批判审（一个任务装不下一整仓）

`examples/code-review/` 里的脚本现在只是这层之上的薄壳 —— 示例和 CLI
共用同一份审查逻辑，避免「示例对了、别处忘了」。
"""
from __future__ import annotations

from .batch import MAX_UNITS_PER_BATCH, Batch, make_batches
from .findings import (CATEGORIES, CHECK_NAMES, SEVERITIES, Finding,
                       ReviewSession, audit_ok, make_tools)
from .pipeline import (CRITIC_PROMPT, PLANNER_PROMPT, REVIEWER_PROMPT,
                       HeuristicMockBrain, ReviewOutcome, build, default_db,
                       make_llm, run_review, task_for)

__all__ = [
    "Finding", "ReviewSession", "make_tools", "audit_ok",
    "SEVERITIES", "CATEGORIES", "CHECK_NAMES",
    "build", "run_review", "task_for", "make_llm", "default_db",
    "Batch", "make_batches", "MAX_UNITS_PER_BATCH",
    "ReviewOutcome", "HeuristicMockBrain",
    "PLANNER_PROMPT", "REVIEWER_PROMPT", "CRITIC_PROMPT",
]
