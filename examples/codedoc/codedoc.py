"""codedoc —— 基于 Statecraft 的代码注释生成与审查多 Agent 流水线。

三块能力叠在一起（也是这个示例想证明的事）：

1. **意图路由（四分类）**：同一个入口进来四种活，路由错了整条流水线白跑
     补注释 doc / 审查代码 review / 解释代码 explain / 闲聊 chitchat
     规则打分定路由，规则没命中的灰区交 LLM 兜底（可插槽，异常即降级）

2. **三路混合检索（dense + 词面 + 代码符号图）**：
     符号图由 ast 抽取（函数/参数/异常/调用关系），不是 NLP 猜实体
     价值在关系型查询：「create_order 依赖哪些子函数」——
     答案所在的那条记忆文本里根本没有 create_order，只有图能捞上来

3. **多角色治理流水线**：
     planner（规划者）：读代码 + 符号图摘要，产出补注释计划
     executor（注释写手，角色 commenter）：edit_file 批量写入，每次写入记账 SagaLog
     critic（审查者）：审计划、审产出；打回 → Saga 逆序回滚 → 重规划 → 重写 → 再审

状态机全流程（离线 MockLLM 驱动，展示「坏注释被自动回滚」的完整闭环）：
  Created → Planning → Reviewing → Executing →(REQUEST_REVIEW)→ Reviewing
  →(打回，注释被回滚)→ Planning → Reviewing → Executing → Reviewing
  →(审核通过 DONE)→ PendingConfirm → Done

运行：python examples/codedoc/codedoc.py
"""
import asyncio
import shutil
import uuid
from pathlib import Path

from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.core.tools import AGENT_POLICY, Tool, ToolRegistry
from statecraft.core.triage import IntentRouter
from statecraft.memory.codegraph import CodeGraph, SymbolIndex
from statecraft.memory.embedding import HashEmbedding
from statecraft.memory.hybrid import HybridMemory
from statecraft.providers.base import LLMReply, ToolCall
from statecraft.providers.mock import MockLLM

# ---- RAG 知识库：术语表 + 注释风格规范（检索增强，writer 写注释前先查）----
KNOWLEDGE_BASE = [
    ("注释规范：docstring 必须说明参数语义、副作用与异常行为（如抛出的 ValueError）。",
     {"type": "style", "source": "注释规范 v2"}),
    ("注释规范：禁止笼统表述（如只写'计算折扣'），必须写清触发条件与边界。",
     {"type": "style", "source": "注释规范 v2"}),
    ("术语约定：折扣指会员折扣，level='vip' 打八折；满减指满 1000 减 100。",
     {"type": "terminology", "source": "业务术语表"}),
]

# ---- 意图路由表（带权重：动词权重高，名词权重低 → 「审查注释」判 review 而非 doc）----
ROUTES = {
    "doc": {"补充注释": 3, "补注释": 3, "写注释": 3, "生成注释": 3,
            "docstring": 2, "注释": 1},
    "review": {"审查": 3, "代码审查": 3, "review": 3, "评审": 3,
               "检查": 2, "质量": 1},
    "explain": {"解释": 3, "讲解": 3, "什么意思": 3, "是干什么": 3,
                "干什么": 2, "说明一下": 2, "explain": 3, "理解": 2},
}

# 路由演示样本：三条规则命中、一条灰区（交 LLM）、一条寒暄
ROUTE_SAMPLES = [
    "给 sample.py 的三个函数补充 docstring",
    "审查一下这段代码的注释质量",
    "解释一下 apply_coupon 是干什么的",
    "这段东西帮我看看",
    "在吗？",
]

# 第三路演示：关系型查询（答案不在任何单条记忆的字面里，只在图的边上）
GRAPH_QUERY = "create_order 依赖哪些子函数"

# ---- 角色权限：谁可以读代码、谁可以写注释（命令级白名单）----
AGENT_POLICY["doc_planner"] = {"role": "coordination",
                               "tools": {"report_progress", "read_file"}}
AGENT_POLICY["commenter"] = {"role": "execution",
                             "tools": {"report_progress", "edit_file"}}

# ---- 靶子文件：复制到探针目录的工作副本，源文件永不直接修改 ----
_PROJECT_DIR = Path(__file__).resolve().parent
_SRC = _PROJECT_DIR / "sample.py"
_WORK = Path("E:/Study/探针") / f"codedoc_{uuid.uuid4().hex}.py"

# edit_file 的撤销快照栈：编辑前压入原内容，undo 弹出写回（LIFO）
_FILE_SNAPSHOTS: dict[str, list[str]] = {}

# 坏注释（第一轮，会被 critic 打回）与好注释（第二轮）的替换对
_BAD_EDITS = [
    ("def calc_discount(price, level):",
     'def calc_discount(price, level):\n    """计算折扣。"""\n'),
    ("def send_email(to, subject, body):",
     'def send_email(to, subject, body):\n    """发邮件。"""\n'),
]
_GOOD_EDITS = [
    ("def calc_discount(price, level):",
     'def calc_discount(price, level):\n'
     '    """按会员等级/满减规则计算折后价；level="vip" 打八折，满 1000 减 100。"""\n'),
    ("def send_email(to, subject, body):",
     'def send_email(to, subject, body):\n'
     '    """发送邮件；地址为空或缺少 @ 时抛 ValueError。"""\n'),
]


def build_code_graph() -> CodeGraph:
    """把示例项目建成符号图（多文件建图；真实项目换成遍历仓库即可）。"""
    graph = CodeGraph()
    for name in ("sample.py", "order_flow.py"):
        graph.add_source((_PROJECT_DIR / name).read_text(encoding="utf-8"),
                         module=name)
    return graph


def mock_llm_classifier(text: str, labels: list[str]) -> dict:
    """模拟 LLM 分拣（真实场景换成 chat LLM + JSON 约束输出）。

    demo 里只在规则拿不准时被调用 —— 打印能看到 level=llm 的判定。
    """
    if "看看" in text:
        return {"route": "explain", "confidence": 0.72,
                "reason": "LLM 判断为让看代码/解释类"}
    return {"route": "doc", "confidence": 0.55, "reason": "LLM 兜底判为注释类"}


def make_edit_file_tool() -> Tool:
    def handler(ctx, path, old, new, **_):
        text = Path(path).read_text(encoding="utf-8")
        _FILE_SNAPSHOTS.setdefault(path, []).append(text)   # 快照入栈
        if old not in text:
            return "anchor not found"
        Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"edited {path}（快照已入 Saga 账本）"

    def undo(ctx, result, path, old, new, **_):
        snap = _FILE_SNAPSHOTS.get(path, [])
        if snap:
            Path(path).write_text(snap.pop(), encoding="utf-8")
        return f"reverted {path}"

    return Tool("edit_file", "用锚点文本替换的方式编辑代码文件（带 undo 快照）",
                {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "old": {"type": "string"},
                                "new": {"type": "string"}},
                 "required": ["path", "old", "new"]},
                handler, undo=undo)


def make_read_file_tool() -> Tool:
    return Tool("read_file", "读取代码文件内容",
                {"type": "object", "properties": {"path": {"type": "string"}},
                 "required": ["path"]},
                lambda ctx, path, **_: Path(path).read_text(encoding="utf-8"))


def make_llm():
    """脚本化 LLM：驱动完整叙事（写坏 → 审出 → 回滚 → 重写 → 过审）。"""
    state = {"critic_calls": 0, "exec_round": 1}

    def detect_role(messages):
        for m in messages:
            if m.get("role") == "system":
                return m["content"].split("[role:", 1)[1].split("]", 1)[0]
        return None

    def handler(messages, tools):
        role = detect_role(messages)
        if role == "planner":
            if state["exec_round"] == 1:
                return LLMReply(content="计划：为 3 个缺注释函数补充 docstring"
                                        "（按符号图的调用关系分批，先补被调用的底层函数）。")
            return LLMReply(content="已按审核意见调整：docstring 需说明参数语义与异常行为。")
        if role == "critic":
            state["critic_calls"] += 1
            if state["critic_calls"] == 2:      # 第 2 次：审产出 → 打回坏注释
                state["exec_round"] = 2
                return LLMReply(content="打回：注释过于笼统，未说明异常行为。")
            if state["critic_calls"] >= 4:      # 第 4 次：再审产出 → 通过收尾
                return LLMReply(content="审核通过：注释覆盖参数语义与异常行为。DONE")
            return LLMReply(content="审核通过")
        # executor（注释写手）：每轮逐个写入，写完请求审核
        round_edits = _BAD_EDITS if state["exec_round"] == 1 else _GOOD_EDITS
        done = sum(1 for m in messages if m.get("role") == "tool")
        if done < len(round_edits):
            old, new = round_edits[done]
            return LLMReply(tool_calls=[ToolCall(
                name="edit_file",
                arguments={"path": str(_WORK), "old": old, "new": new})])
        return LLMReply(content="注释已写入，请审查。REQUEST_REVIEW")

    return MockLLM(handler=handler)


def show_hits(label: str, hits) -> None:
    if not hits:
        print(f"  {label}: （无召回）")
        return
    items = []
    for h in hits:
        name = h.text.split("｜", 1)[0]
        d = h.score_details or {}
        items.append(f"{name}[{d.get('lexical', 0):.2f}/{d.get('dense', 0):.2f}"
                     f"/{d.get('graph', 0):.2f}]")
    print(f"  {label}: " + " ".join(items))


async def main():
    _WORK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_SRC, _WORK)

    # ---- ① 代码符号图：ast 建图（零依赖、确定性）----
    graph = build_code_graph()
    print("=" * 72)
    print(f"[codedoc] 代码符号图：{graph.summary()}")
    for node in graph.symbols.values():
        edges = []
        if node.callees:
            edges.append(f"调用 {', '.join(node.callees)}")
        if node.callers:
            edges.append(f"被 {', '.join(node.callers)} 调用")
        if node.raises:
            edges.append(f"抛出 {'/'.join(node.raises)}")
        print(f"    - {node.module}::{node.name}({', '.join(node.args)})"
              f"{'｜' + '；'.join(edges) if edges else ''}"
              f"{'' if node.has_doc else '  ⚠ 缺注释'}")

    # ---- ② 意图路由：规则打分 → 灰区交 LLM → 保守兜底 ----
    router = IntentRouter(ROUTES, default_route="doc",
                          llm_classifier=mock_llm_classifier)
    print("\n[codedoc] 意图路由（规则优先 + LLM 兜底插槽）")
    print("    " + router.describe().replace("\n", "\n    "))
    for text in ROUTE_SAMPLES:
        v = router.classify(text)
        print(f"    - {text!r}\n        → route={v['route']} "
              f"level={v['level']} conf={v['confidence']} "
              f"intent={v['intent']}｜{v['reason']}")

    # ---- ③ 三路检索：知识库 + 符号条目同库，双路 vs 三路对比 ----
    embedder = HashEmbedding(synonyms={
        "注释": ["docstring"], "docstring": ["注释"],
        "异常": ["抛出", "报错"], "折扣": ["会员"],
    })
    entries = list(KNOWLEDGE_BASE) + graph.to_memory_entries()

    mem = HybridMemory(embedder=embedder, graph_index=SymbolIndex(graph))  # 三路
    mem2 = HybridMemory(embedder=embedder)                                 # 双路对照
    for text, meta in entries:
        mem.add(text, scope="shared", metadata=meta)
        mem2.add(text, scope="shared", metadata=meta)

    print(f"\n[codedoc] 三路检索演示（dense/词面/图）—— query: {GRAPH_QUERY!r}")
    show_hits("双路 top5", mem2.search(GRAPH_QUERY, limit=5, explain=True))
    show_hits("三路 top5", mem.search(GRAPH_QUERY, limit=5, explain=True))
    print("    说明：符号条目的文本只写「我是谁」（签名/docstring/异常），"
          "调用关系只在图的边上。\n"
          "          所以双路只能靠 dense 噪声碰运气（apply_coupon/send_receipt "
          "直接掉出前 5）；\n"
          "          图用 caller/callee 边把它们确定性顶到 2–4 位（graph=0.50）。")

    # ---- ④ RAG：写注释前检索规范（元数据前置过滤，只让规范类条目参与打分）----
    rag_hits = mem.search("docstring 注释规范 术语约定", limit=3, explain=True,
                          where={"type": ["style", "terminology"]})
    knowledge_text = "\n".join(f"- {h.text}" for h in rag_hits)

    registry = ToolRegistry()
    for t in (make_edit_file_tool(), make_read_file_tool()):
        registry.register(t)
    llm = make_llm()
    agents = {
        "planner": Agent("planner", llm, role="doc_planner", registry=registry,
                         system_prompt="你是注释规划者：读代码 + 符号图摘要，"
                                       "规划补注释的顺序与重点。"),
        "critic": Agent("critic", llm, role="review", registry=registry,
                        system_prompt="你是注释审查者：审计划的完整性，审产出注释的准确性。"),
        "executor": Agent("executor", llm, role="commenter", registry=registry,
                          system_prompt="你是注释写手：按计划用 edit_file 写 docstring，"
                                        "写完输出 REQUEST_REVIEW 等待审查。"),
    }

    # 分拣器注入编排器：同一套路由逻辑同时承担「分拣直通」判断
    orch = Orchestrator(agents, mem,
                        AgentCoreConfig(auto_confirm=True, triage_classifier=router))

    # ---- ⑤ 按路由结果分支：只有 doc 才进注释流水线 ----
    user_request = "给 sample.py 的三个函数补充 docstring。"
    verdict = router.classify(user_request)
    print(f"\n[codedoc] 用户请求：{user_request!r} → route={verdict['route']}")
    if verdict["route"] != "doc":
        print("[codedoc] 非注释类请求，跳过注释流水线（真实场景转交对应 Agent）")
        return

    missing = graph.missing_doc(module="sample.py")
    plan_hint = ("；".join(f"{n.name}({', '.join(n.args)})" for n in missing))
    graph_text = f"本批次待补函数：{plan_hint}"

    before = _WORK.read_text(encoding="utf-8")
    print("[codedoc] 多 Agent 注释流水线启动，靶子文件：", _WORK.name)
    print("[codedoc] 注入 writer 的规范条目（RAG）:")
    for h in rag_hits:
        d = h.score_details or {}
        print(f"    - [{d.get('lexical', 0):.2f}/{d.get('dense', 0):.2f}] "
              f"{h.text[:48]}...")

    task = orch.create_task(
        f"为代码文件 {_WORK} 补充 docstring。\n"
        f"[符号图摘要] {graph.summary()}；{graph_text}\n"
        f"[项目注释规范（必须遵循）]\n{knowledge_text}"
    )
    result = await orch.run(task)

    after = _WORK.read_text(encoding="utf-8")
    print("\n[codedoc] 最终状态:", result.state)
    print("[codedoc] 流转轨迹:",
          " → ".join(dict.fromkeys(f["to"] for f in result.flow_log)))
    print("[codedoc] Saga 补偿审计（打回时坏注释被自动回滚）:")
    for rec in orch.saga.history:
        print(f"    - {rec['tool']} status={rec['status']} reason={rec['reason']}")
    print("[codedoc] 好注释已生效:", '"""按会员等级' in after)
    print("[codedoc] 坏注释已消失:", '"""计算折扣。"""' not in after)
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
