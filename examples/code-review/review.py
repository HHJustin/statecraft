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

从 P14/P15 起，真正的实现搬到了包里（`statecraft.review` / `statecraft.incremental`），
这里只留「演示 + 标准答案对照」这两件示例专属的事：
    - `_PLANTED` / `score_against_planted`：靶子文件的埋点答案
    - `_ScriptedBrain`：故意编造一条证据的离线假模型（专门验证防线）

运行方式：
    # 真实模型
    set OPENAI_API_KEY=...
    set OPENAI_BASE_URL=https://www.apigoto.com/v1
    set OPENAI_MODEL=deepseek-flash
    python examples/code-review/review.py

    # 离线演示（无需 key，用脚本化假模型跑完整流程）
    python examples/code-review/review.py --mock

    # 增量审查：只审 git 变更过的函数（P14）
    python examples/code-review/review.py --diff HEAD~1 --mock

    # 只打印会审哪些函数，不调模型（零成本预演）
    python examples/code-review/review.py --diff HEAD~1 --dry-run
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent.parent) not in sys.path:      # 允许直接 python review.py
    sys.path.insert(0, str(_HERE.parent.parent))

from statecraft.incremental import (UnitCache, plan_for_files, plan_from_diff,
                                    plan_full, repo_root)
from statecraft.providers.base import LLMReply, ToolCall
from statecraft.review.findings import Finding, ReviewSession, make_tools
from statecraft.review.pipeline import (build as _build, make_llm,
                                        run_review)
from statecraft.storage import DEFAULT_DB, ReviewStore

TARGET_DIR = _HERE
TARGET_FILES = ["target.py"]
# 审查结果默认落在仓库根目录（.gitignore 已忽略 *.db）
DEFAULT_DB_PATH = _HERE.parent.parent / DEFAULT_DB

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


def build(project_root: Path, llm, session: ReviewSession | None = None,
          plan=None):
    """兼容旧签名：示例与测试都用 `R.build(root, brain, session)`。"""
    return _build(project_root, llm, session, plan)


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
        target = self.session.files[0]
        if target.endswith("target_clean.py"):
            # 干净文件：演示「零发现也要给清白依据」，否则会被完成条件拦下
            if n == 1:
                return LLMReply(tool_calls=[ToolCall("read_file", {"path": target})])
            return LLMReply(tool_calls=[ToolCall("declare_clean", {
                "file": target,
                "reason": "逐个函数看过边界条件、异常处理、命名语义、资源释放、"
                          "默认参数五类，未发现缺陷"})])
        if n == 1:
            return LLMReply(tool_calls=[ToolCall("read_file", {"path": target})])
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
def _plan(root: Path, files: list[str], diff: str | None, head: str | None,
          three_dot: bool, everything: bool, cache):
    if diff is not None:
        base = repo_root(root)
        print(f"[review] git 根目录 {base}（--root 指向子目录时自动对齐）")
        return plan_from_diff(base, base=diff, head=head, three_dot=three_dot,
                              cache=cache)
    if everything:
        return plan_full(root, cache=cache)
    return plan_for_files(root, files, cache=cache)


async def main(mock: bool = False, files: list[str] | None = None,
               root: str | Path | None = None, db: str | None = None,
               review_id: str | None = None, diff: str | None = None,
               head: str | None = None, three_dot: bool = False,
               everything: bool = False, dry_run: bool = False,
               no_cache: bool = False) -> None:
    files = files or TARGET_FILES
    root = Path(root).resolve() if root else TARGET_DIR
    if db != "":
        db_path = db or str(DEFAULT_DB_PATH)
    else:
        db_path = None

    cache, pre = None, None
    if db_path and not no_cache:
        pre = ReviewStore(db_path)
        cache = UnitCache(pre)
    try:
        plan = _plan(root, files, diff, head, three_dot, everything, cache)
    finally:
        if pre is not None:
            pre.close()

    print("[review] 审查范围:")
    for line in plan.summary_lines():
        print("   ", line)
    if dry_run:
        print("\n" + plan.describe_verbose())
        print(f"\n[dry-run] 预计审查 {plan.unit_count} 个函数 / {len(plan.files)} 个文件，"
              f"未调用模型。")
        return

    if mock:
        print("[review] 离线模式：脚本化假模型（--mock）")
        brain_holder: dict = {}

        def factory(session):
            brain_holder["b"] = _ScriptedBrain(session)
            return brain_holder["b"]

        outcome = await run_review(plan, None, db=db_path, review_id=review_id,
                                   llm_factory=factory, model_name="mock")
    else:
        llm = make_llm()
        import os
        print(f"[review] 模型: {os.environ.get('OPENAI_MODEL', '')} @ "
              f"{os.environ.get('OPENAI_BASE_URL', '')}")
        outcome = await run_review(plan, llm, db=db_path, review_id=review_id,
                                   model_name=os.environ.get("OPENAI_MODEL", ""))

    session, result = outcome.session, outcome
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
    if plan.skipped:
        print(f"  增量: 缓存跳过 {len(plan.skipped)} 个函数（函数体哈希未变）"
              f"｜复用结论 {outcome.adopted} 条")
    if not outcome.ran_model:
        print("  本次全部命中缓存，未调用模型。")

    planted = planted_for(session.files) if plan.source == "files" else {}
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
    elif plan.source == "files":
        print(f"  无标准答案文件：报了 {ok} 条（需人工复核，不能直接算误报）")

    if db_path:
        rv = ReviewStore(db_path)
        try:
            r = rv.get_review(outcome.review_id)
            print("\n[review] 已落库（P12）：")
            print(f"  批次 {outcome.review_id}｜状态 {r['status']}｜"
                  f"findings {r['findings_total']} 条（确认 {r['findings_confirmed']}"
                  f" / 推翻 {r['findings_disputed']}）")
            print("  严重度分布:", rv.counts_by_severity(outcome.review_id))
            dist = ", ".join(f"{x['file']}×{x['n']}"
                             for x in rv.counts_by_file(outcome.review_id))
            print("  文件分布:", dist or "（无）")
            print("  清白声明:", [c["file"] for c in rv.clean_claims(outcome.review_id)]
                  or "（无）")
            print(f"  历史批次: {len(rv.list_reviews())} 条")
        finally:
            rv.close()
    print("=" * 72)


if __name__ == "__main__":
    argv = sys.argv[1:]
    _files, _root, _db, _rid = [], None, None, None
    _diff, _head = None, None
    if "--no-db" in argv:
        _db = ""                                  # 显式关掉落库
    for i, a in enumerate(argv):
        def _val(j=i):
            return argv[j + 1] if j + 1 < len(argv) else None
        if a == "--file" and i + 1 < len(argv):
            _files.extend(_val().split(","))
        elif a == "--root" and i + 1 < len(argv):
            _root = _val()
        elif a == "--db" and i + 1 < len(argv):
            _db = _val()
        elif a == "--review-id" and i + 1 < len(argv):
            _rid = _val()                          # 固定批次号 = 重跑覆盖而非新增
        elif a == "--head" and i + 1 < len(argv):
            _head = _val()
    if "--diff" in argv:                           # --diff 或 --diff <rev>
        i = argv.index("--diff")
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        _diff = nxt if nxt and not nxt.startswith("--") else "HEAD"
    asyncio.run(main(mock="--mock" in argv, files=_files or None, root=_root,
                     db=_db, review_id=_rid, diff=_diff, head=_head,
                     three_dot="--three-dot" in argv, everything="--all" in argv,
                     dry_run="--dry-run" in argv, no_cache="--no-cache" in argv))
