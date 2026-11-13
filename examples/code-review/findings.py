"""findings —— 审查结果的结构化载体 + 工具契约 + 六条确定性形式审。

这是整个审查流水线里最值得看的部分。三个设计决定，每个都在解决一个
真实问题：

1. **结构化由工具强制，不靠模型自觉输出 JSON**
   错误做法是让模型输出 JSON 再正则解析：它会在前面加「以下是我的审查
   结果」，会漏字段，会把 line 写成字符串，长输出还会被截断。
   正确做法是提供 record_finding 工具，参数 schema 卡住必填项——缺 line
   直接失败，模型被迫补齐；顺带天然进 AUDIT_LOG，事后能查是谁在什么
   时候记的。

2. **evidence 逐字符核对，把内容幻觉变成确定性检测**
   要求模型从源文件「原样复制 1-3 行」作为证据。比对不上 = 编造。
   这一步不需要第二个 LLM 当裁判，也不需要人工抽查——是代码能给出的
   确定答案。

3. **审计查证：未读即报 = 必然编造**
   它声称在 target.py:46 发现问题，但本会话里没有 read_file("target.py")
   的记录，那这份报告只能是想象的。
   命令级白名单 + 审计日志在这里从「安全设施」升级成「审查者的不在场
   证明系统」。

三条都过了才算 form_ok；有一条不过，这条 finding 就不进最终报告
（但会留在 rejected 里，用于统计审查者的幻觉率）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from statecraft.core.tools import AUDIT_LOG, Tool

SEVERITIES = ("high", "medium", "low")
CATEGORIES = ("correctness", "boundary", "exception", "naming", "perf", "security")

CHECK_NAMES = {
    "fields": "必填字段完整",
    "enums": "severity/category 取值合法",
    "scope": "文件在本次审查范围内",
    "range": "行号未越界",
    "evidence": "证据可在源文件中核对",
    "audit": "本会话确实读过该文件",
}


def _norm(text: str) -> str:
    """归一化：去掉所有空白。

    逐字符比对如果直接比原文，会因缩进、换行、尾随空格大量误判——
    模型复制代码时经常把行首缩进吃掉。去空白后比对，既保留「内容必须
    真实存在」的严格性，又不会把格式差异当成编造。
    """
    return re.sub(r"\s+", "", text or "")


@dataclass
class Finding:
    """一条代码问题。字段与 record_finding 的参数一一对应。"""

    file: str                     # 归一化后的相对路径（posix 风格）
    line: int
    severity: str
    category: str
    message: str
    evidence: str
    suggestion: str = ""
    id: str = ""
    agent: str = ""
    status: str = "proposed"      # proposed/confirmed/disputed/dismissed
    checks: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    critic_note: str = ""

    @property
    def form_ok(self) -> bool:
        """六条形式审全过才为真。空 checks 视为未校验。"""
        return bool(self.checks) and all(self.checks.values())

    def ensure_id(self) -> str:
        """语义主键：文件 + 行号（一行只保留一个最主要的问题）。

        两次真机实测把去重键逐步收紧了两次：
        1. 一开始按「文件+行+类别+message」哈希 → 模型换措辞就把同一处
           报成 3 条（22 条里 17 条是同 5 个问题的复述）；
        2. 改成「文件+行+类别」→ 模型又换个类别再报一遍（文件句柄泄漏
           同时记成 correctness 和 exception）。
        最终按「文件+行」去重。理由不是偷懒，而是审查评论的天然粒度：
        评论挂在行上，同一行挂两条重复评论只会让提交者困惑。真有一行
        存在两个独立问题时，让模型挑最严重的一条报——代价可接受，
        收益是报告不再有重复噪音。
        """
        if not self.id:
            raw = f"{self.file}|{self.line}"
            self.id = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
        return self.id

    def one_line(self) -> str:
        mark = "OK " if self.form_ok else "REJ"
        return (f"[{mark}] #{self.id} {self.severity:<6} {self.category:<11} "
                f"{self.file}:{self.line} {self.message}")


class ReviewSession:
    """一次审查的现场：审了哪些文件、读过什么、记了哪些 finding。

    这个对象在应用层持有，不进内核。内核只负责流程，审查的领域知识
    （什么算合格、证据怎么核对）全在这里。
    """

    def __init__(self, root: str | Path, files: list[str]):
        self.root = Path(root).resolve()
        self.files = [self.normalize(f) for f in files]
        self._sources: dict[str, list[str]] = {}
        self.reads: set[tuple[str, str]] = set()      # (agent, file)
        self.findings: dict[str, Finding] = {}

    # ---- 路径与源码 ----
    def normalize(self, path: str) -> str:
        """把绝对/相对/反斜杠路径统一成相对 root 的 posix 路径。"""
        p = Path(str(path).replace("\\", "/"))
        if p.is_absolute():
            try:
                p = p.resolve().relative_to(self.root)
            except ValueError as e:
                raise ValueError(f"文件不在审查范围内: {path}") from e
        return p.as_posix().lstrip("./")

    def lines_of(self, path: str) -> list[str]:
        key = self.normalize(path)
        if key not in self._sources:
            full = self.root / key
            if not full.is_file():
                raise FileNotFoundError(f"文件不存在: {key}")
            self._sources[key] = full.read_text(encoding="utf-8").splitlines()
        return self._sources[key]

    def read(self, agent: str, path: str) -> str:
        """带行号返回源码，并记录「谁读过什么」。"""
        key = self.normalize(path)
        lines = self.lines_of(key)
        self.reads.add((agent, key))
        body = "\n".join(f"{i:>4}| {t}" for i, t in enumerate(lines, 1))
        return f"{key}（共 {len(lines)} 行）\n{body}"

    # ---- 六条形式审 ----
    def check(self, f: Finding, agent: str) -> None:
        """填 f.checks / f.reasons。确定性规则，不调 LLM、不花钱。"""
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        checks["fields"] = all([f.file, f.line, f.severity, f.category,
                                f.message, f.evidence])
        if not checks["fields"]:
            reasons.append("必填字段缺失（file/line/severity/category/message/evidence）")

        checks["enums"] = f.severity in SEVERITIES and f.category in CATEGORIES
        if not checks["enums"]:
            reasons.append(f"severity 须为 {SEVERITIES}，category 须为 {CATEGORIES}")

        try:
            f.file = self.normalize(f.file)
        except ValueError:
            f.file = str(f.file)
        checks["scope"] = f.file in self.files
        if not checks["scope"]:
            reasons.append(f"{f.file} 不在本次审查范围 {self.files} 内")

        lines = self._sources.get(f.file)
        total = len(lines) if lines else 0
        checks["range"] = bool(lines) and isinstance(f.line, int) and 1 <= f.line <= total
        if lines and not checks["range"]:
            reasons.append(f"行号 {f.line} 越界（该文件共 {total} 行）")

        hit = bool(lines) and bool(f.evidence) and _norm(f.evidence) in _norm("\n".join(lines))
        checks["evidence"] = hit
        if lines and not hit:
            reasons.append("证据在源文件中找不到——疑似编造，请原样复制被引用的行")

        checks["audit"] = (agent, f.file) in self.reads
        if not checks["audit"]:
            reasons.append(f"本会话未读过 {f.file}，请先 read_file 再报问题")

        f.checks, f.reasons = checks, reasons

    # ---- 记录与统计 ----
    def add(self, f: Finding) -> Finding:
        """记录一条 finding。

        同一 id 已存在且已通过形式审 → 视为重复，返回旧的那条；
        已存在但被驳回 → 用新的覆盖，允许模型修正后重新提交
        （否则「证据写错了」会变成死结：改完还叫重复，永远进不了报告）。
        """
        fid = f.ensure_id()
        old = self.findings.get(fid)
        if old is not None and old.form_ok:
            return old
        self.findings[fid] = f
        return f

    def confirmed(self) -> list[Finding]:
        return [f for f in self.findings.values() if f.form_ok and f.status != "disputed"]

    def disputed(self) -> list[Finding]:
        """形式审通过、但被 meta-review 抽查推翻的。单独统计，不混进误报率。"""
        return [f for f in self.findings.values() if f.form_ok and f.status == "disputed"]

    def rejected(self) -> list[Finding]:
        return [f for f in self.findings.values() if not f.form_ok]

    def coverage(self) -> list[str]:
        """哪些被要求审查的文件，审查者一次都没读过。"""
        return [f for f in self.files if not any(k == f for _, k in self.reads)]

    def summary(self) -> str:
        ok, rej, dis = self.confirmed(), self.rejected(), self.disputed()
        head = (f"审查文件 {len(self.files)} 个｜有效 {len(ok)} 条 / "
                f"编造驳回 {len(rej)} 条 / 复核推翻 {len(dis)} 条")
        miss = self.coverage()
        if miss:
            head += f"｜未覆盖: {', '.join(miss)}"
        return head

    def report(self) -> str:
        lines = [self.summary(), ""]
        for f in self.confirmed():
            lines.append(f.one_line())
            lines.append(f"       证据: {f.evidence.strip().splitlines()[0][:80]}")
            if f.suggestion:
                lines.append(f"       建议: {f.suggestion}")
        if self.disputed():
            lines.append("")
            lines.append("-- 复核推翻（形式审通过，但 meta-review 核对后不成立）--")
            for f in self.disputed():
                lines.append(f"       #{f.id} {f.file}:{f.line} {f.message[:40]}")
                lines.append(f"       复核意见: {f.critic_note or '（未给理由）'}")
        if self.rejected():
            lines.append("")
            lines.append("-- 形式审驳回（不计入报告，用于统计审查者幻觉率）--")
            for f in self.rejected():
                lines.append(f"       #{f.id} {f.file}:{f.line} {f.message[:40]} ← {'；'.join(f.reasons)}")
        return "\n".join(lines)


# ---------------------------------------------------------------- 工具契约
def make_tools(session: ReviewSession) -> list[Tool]:
    """构造审查流水线的三个工具。每个都遵守「先校验、失败回填」的约定。"""

    def _read_file(ctx: dict, path: str, **_) -> str:
        return session.read(ctx["agent"].name, path)

    def _record_finding(ctx: dict, file: str, line: int, severity: str,
                        category: str, message: str, evidence: str,
                        suggestion: str = "", **_) -> str:
        agent = ctx["agent"].name
        try:
            line = int(line)
        except (TypeError, ValueError):
            return "记录失败：line 必须是整数行号"
        f = Finding(file=str(file), line=line, severity=str(severity).lower(),
                    category=str(category).lower(), message=message,
                    evidence=evidence, suggestion=suggestion or "", agent=agent)
        session.check(f, agent)          # 立刻确定性校验
        saved = session.add(f)
        if saved is not f:               # 命中重复（同一行已有问题）
            return (f"重复记录，已存在 #{saved.id}（{f.file}:{f.line} 已报过）。"
                    f"**同一行只报一个最主要的问题，不要换措辞或换类别复述**；"
                    f"若该行确有另一个更严重的问题，说明为什么它更严重并只保留一条。")
        if f.form_ok:
            return (f"已记录 #{f.id}（形式审通过）。"
                    f"继续报下一条，全部报完输出 REQUEST_REVIEW。")
        return ("记录已被形式审驳回，这条不计入报告：" + "；".join(f.reasons) +
                "。请修正后重新提交，或放弃这条。")

    def _confirm_finding(ctx: dict, finding_id: str, verdict: str,
                         note: str = "", **_) -> str:
        """meta-review：critic 抽查一条 finding，给出 confirm / dispute。"""
        fid = str(finding_id).lstrip("#").strip()
        f = session.findings.get(fid)
        if f is None:
            return f"没有编号为 {fid} 的 finding（现有: {sorted(session.findings)}）"
        v = str(verdict).lower()
        if v not in ("confirm", "dispute"):
            return "verdict 只能是 confirm 或 dispute"
        f.status = "confirmed" if v == "confirm" else "disputed"
        f.critic_note = note or ""
        target = "确认成立" if v == "confirm" else "判定不成立，已移除"
        return f"#{fid} {target}。{note}"

    return [
        Tool("read_file", "读取代码文件（带行号）。报问题前必须先读，否则视为编造。",
             {"type": "object",
              "properties": {"path": {"type": "string", "description": "文件路径"}},
              "required": ["path"]},
             _read_file),
        Tool("record_finding",
             "记录一条代码问题。evidence 必须从源文件原样复制 1-3 行（含代码本身），"
             "用于逐字符核对；编造的证据会被自动驳回。",
             {"type": "object",
              "properties": {
                  "file": {"type": "string", "description": "文件相对路径"},
                  "line": {"type": "integer", "description": "问题所在行号"},
                  "severity": {"type": "string", "enum": list(SEVERITIES)},
                  "category": {"type": "string", "enum": list(CATEGORIES)},
                  "message": {"type": "string", "description": "一句话说明问题及其后果"},
                  "evidence": {"type": "string", "description": "从源文件原样复制的 1-3 行"},
                  "suggestion": {"type": "string", "description": "修复建议"}},
              "required": ["file", "line", "severity", "category",
                           "message", "evidence"]},
             _record_finding),
        Tool("confirm_finding",
             "抽复核一条 finding：自己 read_file 核对后判定 confirm 或 dispute。",
             {"type": "object",
              "properties": {
                  "finding_id": {"type": "string"},
                  "verdict": {"type": "string", "enum": ["confirm", "dispute"]},
                  "note": {"type": "string", "description": "一句话理由"}},
              "required": ["finding_id", "verdict"]},
             _confirm_finding),
    ]


def audit_ok(agent_name: str, tool_name: str) -> bool:
    """交叉验证：AUDIT_LOG 里确实有该 Agent 对该工具的 OK 记录。

    与 session.reads 互为印证——reads 是工具处理器自己记的（可信），
    AUDIT_LOG 是权限层记的（独立来源）。两者都说读过，才算读过。
    注意：AUDIT_LOG 不记录调用参数，所以无法单靠它判断「读的是哪个文件」，
    reads 才是那个证据；这个函数用来兜住「工具被绕过权限层直接调用」的情况。
    """
    return any(e.get("agent") == agent_name and e.get("tool") == tool_name
               and e.get("verdict") == "OK" for e in AUDIT_LOG)
