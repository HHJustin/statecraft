"""审查流水线的装配 —— 从「有一堆零件」到「一条能跑的生产线」。

这里放三类东西：

1. **三份 prompt**：规划者 / 审查员 / 复核者。改 prompt 就是改产品行为，
   所以它们和装配代码放在一起，不散在示例里。
2. **`build()`**：状态机、权限白名单、工具集、完成条件（每个待审对象都要有
   结论）在这里对齐。内核零改动 —— `STATE_AGENT_MAP` 的键仍是
   planner/critic/executor，只换 role 指向的策略。
3. **`run_review()`**：一次完整审查的生命周期（建批次 → 跑 → 落终态 →
   写函数级缓存）。CLI 和 examples 共用它，区别只在打印。

为什么 examples 和 CLI 共用而不是各写一套：审查的**正确性**（形式审、覆盖闸门、
落库时机）必须只有一份实现。第二份实现的存在本身就是 bug 的温床 ——
P11 的两次真机事故里，一次就是「示例里对了、别处忘了」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.tools import AGENT_POLICY, ToolRegistry, default_registry
from statecraft.incremental.cache import UnitCache
from statecraft.incremental.plan import ReviewPlan
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply, ToolCall
from statecraft.storage import DEFAULT_DB, ReviewStore

from .batch import make_batches
from .findings import ReviewSession, make_tools

# ---------------------------------------------------------------- 角色权限
# 内核零改动：agents 字典的键仍是 planner/critic/executor（STATE_AGENT_MAP 约定），
# 只把 role 换成审查流水线自己的策略名，resolve_policy 会先按 role 查。
AGENT_POLICY["review_planner"] = {"role": "coordination",
                                  "tools": {"report_progress", "read_file"}}
AGENT_POLICY["reviewer"] = {"role": "execution",
                            "tools": {"report_progress", "read_file",
                                      "record_finding", "declare_clean"}}
AGENT_POLICY["review_critic"] = {"role": "review",
                                 "tools": {"report_progress", "read_file",
                                           "confirm_finding", "review_status"}}

# ---------------------------------------------------------------- 三段 prompt
PLANNER_PROMPT = (
    "你是代码审查规划者。用 read_file 读待审文件，然后输出一份简短审查计划：\n"
    "逐个函数列出要检查什么（边界条件 / 异常处理 / 命名语义 / 资源泄漏 / 默认参数），"
    "不要展开分析，不要报具体问题（报问题是审查员的工作）。\n"
    "输出纯文本，不要写 DONE。"
)

REVIEWER_PROMPT = (
    "你是代码审查员。严格按下面顺序工作：\n"
    "1. 用 read_file 读文件。**没读过的文件不许报任何问题**，未读即报会被系统判定为编造。\n"
    "2. 每发现一个真实缺陷，调用 record_finding 记录一次。要点：\n"
    "   - evidence 必须从源文件**原样复制 1-3 行**（含代码本身），系统会逐字符核对；\n"
    "   - message 说清「哪里不对 + 会导致什么后果」，禁止「这里可能有问题」这类空话；\n"
    "   - 只报你确认存在的缺陷。**没有缺陷就一条都不报**，凑数编造会被形式审驳回并记录在案；\n"
    "   - **同一行只报一个最主要的问题**。不要换措辞或换类别把同一处复述多遍，\n"
    "     重复记录会被系统识别并丢弃。\n"
    "3. **每个待审文件都必须有结论**：要么至少一条 record_finding，要么调用 " \
    "declare_clean 说明你检查了哪些函数、哪几类问题、为什么判定不构成缺陷。\n"
    "   零结论的文件会被系统拦下重跑——「看完了觉得没问题」必须留下依据。\n"
    "4. 全部处理完后，输出审查摘要，每条一行，格式：`#<id> <file>:<line> <severity> <一句话>`，" \
    "然后单独一行输出 `REQUEST_REVIEW`（硬性要求，否则流程无法推进）。"
)

# 增量模式追加的一段：不追加的话，审查员会以为整个仓库都要审，
# 于是把历史遗留问题全报一遍 —— 那不是「审这个提交」。
REVIEWER_DIFF_NOTE = (
    "\n【本次是增量审查】任务描述里列出了本次变更过的函数与行区间。\n"
    "- 优先用 `read_file(path, start_line=..., end_line=...)` 只读变更区间，省上下文；\n"
    "- 报问题请落在这些区间或这些函数内。报在未变更行上不会被驳回，但会被标注\n"
    "  「未变更行」——那属于历史问题，不是这个提交引入的；\n"
    "- 未列出的函数不在本次范围内，不用逐个审。"
)

CRITIC_PROMPT = (
    "你是审查质量复核者。上下文会用 `[审核对象｜来自 X]` 标明这一轮审什么：\n"
    "A. 来自 Planning（审计划）：只判断计划是否覆盖了全部待审文件、每个检查项是否具体。"
    "计划阶段本来就没有具体问题清单，不要因此打回。\n"
    "B. 来自 Executing（审报告）：**先调用 review_status 查覆盖**。"
    "若仍有「尚无结论的文件/函数」，直接打回要求补齐，不必看具体 finding——"
    "漏审一个对象的报告不能算通过。覆盖齐了再抽查 1-2 条 finding，自己 read_file 核对——"
    "证据那几行是否真的存在、问题描述是否成立。核对后用 confirm_finding 给出 "
    "confirm 或 dispute；发现明显不符的直接 dispute。\n"
    "输出契约（二选一，只输出一种，不要解释过程）：\n"
    "  合格 → 只输出「审核通过」\n"
    "  不合格 → 只输出「打回：<一句话理由>」\n"
    "不要写 DONE。"
)


def reviewer_prompt_for(plan: ReviewPlan | None) -> str:
    if plan is not None and plan.source == "diff":
        return REVIEWER_PROMPT + REVIEWER_DIFF_NOTE
    return REVIEWER_PROMPT


# ---------------------------------------------------------------- 装配
def build(project_root: Path, llm, session: ReviewSession | None = None,
          plan: ReviewPlan | None = None,
          batch=None) -> tuple[Orchestrator, ReviewSession]:
    """装配一条流水线。`batch` 给了就按这一批的规模算轮次预算。"""
    session = session or ReviewSession(project_root, [])

    registry = ToolRegistry()
    for name in ("report_progress", "request_block"):   # 复用内核内置工具
        registry.register(default_registry().get(name))
    for t in make_tools(session):
        registry.register(t)

    # 轮次预算：审查是「每条问题一次工具调用」的活。真机实测 428 行代码要报
    # 15 条以上，固定 4 轮必被截断（P11 那次 Blocked 就是它）。
    #
    # 分批后按**本批**的规模算：读文件每文件 1 次、结论每个函数 1 次、
    # 再加几条 finding 的余量。按整批 186 个函数算会得出 60（上限），
    # 而一批只有 12 个函数时 60 轮是浪费 —— 也会让「跑不完」被误当成正常。
    files = len(batch.files) if batch is not None else len(session.files)
    units = batch.unit_count if batch is not None else \
        sum(len(v) for v in session.units.values())
    budget = min(60, max(10, 6 + 2 * files + 2 * units))

    agents = {
        "planner": Agent("planner", llm, role="review_planner", registry=registry,
                         system_prompt=PLANNER_PROMPT),
        "critic": Agent("critic", llm, role="review_critic", registry=registry,
                        system_prompt=CRITIC_PROMPT),
        "executor": Agent("executor", llm, role="reviewer", registry=registry,
                          system_prompt=reviewer_prompt_for(plan),
                          max_tool_iters=budget),
    }
    # 规划原文是「为什么这么审」的唯一证据，但内核不保存历史产出
    # （_last_output 只留最后一步）。这里包一层 run 把它摘出来 —— 换的是
    # Agent 实例的属性，不是内核代码。
    _inner_run = agents["planner"].run

    async def _capture_plan(task, context_text: str = ""):
        out = await _inner_run(task, context_text)
        session.plan = out or ""
        return out

    agents["planner"].run = _capture_plan

    def _complete(task) -> str | None:
        """应用层完成条件：每个待审对象（文件/函数）都要有结论。

        真机实测教训：审查员读完 388 行 Flask 代码一条没报，审核者也盖章通过，
        系统就"成功"产出一份空报告。内核不知道什么叫"审完了"，所以由应用
        把这个条件注册进去——缺结论就退回重跑，不许悄悄当 Done。
        """
        gap = session.unaccounted()
        if gap:
            unit = "函数" if session.strict_units else "文件"
            return (f"以下{unit}还没有任何结论（既没有问题记录，也没有 declare_clean "
                    f"清白声明）：{'、'.join(gap)}")
        return None

    orch = Orchestrator(agents, InMemoryMemory(),
                        AgentCoreConfig(auto_confirm=True),
                        completion_check=_complete)
    return orch, session


def make_llm(mock: bool = False):
    """有 key 走真实模型，否则报错——离线请用 --mock。"""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("未设置 OPENAI_API_KEY。想离线看流程请加 --mock。")
    from statecraft.providers.openai_compat import OpenAICompatLLM
    base = os.environ.get("OPENAI_BASE_URL", "https://www.apigoto.com/v1")
    model = os.environ.get("OPENAI_MODEL", "deepseek-flash")
    return OpenAICompatLLM(base_url=base, model=model, api_key=key)


# ---------------------------------------------------------------- 通用离线假模型
class HeuristicMockBrain:
    """通用离线假模型：两条确定性规则，用来跑通流程而不是冒充审查能力。

    为什么需要它：`--mock` 如果只会「读文件 + 声明清白」，那 CLI 的 demo 就是
    一份空报告，看不出报告长什么样。这里给它两条最容易判的规则（宽泛 except、
    可变默认参数），产出**证据真实**（从源码原样取行）的 findings，
    形式审能过、报告有内容。

    它明确不模拟「判断力」：规则漏掉的缺陷它一律不报。CLI 里必须打上
    「离线假模型」的横幅，否则读者会以为这就是审查效果。
    """

    def __init__(self, session: ReviewSession):
        self.session = session
        self.turn: dict[str, int] = {}
        self._read: set[str] = set()

    def _n(self, role: str) -> int:
        self.turn[role] = self.turn.get(role, 0) + 1
        return self.turn[role]

    async def chat(self, messages, tools=None) -> LLMReply:
        role = "?"
        for m in messages:
            if m.get("role") == "system":
                txt = m.get("content", "")
                if "[role:" in txt:
                    role = txt.split("[role:", 1)[1].split("]", 1)[0].strip()
                break
        n = self._n(role)
        if role == "planner":
            plan = "、".join(self.session.files[:6]) or "（无文件）"
            return LLMReply(content=f"审查计划：逐个文件检查边界条件、异常处理、"
                                    f"命名语义、资源释放、默认参数。待审：{plan}")
        if role == "critic":
            if n == 1:
                return LLMReply(content="审核通过")
            if n == 2:
                return LLMReply(tool_calls=[ToolCall("review_status", {})])
            return LLMReply(content="审核通过")
        return self._executor(n)

    def _pending(self) -> list[str]:
        """本批里还没结论的文件。分批之后不能再用「第 n 轮取第 n//2 个文件」——
        那个计数跨批不清零，第二批一开始就以为「都审完了」。"""
        s = self.session
        done = {f.file for f in s.findings.values() if f.form_ok}
        out: list[str] = []
        for file in s.scoped_files():
            units = s.scoped_units(file)
            if units:
                if any(not s.unit_concluded(file, u) for u in units):
                    out.append(file)
            elif file not in s.clean_claims and file not in done:
                out.append(file)
        return out

    def _executor(self, n: int) -> LLMReply:
        """先读后判：读过的文件再给结论，直到本批没有待审文件为止。"""
        pending = self._pending()
        if not pending:
            ids = [f"#{f.id}" for f in self.session.findings.values() if f.form_ok]
            return LLMReply(content="审查完成，共 %d 条问题：\n" % len(ids)
                                    + "\n".join(ids) + "\nREQUEST_REVIEW")
        target = pending[0]
        if target not in self._read:
            self._read.add(target)
            return LLMReply(tool_calls=[ToolCall("read_file", {"path": target})])
        try:
            lines = self.session.lines_of(target)
        except FileNotFoundError:
            self._read.discard(target)
            return LLMReply(content=f"{target} 读不到，跳过。\nREQUEST_REVIEW")
        hits = _obvious_defects(target, lines)
        if not hits:
            return LLMReply(tool_calls=[ToolCall("declare_clean", {
                "file": target,
                "reason": "按宽泛 except、可变默认参数两条规则逐行扫过，未命中；"
                          "其余类别（边界/命名/资源泄漏）本假模型不判断"})])
        calls = [ToolCall("record_finding", h) for h in hits]
        # 报了问题的文件里，剩下的函数也得有结论（增量模式下覆盖闸门按函数算）
        rest = [u.qualname for u in self.session.scoped_units(target)
                if not any(u.contains(h["line"]) for h in hits)]
        if rest:
            calls.append(ToolCall("declare_clean", {
                "file": target,
                "reason": "上列函数之外的函数按两条规则逐行扫过，未命中；"
                          "其余类别本假模型不判断",
                "units": rest}))
        return LLMReply(tool_calls=calls)


def _obvious_defects(file: str, lines: list[str]) -> list[dict]:
    """两条确定性规则。返回 record_finding 的参数。"""
    out: list[dict] = []
    for i, line in enumerate(lines):
        ln = i + 1
        if "except Exception" in line:
            tail = " ".join(lines[i + 1:i + 4])
            if "pass" in tail:
                out.append(dict(
                    file=file, line=ln, severity="high", category="exception",
                    message="except Exception + pass 把异常静默吞掉，调用方永远不知道失败",
                    evidence=line.strip(), suggestion="只捕获具体异常并带上上下文重新抛出"))
        if "=[]" in line.replace(" ", "") or "={}" in line.replace(" ", ""):
            if line.lstrip().startswith(("def ", "async def ")):
                out.append(dict(
                    file=file, line=ln, severity="high", category="correctness",
                    message="可变默认参数跨调用共享，第二次调用会带上上次的数据",
                    evidence=line.strip(), suggestion="改用 None 哨兵再在函数体内初始化"))
    return out


# ---------------------------------------------------------------- 一次审查
@dataclass
class ReviewOutcome:
    session: ReviewSession
    plan: ReviewPlan
    state: str = "Done"
    review_id: str | None = None
    flow_log: list = field(default_factory=list)
    reject_count: int = 0
    blocked_reason: str = ""
    ran_model: bool = True                  # False = 全部命中缓存，没花一分钱
    cached_units: int = 0
    adopted: int = 0                        # 复用上次结论的条数
    batches: int = 1                        # 分了几批跑（见 review/batch.py）
    error: str = ""

    def stats(self) -> dict:
        s = self.session
        confirmed = s.confirmed()
        return {
            "state": self.state,
            "files": len(s.files),
            "units": sum(len(v) for v in s.units.values()),
            "confirmed": len(confirmed),
            "cached_findings": len([f for f in confirmed if f.cached]),
            "out_of_diff": len(s.out_of_diff()),
            "rejected": len(s.rejected()),
            "disputed": len(s.disputed()),
            "skipped_units": self.cached_units,
            "batches": self.batches,
            "ran_model": self.ran_model,
            "review_id": self.review_id,
            "reject_count": self.reject_count,
            "blocked_reason": self.blocked_reason,
        }


def task_for(plan: ReviewPlan, batch=None) -> str:
    head = batch.brief(plan) if batch is not None else plan.task_brief()
    return (f"{head}\n"
            f"[要求] 每条问题必须带文件、行号、严重度、类别与源文件证据；"
            f"证据需逐字符可核对。只报真实缺陷。")


async def run_review(plan: ReviewPlan, llm=None, *, db: str | None = None,
                     review_id: str | None = None, use_cache: bool = True,
                     model_name: str = "", llm_factory=None) -> ReviewOutcome:
    """跑完一次审查：建批次 → 编排 → 落终态 → 写函数级缓存。

    `db=None` 表示不落库（内存里跑完就散）。落库时无论 Done 还是 Blocked
    都写终态 —— 库里躺着一个「正在跑」的审查，比明确失败更糟。

    `llm_factory(session) -> llm`：离线假模型要读 session 里的文件与结论，
    但 session 是在这里造的；用工厂而不是提前构造，避免调用方去猜内部顺序。
    """
    session = ReviewSession(plan.root, plan.files, units=plan.units,
                            ranges=plan.ranges, strict_units=plan.strict_units)
    outcome = ReviewOutcome(session=session, plan=plan,
                            cached_units=len(plan.skipped))
    if plan.replayed:
        outcome.adopted = session.adopt_cached(plan.replayed)
    if llm_factory is not None:
        llm = llm_factory(session)

    if not plan.files:
        # 全部命中缓存：不调模型，但批次照样落库、照样有报告。
        # 悄悄跳过（不留批次）会让「今天怎么没结果」无从查起。
        outcome.ran_model = False
        outcome.state = "Done"
        if db is not None:
            store = ReviewStore(db)
            outcome.review_id = store.start_review(
                repo_root=plan.root, scope=plan.files, task_id="",
                review_id=review_id, model=model_name)
            session.attach(store, outcome.review_id)
            store.finish_review(
                outcome.review_id, "done",
                plan=f"全部命中函数级缓存，跳过 {len(plan.skipped)} 个函数")
            session.flush_units(UnitCache(store), outcome.review_id)
            store.close()
        return outcome

    batches = make_batches(plan)
    outcome.batches = len(batches)

    store = ReviewStore(db) if db is not None else None
    states: list[str] = []
    reasons: list[str] = []
    plans: list[str] = []
    task_id = ""
    try:
        for batch in batches:
            # 每批只对「本批的函数」负责：覆盖闸门、critic 的 review_status
            # 都看 scope，否则第一批就会因为后面几批还没开始而被判缺结论。
            session.scope = batch.scope
            orch, _ = build(plan.root, llm, session, plan, batch=batch)
            task = orch.create_task(task_for(plan, batch))
            if store is not None and outcome.review_id is None:
                task_id = task.id
                outcome.review_id = store.start_review(
                    repo_root=plan.root, scope=plan.files, task_id=task_id,
                    review_id=review_id, model=model_name)
                session.attach(store, outcome.review_id)   # 之后每条结论都自动落库

            result = await orch.run(task)
            # 每批的流转路径都是同一串，累加只会印出一条几百节点的长龙。
            # 留最后一批的（分支退出时就是失败那批），批次数量另外报。
            outcome.flow_log = list(result.flow_log)
            outcome.reject_count += result.reject_count
            plans.append(session.plan)
            states.append(result.state)
            if result.state != "Done":
                reasons.append(f"{batch.label() or '审查'}：{result.blocked_reason or result.state}")
                break                      # 一批失败就停：继续跑只是多烧钱
    except Exception as exc:                       # noqa: BLE001
        # 任何异常都不能让批次停在 draft —— 库里躺着一个「正在跑」的审查，
        # 比明确失败更糟：第二天没人知道它是死是活。
        if store is not None:
            if outcome.review_id:
                store.finish_review(outcome.review_id, "blocked",
                                    plan="\n\n".join(p for p in plans if p) or None)
            store.close()
        raise

    session.scope = None                           # 收尾统计要算全量，不是最后一批
    outcome.state = "Done" if states and all(x == "Done" for x in states) else "Blocked"
    outcome.blocked_reason = "；".join(reasons)
    if len(batches) > 1:
        session.plan = "\n\n".join(f"[{b.label()}]\n{plans[i]}"
                                   for i, b in enumerate(batches[:len(plans)]) if plans[i])
    else:
        session.plan = plans[0] if plans else ""

    if store is not None:
        # Done 与 Blocked 都落终态：Blocked 的 findings 恰恰是最该留档的
        store.finish_review(outcome.review_id,
                            "done" if outcome.state == "Done" else "blocked",
                            plan=session.plan or None)
        session.flush_units(UnitCache(store), outcome.review_id)
        store.close()
    return outcome


def default_db(root: str | Path) -> str:
    return str(Path(root) / DEFAULT_DB)

