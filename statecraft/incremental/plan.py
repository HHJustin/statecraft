"""审查计划 —— 决定「这一次到底审什么」（P14）。

三种来源，一个出口（`ReviewPlan`）：

| 来源 | 触发 | 范围 |
|---|---|---|
| `diff` | `--diff HEAD~1` | 只审改动过的函数（AST 映射，函数体哈希命中则跳过） |
| `files` | `--path x.py` / `--file` | 指定文件，整文件 |
| `full` | `--path .`（默认全仓） | 整仓所有 .py，靠缓存把重跑成本压到接近 0 |

**为什么 `diff` 模式下「缓存命中」几乎总是发生在「只改了函数外的行」**：
改了函数体，哈希必然变（哈希就是函数体算出来的）。所以 diff 模式命中缓存
只有两种情形：一是这次 diff 只动了这个文件里的别处（比如加了 import），
二是同一个提交被重跑了一遍。两种都该跳过 —— 唯一要小心的是第一种
不能把该函数上次的**问题**一起丢掉：报告里会带回来并标注「复用」。

计划对象只描述「审什么」，不碰模型、不碰状态机 —— 这样 `--dry-run`
可以零成本打印它，测试也不用起流水线。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .cache import UnitCache
from .diff import FileChange, collect_diff, worktree_differs
from .units import CodeUnit, changed_units, iter_python_files, read_units

_MAX_LISTED = 12                      # 单个文件在 brief 里最多列这么多函数


@dataclass
class ReviewPlan:
    root: Path
    files: list[str] = field(default_factory=list)
    units: dict[str, list[CodeUnit]] = field(default_factory=dict)
    ranges: dict[str, list[tuple[int, int]]] | None = None
    source: str = "files"                       # diff / files / full
    origin: str = ""                            # 人话来源，写进报告
    strict_units: bool = False                  # 完成条件是否细到函数
    skipped: list[tuple[CodeUnit, str]] = field(default_factory=list)   # (单位, 来源批次)
    replayed: list[dict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    pure_deletions: list[str] = field(default_factory=list)   # 「文件（N 段纯删除）」
    deleted_files: list[str] = field(default_factory=list)    # 整个文件被删
    missing_files: list[str] = field(default_factory=list)    # 变更里有、磁盘上没有
    warnings: list[str] = field(default_factory=list)         # 需要在报告里说清的疑点
    parse_errors: list[str] = field(default_factory=list)

    @property
    def deletions(self) -> list[str]:
        """三类「没有内容可审」的合并视图（老调用方与测试仍按这个读）。"""
        out = list(self.pure_deletions)
        if self.deleted_files:
            out.append("、".join(f"已删除 {p}" for p in self.deleted_files))
        if self.missing_files:
            out.append(f"磁盘上已不存在：{'、'.join(self.missing_files)}")
        return out

    # ------------------------------------------------------------ 摘要
    @property
    def unit_count(self) -> int:
        return sum(len(v) for v in self.units.values())

    def summary_lines(self) -> list[str]:
        head = {
            "diff": f"增量审查｜{self.origin}",
            "files": "指定文件审查",
            "full": "整仓审查",
        }.get(self.source, self.source)
        if not self.files:
            if self.skipped:
                out = [f"{head}：无需审查 —— {len(self.skipped)} 个函数全部命中缓存"
                       f"（函数体哈希与上次审查一致），本次不调用模型"]
            else:
                out = [f"{head}：没有可审的 Python 变更"]
            return out + self._tail_lines()
        out = [f"{head}：{len(self.files)} 个文件、{self.unit_count} 个函数待审"]
        return out + self._tail_lines()

    def _tail_lines(self) -> list[str]:
        out: list[str] = []
        if self.skipped and self.files:
            files = len({u.file for u, _ in self.skipped})
            out.append(f"缓存命中跳过 {len(self.skipped)} 个函数（分布在 {files} 个文件，"
                       f"函数体哈希与上次审查一致）")
        if self.replayed:
            out.append(f"复用上次结论 {len(self.replayed)} 条（已按新行号重定位）")
        if self.dropped:
            out.append(f"缓存失效丢弃 {len(self.dropped)} 条：{self.dropped[0]}"
                       + ("…" if len(self.dropped) > 1 else ""))
        if self.pure_deletions:
            # 这行原来把「纯删除」和「删了整文件」「磁盘上没有了」混成一个数字打印，
            # 结果「已删除 newmod.py」在报告里根本没出现（用户以为审过了整仓）。
            out.append(f"纯删除变更 {len(self.pure_deletions)} 个文件（映射到删除点所在"
                       f"函数，整个函数被删时无法映射）："
                       + "、".join(self.pure_deletions[:4])
                       + ("…" if len(self.pure_deletions) > 4 else ""))
        if self.deleted_files:
            out.append(f"文件被整份删除 {len(self.deleted_files)} 个（没有内容可审，"
                       f"仅留档）：" + "、".join(self.deleted_files[:6])
                       + ("…" if len(self.deleted_files) > 6 else ""))
        if self.missing_files:
            out.append(f"变更清单里有、磁盘上已不存在 {len(self.missing_files)} 个"
                       f"（构建产物或未 checkout）："
                       + "、".join(self.missing_files[:6]))
        for w in self.warnings:
            out.append(f"注意：{w}")
        if self.parse_errors:
            out.append(f"语法解析失败 {len(self.parse_errors)} 个文件（退化为整文件审查）："
                       + "、".join(self.parse_errors))
        return out

    def task_brief(self) -> str:
        """给模型的「本次范围」说明。这是增量审查省 Token 的关键一段。"""
        if self.source == "diff":
            lines = [f"本次只审下列变更过的函数（来源：{self.origin}）："]
            for file in self.files:
                lines.append(f"- {file}")
                units = self.units.get(file, [])
                for u in units[:_MAX_LISTED]:
                    lines.append(f"    {u.describe()}")
                if len(units) > _MAX_LISTED:
                    lines.append(f"    …等 {len(units)} 个函数")
            lines.append("上面没列出的函数属于未改动代码，不在本次审查范围。")
            return "\n".join(lines)
        files = self.files
        shown = "、".join(files[:_MAX_LISTED])
        more = f"……等 {len(files)} 个" if len(files) > _MAX_LISTED else ""
        return (f"本次审查 {len(files)} 个文件：{shown}{more}。\n"
                f"每个文件都必须有结论（record_finding 或 declare_clean）。")

    def describe_verbose(self) -> str:
        lines = self.summary_lines() + [""]
        for file in self.files:
            lines.append(f"{file}")
            for u in self.units.get(file, []):
                lines.append(f"    {u.describe():<44} hash={u.body_hash}")
        if self.skipped:
            lines.append("")
            lines.append("缓存命中（跳过）：")
            for u, rid in self.skipped:
                lines.append(f"    {u.file}:{u.qualname}（来自批次 {rid}）")
        return "\n".join(lines)


def _apply_cache(root: Path, plan: ReviewPlan, cache: UnitCache | None) -> None:
    """按函数体哈希过滤掉已审过的单位，并把上次的问题带回来。"""
    if cache is None:
        return
    keep: dict[str, list[CodeUnit]] = {}
    seen: set[tuple] = set()          # 去重键：同一条问题可能被多个单位带回
    for file, units in plan.units.items():
        lines = None
        for u in units:
            hit = cache.hit(u)
            if hit is None:
                keep.setdefault(file, []).append(u)
                continue
            if lines is None:
                try:
                    lines = (root / file).read_text(encoding="utf-8",
                                                    errors="replace").splitlines()
                except OSError:
                    lines = []
            got, dropped = cache.replay(u, hit, lines)
            # 整文件模式下，`<module>` 和它里面的每个函数都有一行缓存，
            # 同一条 finding 会被带回多次（报告里「复用 4 条」其实只有 2 条）。
            # 按「文件+行+文案」去重：位置不同就是不同的问题，文案不同也是。
            for f in got:
                key = (f.get("file"), f.get("line"),
                       "".join((f.get("message") or "").split()))
                if key in seen:
                    continue
                seen.add(key)
                plan.replayed.append(f)
            plan.dropped.extend(dropped)
            plan.skipped.append((u, hit.review_id))
    plan.units = keep
    plan.files = [f for f in plan.files if keep.get(f)]


def plan_from_diff(root: str | Path, base: str = "HEAD", head: str | None = None,
                   patterns: tuple[str, ...] = ("*.py",), three_dot: bool = False,
                   cache: UnitCache | None = None) -> ReviewPlan:
    root = Path(root).resolve()
    changes: list[FileChange] = collect_diff(root, base=base, head=head,
                                             paths=patterns, three_dot=three_dot)
    origin = f"git diff {base}...{head}" if (head and three_dot) else \
             (f"git diff {base} {head}" if head else f"git diff {base}")
    ranges = {c.path: c.new_ranges for c in changes if c.reviewable}
    pure = [f"{c.path}（{len(c.pure_deletions)} 段纯删除）"
            for c in changes if c.reviewable and c.pure_deletions]
    deleted = [c.path for c in changes if c.status == "deleted"]
    warnings: list[str] = []
    # 审 A..B 时行区间来自提交 B，函数却按工作区文件解析。工作区脏了，
    # 行号就对不上提交 B —— 映射到错的函数比报错更难发现。
    if head is not None and worktree_differs(root, head, patterns):
        warnings.append(
            f"工作区与 {head} 不一致：行号按工作区文件解析，可能已错位。"
            f"要对提交做准审，请先 git checkout {head}")

    errors: list[str] = []
    missing: list[str] = []
    for file in list(ranges):
        if not (root / file).is_file():
            missing.append(file)                  # 变更清单里有、磁盘上没有
            ranges.pop(file)
            continue
        src = (root / file).read_text(encoding="utf-8", errors="replace")
        try:
            compile(src, file, "exec")
        except SyntaxError:
            # 改坏语法的提交最该被审，所以不退避：退化成整文件一个单位
            errors.append(file)

    plan = ReviewPlan(root=root, files=sorted(ranges), ranges=ranges,
                      source="diff", origin=origin, pure_deletions=pure,
                      deleted_files=deleted, missing_files=missing,
                      warnings=warnings, parse_errors=errors)
    plan.units = changed_units(root, ranges)
    plan.strict_units = True
    _apply_cache(root, plan, cache)
    return plan


def plan_for_files(root: str | Path, files: list[str],
                   cache: UnitCache | None = None) -> ReviewPlan:
    root = Path(root).resolve()
    files = [f.replace("\\", "/").lstrip("./") for f in files]
    plan = ReviewPlan(root=root, files=list(files), ranges=None,
                      source="files", origin="显式指定文件")
    plan.units = {f: read_units(root / f, f) for f in files}
    _apply_cache(root, plan, cache)
    return plan


def plan_full(root: str | Path, patterns: tuple[str, ...] = ("*.py",),
              cache: UnitCache | None = None, limit: int = 0) -> ReviewPlan:
    root = Path(root).resolve()
    files = iter_python_files(root, patterns)
    if limit:
        files = files[:limit]
    plan = ReviewPlan(root=root, files=files, ranges=None, source="full",
                      origin=f"整仓 {len(files)} 个 .py")
    plan.units = {f: read_units(root / f, f) for f in files}
    _apply_cache(root, plan, cache)
    return plan
