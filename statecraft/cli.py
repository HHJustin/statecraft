"""命令行入口（P15）—— `python -m statecraft`。

设计原则只有一条：**CLI 是薄壳，逻辑全在包里。**
命令里不出现任何「顺手实现一遍」的判断（形式审、覆盖闸门、落库时机），
它们都在 `statecraft.review` / `statecraft.qa` 里，CLI 只负责参数、打印、
退出码。

五个子命令：

    review   跑一次审查（增量 / 指定路径 / 整仓），落库
    report   看最近一次（或指定批次）的报告
    ask      就审查记录提问
    reviews  列历史批次（不然 report --review 的 ID 无处可查）
    units    只算「会审哪些函数」，不调模型 —— 零成本预演

退出码：0 = 正常（含「没有问题」），1 = 业务失败（Blocked / 查无批次），
2 = 用法或环境错误（没 key、不是 git 仓库、路径不存在）。
CI 里靠退出码卡门禁，所以 1 和 2 必须分开：门禁要拦「审出高危」，
但「没装 git」不该被当成「审查未通过」。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from statecraft.incremental import (GitError, UnitCache, is_git_repo,
                                    plan_for_files, plan_from_diff, plan_full,
                                    repo_root)
from statecraft.incremental.plan import ReviewPlan
from statecraft.review.batch import MAX_UNITS_PER_BATCH
from statecraft.review.pipeline import (HeuristicMockBrain, make_llm,
                                        run_review)
from statecraft.storage import DEFAULT_DB, ReviewStore

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------- 公共小工具
def _db_of(args, root: Path) -> str | None:
    if getattr(args, "no_db", False):
        return None
    return str(getattr(args, "db", None) or (root / DEFAULT_DB))


def _keep_sev(items, floor: str | None):
    if not floor:
        return list(items)
    top = SEV_ORDER[floor]
    return [f for f in items if SEV_ORDER.get(f.get("severity") if isinstance(f, dict)
                                              else f.severity, 9) <= top]


def _finding_dict(f) -> dict:
    return {"id": f.id, "file": f.file, "line": f.line, "severity": f.severity,
            "category": f.category, "message": f.message, "suggestion": f.suggestion,
            "cached": f.cached, "in_diff": f.in_diff, "status": f.status}


def _make_plan(args, root: Path, cache: UnitCache | None) -> ReviewPlan:
    """三种来源一个出口。参数优先级：--diff > --file > --path。"""
    patterns = tuple(args.include or ("*.py",))
    if getattr(args, "diff", None) is not None:
        if not is_git_repo(root):
            raise GitError(f"{root} 不是 git 仓库，--diff 用不了")
        base = repo_root(root)
        # --path 指向子目录时把它变成 pathspec —— 否则「只看 src/ 的变更」
        # 会把整仓的改动都拉进来，增量审查白做一次。
        if base != root:
            rel = root.relative_to(base).as_posix()
            patterns = tuple(f"{rel}/{p}" for p in patterns)
        return plan_from_diff(base, base=args.diff, head=args.head,
                              patterns=patterns, three_dot=args.three_dot,
                              cache=cache)
    if getattr(args, "file", None):
        files = [x for chunk in args.file for x in chunk.split(",") if x.strip()]
        return plan_for_files(root, files, cache=cache)
    return plan_full(root, patterns, cache=cache, limit=getattr(args, "limit", 0) or 0)


def _header(title: str) -> None:
    print(f"\n{title}")
    print("─" * 64)


# ---------------------------------------------------------------- review
def cmd_review(args) -> int:
    root = Path(args.path or ".").expanduser()
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    root = root.resolve()
    if root.is_file():
        args.file, args.path = [root.name], str(root.parent)
        root = root.parent

    db = _db_of(args, root)
    cache, pre = None, None
    if db and not args.no_cache:
        pre = ReviewStore(db)
        cache = UnitCache(pre)

    try:
        plan = _make_plan(args, root, cache)
    except GitError as e:
        print(f"[git] {e}", file=sys.stderr)
        if pre:
            pre.close()
        return 2
    finally:
        if pre is not None:
            pre.close()

    if args.dry_run:
        print(f"[dry-run] {root}")
        print(plan.describe_verbose())
        print(f"\n预计需要审查 {plan.unit_count} 个函数、{len(plan.files)} 个文件"
              f"（缓存跳过 {len(plan.skipped)} 个）。未调用模型。")
        return 0

    print(f"[review] 根目录 {root}")
    for line in plan.summary_lines():
        print(f"[review] {line}")
    if not plan.files:
        print("[review] 没有需要审查的变更（全部命中缓存或没有 .py 改动）。")

    llm, factory = None, None
    if not plan.files:
        pass                                  # 没有要审的东西：不初始化模型
    elif args.mock:
        print("[review] 离线模式：脚本化假模型（结果只用于演示流程，不代表审查能力）")
        factory = HeuristicMockBrain
    else:
        try:
            llm = make_llm()
        except SystemExit as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"[review] 模型 {os.environ.get('OPENAI_MODEL', '') or '（默认）'}")

    try:
        outcome = asyncio.run(run_review(
            plan, llm, db=db, review_id=args.review_id,
            use_cache=not args.no_cache, llm_factory=factory,
            model_name="mock" if args.mock else os.environ.get("OPENAI_MODEL", "")))
    except GitError as e:
        print(f"[git] {e}", file=sys.stderr)
        return 2
    except Exception as exc:                       # noqa: BLE001
        print(f"[review] 运行中断：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "review_id": outcome.review_id, "db": db, **outcome.stats(),
            "plan": {"source": plan.source, "origin": plan.origin,
                     "files": plan.files,
                     "skipped": [f"{u.file}:{u.qualname}" for u, _ in plan.skipped]},
            "findings": [_finding_dict(f) for f in
                         _keep_sev(outcome.session.confirmed(), args.severity)],
        }, ensure_ascii=False, indent=2))
        return 0 if outcome.state == "Done" else 1

    s = outcome.session
    _header(f"[完成] {outcome.state}｜批次 {outcome.review_id or '（未落库）'}")
    if outcome.batches > 1:
        print(f"分 {outcome.batches} 批执行（每批最多 "
              f"{MAX_UNITS_PER_BATCH} 个函数，批内独立走完规划→审查→复核）")
    print("流转:", " → ".join(["Created"] + [r["to"] for r in outcome.flow_log]))
    if outcome.blocked_reason:
        print("Blocked 原因:", outcome.blocked_reason)
    print(s.summary())

    shown = _keep_sev(s.confirmed(), args.severity)
    if shown:
        print(f"\n问题清单（{len(shown)} 条"
              + (f"，仅显示 {args.severity} 及以上" if args.severity else "") + "）:")
        for f in shown:
            print(f"  {f.severity:<6} {f.file}:{f.line:<5} {f.message}")
            if f.suggestion:
                print(f"        建议: {f.suggestion}")
    if s.out_of_diff():
        print(f"\n另有 {len(s.out_of_diff())} 条问题落在未变更行上（历史问题，"
              f"不是这个提交引入的）：")
        for f in s.out_of_diff():
            print(f"  {f.file}:{f.line} {f.message[:56]}")
    if s.rejected():
        print(f"\n形式审驳回 {len(s.rejected())} 条（编造证据/越界/未读即报，不计入报告）")

    print(f"\n[统计] 文件 {len(s.files)}｜函数 {sum(len(v) for v in s.units.values())}"
          f"｜缓存跳过 {outcome.cached_units}｜复用结论 {outcome.adopted}")
    if db:
        print(f"[落库] {db}｜批次 {outcome.review_id}")
        print(f"       查看报告: python -m statecraft report --db \"{db}\"")
    return 0 if outcome.state == "Done" else 1


# ---------------------------------------------------------------- report
def cmd_report(args) -> int:
    db = _db_of(args, Path.cwd())
    if not db or not Path(db).exists():
        print(f"库不存在：{db}\n先跑一次审查：python -m statecraft review --path . --mock",
              file=sys.stderr)
        return 2
    store = ReviewStore(db)
    with store:
        rv = store.get_review(args.review) if args.review else store.latest_review()
        if rv is None:
            print("还没有已完成的审查批次。", file=sys.stderr)
            return 1
        rows = _keep_sev(store.findings(rv["id"], form_ok=True), args.severity)
        if args.json:
            print(json.dumps({
                "review": {k: rv.get(k) for k in
                           ("id", "status", "repo_root", "scope", "review_model",
                            "started_at", "finished_at", "findings_total",
                            "findings_confirmed", "findings_disputed", "plan")},
                "counts_by_severity": store.counts_by_severity(rv["id"]),
                "counts_by_file": store.counts_by_file(rv["id"]),
                "clean_claims": store.clean_claims(rv["id"]),
                "findings": [{k: r.get(k) for k in
                              ("id", "file", "line", "severity", "category",
                               "message", "evidence", "suggestion", "status")}
                             for r in rows],
            }, ensure_ascii=False, indent=2))
            return 0

        _header(f"审查批次 {rv['id']}｜{rv['status']}")
        print(f"仓库  {rv['repo_root']}")
        print(f"范围  {', '.join(rv['scope']) if isinstance(rv['scope'], list) else rv['scope']}")
        print(f"时间  {rv['started_at']} → {rv['finished_at'] or '（未收尾）'}"
              f"｜模型 {rv['review_model'] or '（未记录）'}")
        print(f"统计  有效 {rv['findings_total']}（确认 {rv['findings_confirmed']}"
              f" / 推翻 {rv['findings_disputed']}）"
              f"｜分布 {store.counts_by_severity(rv['id'])}")
        if rv.get("plan"):
            print(f"\n审查计划（规划者原文前 200 字）:\n  {str(rv['plan'])[:200]}")

        print(f"\n问题（{len(rows)} 条"
              + (f"，仅 {args.severity} 及以上" if args.severity else "") + "）:")
        for r in rows:
            print(f"  {r['severity']:<6} {r['file']}:{r['line']:<5} {r['message']}")
            if r.get("suggestion"):
                print(f"        建议: {r['suggestion']}")
        claims = store.clean_claims(rv["id"])
        if claims:
            print(f"\n清白声明（{len(claims)} 个文件审过且判定无问题）:")
            for c in claims:
                print(f"  {c['file']}: {c['reason'][:70]}")
    return 0


# ---------------------------------------------------------------- ask
def cmd_ask(args) -> int:
    db = _db_of(args, Path.cwd())
    if not db or not Path(db).exists():
        print(f"库不存在：{db}\n先跑一次审查落库。", file=sys.stderr)
        return 2
    from statecraft.qa.answer import CodebaseQA
    from statecraft.storage.findings import ReviewStore as RS

    store = RS(db)
    llm = None
    if not args.no_llm:
        if args.mock:
            from statecraft.providers.base import LLMReply
            from statecraft.providers.mock import MockLLM

            def handler(messages, tools):
                ctx = messages[-1]["content"]
                first = next((ln for ln in ctx.splitlines() if ln.startswith("[F1]")), "")
                return LLMReply(content=f"根据审查记录：{first[5:].strip()} [F1]")

            llm = MockLLM(handler=handler)
        else:
            if os.environ.get("OPENAI_API_KEY", "").strip():
                llm = make_llm()
    mem = None
    if args.memory:
        from statecraft.memory.sqlite import SqliteMemory

        mem = SqliteMemory(db)             # 与 findings 共用同一个库文件

    qa = CodebaseQA(store, llm=llm, memory=mem)
    if not store.findings(limit=1):
        print("库里还没有 findings，先跑一次审查。", file=sys.stderr)
        return 1
    if mem is not None:
        n = qa.index_findings()            # 语义召回的候选源；DB 仍是真相源
        print(f"[ask] 已确保 {n} 条 finding 在记忆层中（供召回，重复的不新增）")
    ans = qa.ask(args.question, review_id=args.review)
    if args.json:
        print(json.dumps(ans.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(ans)
        if args.notes and ans.notes:
            print("\n判定依据：")
            for n in ans.notes:
                print(f"  · {n}")
    return 0


# ---------------------------------------------------------------- reviews
def cmd_reviews(args) -> int:
    db = _db_of(args, Path.cwd())
    if not db or not Path(db).exists():
        print(f"库不存在：{db}", file=sys.stderr)
        return 2
    store = ReviewStore(db)
    with store:
        rows = store.list_reviews(limit=args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("还没有审查批次。")
            return 0
        print(f"{'批次':<14}{'状态':<10}{'有效':>4}{'文件':>4}  {'开始时间':<26}仓库")
        for r in rows:
            n = len(r["scope"]) if isinstance(r["scope"], list) else 0
            print(f"{r['id']:<14}{r['status']:<10}{r['findings_total']:>4}{n:>4}  "
                  f"{str(r['started_at'])[:19]:<26}{Path(str(r['repo_root'])).name}")
    return 0


# ---------------------------------------------------------------- units
def cmd_units(args) -> int:
    root = Path(args.path or ".").expanduser().resolve()
    db = _db_of(args, root)
    cache, pre = None, None
    if db and Path(db).exists() and not args.no_cache:
        pre = ReviewStore(db)
        cache = UnitCache(pre)
    try:
        plan = _make_plan(args, root, cache)
    except GitError as e:
        print(f"[git] {e}", file=sys.stderr)
        return 2
    finally:
        if pre is not None:
            pre.close()
    if args.json:
        print(json.dumps({
            "source": plan.source, "origin": plan.origin,
            "files": {f: [u.as_dict() for u in us] for f, us in plan.units.items()},
            "skipped": [{"file": u.file, "qualname": u.qualname,
                         "from": rid} for u, rid in plan.skipped],
            "replayed": plan.replayed, "dropped": plan.dropped,
            "deletions": plan.deletions, "parse_errors": plan.parse_errors,
        }, ensure_ascii=False, indent=2))
        return 0
    print(f"根目录 {root}")
    print(plan.describe_verbose())
    return 0


# ---------------------------------------------------------------- 解析器
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="statecraft",
        description="代码注释生成 / 代码审查 多 Agent 内核的命令行入口")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help=f"SQLite 路径（默认 ./{DEFAULT_DB}）")
    common.add_argument("--no-db", action="store_true", help="不落库（内存里跑完就散）")
    common.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")

    r = sub.add_parser("review", parents=[common], help="跑一次审查")
    r.add_argument("--path", default=None, help="审查根目录或文件（默认当前目录）")
    r.add_argument("--file", action="append", help="指定文件，可重复或逗号分隔")
    r.add_argument("--diff", nargs="?", const="HEAD", default=None,
                   help="只审 git 变更：--diff 或 --diff HEAD~3")
    r.add_argument("--head", default=None, help="与 --diff 搭配：比较 base..head")
    r.add_argument("--three-dot", action="store_true",
                   help="用 A...B（按 merge-base，CI 比分支时用这个）")
    r.add_argument("--include", action="append", help="文件模式，可重复（默认 *.py）")
    r.add_argument("--severity", choices=list(SEV_ORDER), default=None,
                   help="只显示该严重度及以上的问题")
    r.add_argument("--mock", action="store_true", help="离线假模型（不花 key）")
    r.add_argument("--dry-run", action="store_true", help="只打印审查计划，不调模型")
    r.add_argument("--no-cache", action="store_true", help="忽略函数级缓存（强制全审）")
    r.add_argument("--review-id", default=None, help="固定批次号（重跑覆盖同一批次）")
    r.add_argument("--limit", type=int, default=0, help="整仓模式最多审几个文件")
    r.set_defaults(func=cmd_review)

    rp = sub.add_parser("report", parents=[common], help="看审查报告")
    rp.add_argument("--review", default=None, help="批次号（默认最近一次）")
    rp.add_argument("--last", action="store_true", help="最近一次（默认行为）")
    rp.add_argument("--severity", choices=list(SEV_ORDER), default=None)
    rp.set_defaults(func=cmd_report)

    a = sub.add_parser("ask", parents=[common], help="就审查记录提问")
    a.add_argument("question", help="问题，如「auth.py 有哪些高危问题」")
    a.add_argument("--review", default=None, help="限定批次号")
    a.add_argument("--no-llm", action="store_true", help="不调模型（纯 SQL/清单回答）")
    a.add_argument("--mock", action="store_true", help="离线假模型")
    a.add_argument("--memory", action="store_true", help="启用语义召回（与 findings 共用库）")
    a.add_argument("--notes", action="store_true", help="显示路由判定依据")
    a.set_defaults(func=cmd_ask)

    rv = sub.add_parser("reviews", parents=[common], help="列历史批次")
    rv.add_argument("--limit", type=int, default=20)
    rv.set_defaults(func=cmd_reviews)

    u = sub.add_parser("units", parents=[common], help="只算审查范围（不调模型）")
    u.add_argument("--path", default=None)
    u.add_argument("--file", action="append")
    u.add_argument("--diff", nargs="?", const="HEAD", default=None)
    u.add_argument("--head", default=None)
    u.add_argument("--three-dot", action="store_true")
    u.add_argument("--include", action="append")
    u.add_argument("--no-cache", action="store_true")
    u.add_argument("--limit", type=int, default=0)
    u.set_defaults(func=cmd_units)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except GitError as e:
        print(f"[git] {e}", file=sys.stderr)
        return 2
