"""Saga 补偿事务 + 工具 undo（路线图决策四·回滚手段 c，自建补充）。

设计（与决策四对齐）：
- Tool 声明可选 undo 处理器：undo(ctx, result, **arguments) -> str（须为同步函数）
- call_tool 执行成功后，若工具带 undo 且 ctx 里有 saga/task，自动记账入 SagaLog
- 补偿 = 按记录逆序（LIFO）逐条执行 undo，尽力而为：单条失败不中断，全部进 history
- 编排器在「审核打回」与「任务取消」时触发补偿 —— Blocked 保留现场，不补偿，
  因为 Blocked 可能恢复继续执行，此时撤销副作用反而破坏状态
"""
import inspect


class SagaLog:
    """按任务记录可补偿的工具副作用，支持逆序撤销（Saga 补偿事务）。"""

    def __init__(self):
        self._stacks: dict[str, list[dict]] = {}   # task_id -> 记账栈
        self.history: list[dict] = []              # 全部补偿记录（审计/可观测）

    def record(self, task_id: str, tool_name: str, arguments: dict,
               result, undo, ctx: dict) -> None:
        """工具执行成功后记账（由 call_tool 自动调用）。"""
        self._stacks.setdefault(task_id, []).append({
            "tool": tool_name,
            "arguments": dict(arguments or {}),
            "result": result,
            "undo": undo,
            "ctx": ctx,          # 补偿时的上下文（记录时刻快照）
        })

    def pending(self, task_id: str) -> int:
        """该任务尚未补偿的记账条数。"""
        return len(self._stacks.get(task_id, []))

    def compensate(self, task_id: str, reason: str = "") -> list[dict]:
        """逆序补偿该任务的所有已记账副作用。

        尽力而为语义：单条 undo 失败不中断后续补偿，失败记录进 history 供人工排查。
        返回本次补偿记录列表（每条含 tool/status/detail/reason）。
        """
        entries = self._stacks.pop(task_id, [])
        out: list[dict] = []
        for entry in reversed(entries):
            rec = {"tool": entry["tool"], "reason": reason,
                   "status": "ok", "detail": ""}
            try:
                r = entry["undo"](entry["ctx"], entry["result"],
                                  **entry["arguments"])
                if inspect.isawaitable(r):
                    rec["status"] = "failed"
                    rec["detail"] = "undo 返回未等待的协程（undo 须为同步函数）"
            except Exception as e:                     # noqa: BLE001 尽力而为
                rec["status"] = "failed"
                rec["detail"] = f"{type(e).__name__}: {e}"
            self.history.append(rec)
            out.append(rec)
        return out
