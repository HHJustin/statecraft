"""一键跑分（离线，秒级）：python -m statecraft.eval.runner

设计要点（呼应路线图第 6 节）：
- 黄金集是「标准答案」，reviewer / 检索器可插拔 —— 离线用规则/Mock 回归框架本身，
  接入真实 LLM 后同一套黄金集直接量化真实效果
- 确定性校验优先：L1/L2 全部可代码断言，不依赖 LLM-as-judge
"""
import asyncio

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.mock import MockLLM

from .golden import GoldenL1, GoldenL2, GoldenL3, load_l1, load_l2, load_l3
from .metrics import confusion, hit_at_k, mrr, recall_at_k

# ---- 可插拔的审核器：离线默认规则兜底，接真实 LLM 后替换即可 ----
_REJECT_MARKS = ("缺少数据来源", "未验证", "风险未评估", "无法追溯")


def rule_reviewer(text: str) -> str:
    return "reject" if any(m in text for m in _REJECT_MARKS) else "approve"


# ---- L1 检索质量 ----
def run_l1(cases: list[GoldenL1] | None = None, k: int = 5) -> dict:
    cases = cases if cases is not None else load_l1()
    recalls, mrrs, hits = [], [], []
    for c in cases:
        mem = InMemoryMemory()
        for t in c.corpus:
            mem.add(t)
        ranked = [h.text for h in mem.search(c.query, limit=k)]
        rel = {c.corpus[i] for i in c.relevant}
        recalls.append(recall_at_k(ranked, rel, k))
        mrrs.append(mrr(ranked, rel))
        hits.append(hit_at_k(ranked, rel, k))
    n = len(cases) or 1
    return {"cases": len(cases), "recall_at_k": sum(recalls) / n,
            "mrr": sum(mrrs) / n, "hit_at_k": sum(hits) / n}


# ---- L2 审核质量 ----
def run_l2(cases: list[GoldenL2] | None = None,
           reviewer=rule_reviewer) -> dict:
    cases = cases if cases is not None else load_l2()
    y_true = [c.expected for c in cases]
    y_pred = [reviewer(c.text) for c in cases]
    return {"cases": len(cases), **confusion(y_true, y_pred, positive="reject")}


# ---- L3 端到端 ----
def run_l3(cases: list[GoldenL3] | None = None, config=None) -> dict:
    cases = cases if cases is not None else load_l3()
    config = config or AgentCoreConfig(
        retry_backoff=[0.0], max_retries=2,
        state_timeouts={"Planning": 5.0, "Reviewing": 5.0, "Executing": 5.0})
    achieved, visits_ok, rounds = 0, 0, []
    for c in cases:
        llm = MockLLM(script=c.script)
        agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
        orch = Orchestrator(agents, InMemoryMemory(), config)
        task = asyncio.run(orch.run(orch.create_task(c.description)))
        visited = {f["to"] for f in task.flow_log} | {task.state}
        if task.state == c.expected_state:
            achieved += 1
        if all(m in visited for m in c.must_visit):
            visits_ok += 1
        rounds.append(len(task.flow_log))
    n = len(cases) or 1
    return {"cases": len(cases), "completion_rate": achieved / n,
            "must_visit_rate": visits_ok / n,
            "avg_transitions": sum(rounds) / n}


def main() -> None:
    l1 = run_l1()
    l2 = run_l2()
    l3 = run_l3()
    print("=" * 62)
    print("L1 检索质量  Recall@5=%.3f  MRR=%.3f  Hit@5=%.3f  (%d cases)"
          % (l1["recall_at_k"], l1["mrr"], l1["hit_at_k"], l1["cases"]))
    print("L2 审核质量  P=%.3f  R=%.3f  F1=%.3f  误杀率=%.3f  漏放率=%.3f  (%d cases)"
          % (l2["precision"], l2["recall"], l2["f1"],
             l2["false_kill_rate"], l2["false_pass_rate"], l2["cases"]))
    print("L3 端到端    完成率=%.3f  必经状态覆盖=%.3f  平均流转=%.1f  (%d cases)"
          % (l3["completion_rate"], l3["must_visit_rate"],
             l3["avg_transitions"], l3["cases"]))
    print("=" * 62)


if __name__ == "__main__":
    main()
