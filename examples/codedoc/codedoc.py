"""codedoc —— 基于 Statecraft 的代码注释生成与审查多 Agent 流水线。

三个角色的分工（这就是"多 Agent 框架怎么包装成垂直应用"的答案）：
  planner（规划者）：读代码，产出"哪些函数要补 docstring"的计划
  executor（注释写手，角色 commenter）：调用 edit_file 工具批量写入，
      每次写入自动记账 SagaLog（undo = 恢复文件快照）
  critic（审查者）：审计划、审产出；打回 → Saga 逆序回滚已写入的注释
      → 回 Planning 重新规划 → 重写 → 再审

状态机全流程（离线 MockLLM 驱动，展示"坏注释被自动回滚"的完整闭环）：
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
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.base import LLMReply, ToolCall
from statecraft.providers.mock import MockLLM

# ---- 角色权限：谁可以读代码、谁可以写注释（命令级白名单）----
AGENT_POLICY["doc_planner"] = {"role": "coordination",
                               "tools": {"report_progress", "read_file"}}
AGENT_POLICY["commenter"] = {"role": "execution",
                             "tools": {"report_progress", "edit_file"}}

# ---- 靶子文件：复制到探针目录的工作副本，源文件永不直接修改 ----
_SRC = Path(__file__).resolve().parent / "sample.py"
_WORK = Path("E:/Study/探针") / f"codedoc_{uuid.uuid4().hex}.py"

# edit_file 的撤销快照栈：编辑前压入原内容，undo 弹出写回（LIFO）
# （生产实现会把快照编码进工具结果，这里用内存栈简化）
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
            plan2 = "已按审核意见调整：docstring 需说明参数语义与异常行为。"
            return LLMReply(content="计划：为 3 个函数补充 docstring。"
                            if state["exec_round"] == 1 else plan2)
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


async def main():
    _WORK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_SRC, _WORK)

    registry = ToolRegistry()
    for t in (make_edit_file_tool(), make_read_file_tool()):
        registry.register(t)
    llm = make_llm()
    agents = {
        "planner": Agent("planner", llm, role="doc_planner", registry=registry,
                         system_prompt="你是注释规划者：读代码，规划补注释的顺序与重点。"),
        "critic": Agent("critic", llm, role="review", registry=registry,
                        system_prompt="你是注释审查者：审计划的完整性，审产出注释的准确性。"),
        "executor": Agent("executor", llm, role="commenter", registry=registry,
                          system_prompt="你是注释写手：按计划用 edit_file 写 docstring，"
                                        "写完输出 REQUEST_REVIEW 等待审查。"),
    }
    orch = Orchestrator(agents, InMemoryMemory(),
                        AgentCoreConfig(auto_confirm=True))

    before = _WORK.read_text(encoding="utf-8")
    print("=" * 60)
    print("[codedoc] 多 Agent 注释流水线启动，靶子文件：", _WORK.name)
    task = orch.create_task(f"为代码文件 {_WORK} 补充 docstring")
    result = await orch.run(task)

    after = _WORK.read_text(encoding="utf-8")
    print("[codedoc] 最终状态:", result.state)
    print("[codedoc] 流转轨迹:",
          " → ".join(dict.fromkeys(f["to"] for f in result.flow_log)))
    print("[codedoc] Saga 补偿审计（打回时坏注释被自动回滚）:")
    for rec in orch.saga.history:
        print(f"    - {rec['tool']} status={rec['status']} reason={rec['reason']}")
    print("[codedoc] 好注释已生效:", '"""按会员等级' in after)
    print("[codedoc] 坏注释已消失:", '"""计算折扣。"""' not in after)
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
