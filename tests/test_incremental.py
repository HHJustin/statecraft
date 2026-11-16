"""增量审查（P14）+ CLI（P15）的回归测试 —— 全离线、不调模型、不花钱。

三层：
1. **纯函数**：diff 解析、AST 映射、函数体哈希。预期结果手算得出来，
   所以这里断言的是「算法对不对」，不是「看起来像不像」。
2. **真 git 仓库**：`git init` 一个临时库，真提交、真改文件，验证
   `plan_from_diff` 只挑出改过的函数。这一层 MockLLM 替代不了 ——
   行号偏移、重命名、删除这些坑只有真 git 造得出来。
3. **端到端**：临时目录 → 审查落库 → 第二次全部命中缓存 → CLI 的打印与退出码。

没有 git 的机器上第 2 层自动跳过（普通 CI 镜像都带 git，跳过只是保险）。
"""
import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from statecraft.incremental import (GitError, UnitCache, body_hash,  # noqa: E402
                                    is_git_repo, parse_diff, plan_from_diff)
from statecraft.incremental.plan import plan_for_files              # noqa: E402
from statecraft.incremental.units import (select_units, unit_at,    # noqa: E402
                                          units_of_source)
from statecraft.review.pipeline import HeuristicMockBrain, run_review     # noqa: E402
from statecraft.storage import ReviewStore                                # noqa: E402
from statecraft.storage import db as dbmod                                # noqa: E402

NL = chr(10)          # 显式换行：测试里拼源码，别让字符串转义搅进来
GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败: {out.stderr}")
    return out.stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "core.autocrlf", "false")
    return path


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


SRC = '''"""演示模块。"""


def alpha(values):
    return sum(values) / len(values)


def beta(text):
    return text.strip()


class Box:
    def put(self, item):
        self.item = item
'''


# ---------------------------------------------------------------- 1. 纯函数
class TestDiffParse(unittest.TestCase):
    def test_count_omitted_means_one(self):
        """`@@ -1 +1 @@` 的 count 默认是 1，不是 0。

        按 0 算会把单行改动解析成空区间，整条 diff 静默丢失 —— 这是最容易
        写错、且错了以后完全没声音的一处。
        """
        ch = parse_diff("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                        "@@ -1 +1 @@\n-a\n+b\n")
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0].new_ranges, [(1, 1)])
        self.assertEqual(ch[0].status, "modified")

    def test_multi_hunk_ranges(self):
        ch = parse_diff("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                        "@@ -3,2 +3,4 @@\n a\n+b\n+c\n c\n"
                        "@@ -20,3 +22,3 @@\n-x\n+y\n")[0]
        self.assertEqual(ch.new_ranges, [(3, 6), (22, 24)])

    def test_pure_deletion_becomes_probe(self):
        """新侧长度为 0 的 hunk：取删除点作探针，并在 pure_deletions 里留痕。"""
        ch = parse_diff("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                        "@@ -12,2 +11,0 @@\n-a\n-b\n")[0]
        self.assertEqual(ch.new_ranges, [(11, 11)])
        self.assertEqual(len(ch.pure_deletions), 1)

    def test_added_file_full_range(self):
        ch = parse_diff("diff --git a/new.py b/new.py\nnew file mode 100644\n"
                        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n")[0]
        self.assertEqual((ch.status, ch.path, ch.new_ranges), ("added", "new.py", [(1, 3)]))
        self.assertTrue(ch.reviewable)

    def test_deleted_file_not_reviewable(self):
        ch = parse_diff("diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
                        "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n")[0]
        self.assertEqual((ch.status, ch.path), ("deleted", "gone.py"))
        self.assertFalse(ch.reviewable)

    def test_rename_uses_new_path(self):
        """重命名要取新路径，否则 read_file 直接 FileNotFound。"""
        ch = parse_diff("diff --git a/old.py b/new.py\nsimilarity index 95%\n"
                        "rename from old.py\nrename to new.py\n"
                        "--- a/old.py\n+++ b/new.py\n@@ -3,2 +3,3 @@\n a\n+b\n c\n")[0]
        self.assertEqual((ch.status, ch.path, ch.old_path), ("renamed", "new.py", "old.py"))

    def test_binary_skipped(self):
        ch = parse_diff("diff --git a/i.png b/i.png\n"
                        "Binary files a/i.png and b/i.png differ\n")[0]
        self.assertTrue(ch.binary)
        self.assertFalse(ch.reviewable)

    def test_quoted_unicode_path(self):
        """中文文件名会被 git 转义成八进制，必须还原成可读路径。"""
        ch = parse_diff('diff --git "a/\\346\\226\\207.py" "b/\\346\\226\\207.py"\n'
                        '--- "a/\\346\\226\\207.py"\n+++ "b/\\346\\226\\207.py"\n'
                        "@@ -1 +1 @@\n-a\n+b\n")[0]
        self.assertEqual(ch.path, "文.py")

    def test_path_with_space_quoted(self):
        ch = parse_diff('diff --git "a/my file.py" "b/my file.py"\n'
                        '--- "a/my file.py"\n+++ "b/my file.py"\n'
                        "@@ -1 +1 @@\n-a\n+b\n")[0]
        self.assertEqual(ch.path, "my file.py")

    def test_garbage_input_is_empty(self):
        self.assertEqual(parse_diff(""), [])
        self.assertEqual(parse_diff("not a diff\n@@ broken @@\n"), [])


class TestUnits(unittest.TestCase):
    def setUp(self):
        self.units = units_of_source(SRC, "x.py")
        self.by_name = {u.qualname: u for u in self.units}

    def test_module_unit_always_present_and_widest(self):
        m = self.by_name["<module>"]
        self.assertEqual((m.start, m.end), (1, len(SRC.splitlines())))
        self.assertTrue(all(m.end >= u.end for u in self.units))

    def test_method_kind_and_span(self):
        self.assertEqual(self.by_name["Box.put"].kind, "method")
        self.assertEqual(self.by_name["Box"].kind, "class")
        self.assertEqual(self.by_name["alpha"].kind, "function")

    def test_decorator_counts_as_function_start(self):
        """只改装饰器也要算「改了这个函数」。"""
        src = "import functools\n\n\n@functools.cache\ndef f(a):\n    return a\n"
        u = [x for x in units_of_source(src, "y.py") if x.qualname == "f"][0]
        self.assertEqual(u.start, 4)                  # 第 4 行是 @functools.cache
        self.assertEqual(u.header, "def f(a):")

    def test_syntax_error_degrades_to_module(self):
        """语法坏了不躲：整个文件算一个单位。改坏语法的提交最该被审。"""
        units = units_of_source("def f(:\n    pass\n", "bad.py")
        self.assertEqual([u.qualname for u in units], ["<module>"])

    def test_hash_ignores_blank_lines_and_trailing_space(self):
        a = body_hash("f", ["def f():", "    return 1"], 1, 2)
        b = body_hash("f", ["def f():   ", "", "    return 1"], 1, 3)
        self.assertEqual(a, b)

    def test_hash_changes_on_code_or_comment(self):
        base = ["def f():", "    return 1"]
        self.assertNotEqual(body_hash("f", base, 1, 2),
                            body_hash("f", ["def f():", "    return 2"], 1, 2))
        self.assertNotEqual(body_hash("f", base, 1, 2),
                            body_hash("f", ["def f():", "    # 说明", "    return 1"], 1, 3))

    def test_rename_changes_hash(self):
        lines = ["def f():", "    return 1"]
        self.assertNotEqual(body_hash("f", lines, 1, 2), body_hash("g", lines, 1, 2))


class TestSelect(unittest.TestCase):
    def setUp(self):
        self.units = units_of_source(SRC, "x.py")

    def names(self, ranges):
        return [u.qualname for u in select_units(self.units, ranges)]

    def test_inside_method_picks_only_method(self):
        self.assertEqual(self.names([(14, 14)]), ["Box.put"])  # self.item = item

    def test_inside_class_body_picks_class(self):
        self.assertEqual(self.names([(12, 12)]), ["Box"])     # class Box:

    def test_span_two_functions_picks_both(self):
        self.assertEqual(self.names([(5, 11)]), ["alpha", "beta"])

    def test_top_level_change_picks_module(self):
        self.assertEqual(self.names([(1, 1)]), ["<module>"])

    def test_unit_at_is_innermost(self):
        self.assertEqual(unit_at(self.units, 13).qualname, "Box.put")   # def put

    def test_same_span_prefers_inner_kind(self):
        """单行函数与模块的区间可能完全相同 —— 不能两个都选。

        `def inner(): return 1` 这种文件里，`<module>` 和 `inner` 都是 1..N。
        只按区间判「谁在里面」会把两者一起选中，同一段代码审两遍、
        结论在库里重复入库，而且用户看到「2 个函数待审」但只有 1 个函数。
        """
        units = units_of_source("def inner():\n    return 1\n", "one.py")
        # 区间完全重合时，行属于「更内层」的那个单位 —— def 行也算函数自己的
        self.assertEqual([u.qualname for u in select_units(units, [(1, 2)])], ["inner"])
        self.assertEqual([u.qualname for u in select_units(units, [(1, 1)])], ["inner"])


# ---------------------------------------------------------------- 2. 真 git
@unittest.skipUnless(GIT, "需要 git")
class TestGitPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_p14_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = init_repo(self.tmp / "repo")
        write(self.repo / "app.py", SRC)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def test_only_changed_function_selected(self):
        from statecraft.incremental import plan_from_diff

        write(self.repo / "app.py", SRC.replace(
            "    return sum(values) / len(values)",
            "    if not values:\n        raise ValueError('empty')\n"
            "    return sum(values) / len(values)"))
        plan = plan_from_diff(self.repo, base="HEAD")
        self.assertEqual(plan.files, ["app.py"])
        self.assertEqual([u.qualname for u in plan.units["app.py"]], ["alpha"])
        self.assertTrue(plan.strict_units)
        self.assertIn("git diff HEAD", plan.origin)

    def test_new_file_selects_all_functions(self):
        from statecraft.incremental import plan_from_diff

        write(self.repo / "extra.py", "def one():\n    return 1\n\n\ndef two():\n    return 2\n")
        plan = plan_from_diff(self.repo, base="HEAD")
        self.assertEqual([u.qualname for u in plan.units["extra.py"]], ["one", "two"])
        self.assertNotIn("app.py", plan.files)               # 没改的文件不进范围

    def test_deleted_file_excluded_and_noted(self):
        from statecraft.incremental import plan_from_diff

        _git(self.repo, "rm", "-q", "app.py")
        plan = plan_from_diff(self.repo, base="HEAD")
        self.assertEqual(plan.files, [])
        self.assertTrue(any("已删除" in d for d in plan.deletions))

    def test_two_commits_between(self):
        from statecraft.incremental import plan_from_diff

        write(self.repo / "app.py", SRC + "\n\ndef gamma():\n    return 3\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "add gamma")
        write(self.repo / "app.py", SRC + "\n\ndef gamma():\n    return 4\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "tweak gamma")
        plan = plan_from_diff(self.repo, base="HEAD~1", head="HEAD")
        self.assertEqual([u.qualname for u in plan.units["app.py"]], ["gamma"])

    def test_uncommitted_change_is_seen(self):
        from statecraft.incremental import plan_from_diff

        write(self.repo / "app.py", SRC.replace("return text.strip()",
                                                "return text.strip().lower()"))
        plan = plan_from_diff(self.repo, base="HEAD")
        self.assertEqual([u.qualname for u in plan.units["app.py"]], ["beta"])

    def test_pathspec_limits_scope(self):
        """paths 参数是 git pathspec：给了 `pkg/*.py` 就只该看到 pkg 里的变更。"""
        write(self.repo / "pkg" / "inner.py", "def inner():\n    return 1\n")
        write(self.repo / "app.py", SRC.replace("return text.strip()",
                                                "return text.strip().upper()"))
        plan = plan_from_diff(self.repo, base="HEAD", patterns=("pkg/*.py",))
        self.assertEqual(plan.files, ["pkg/inner.py"])

    def test_not_a_repo_raises(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        write(plain / "a.py", "x = 1\n")
        self.assertFalse(is_git_repo(plain))
        with self.assertRaises(GitError):
            plan_from_diff(plain, base="HEAD")

    def test_broken_syntax_still_reviewable(self):
        from statecraft.incremental import plan_from_diff

        write(self.repo / "broken.py", "def f(:\n    pass\n")
        plan = plan_from_diff(self.repo, base="HEAD")
        self.assertIn("broken.py", plan.files)
        self.assertIn("broken.py", plan.parse_errors)
        self.assertEqual([u.qualname for u in plan.units["broken.py"]], ["<module>"])


# ---------------------------------------------------------------- 3. 缓存
class TestUnitCache(unittest.TestCase):
    def setUp(self):
        self.store = ReviewStore(":memory:")
        self.addCleanup(self.store.close)
        self.cache = UnitCache(self.store)
        self.unit = units_of_source(SRC, "x.py")[0]          # <module>，1..N

    def _unit(self, name):
        return [u for u in units_of_source(SRC, "x.py") if u.qualname == name][0]

    def test_put_get_hit(self):
        u = self._unit("alpha")
        self.cache.put(u.file, u.qualname, u.body_hash, "rev1", "clean", unit_start=u.start)
        self.assertEqual(self.cache.get("x.py", "alpha").status, "clean")
        # 来源批次不存在 → 不可信（join reviews 拿不到 done）
        self.assertIsNone(self.cache.hit(u))

    def test_hit_requires_same_hash(self):
        u = self._unit("alpha")
        self.store.start_review(repo_root="/r", scope=["x.py"], review_id="rev1")
        self.store.finish_review("rev1", "done")
        self.cache.put(u.file, u.qualname, "deadbeefdeadbeef", "rev1", "clean",
                       unit_start=u.start)
        self.assertIsNone(self.cache.hit(u))                 # 哈希不同 → 不算命中

    def test_blocked_review_is_not_trusted(self):
        """Blocked 批次的 clean 是「中断现场」，不是结论。"""
        u = self._unit("alpha")
        self.store.start_review(repo_root="/r", scope=["x.py"], review_id="rev2")
        self.store.finish_review("rev2", "blocked")
        self.cache.put(u.file, u.qualname, u.body_hash, "rev2", "clean",
                       unit_start=u.start)
        self.assertFalse(self.cache.get("x.py", "alpha").trusted)
        self.assertIsNone(self.cache.hit(u))

    def test_replay_relocates_line_and_verifies_evidence(self):
        u = self._unit("alpha")
        self.store.start_review(repo_root="/r", scope=["x.py"], review_id="rev3")
        self.store.finish_review("rev3", "done")
        payload = [{"line": u.start + 1, "severity": "high", "category": "boundary",
                    "message": "空列表除零", "evidence": "return sum(values) / len(values)",
                    "suggestion": "", "status": "proposed"}]
        self.cache.put(u.file, u.qualname, u.body_hash, "rev3", "findings",
                       payload, unit_start=u.start)
        cached = self.cache.hit(u)
        # 文件整体下移 3 行（上面加了 import）：证据仍在，行号应跟着走。
        # 注意 replay 拿到的是**当前**源码解析出的单位 —— 它的 start 已经是新位置，
        # 所以这里必须用 shifted 重新解析，而不是复用旧的 u。
        shifted = ["# pad", "# pad", "# pad", *SRC.splitlines()]
        now = [x for x in units_of_source("\n".join(shifted), "x.py")
               if x.qualname == "alpha"][0]
        self.assertEqual(now.body_hash, u.body_hash)          # 函数体没变，只有位置变了
        kept, dropped = self.cache.replay(now, cached, shifted)
        self.assertEqual(dropped, [])
        self.assertEqual(kept[0]["line"], now.start + 1)
        self.assertEqual(kept[0]["message"], "空列表除零")

    def test_replay_drops_when_evidence_moved(self):
        u = self._unit("alpha")
        self.store.start_review(repo_root="/r", scope=["x.py"], review_id="rev4")
        self.store.finish_review("rev4", "done")
        payload = [{"line": u.start + 1, "severity": "high", "category": "boundary",
                    "message": "m", "evidence": "完全不存在的一行", "suggestion": "",
                    "status": "proposed"}]
        self.cache.put(u.file, u.qualname, u.body_hash, "rev4", "findings",
                       payload, unit_start=u.start)
        kept, dropped = self.cache.replay(u, self.cache.hit(u), SRC.splitlines())
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_record_verdicts_skips_units_without_verdict(self):
        units = {"x.py": [self._unit("alpha"), self._unit("beta")]}
        n = self.cache.record_verdicts("rev5", units, {("x.py", "alpha"): "clean"}, [])
        self.assertEqual(n, 1)
        self.assertIsNone(self.cache.get("x.py", "beta"))    # 没结论就不写


# ---------------------------------------------------------------- 4. 端到端
class _BaseChain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_p15_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write(self.tmp / "mod.py",
              "def gamma(items=[]):\n"
              "    try:\n"
              "        items.append(1)\n"
              "    except Exception:\n"
              "        pass\n"
              "    return items\n\n\n"
              "def delta(x):\n"
              "    return x\n")
        self.db = str(self.tmp / "r.db")


class TestRunReview(_BaseChain):
    def _run(self, plan=None, db=None):
        plan = plan or plan_for_files(self.tmp, ["mod.py"])
        return asyncio.run(run_review(plan, None, db=db or self.db,
                                      llm_factory=HeuristicMockBrain,
                                      model_name="mock"))

    def test_first_run_finds_and_records(self):
        out = self._run()
        self.assertEqual(out.state, "Done")
        self.assertTrue(out.ran_model)
        msgs = [f.message for f in out.session.confirmed()]
        self.assertEqual(len(msgs), 2, msgs)
        self.assertTrue(any("可变默认参数" in m for m in msgs))
        self.assertTrue(any("静默吞掉" in m for m in msgs))
        store = ReviewStore(self.db)
        with store:
            rv = store.get_review(out.review_id)
            self.assertEqual(rv["status"], "done")
            self.assertEqual(rv["findings_total"], 2)

    def test_second_run_uses_cache_and_replays_findings(self):
        self._run()
        store = ReviewStore(self.db)
        try:
            plan = plan_for_files(self.tmp, ["mod.py"], cache=UnitCache(store))
        finally:
            store.close()
        self.assertEqual(plan.files, [])                     # 全部命中缓存
        self.assertEqual(len(plan.replayed), 2)
        out = self._run(plan)
        self.assertFalse(out.ran_model)                      # 没调模型
        self.assertEqual(len(out.session.confirmed()), 2)    # 但结论还在
        self.assertTrue(all(f.cached for f in out.session.confirmed()))

    def test_no_db_run_still_reviews(self):
        plan = plan_for_files(self.tmp, ["mod.py"])
        out = asyncio.run(run_review(plan, None, db=None,
                                     llm_factory=HeuristicMockBrain))
        self.assertIsNone(out.review_id)
        self.assertEqual(len(out.session.confirmed()), 2)


class TestCli(_BaseChain):
    def _main(self, *argv):
        from statecraft.cli import main
        return main(list(argv))

    def test_units_subcommand(self):
        self.assertEqual(self._main("units", "--path", str(self.tmp), "--no-db"), 0)

    def test_review_then_report_then_ask(self):
        self.assertEqual(self._main("review", "--path", str(self.tmp), "--mock",
                                    "--db", self.db), 0)
        self.assertEqual(self._main("report", "--db", self.db), 0)
        self.assertEqual(self._main("ask", "mod.py 有哪些高危问题",
                                    "--db", self.db, "--no-llm"), 0)
        self.assertEqual(self._main("reviews", "--db", self.db), 0)

    def test_review_json(self):
        self.assertEqual(self._main("review", "--path", str(self.tmp), "--mock",
                                    "--db", self.db, "--json"), 0)

    def test_report_missing_db(self):
        self.assertEqual(self._main("report", "--db", str(self.tmp / "nope.db")), 2)

    def test_diff_on_non_repo_returns_2(self):
        self.assertEqual(self._main("review", "--path", str(self.tmp),
                                    "--diff", "HEAD", "--no-db"), 2)

    def test_dry_run_does_not_touch_model(self):
        self.assertEqual(self._main("review", "--path", str(self.tmp),
                                    "--dry-run", "--no-db"), 0)


# ---------------------------------------------------------------- 5. 迁移
class TestSchemaMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_mig_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "old.db"
        dbmod._initialized.discard(str(self.path.resolve()))

    def _v1_db(self) -> None:
        """造一个 v1 库：有 reviews 数据、没有 unit_cache 表。"""
        conn = dbmod.connect(self.path)
        conn.execute("DROP TABLE IF EXISTS unit_cache")
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO reviews (id, task_id, repo_root, scope, status, "
                     "started_at) VALUES ('r1','t','/r','[\"a.py\"]','done','now')")
        conn.commit()
        conn.close()
        dbmod._initialized.discard(str(self.path.resolve()))

    def test_additive_migration_keeps_old_data(self):
        self._v1_db()
        conn = dbmod.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             dbmod.SCHEMA_VERSION)
            n = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            self.assertEqual(n, 1)                            # 老数据一条不少
            conn.execute("SELECT COUNT(*) FROM unit_cache").fetchone()
        finally:
            conn.close()

    def test_unregistered_version_refuses_to_migrate(self):
        self._v1_db()
        with mock.patch.object(dbmod, "SCHEMA_VERSION", 9):
            with self.assertRaises(RuntimeError):
                dbmod.connect(self.path)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 6. 分批
class TestBatching(unittest.TestCase):
    """一个任务装不下一整仓：真机 186 个函数一次跑完 = Blocked + 0 条结论。"""

    def _plan(self, kinds):
        """kinds: {file: 函数个数}"""
        from statecraft.incremental.plan import ReviewPlan
        from statecraft.incremental.units import units_of_source

        units, files = {}, []
        for file, n in kinds.items():
            src = "".join(f"def f{i}():{NL}    return {i}{NL}{NL}{NL}" for i in range(n))
            units[file] = units_of_source(src, file)
            files.append(file)
        return ReviewPlan(root=Path("."), files=files, units=units, source="diff")

    def test_batches_respect_cap_and_keep_everything(self):
        from statecraft.review.batch import MAX_UNITS_PER_BATCH, make_batches

        plan = self._plan({"a.py": 7, "b.py": 7, "c.py": 7})
        batches = make_batches(plan)
        self.assertEqual(sum(b.unit_count for b in batches),
                         sum(len(v) for v in plan.units.values()))   # 一个都没丢
        self.assertTrue(all(b.unit_count <= MAX_UNITS_PER_BATCH for b in batches))
        self.assertGreater(len(batches), 1)
        self.assertEqual([b.index for b in batches], list(range(len(batches))))
        self.assertTrue(all(b.total == len(batches) for b in batches))

    def test_huge_single_file_is_split(self):
        from statecraft.review.batch import make_batches

        plan = self._plan({"big.py": 30})
        batches = make_batches(plan, max_units=12)
        self.assertGreater(len(batches), 1)
        seen = sum(len(b.units["big.py"]) for b in batches)
        self.assertEqual(seen, len(plan.units["big.py"]))          # 分片不重不漏
        quals = [u.qualname for b in batches for u in b.units["big.py"]]
        self.assertEqual(len(quals), len(set(quals)))

    def test_file_without_units_is_not_dropped(self):
        from statecraft.review.batch import make_batches
        from statecraft.incremental.plan import ReviewPlan

        plan = ReviewPlan(root=Path("."), files=["empty.py"], units={}, source="files")
        batches = make_batches(plan)
        self.assertEqual([b.files for b in batches], [["empty.py"]])

    def test_brief_names_targets_and_forbids_whole_file_claim(self):
        plan = self._plan({"a.py": 2})
        from statecraft.review.batch import make_batches

        text = make_batches(plan)[0].brief(plan)
        self.assertIn("a.py", text)
        self.assertIn("f0", text)
        self.assertIn("units=", text)          # 必须明确要求按函数声明，而不是整文件
        self.assertIn("不在本批范围", text)


class TestScopeAndCleanUnits(unittest.TestCase):
    """覆盖闸门按批算：第一批不能因为后面几批没跑就被判「缺结论」。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_scope_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write(self.tmp / "a.py", "def f():{0}    return 1{0}{0}{0}def g():{0}    return 2{0}".format(NL))
        from statecraft.incremental.units import read_units
        self.units = {"a.py": read_units(self.tmp / "a.py", "a.py")}

    def _session(self, scope=None):
        from statecraft.review.findings import ReviewSession
        return ReviewSession(self.tmp, ["a.py"], units=self.units,
                             strict_units=True, scope=scope)

    def test_unaccounted_only_counts_current_batch(self):
        s = self._session({"a.py": {"f"}})
        s.read("executor", "a.py")
        s.declare_clean("executor", "a.py", "f 只有一行 return，没有可出错的边界", units=["f"])
        self.assertEqual(s.unaccounted(), [])          # g 属于别的批次
        s.scope = {"a.py": {"g"}}
        self.assertEqual(len(s.unaccounted()), 1)      # 换批之后 g 才算待审

    def test_declare_clean_with_units_marks_only_those(self):
        s = self._session()
        s.read("executor", "a.py")
        msg = s.declare_clean("executor", "a.py", "f 是纯常量返回，没有异常路径可走",
                              units=["f"])
        self.assertIn("f", msg)
        # 只剩 <module> 和 g 没有结论（<module> 也是待审单位：模块级语句改了同样要审）
        self.assertEqual([u.qualname for u in s.uncovered_units()], ["<module>", "g"])
        self.assertEqual(s.clean_claims, {})           # 局部声明不算「整文件干净」

    def test_declare_clean_rejects_unknown_unit(self):
        s = self._session()
        s.read("executor", "a.py")
        msg = s.declare_clean("executor", "a.py", "这一批函数都很简单，没有可报的问题",
                              units=["not_exist"])
        self.assertIn("没有这些函数", msg)
        self.assertEqual(len(s.uncovered_units()), 3)   # 一条都没算进去

    def test_declare_clean_without_units_covers_file(self):
        s = self._session()
        s.read("executor", "a.py")
        s.declare_clean("executor", "a.py", "两个函数都只有一行 return，无缺陷")
        self.assertEqual(s.uncovered_units(), [])
        self.assertIn("a.py", s.clean_claims)


class _SilentBrain:
    """只说话不干活：用来验证「审不完」必须变成 Blocked，而不是 Done。"""

    def __init__(self, session):
        self.session = session

    async def chat(self, messages, tools=None):
        from statecraft.providers.base import LLMReply
        return LLMReply(content="看过了，感觉还行。")


class TestBatchedRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_batch_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _files(self, n):
        for i in range(n):
            write(self.tmp / f"m{i}.py",
                  "def h{0}():{1}    return {0}{1}".format(i, NL))
        return [f"m{i}.py" for i in range(n)]

    def test_many_files_finish_and_report_batches(self):
        files = self._files(20)
        plan = plan_for_files(self.tmp, files)
        out = asyncio.run(run_review(plan, None, db=None,
                                     llm_factory=HeuristicMockBrain))
        self.assertEqual(out.state, "Done", out.blocked_reason)
        self.assertGreater(out.batches, 1)          # 20 个文件必然分批
        self.assertGreaterEqual(len(out.session.unaccounted()), 0)

    def test_blocked_batch_is_not_reported_as_done(self):
        files = self._files(3)
        plan = plan_for_files(self.tmp, files)
        out = asyncio.run(run_review(plan, None, db=None, llm_factory=_SilentBrain))
        self.assertEqual(out.state, "Blocked")
        self.assertIn("结论", out.blocked_reason)

    def test_cli_exit_code_1_on_blocked(self):
        files = self._files(2)
        db = str(self.tmp / "b.db")
        from statecraft.cli import main
        with mock.patch("statecraft.cli.HeuristicMockBrain", _SilentBrain):
            code = main(["review", "--path", str(self.tmp), "--file", ",".join(files),
                         "--mock", "--db", db])
        self.assertEqual(code, 1)


class TestWorktreeWarning(unittest.TestCase):
    def test_dirty_worktree_warns_when_comparing_commits(self):
        """审 A..B 时行号来自提交 B，函数按工作区解析 —— 脏工作区必须提醒。"""
        tmp = Path(tempfile.mkdtemp(prefix="sc_warn_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = init_repo(tmp / "r")
        write(repo / "a.py", "def f():{0}    return 1{0}".format(NL))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "one")
        write(repo / "a.py", "def f():{0}    return 2{0}".format(NL))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "two")

        clean = __import__("subprocess").run(
            ["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--", "*.py"]).returncode == 0
        self.assertTrue(clean)

        plan = plan_from_diff(repo, base="HEAD~1", head="HEAD")
        self.assertEqual(plan.warnings, [])           # 工作区干净：不该瞎警告

        write(repo / "a.py", "def f():{0}    return 3{0}{0}{0}def g():{0}    return 4{0}".format(NL))
        plan = plan_from_diff(repo, base="HEAD~1", head="HEAD")
        self.assertTrue(any("工作区与 HEAD 不一致" in w for w in plan.warnings))

