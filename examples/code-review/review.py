"""代码审查流水线 —— Statecraft 作为「框架」的第二个落地应用。

与 codedoc（注释生成）共用同一套内核，差别只在三处：
    换 Agent 的 system prompt、换工具集、换权限白名单。

流程一模一样，这是「框架」这个定位的最硬证据：

    Created → Planning → Reviewing → Executing → Reviewing → PendingConfirm → Done
              规划审查     审计划        执行审查      审报告      二次确认     收尾

**代码审查为什么也必过审计（Reviewing）？**
因为审查报告本身也是产出。审查者会漏报、会误报、会编造。
「谁来审审查者」不靠养更高级的审查者（那是无限递归），而靠换证据类型：
    流程契约  → 状态机审（确定）
    报告形式  → 六条确定性规则审（确定）
    技术结论  → meta-review 抽查 + 证据核对（概率）
详见 findings.py 顶部注释。

运行方式：
    # 真实模型
    set OPENAI_API_KEY=...
    set OPENAI_BASE_URL=https://www.apigoto.com/v1
    set OPENAI_MODEL=deepseek-flash
    python examples/code-review/review.py

    # 离线演示（无需 key，用脚本化假模型跑完整流程）
    python examples/code-review/review.py --mock
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent.parent) not in sys.path:      # 允许直接 python review.py
    sys.path.insert(0, str(_HERE.parent.parent))

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.tools import AGENT_POLICY, ToolRegistry, default_registry
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply, ToolCall

from findings import ReviewSession, make_tools

TARGET_DIR = _HERE
TARGET_FILES = ["target.py"]

# 标准答案：(文件, 行号) -> (类别, 期望严重度)。
# 有了它「检出率」才是可算的数字，而不是「看着挺好的」。
# 只按行号匹配：类别/严重度允许模型有自己的判断（同一处缺陷归到
# correctness 还是 exception 经常都有道理），但漏掉或报错行就是真漏报。
_PLANTED = {
    ("target.py", 23): ("boundary", "high"),       # 空列表除零
    ("target.py", 42): ("exception", "high"),      # 宽泛 except 吞异常
    ("target.py", 46): ("naming", "medium"),       # d / t 无语义
    ("target.py", 52): ("correctness", "medium"),  # 裸 open 泄漏句柄
    ("target.py", 56): ("correctness", "high"),    # 可变默认参数
}


def planted_for(files: list[str]) -> dict:
    """干净文件没有标准答案；有了反而会把自己的埋点当误报。"""
    return dict(_PLANTED) if list(files) == ["target.py"] else {}


def score_against_planted(session: ReviewSession, planted: dict) -> dict:
    """把审查结果与标准答案比对：命中 / 漏报 / 额外发现。

    「额外发现」不直接算误报——真模型实测里它多报的 3 条
    （常量未使用 / 金额浮点误差 / 异常类型语义错）都确实成立。
    所以这里分开列，由人复核；只有审**干净文件**时非空才算误报。
    """
    got = {(f.file, f.line): f for f in session.confirmed()}
    hit = [k for k in planted if k in got]
    miss = [k for k in planted if k not in got]
    extra = [f for k, f in got.items() if k not in planted]
    cat_ok = [k for k in hit if got[k].category == planted[k][0]]
    return {"expected": len(planted), "hit": len(hit), "miss": miss,
            "extra": extra, "cat_ok": len(cat_ok),
            "rate": len(hit) / len(planted) if planted else None}

# ---------------------------------------------------------------- 角色权限
# 内核零改动：agents 字典的键仍是 planner/critic/executor（STATE_AGENT_MAP 约定），
# 只把 role 换成审查流水线自己的策略名，resolve_policy 会先按 role 查。
AGENT_POLICY["review_planner"] = {"role": "coordination",
                                  "tools": {"report_progress", "read_file"}}
AGENT_POLICY["reviewer"] = {"role": "execution",
                            "tools": {"report_progress", "read_file", "record_finding"}}
AGENT_POLICY["review_critic"] = {"role": "review",
                                 "tools": {"report_progress", "read_file", "confirm_finding"}}

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
    "3. 全部处理完后，输出审查摘要，每条一行，格式：`#<id> <file>:<line> <severity> <一句话>`，"
    "然后单独一行输出 `REQUEST_REVIEW`（硬性要求，否则流程无法推进）。"
)

CRITIC_PROMPT = (
    "你是审查质量复核者。上下文会用 `[审核对象｜来自 X]` 标明这一轮审什么：\n"
    "A. 来自 Planning（审计划）：只判断计划是否覆盖了全部待审文件、每个检查项是否具体。"
    "计划阶段本来就没有具体问题清单，不要因此打回。\n"
    "B. 来自 Executing（审报告）：抽查 1-2 条 finding，自己 read_file 核对——"
    "证据那几行是否真的存在、问题描述是否成立。核对后用 confirm_finding 给出 "
    "confirm 或 dispute；发现明显不符的直接 dispute。\n"
    "输出契约（二选一，只输出一种，不要解释过程）：\n"
    "  合格 → 只输出「审核通过」\n"
    "  不合格 → 只输出「打回：<一句话理由>」\n"
    "不要写 DONE。"
)


# ---------------------------------------------------------------- 装配
def build(project_root: Path, llm,
          session: ReviewSession | None = None) -> tuple[Orchestrator, ReviewSession]:
    session = session or ReviewSession(project_root, TARGET_FILES)

    registry = ToolRegistry()
    for name in ("report_progress", "request_block"):   # 复用内核内置工具
        registry.register(default_registry().get(name))
    for t in make_tools(session):
        registry.register(t)

    agents = {
        "planner": Agent("planner", llm, role="review_planner", registry=registry,
                         system_prompt=PLANNER_PROMPT),
        "critic": Agent("critic", llm, role="review_critic", registry=registry,
                        system_prompt=CRITIC_PROMPT),
        "executor": Agent("executor", llm, role="reviewer", registry=registry,
                          system_prompt=REVIEWER_PROMPT),
    }
    orch = Orchestrator(agents, InMemoryMemory(),
                        AgentCoreConfig(auto_confirm=True))
    return orch, session


def make_llm():
    """有 key 走真实模型，否则报错——离线请用 --mock。"""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise SystemExit("未设置 OPENAI_API_KEY。想离线看流程请加 --mock。")
    from statecraft.providers.openai_compat import OpenAICompatLLM
    base = os.environ.get("OPENAI_BASE_URL", "https://www.apigoto.com/v1")
    model = os.environ.get("OPENAI_MODEL", "deepseek-flash")
    print(f"[review] 模型: {model} @ {base}")
    return OpenAICompatLLM(base_url=base, model=model, api_key=key)


# ---------------------------------------------------------------- 离线假模型
class _ScriptedBrain:
    """离线演示用的「假模型」：按角色 + 轮次返回预设工具调用。

    它故意做两件事来验证防线：
      1. 第 6 条 finding 的 evidence 是编造的 → 形式审必须驳回；
      2. 有一条 finding 在记录前没有 read_file → 先读再报，避免触发未读即报。
    """

    def __init__(self, session: ReviewSession):
        self.session = session
        self.turn: dict[str, int] = {}

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
        return self._reply(role, n)

    def _reply(self, role: str, n: int) -> LLMReply:
        if role == "planner":
            return LLMReply(content="审查计划：对 target.py 的 average / send_receipt / "
                                    "process / load_config / register 逐个检查边界条件、"
                                    "异常处理、命名语义、资源释放、默认参数。")
        if role == "critic":
            if n == 1:
                return LLMReply(content="审核通过")          # 审计划
            if n == 2:                                       # 审报告：先读文件
                return LLMReply(tool_calls=[ToolCall("read_file", {"path": "target.py"})])
            if n == 3:                                       # 抽查第一条
                ok = [f for f in self.session.findings.values() if f.form_ok]
                if ok:
                    return LLMReply(tool_calls=[ToolCall("confirm_finding", {
                        "finding_id": ok[0].id, "verdict": "confirm",
                        "note": f"已核对 target.py:{ok[0].line}，证据那几行确实存在"})])
            return LLMReply(content="审核通过")
        # executor
        if n == 1:
            return LLMReply(tool_calls=[ToolCall("read_file", {"path": "target.py"})])
        if n == 2:
            return LLMReply(tool_calls=[
                ToolCall("record_finding", {
                    "file": "target.py", "line": 23, "severity": "high",
                    "category": "boundary",
                    "message": "average() 对空列表直接除零，调用方会收到 ZeroDivisionError 而非可读错误",
                    "evidence": "    return sum(values) / len(values)",
                    "suggestion": "先判空并抛 ValueError"}),
                ToolCall("record_finding", {
                    "file": "target.py", "line": 42, "severity": "high",
                    "category": "exception",
                    "message": "except Exception + pass 把投递失败静默吞掉，上游永远以为发送成功",
                    "evidence": "    except Exception:\n        pass",
                    "suggestion": "只捕获具体异常并转成带上下文的错误"}),
                ToolCall("record_finding", {
                    "file": "target.py", "line": 46, "severity": "medium",
                    "category": "naming",
                    "message": "process(d, t) 的形参名看不出语义，无法判断是金额还是数量",
                    "evidence": "def process(d, t):\n    return d * t",
                    "suggestion": "按语义重命名为 amount / tax_rate"}),
                ToolCall("record_finding", {
                    "file": "target.py", "line": 52, "severity": "medium",
                    "category": "correctness",
                    "message": "open() 没用 with，read 抛异常时文件句柄泄漏",
                    "evidence": '    f = open(path, encoding="utf-8")',
                    "suggestion": "用 with open(...) as f"}),
                ToolCall("record_finding", {
                    "file": "target.py", "line": 56, "severity": "high",
                    "category": "correctness",
                    "message": "可变默认参数 bucket=[] 跨调用共享，第二次调用会带出上次的条目",
                    "evidence": "def register(item, bucket=[]):",
                    "suggestion": "改用 bucket=None 哨兵"}),
                # 故意编造：这行在源文件里不存在
                ToolCall("record_finding", {
                    "file": "target.py", "line": 30, "severity": "low",
                    "category": "perf",
                    "message": "format_money 每次都重建 f-string（编造示例，应被驳回）",
                    "evidence": "    return str(round(cents / 100, 2))",
                    "suggestion": "无"}),
            ])
        ids = [f"#{f.id}" for f in self.session.findings.values() if f.form_ok]
        return LLMReply(content="审查完成，共 %d 条有效问题：\n" % len(ids)
                                + "\n".join(ids) + "\nREQUEST_REVIEW")


# ---------------------------------------------------------------- 主流程
async def main(mock: bool = False, files: list[str] | None = None) -> None:
    files = files or TARGET_FILES
    session = ReviewSession(TARGET_DIR, files)
    if mock:
        print("[review] 离线模式：脚本化假模型（--mock）")
        llm = _ScriptedBrain(session)
    else:
        llm = make_llm()

    orch, session = build(TARGET_DIR, llm, session)

    print("[review] 待审文件:", ", ".join(files))
    task = orch.create_task(
        f"对代码文件 {', '.join(files)} 做代码审查，输出结构化问题清单。\n"
        f"[要求] 每条问题必须带文件、行号、严重度、类别与源文件证据；"
        f"证据需逐字符可核对。只报真实缺陷。"
    )
    result = await orch.run(task)

    print("\n" + "=" * 72)
    print("[review] 最终状态:", result.state)
    print("[review] 流转轨迹:",
          " → ".join(["Created"] + [r["to"] for r in result.flow_log]))
    print("[review] 打回次数:", result.reject_count)
    if result.blocked_reason:
        print("[review] Blocked 原因:", result.blocked_reason)

    print("\n[review] 形式审明细（六条确定性规则）:")
    for f in session.findings.values():
        flags = " ".join(f"{k}={'✓' if v else '✗'}" for k, v in f.checks.items())
        print(f"  {f.one_line()}")
        print(f"      {flags}")
        for r in f.reasons:
            print(f"      ← {r}")

    print("\n[review] 最终报告:")
    print(session.report())

    print("[review] 统计:")
    ok, rej, dis = len(session.confirmed()), len(session.rejected()), len(session.disputed())
    total = max(1, len(session.findings))
    print(f"  文件覆盖: {len(session.files) - len(session.coverage())}/{len(session.files)}"
          f"  有效: {ok}  编造驳回: {rej}  复核推翻: {dis}")
    print(f"  幻觉率(编造): {rej / total:.0%}"
          f"  复核推翻率: {dis / max(1, ok + dis):.0%}")

    planted = planted_for(files)
    if planted:
        sc = score_against_planted(session, planted)
        print(f"  埋点检出: {sc['hit']}/{sc['expected']}（{sc['rate']:.0%}）"
              f"  类别相符: {sc['cat_ok']}/{sc['hit']}")
        if sc["miss"]:
            print("  漏报:", ", ".join(f"{f}:{ln}" for f, ln in sc["miss"]))
        if sc["extra"]:
            print(f"  额外发现 {len(sc['extra'])} 条（标准答案之外，需人工复核）:")
            for f in sc["extra"]:
                print(f"    · {f.file}:{f.line} {f.severity} {f.message[:52]}")
    else:
        print(f"  干净文件误报: {ok} 条（期望 0）")
    print("=" * 72)


if __name__ == "__main__":
    _files = []
    for i, a in enumerate(sys.argv):
        if a == "--file" and i + 1 < len(sys.argv):
            _files.extend(sys.argv[i + 1].split(","))
    asyncio.run(main(mock="--mock" in sys.argv, files=_files or None))
