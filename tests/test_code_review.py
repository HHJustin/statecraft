"""代码审查流水线的回归测试（全离线，MockLLM/脚本化，不花 API 费用）。

覆盖两个层次：
- **形式审六条规则**：逐条构造违规 finding，验证拦截。
- **整条流水线**：脚本化假模型驱动，验证状态流转、权限、meta-review、
  以及「证据造假不进报告」这条最关键的约束。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW_DIR = ROOT / "examples" / "code-review"
for p in (str(ROOT), str(REVIEW_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import findings as F                       # noqa: E402
import review as R                          # noqa: E402
from statecraft.core.state_machine import Task           # noqa: E402
from statecraft.core.tools import (AGENT_POLICY, PermissionDenied,       # noqa: E402
                                   call_tool, ToolRegistry, default_registry)


def _session(files=("target.py",)) -> F.ReviewSession:
    return F.ReviewSession(REVIEW_DIR, list(files))


class _FakeAgent:
    """满足 call_tool 需要的两个属性（name / role）。"""

    def __init__(self, name, role):
        self.name, self.role = name, role
        self.ctx = {}


def _ctx(agent_name="executor", role="reviewer", saga=None) -> dict:
    a = _FakeAgent(agent_name, role)
    a.ctx = {"agent": a, "task": Task(id="tX", description="审查 target.py"),
             "saga": saga, "orchestrator": None}
    return a.ctx


def _tools(session):
    reg = ToolRegistry()
    for name in ("report_progress", "request_block"):
        reg.register(default_registry().get(name))
    for t in F.make_tools(session):
        reg.register(t)
    return reg


def _call(session, ctx, name, **args):
    """直接走 handler（绕过权限层），专测形式审本身。"""
    reg = _tools(session)
    return reg.get(name).handler(ctx, **args)


VALID = dict(file="target.py", line=23, severity="high", category="boundary",
             message="average() 对空列表直接除零",
             evidence="    return sum(values) / len(values)")


class TestFormChecks(unittest.TestCase):
    """六条确定性规则，逐条验证「该拦的拦住」。"""

    def test_happy_path_all_six_pass(self):
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **VALID)
        f = list(s.findings.values())[0]
        self.assertTrue(f.form_ok, f.reasons)
        self.assertEqual(set(f.checks), set(F.CHECK_NAMES))
        self.assertTrue(all(f.checks.values()))

    def test_fabricated_evidence_rejected(self):
        """证据写的是源码里不存在的行 → 判定编造。"""
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding",
              **{**VALID, "evidence": "    return str(round(cents / 100, 2))"})
        f = list(s.findings.values())[0]
        self.assertFalse(f.checks["evidence"])
        self.assertFalse(f.form_ok)
        self.assertIn("疑似编造", "".join(f.reasons))
        self.assertEqual(len(s.confirmed()), 0)      # 不进报告

    def test_line_out_of_range_rejected(self):
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **{**VALID, "line": 9999})
        f = list(s.findings.values())[0]
        self.assertFalse(f.checks["range"])
        self.assertIn("越界", "".join(f.reasons))

    def test_report_without_reading_rejected(self):
        """未读即报：本会话没有 read_file 记录 → audit 不过。"""
        s = _session()
        ctx = _ctx()
        _call(s, ctx, "record_finding", **VALID)      # 故意不读
        f = list(s.findings.values())[0]
        self.assertFalse(f.checks["audit"])
        self.assertIn("未读过", "".join(f.reasons))

    def test_file_out_of_scope_rejected(self):
        s = _session()
        ctx = _ctx()
        _call(s, ctx, "record_finding", **{**VALID, "file": "statecraft/config.py"})
        f = list(s.findings.values())[0]
        self.assertFalse(f.checks["scope"])

    def test_bad_enum_rejected(self):
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **{**VALID, "severity": "critical"})
        f = list(s.findings.values())[0]
        self.assertFalse(f.checks["enums"])

    def test_rejected_finding_can_be_fixed_and_resubmitted(self):
        """被驳回的那条允许改证据后重新提交（否则「写错了」变成死结）。"""
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **{**VALID, "evidence": "不存在的行"})
        self.assertEqual(len(s.confirmed()), 0)
        _call(s, ctx, "record_finding", **VALID)      # 同一 id，修正后重提
        self.assertEqual(len(s.confirmed()), 1)
        self.assertEqual(len(s.findings), 1)          # 没有产生两条

    def test_duplicate_valid_finding_not_stored_twice(self):
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **VALID)
        msg = _call(s, ctx, "record_finding", **VALID)
        self.assertIn("重复记录", msg)
        self.assertEqual(len(s.findings), 1)

    def test_paraphrased_duplicate_deduped_by_line(self):
        """真模型实测：同一处缺陷换措辞、换类别各报一遍。

        去重键收窄到「文件+行号」——同一行只保留一个最主要的问题，
        与真实 Code Review 的评论粒度一致（一条线只挂一条评论）。
        """
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **VALID)
        _call(s, ctx, "record_finding",
              **{**VALID, "message": "average() 未处理空序列，空列表会除零"})
        _call(s, ctx, "record_finding", **{**VALID, "category": "correctness"})
        _call(s, ctx, "record_finding", **{**VALID, "category": "exception",
                                          "message": "空列表导致除零异常未转换"})
        self.assertEqual(len(s.findings), 1)

    def test_coverage_reports_unread_files(self):
        s = _session(("target.py", "target_clean.py"))
        s.read("executor", "target.py")
        self.assertEqual(s.coverage(), ["target_clean.py"])

    def test_clean_file_produces_zero_findings(self):
        """误报率底线：审查者读了干净文件但一条都没报。"""
        s = _session(("target_clean.py",))
        s.read("reviewer", "target_clean.py")
        self.assertEqual(len(s.findings), 0)
        self.assertEqual(s.coverage(), [])
        self.assertEqual(len(s.confirmed()), 0)


class TestMetaReview(unittest.TestCase):
    def test_dispute_removes_finding_from_report(self):
        s = _session()
        ctx = _ctx()
        s.read("executor", "target.py")
        _call(s, ctx, "record_finding", **VALID)
        fid = list(s.findings)[0]
        _call(s, _ctx("critic", "review_critic"), "confirm_finding",
              finding_id=fid, verdict="dispute", note="核对后不成立")
        self.assertEqual(len(s.findings), 1)          # 仍在现场（留痕）
        self.assertEqual(len(s.confirmed()), 0)       # 但不进报告

    def test_unknown_finding_id_is_reported_not_crashed(self):
        s = _session()
        msg = _call(s, _ctx("critic", "review_critic"), "confirm_finding",
                    finding_id="deadbeef", verdict="confirm")
        self.assertIn("没有编号", msg)


class TestPermission(unittest.IsolatedAsyncioTestCase):
    """审查员只能报问题，不能改文件——权限在执行前拦，不靠 prompt。"""

    def setUp(self):
        self.s = _session()
        self.reg = _tools(self.s)

    async def test_reviewer_cannot_record_after_revoked(self):
        # 用独立的策略名，不污染全局 review_critic（否则后续用例会缺权限）
        AGENT_POLICY["review_critic_revoked"] = {
            "role": "review", "tools": {"report_progress", "read_file"}}
        ctx = _ctx("critic", "review_critic_revoked")
        with self.assertRaises(PermissionDenied):
            await call_tool(ctx["agent"], self.reg, "record_finding", VALID)

    async def test_write_tool_not_reachable_from_review_roles(self):
        """写文件工具既没注册、也不在白名单 → 权限层先拦，比「查无此工具」更早。"""
        ctx = _ctx("executor", "reviewer")
        with self.assertRaises(PermissionDenied):
            await call_tool(ctx["agent"], self.reg, "edit_file",
                            {"path": "target.py", "old": "a", "new": "b"})


class TestPipeline(unittest.IsolatedAsyncioTestCase):
    """整条流水线：脚本化假模型跑真实状态机。"""

    async def test_full_run_reaches_done_with_valid_findings_only(self):
        session = F.ReviewSession(REVIEW_DIR, R.TARGET_FILES)
        orch, session = R.build(REVIEW_DIR, R._ScriptedBrain(session), session)
        task = await orch.run(orch.create_task("审查 target.py 的代码质量"))

        self.assertEqual(task.state, "Done")
        trace = [r["to"] for r in task.flow_log]
        self.assertEqual(trace, ["Planning", "Reviewing", "Executing",
                                 "Reviewing", "PendingConfirm", "Done"])
        self.assertEqual(len(session.confirmed()), 5)     # 五类真实缺陷
        self.assertEqual(len(session.rejected()), 1)      # 一条编造被驳回
        self.assertEqual(session.coverage(), [])          # 文件都读了

    async def test_findings_all_carry_verified_evidence(self):
        session = F.ReviewSession(REVIEW_DIR, R.TARGET_FILES)
        orch, session = R.build(REVIEW_DIR, R._ScriptedBrain(session), session)
        await orch.run(orch.create_task("审查 target.py"))
        for f in session.confirmed():
            self.assertTrue(f.checks["evidence"], f"#{f.id} 证据未通过核对")
            self.assertTrue(f.checks["audit"], f"#{f.id} 未读即报")

    async def test_detection_rate_against_planted_answers(self):
        """检出率是「可算的数字」，不是「看着挺好」。5 类埋点必须全中。"""
        session = F.ReviewSession(REVIEW_DIR, R.TARGET_FILES)
        orch, session = R.build(REVIEW_DIR, R._ScriptedBrain(session), session)
        await orch.run(orch.create_task("审查 target.py"))
        sc = R.score_against_planted(session, R.planted_for(R.TARGET_FILES))
        self.assertEqual(sc["expected"], 5)
        self.assertEqual(sc["hit"], 5, f"漏报: {sc['miss']}")
        self.assertEqual(sc["rate"], 1.0)
        self.assertEqual(sc["cat_ok"], 5, "类别也应对得上")

    async def test_clean_file_has_no_false_positive(self):
        """误报率底线：审干净文件必须一条都不报。"""
        session = F.ReviewSession(REVIEW_DIR, ["target_clean.py"])
        orch, session = R.build(REVIEW_DIR, R._ScriptedBrain(session), session)
        await orch.run(orch.create_task("审查 target_clean.py"))
        self.assertEqual(len(session.confirmed()), 0)
        self.assertEqual(R.planted_for(["target_clean.py"]), {})   # 干净文件无标准答案

    async def test_critic_spot_check_marks_finding_confirmed(self):
        session = F.ReviewSession(REVIEW_DIR, R.TARGET_FILES)
        orch, session = R.build(REVIEW_DIR, R._ScriptedBrain(session), session)
        await orch.run(orch.create_task("审查 target.py"))
        self.assertTrue(any(f.status == "confirmed" for f in session.findings.values()),
                        "meta-review 应当至少抽查确认一条")


if __name__ == "__main__":
    unittest.main()
