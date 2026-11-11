"""编排器：状态机 + 审核关卡 + 调度可靠性三件套 + 记忆注入。

循环骨架（对应路线图第 8 节最小内核）：
    意图识别（分拣：闲聊 → Created 直通 Done，不进流水线）
    → 查状态 → 唤醒 STATE_AGENT_MAP 对应 Agent → 记忆注入
    → snapshot() → run_with_retry（wait_for 超时 + 退避重试）
    → TaskStalled → escalate()（逐级退回，到顶 Blocked）
    → 记忆沉淀 → decide_next → transition（白名单校验）
    → Reviewing 打回 → Planning（undo 钩子留给阶段 6 Saga）
    → Executing 产出缺 DONE → 计次重跑，超限 Blocked
    → Executing 产出 REQUEST_REVIEW → 回 Reviewing 审产出（执行后审核，
      供代码审查类应用：审不过 → 打回补偿，审过 → Done）
    → PendingConfirm：auto_confirm 自动过，否则等待人工
"""
import asyncio

from ..config import AgentCoreConfig
from ..memory.base import MemoryBase
from .delegation import DelegationTracker
from .events import Event
from .saga import SagaLog
from .security import scan_injection, wrap_untrusted
from .state_machine import (STATE_AGENT_MAP, Task, TaskStalled, approve,
                            escalate, transition)
from .runner import Agent
from .triage import RuleTriage

_DONE_MARK = "DONE"
_REJECT_MARKS = ("打回", "REJECT")
_REVIEW_MARK = "REQUEST_REVIEW"   # 执行后审核标记：产出先审再收尾（代码审查类应用）


def _hits(text: str, marks) -> bool:
    """契约标记匹配（大小写不敏感）。

    真模型不保证全大写：输出 "reject: 缺少数据来源" 时，原实现
    `"REJECT" in output` 会漏判，任务继续往下走而不是退回重规划。
    """
    up = (text or "").upper()
    return any(m.upper() in up for m in marks)


class Orchestrator:
    def __init__(self, agents: dict[str, Agent], memory: MemoryBase,
                 config: AgentCoreConfig | None = None,
                 outbox=None, logger=None,
                 saga: SagaLog | None = None,
                 delegations: DelegationTracker | None = None):
        self.agents = agents
        self.memory = memory
        self.config = config or AgentCoreConfig()
        self.outbox = outbox        # P6：可选，业务事件经发件箱投递
        self.logger = logger        # P7：可选，JSONL 会话日志
        self.saga = saga or SagaLog()             # P6：工具副作用记账 + 逆序补偿
        self.delegations = delegations or DelegationTracker(
            max_depth=self.config.max_delegation_depth)   # 委派关系账本
        self.triage = self.config.triage_classifier or RuleTriage()  # 意图识别（可插拔）
        self._seq = 0
        self._flow_idx: dict = {}   # 已投递的 flow_log 进度（按任务）
        self._prog_idx: dict = {}
        self._last_output: dict = {}  # task_id -> (agent_name, output)：上一步产出

    # ---- 入口 ----
    def create_task(self, description: str, parent: Task | None = None) -> Task:
        """创建任务；parent 不为空时走委派（深度/成环闸门拒绝则抛 DelegationDenied）。"""
        self._seq += 1
        task = Task(id=f"t{self._seq}", description=description)
        # 意图识别 / 分拣（源自 Edict「太子分拣」）：闲聊任务不进流水线
        if self.config.triage_enabled:
            verdict = self.triage.classify(description)
            task.intent = verdict["intent"]
            task.intent_reason = verdict["reason"]
            if self.logger:
                self.logger.log("triage", task_id=task.id, **verdict)
            if self.outbox:
                self.outbox.enqueue(Event("task.triaged",
                                          {"task_id": task.id, **verdict}))
        if parent is not None:
            self.delegations.register(parent.id, task.id)   # 闸门：超深/成环在此拒绝
            task.parent_id = parent.id
            if self.outbox:
                self.outbox.enqueue(Event("task.delegated",
                                          {"task_id": task.id,
                                           "parent_id": parent.id}))
            if self.logger:
                self.logger.log("delegated", task_id=task.id,
                                parent_id=parent.id, description=description)
        if self.outbox:
            self.outbox.enqueue(Event("task.created",
                                      {"task_id": task.id, "description": description}))
        if self.logger:
            self.logger.log("task_created", task_id=task.id, description=description,
                            parent_id=task.parent_id)
        return task

    async def cancel(self, task: Task) -> Task:
        """取消任务并补偿已产生的副作用（Saga 逆序撤销）。

        Done 任务不可取消（白名单拒绝）；Blocked 保留现场不补偿（可能恢复继续执行）。
        """
        transition(task, "Cancelled")
        await self._compensate(task, "task_cancelled")
        self._flush(task)
        if self.logger:
            self.logger.log("task_cancelled", task_id=task.id)
        return task

    # ---- Saga 补偿：撤销本任务全部已记账副作用 + 非 tool 副作用钩子 ----
    async def _compensate(self, task: Task, reason: str) -> None:
        for rec in self.saga.compensate(task.id, reason):
            if self.logger:
                self.logger.log("saga_compensate", task_id=task.id, **rec)
            if self.outbox:
                self.outbox.enqueue(Event("task.compensate",
                                          {"task_id": task.id, **rec}))
        for agent in self.agents.values():
            try:
                await agent.undo()
            except Exception:                      # 钩子尽力而为，不阻断补偿流程
                pass

    async def run(self, task: Task) -> Task:
        # 分拣直通：闲聊/无效输入不进流水线（Created → Done，白名单合法边）
        if task.intent == "chitchat" and task.state == "Created":
            transition(task, "Done", reason="triage_chitchat")
            self._flush(task)
            if self.logger:
                self.logger.log("task_finished", task_id=task.id, state=task.state,
                                intent=task.intent, reason=task.intent_reason)
            return task
        self.memory.add(f"[任务目标] {task.description}",
                        scope="task", task_id=task.id)
        while task.state not in ("Done", "Cancelled", "Blocked"):
            self._flush(task)   # P6/P7：新增流转/进展 → outbox + 会话日志
            # 高风险二次确认（源自 Edict PendingConfirm）
            if task.state == "PendingConfirm":
                if self.config.auto_confirm:
                    approve(task)
                    continue
                break   # 停下等人工 approve/reject
            # 初始态自动进入规划（Created 无对应 Agent）
            if task.state == "Created":
                transition(task, "Planning")
                continue
            agent = self.agents.get(STATE_AGENT_MAP.get(task.state, ""))
            if agent is None:
                task.blocked_reason = f"状态 {task.state} 无对应 Agent"
                task._set("Blocked", "no_agent")
                break

            # 上下文注入 = 上一步产出（必须有）+ 检索到的相关记忆（补充）
            # 只按 task.description 检索回答不了「现在该审什么」：critic 拿不到
            # planner 刚写的计划，只能反复打回（真模型实测踩到）。
            hits = self.memory.search(task.description,
                                      limit=self.config.inject_memory_topk)
            mem_text = "\n".join(f"- {h.text}" for h in hits)
            ctx_text = mem_text
            prior = self._last_output.get(task.id)
            if prior:
                who, what = prior
                ctx_text = (f"[本任务上一步产出｜{who}]\n{what}\n\n"
                            f"[相关记忆]\n{mem_text or '（无）'}")
            # Reviewing 承担两种审核语义（审计划 / 审产出），必须告诉审核者当前是哪种。
            # 判据用「进入 Reviewing 前的状态」而不是 Agent 名字：from=Planning → 审计划，
            # from=Executing → 审产出。真模型实测：不说清就会一直挑「这不是 docstring」。
            if task.state == "Reviewing" and task.flow_log:
                src = task.flow_log[-1].get("from", "")
                focus = {"Planning": "计划（未执行，通过后才进入执行，本阶段不该出现 docstring）",
                         "Executing": "执行产出（已落盘，请审查实际写入的内容）"}.get(src)
                if focus:
                    ctx_text = f"[审核对象｜来自 {src}] {focus}\n\n{ctx_text}"
            # P10：注入检测 —— 命中则隔离包装为不可信数据
            inj = scan_injection(task.description + "\n" + ctx_text)
            if inj:
                ctx_text = wrap_untrusted(ctx_text, inj)
                if self.logger:
                    self.logger.log("injection_warning", task_id=task.id,
                                    patterns=inj)

            task.snapshot()                                # ① 存快照
            agent.orchestrator = self                      # 供 delegate_subtask 工具用
            agent.saga = self.saga                         # 供 call_tool 记账用
            try:
                output = await self._run_with_retry(agent, task, ctx_text)  # ② 超时重试
            except TaskStalled:
                escalate(task)                             # ③ 停滞升级
                task.retries = 0
                if task.state == "Created":
                    # 升级到顶（回到源头）仍停滞 → Blocked 人工兜底，防止 Planning↔Created 死循环
                    task.blocked_reason = "升级到顶（Created）仍停滞，需人工介入"
                    task._set("Blocked", "escalation_to_top")
                continue

            # 记忆沉淀（失败恢复时可 search 回放现场）
            self.memory.add(f"[{agent.name}@{task.state}] {output}",
                            scope="task", task_id=task.id)
            self._last_output[task.id] = (agent.name, output)   # 下一步注入用
            await self._advance(task, agent, output)
        self._flush(task)
        if self.logger:
            self.logger.log("task_finished", task_id=task.id, state=task.state,
                            blocked_reason=task.blocked_reason)
        return task

    # ---- P6/P7：把新增的流转/进展事件推到 outbox 与会话日志 ----
    def _flush(self, task: Task) -> None:
        fi = self._flow_idx.get(task.id, 0)
        for f in task.flow_log[fi:]:
            if self.outbox:
                self.outbox.enqueue(Event("task.status",
                                          {"task_id": task.id, **f}))
            if self.logger:
                self.logger.log("flow", task_id=task.id, **f)
        self._flow_idx[task.id] = len(task.flow_log)

        pi = self._prog_idx.get(task.id, 0)
        for p in task.progress_log[pi:]:
            if self.outbox:
                self.outbox.enqueue(Event("task.progress",
                                          {"task_id": task.id, **p}))
            if self.logger:
                self.logger.log("progress", task_id=task.id, **p)
        self._prog_idx[task.id] = len(task.progress_log)

    # ---- 超时重试（时钟超时，与心跳停滞互补）----
    async def _run_with_retry(self, agent: Agent, task: Task, ctx_text: str) -> str:
        for i in range(self.config.max_retries + 1):
            try:
                return await asyncio.wait_for(
                    agent.run(task, ctx_text),
                    timeout=self.config.state_timeouts.get(task.state, 300.0))
            except asyncio.TimeoutError:
                if i < self.config.max_retries:
                    backoff = self.config.retry_backoff
                    await asyncio.sleep(backoff[min(i, len(backoff) - 1)])
                else:
                    raise TaskStalled(task.state) from None
        raise TaskStalled(task.state)   # 理论不可达

    # ---- 决定下一状态并流转 ----
    async def _advance(self, task: Task, agent: Agent, output: str) -> None:
        cur = task.state
        if cur != "Executing":
            task.retries = 0          # 计数器专职「产出质量重试」，换状态即清零
        if cur == "Planning":
            transition(task, "Reviewing")              # 计划必须过审核关
        elif cur == "Reviewing":
            # 「审核通过」的去向由「从哪来」决定，不能靠模型措辞：
            #   from=Planning  → 审的是计划，通过后进执行
            #   from=Executing → 审的是产出，通过后收尾（Done 内部会先进 PendingConfirm）
            # 原来靠「输出里有 DONE」区分，真模型只说「审核通过」不说 DONE，
            # 结果审完产出又回 Executing 重做，无限循环。
            src = task.flow_log[-1].get("from", "") if task.flow_log else ""
            if _hits(output, _REJECT_MARKS):
                task.reject_count += 1
                await self._compensate(task, "review_rejected")  # 打回 → Saga 逆序补偿
                if task.reject_count > self.config.max_rejections:
                    # 反复打回说明分歧无法自动收敛（标准过严 / 审核者缺信息）：交人工
                    task.blocked_reason = (f"审核连续打回 {task.reject_count} 次，"
                                           f"超过上限 {self.config.max_rejections}")
                    task._set("Blocked", "review_rejected_too_many")
                else:
                    transition(task, "Planning")       # 白名单允许的打回路径
            elif src == "Executing":
                transition(task, "Done")               # 执行后审核通过 → 收尾
            else:
                transition(task, "Executing")          # 计划审核通过 → 进入执行
        elif cur == "Executing":
            if _hits(output, (_REVIEW_MARK,)):
                transition(task, "Reviewing")          # 执行后审核：产出先审再收尾
            elif _hits(output, (_DONE_MARK,)):
                # 高风险：先入 PendingConfirm，下一轮 auto_confirm/人工处理
                transition(task, "Done")
            else:
                task.retries += 1                      # 产出质量不达标 → 重跑
                if task.retries > self.config.max_retries:
                    task.blocked_reason = "执行产出缺少 DONE 标记"
                    task._set("Blocked", "output_quality")
