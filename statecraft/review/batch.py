"""分批判审 —— 一个任务装不下一整个仓库的变更（P14 真机暴露的必修项）。

真机实测（在 statecraft 自己的仓库上跑 `--diff HEAD`）：14 个文件、**186 个函数**。
原来的实现把 186 个函数塞进一个任务，覆盖闸门要求「每个函数都要有结论」，
模型要在一次执行里产出上百次工具调用 —— 轮次预算（`max_tool_iters`）先耗尽，
任务以 `Blocked` 收场，审查一条有效结论都没有。

这不是「预算调大点」能解决的：
- 预算再大，上下文也会先爆（186 个函数的源码塞不进一次对话）；
- 一次失败就整批 Blocked，正确的 3 条 findings 也一起废掉。

所以按「函数数」切批，每批独立跑完整流程（规划 → 审查 → 复核），结论累加进
同一个 `ReviewSession`、同一个批次号。任何一批失败只影响那一批。

**为什么按函数数而不是按文件数切**：一个 3000 行的文件有 80 个函数，
按文件切等于没切。也不能无脑按文件切完再按函数切，否则同一个文件被拆到两批、
每批都要重新读一遍整文件。

**切批的边界**：一个文件内的函数尽量落在同一批（`_split` 里先整块放，放不下再拆），
因为读文件是批里最贵的动作（planner 和 reviewer 各读一遍）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 一批最多审多少个函数。12 是「读 3-6 个文件 + 12 次结论」的量级：
# 上下文足够模型看清每个函数，轮次预算也够它把 12 个结论全部说完。
MAX_UNITS_PER_BATCH = 12


@dataclass
class Batch:
    """一批待审对象：文件 → 本批要审的函数。"""

    files: list[str] = field(default_factory=list)
    units: dict[str, list] = field(default_factory=dict)
    index: int = 0
    total: int = 1

    @property
    def unit_count(self) -> int:
        return sum(len(v) for v in self.units.values())

    @property
    def scope(self) -> dict[str, set[str]]:
        """`ReviewSession.scope` 的入参：文件 → 本批负责的函数名集合。"""
        return {f: {u.qualname for u in us} for f, us in self.units.items()}

    def label(self) -> str:
        if self.total <= 1:
            return ""
        return f"第 {self.index + 1}/{self.total} 批"

    def brief(self, plan) -> str:
        """给模型的「本批范围」。逐函数列出，不截断 —— 截断了模型就不知道要审谁。"""
        head = f"{self.label()}｜来源：{plan.origin}" if self.label() else f"来源：{plan.origin}"
        lines = [f"{head}",
                 f"本批只审下列 {len(self.files)} 个文件、{self.unit_count} 个函数："]
        for file in self.files:
            lines.append(f"- {file}")
            units = self.units.get(file) or []
            if not units:
                lines.append("    （整个文件）")
            for u in units:
                lines.append(f"    {u.describe()}")
        lines.append("上面没列出的函数属于未改动代码或其它批次，不在本批范围。")
        lines.append("**本批列出的每个函数都必须有结论**：发现问题用 record_finding；"
                     "判定无问题用 declare_clean(file=..., reason=..., units=[函数名...]) "
                     "把这批里其余函数一次列全 —— 只写 reason 不写 units 等于声明整个文件干净，"
                     "在增量审查里会被当成「把没读的函数也说成干净」。")
        return "\n".join(lines)


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def make_batches(plan, max_units: int = MAX_UNITS_PER_BATCH) -> list[Batch]:
    """把一份计划切成若干批。单批够用时返回长度 1 的列表（调用方不必分情况）。"""
    size = max(1, int(max_units or MAX_UNITS_PER_BATCH))

    # 先按「文件 → 它的函数」排队；没有函数的文件也要进批 ——
    # 模型至少要对它给一个文件级结论，直接丢掉等于漏审。
    groups: list[tuple[str, list]] = []
    for file in plan.files:
        units = list(plan.units.get(file) or [])
        if not units:
            groups.append((file, []))
        else:
            for part in _chunk(units, size):        # 单个文件超大时自己拆
                groups.append((file, part))

    batches: list[Batch] = []
    cur = Batch()
    n = 0
    for file, units in groups:
        add = len(units) or 1                       # 无单位文件按 1 个位置算
        if n and n + add > size:
            batches.append(cur)
            cur, n = Batch(), 0
        cur.files.append(file)
        if units:
            cur.units.setdefault(file, []).extend(units)
        n += add
    if cur.files:
        batches.append(cur)

    for i, b in enumerate(batches):
        b.index, b.total = i, len(batches)
    return batches
