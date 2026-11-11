"""codedoc 示例的离线测试：多 Agent 注释流水线的完整闭环。

覆盖：执行后审核（REQUEST_REVIEW）、打回触发 Saga 回滚注释、
重写后过审、最终文件内容正确。
"""
import asyncio
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples" / "codedoc"))

from codedoc import _GOOD_EDITS, _BAD_EDITS, make_llm, _WORK, _SRC  # noqa: E402

from statecraft.config import AgentCoreConfig  # noqa: E402
from statecraft.core.orchestrator import Orchestrator  # noqa: E402
from statecraft.core.runner import Agent  # noqa: E402
from statecraft.core.tools import (AGENT_POLICY, ToolRegistry)  # noqa: E402
from statecraft.memory.local import InMemoryMemory  # noqa: E402

import codedoc  # noqa: E402


class TestCodeDocPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_full_loop_with_reject_and_compensation(self):
        # 每个测试用独立工作副本，避免 demo 的模块级状态串扰
        work = Path("E:/Study/探针") / f"codedoc_test_{uuid.uuid4().hex}.py"
        shutil.copy(_SRC, work)
        # 把 demo 的编辑锚点改指向测试副本
        bad = [(old.replace(str(_WORK), str(work)), new) for old, new in _BAD_EDITS]
        good = [(old.replace(str(_WORK), str(work)), new) for old, new in _GOOD_EDITS]
        saved = (codedoc._BAD_EDITS, codedoc._GOOD_EDITS, codedoc._WORK)
        codedoc._BAD_EDITS, codedoc._GOOD_EDITS, codedoc._WORK = bad, good, work

        try:
            # 重新生成 handler（引用模块级 _BAD_EDITS/_GOOD_EDITS/_WORK）
            llm = make_llm()
            registry = ToolRegistry()
            registry.register(codedoc.make_edit_file_tool())
            agents = {
                "planner": Agent("planner", llm, role="doc_planner",
                                 registry=registry, system_prompt="planner"),
                "critic": Agent("critic", llm, role="review",
                                registry=registry, system_prompt="critic"),
                "executor": Agent("executor", llm, role="commenter",
                                  registry=registry, system_prompt="writer"),
            }
            orch = Orchestrator(agents, InMemoryMemory(),
                                AgentCoreConfig(auto_confirm=True))
            task = orch.create_task(f"为代码文件 {work} 补充 docstring")
            result = await orch.run(task)

            # 1. 最终完成
            self.assertEqual(result.state, "Done")
            # 2. 走过「执行后审核 → 打回 → 重规划 → 重写 → 再审」闭环：
            #    Reviewing 出现 4 次（审计划×2 + 审产出×2），且发生过 Reviewing→Planning 打回
            states = [f["to"] for f in result.flow_log]
            self.assertEqual(states.count("Reviewing"), 4)
            self.assertIn(("Reviewing", "Planning"),
                          [(f["from"], f["to"]) for f in result.flow_log])
            # 审核通过后走高风险确认路径收尾（Reviewing → PendingConfirm → Done）
            self.assertIn(("Reviewing", "PendingConfirm"),
                          [(f["from"], f["to"]) for f in result.flow_log])
            # 3. Saga：坏注释在打回时被回滚（2 条补偿，成功）
            rejected = [r for r in orch.saga.history
                        if r["reason"] == "review_rejected"]
            self.assertEqual(len(rejected), 2)
            self.assertTrue(all(r["status"] == "ok" for r in rejected))
            # 4. 文件终态：好注释在、坏注释不在、锚点行完好
            final = work.read_text(encoding="utf-8")
            for _, new in good:
                self.assertIn(new.strip(), final)
            for _, new in bad:
                self.assertNotIn(new.strip(), final)
            self.assertIn("def parse_config(raw):", final)  # 未处理的函数原样保留
        finally:
            codedoc._BAD_EDITS, codedoc._GOOD_EDITS, codedoc._WORK = saved


if __name__ == "__main__":
    unittest.main()
