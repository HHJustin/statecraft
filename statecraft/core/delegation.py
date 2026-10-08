"""委派 + 委派防死锁（源自 Edict cmd_delegate，自建补充）。

委派 = 父任务把一块工作交给子任务（子任务有独立状态机，可再委派）。
防死锁两道闸（与路线图决策八一致）：
1. 深度限制：委派链深度超过 max_delegation_depth → DelegationDenied
2. 成环检测：把任务委派给它的祖先（或自己）→ DelegationDenied

Tracker 只管「关系账本」，不管执行 —— 创建子任务/关联已有任务都先过这两道闸。
"""


class DelegationDenied(Exception):
    """委派被拒绝：超过深度上限或构成环。"""


class DelegationTracker:
    """记录 parent -> child 委派边，提供深度/成环校验。"""

    def __init__(self, max_depth: int = 3):
        self.max_depth = max_depth
        self.parent_of: dict[str, str] = {}          # child -> parent
        self.children: dict[str, set[str]] = {}      # parent -> {children}
        self.edges: list[tuple[str, str]] = []       # 全部委派边（按序，可观测）

    # ---- 查询 ----
    def ancestors(self, task_id: str) -> list[str]:
        """从直接父级到根的祖先链。"""
        out, cur = [], self.parent_of.get(task_id)
        while cur is not None:
            out.append(cur)
            cur = self.parent_of.get(cur)
        return out

    def depth(self, task_id: str) -> int:
        """委派深度：根任务为 0，每委派一层 +1。"""
        return len(self.ancestors(task_id))

    def chain(self, task_id: str) -> list[str]:
        """完整委派链（根 → ... → 自身），排查用。"""
        return list(reversed(self.ancestors(task_id))) + [task_id]

    def subtree(self, task_id: str) -> set[str]:
        """以 task_id 为根的全部后代（含自身）。"""
        out = {task_id}
        frontier = [task_id]
        while frontier:
            cur = frontier.pop()
            for c in self.children.get(cur, ()):
                if c not in out:
                    out.add(c)
                    frontier.append(c)
        return out

    # ---- 校验 + 记账（唯一入口，两道闸都在这里）----
    def register(self, parent_id: str, child_id: str) -> None:
        """登记一条委派边；深度超限或成环直接拒绝。

        - 新建子任务：child_id 是新 id，天然不成环，走深度闸
        - 委派已有任务：child_id 已存在于关系账本，两道闸都生效
        """
        if child_id == parent_id or child_id in self.ancestors(parent_id):
            chain_str = " → ".join(self.chain(parent_id))
            raise DelegationDenied(
                f"委派成环：{parent_id} -> {child_id}（链路 {chain_str}）")
        if self.depth(parent_id) + 1 > self.max_depth:
            raise DelegationDenied(
                f"超过最大委派深度 {self.max_depth}：{parent_id} -> {child_id} "
                f"（当前深度 {self.depth(parent_id)}）")
        self.parent_of[child_id] = parent_id
        self.children.setdefault(parent_id, set()).add(child_id)
        self.edges.append((parent_id, child_id))
