"""打包回归 —— 「装出来的包」与「源码树里跑」是不是同一个东西。

这些用例不测业务逻辑，只测**资源有没有跟着包走**。它们能存在的理由：
`pip install .` 装出来的包缺文件时，程序不会有任何异常 ——
`load_prompt()` 静默降级成「你是 executor。」，`eval` 才在加载黄金集时崩，
而单元测试全绿。所以必须有一条用例专门盯着文件清单。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from statecraft.core.runner import (PromptResourceMissing,  # noqa: E402
                                    load_prompt)

PKG = ROOT / "statecraft"


class TestPromptResources(unittest.TestCase):
    def test_agents_dir_has_every_prompt(self):
        agents = PKG / "agents"
        self.assertTrue(agents.is_dir(), "包内没有 agents/ —— wheel 里也不会有")
        names = {p.name for p in agents.glob("*.md")}
        for need in ("GLOBAL.md", "planner.md", "critic.md", "executor.md"):
            self.assertIn(need, names, f"缺 {need}：多 agent 的约束会静默消失")

    def test_prompt_is_not_the_fallback(self):
        """GLOBAL + 角色两层都要拼进去，且远长于兜底文案。"""
        for name in ("planner", "critic", "executor"):
            text = load_prompt(name)
            self.assertNotEqual(text, f"你是 {name}。")
            self.assertGreater(len(text), 100, f"{name} 的 prompt 太短，像是没读到文件")
            self.assertIn("全局规范", text)          # GLOBAL.md 的第一句

    def test_global_is_not_duplicated(self):
        """name="GLOBAL" 时 r 与 g 是同一个文件 —— 不能读两遍。"""
        from pathlib import Path

        one = (PKG / "agents" / "GLOBAL.md").read_text(encoding="utf-8")
        self.assertEqual(load_prompt("GLOBAL"), one)
        self.assertEqual(len(load_prompt("executor")),
                         len(one) + 2 + len((PKG / "agents" / "executor.md")
                                            .read_text(encoding="utf-8")))

    def test_missing_resource_raises_instead_of_guessing(self):
        """包内缺文件必须炸，不能悄悄降级。"""
        empty = Path(tempfile.mkdtemp(prefix="sc_prompt_"))
        self.addCleanup(shutil.rmtree, empty, True)
        # 传 prompts_dir 是显式指定（测试/自定义场景），仍允许兜底
        self.assertEqual(load_prompt("executor", prompts_dir=empty), "你是 executor。")

        import statecraft.core.runner as R
        old = R._PROMPTS_DIR
        R._PROMPTS_DIR = empty
        try:
            with self.assertRaises(PromptResourceMissing) as ctx:
                load_prompt("executor")
            self.assertIn("package-data", str(ctx.exception))
        finally:
            R._PROMPTS_DIR = old


class TestDataResources(unittest.TestCase):
    def test_schema_sql_exists(self):
        schema = PKG / "storage" / "schema.sql"
        self.assertTrue(schema.is_file())
        self.assertIn("CREATE TABLE", schema.read_text(encoding="utf-8").upper())

    def test_schema_can_build_a_database(self):
        """不只检查文件在，而是真的拿它建一个库。"""
        from statecraft.storage import ReviewStore

        tmp = Path(tempfile.mkdtemp(prefix="sc_schema_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = ReviewStore(str(tmp / "a.db"))
        self.addCleanup(store.close)
        rid = store.start_review(repo_root=str(tmp), scope=["x.py"])
        self.assertTrue(rid)

    def test_golden_sets_load(self):
        from statecraft.eval import golden

        self.assertGreater(len(golden.load_l1()), 0)
        self.assertGreater(len(golden.load_l2()), 0)
        self.assertGreater(len(golden.load_l3()), 0)


class TestPyprojectDeclaresResources(unittest.TestCase):
    """资源在源码树里存在还不够 —— 必须写进 package-data，否则 wheel 里没有。"""

    def setUp(self):
        self.cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_package_data_covers_agents_schema_golden(self):
        data = self.cfg["tool"]["setuptools"]["package-data"]
        self.assertIn("agents/*.md", data.get("statecraft", []))
        self.assertIn("schema.sql", data.get("statecraft.storage", []))
        self.assertIn("golden/*.jsonl", data.get("statecraft.eval", []))

    def test_console_script_points_at_real_entry(self):
        script = self.cfg["project"]["scripts"]["statecraft"]
        mod, _, fn = script.partition(":")
        mod = __import__(mod, fromlist=[fn])
        self.assertTrue(callable(getattr(mod, fn)))

    def test_license_is_spdx_string(self):
        """`license = {text = "MIT"}` 在 setuptools>=77 已弃用，2027 年后直接构建失败。"""
        self.assertIsInstance(self.cfg["project"]["license"], str)

    def test_src_layout_has_init_everywhere(self):
        for p in PKG.rglob("*"):
            if p.is_dir() and "__pycache__" not in p.parts:
                py = list(p.glob("*.py"))
                if py:
                    self.assertTrue((p / "__init__.py").is_file(),
                                    f"{p} 有 .py 但没有 __init__.py，不会被打包")


if __name__ == "__main__":
    unittest.main()
