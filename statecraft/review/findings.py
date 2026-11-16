"""findings —— 审查结果的结构化载体 + 工具契约 + 六条确定性形式审。

从 P15 起这个模块住在包里（`statecraft.review`），不再是 `examples/` 的
脚本 —— 因为 CLI 要能 `python -m statecraft review`，而包依赖示例目录是
反的。`examples/code-review/findings.py` 现在只是一层 re-export 壳子。

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

P14 加的两件东西都在同一个对象上，因为它们是「同一批数据的不同属性」：
- `Finding.in_diff`：这条问题落在本次 git 变更行上没有（增量模式才有意义）
- `ReviewSession.unit_verdicts`：每个**函数**有没有结论。文件级覆盖不够用——
  一个文件改了 5 个函数、只报了 1 条，文件级覆盖是「齐了」，实际漏了 4 个。
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
    id: str = ""                   # 会话内编号（8 位，报告里显示的 #xxxx）
    db_id: str = ""                # 库内主键（16 位，含批次，跨批不会撞）
    agent: str = ""
    status: str = "proposed"      # proposed/confirmed/disputed/dismissed
    checks: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    critic_note: str = ""
    in_diff: bool | None = None   # 增量模式：是否落在本次变更行上（None = 不适用）
    cached: bool = False          # 复用自上次审查的结论（见 incremental/cache.py）

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
        tags = []
        if self.cached:
            tags.append("复用")
        if self.in_diff is False:
            tags.append("未变更行")
        tail = f"｜{'/'.join(tags)}" if tags else ""
        return (f"[{mark}] #{self.id} {self.severity:<6} {self.category:<11} "
                f"{self.file}:{self.line} {self.message}{tail}")


class ReviewSession:
    """一次审查的现场：审了哪些文件、读过什么、记了哪些 finding。

    这个对象在应用层持有，不进内核。内核只负责流程，审查的领域知识
    （什么算合格、证据怎么核对）全在这里。
    """

    def __init__(self, root: str | Path, files: list[str], *,
                 units: dict | None = None, ranges: dict | None = None,
                 strict_units: bool = False, scope: dict | None = None):
        self.root = Path(root).resolve()
        self.files = [self.normalize(f) for f in files]
        self._sources: dict[str, list[str]] = {}
        self.reads: set[tuple[str, str]] = set()      # (agent, file)
        self.findings: dict[str, Finding] = {}
        # 清白声明：确实看过、判定没问题时的举证。没有它，「一条都不报」
        # 就无法与「根本没看」区分——真机实测审查员读完 388 行 Flask 代码
        # 报了 0 条，审核者也照样通过，形成双盲通过（漏报比误报危险得多）。
        self.clean_claims: dict[str, str] = {}
        # 持久层（可选）：绑定后每条结论都落库。不绑也能跑——审查的正确性
        # 不依赖存储，存储只负责「事后能查」。这也是为什么 attach 是独立的
        # 一步而不是构造参数：先跑起来，再决定存哪。
        self.store = None
        self.review_id: str | None = None
        self.plan: str = ""          # 规划原文，收尾时落库（事后能复盘「为什么这么审」）

        # ---- P14：增量审查的两个维度 ----
        # units: file -> [CodeUnit]，本次范围内的函数（含 <module>）
        # ranges: file -> [(start, end)]，本次 diff 的变更行区间；None = 整文件
        self.units: dict[str, list] = {k: list(v) for k, v in (units or {}).items()}
        self.ranges: dict[str, list[tuple[int, int]]] | None = ranges
        self.strict_units = bool(strict_units)
        self.unit_verdicts: dict[tuple[str, str], str] = {}
        # 分批判审时当前批的范围：file -> {qualname}；None = 全部文件都算本批。
        # 它只影响「覆盖检查算谁」，不影响结论落库 —— 批次是执行细节，
        # 一个文件被拆到两批里，两次的结论仍然属于同一次审查。
        self.scope: dict | None = scope

    def attach(self, store, review_id: str) -> "ReviewSession":
        """绑定 SQLite。已在内存里的结论会补写一遍，避免先记录后绑定丢数据。"""
        self.store, self.review_id = store, review_id
        for f in list(self.findings.values()):
            self._persist(f)
        for path, why in list(self.clean_claims.items()):
            store.add_clean_claim(review_id, path, "unknown", why)
        return self

    def _persist(self, f: Finding) -> str | None:
        """把一条 finding 写进库（幂等：同批次 文件+行 只会有一行）。"""
        if self.store is None:
            return None
        f.db_id = self.store.record(
            self.review_id, file=f.file, line=f.line, severity=f.severity,
            category=f.category, message=f.message, evidence=f.evidence,
            suggestion=f.suggestion, status=f.status, form_ok=f.form_ok,
            evidence_ok=bool(f.checks.get("evidence")),
            audit_ok=bool(f.checks.get("audit")),
            critic_note=f.critic_note)
        return f.db_id

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

    def read(self, agent: str, path: str,
             start_line: int | None = None, end_line: int | None = None) -> str:
        """带行号返回源码，并记录「谁读过什么」。

        增量模式下可以只读变更区间（`start_line/end_line`）：省 Token 是真的，
        但**记录的是「读过这个文件」**，不是「读过这几行」—— 形式审的
        evidence 核对用的是整份源码，读切片不影响编造检测的严格性。
        """
        key = self.normalize(path)
        lines = self.lines_of(key)
        self.reads.add((agent, key))
        lo = max(1, int(start_line)) if start_line else 1
        hi = min(len(lines), int(end_line)) if end_line else len(lines)
        if hi < lo:
            lo, hi = 1, len(lines)
        body = "\n".join(f"{i:>4}| {lines[i - 1]}" for i in range(lo, hi + 1))
        span = f"（{len(lines)} 行）" if (lo, hi) == (1, len(lines)) else \
               f"（共 {len(lines)} 行，本次只给 {lo}-{hi} 行）"
        return f"{key}{span}\n{body}"

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

        # 增量模式：不在变更行上不算编造（代码里确实有这个问题），
        # 但要标出来 —— 「审一个提交却报了别人的历史问题」需要人工判断收不收。
        if self.ranges is not None:
            spans = self.ranges.get(f.file) or []
            f.in_diff = any(s <= f.line <= e for s, e in spans) if spans else None

        f.checks, f.reasons = checks, reasons

    # ---- 函数级覆盖（P14）----
    def mark_unit_verdict(self, file: str, line: int, verdict: str) -> None:
        """给「包含这一行的最内层函数」记一个有结论的标记。"""
        hits = [u for u in self.units.get(file, []) if u.contains(line)]
        if not hits:
            return
        u = min(hits, key=lambda x: (x.end - x.start, x.start))
        self.unit_verdicts[(file, u.qualname)] = verdict

    # ---- 批次范围 ----
    def scoped_files(self) -> list[str]:
        """本批要审的文件（`scope is None` 时就是全部）。"""
        if self.scope is None:
            return list(self.files)
        return [f for f in self.files if self.scope.get(f)]

    def scoped_units(self, file: str) -> list:
        """本批要审的函数（`scope is None` 时就是该文件的全部）。"""
        units = self.units.get(file, [])
        if self.scope is None:
            return units
        allow = self.scope.get(file) or set()
        return [u for u in units if u.qualname in allow]

    def unit_concluded(self, file: str, unit) -> bool:
        """这个函数有没有结论：报过形式审通过的问题，或被显式判定干净。"""
        if (file, unit.qualname) in self.unit_verdicts:
            return True
        return any(f.form_ok and f.file == file and unit.contains(f.line)
                   for f in self.findings.values())

    def uncovered_units(self) -> list:
        """本次范围内还没结论的函数。

        「有结论」= 报了一条形式审通过的问题，或被 `declare_clean` 显式点名。
        比文件级覆盖严格一档：文件里有 1 条 finding 不代表另外 4 个函数审过。

        为什么不再「文件有清白声明就整文件跳过」：分批之后一个文件可能被拆到
        两批，整文件跳过会让第二批的函数凭空算作已审 —— 那正是「看着审完了、
        其实一段代码没人读过」。
        """
        out = []
        for file in self.scoped_files():
            for u in self.scoped_units(file):
                if not self.unit_concluded(file, u):
                    out.append(u)
        return out

    # ---- 记录与统计 ----
    def add(self, f: Finding) -> Finding:
        """记录一条 finding。

        同一 id 已存在且已通过形式审 → 视为重复，返回旧的那条；
        已存在但被驳回 → 用新的覆盖，允许模型修正后重新提交
        （否则「证据写错了」会变成死结：改完还叫重复，永远进不了报告）。
        缓存复用的条目可以被新的实测结论覆盖 —— 没人读过的东西不该挡住读过的人。
        """
        fid = f.ensure_id()
        old = self.findings.get(fid)
        if old is not None and old.form_ok and not old.cached:
            return old
        self.findings[fid] = f
        if f.form_ok:
            self.mark_unit_verdict(f.file, f.line, "findings")
        self._persist(f)
        return f

    def adopt_cached(self, items: list[dict]) -> int:
        """接住增量计划里复用来的结论（上次审查过、函数体哈希未变）。

        这些条目**不经本会话的形式审**——本会话根本没读过这个文件，
        audit 一定会失败。可信度来自「上一批次的六条规则 + 函数体逐字节相同」，
        报告里标「复用」，不假装是这次看出来的。
        """
        n = 0
        for it in items:
            try:
                f = Finding(
                    file=self.normalize(it["file"]), line=int(it["line"]),
                    severity=it.get("severity", "medium"),
                    category=it.get("category", "correctness"),
                    message=it.get("message", ""), evidence=it.get("evidence", ""),
                    suggestion=it.get("suggestion", ""),
                    status=it.get("status", "proposed"), agent="cached")
            except (KeyError, TypeError, ValueError):
                continue
            f.ensure_id()
            f.cached = True
            f.checks = {"cached": True}
            f.reasons = [f"复用批次 {it.get('cached_from', '?')} 的结论"
                         f"（函数体哈希未变，行号已重定位到 {f.line}）"]
            self.findings.setdefault(f.id, f)
            self._persist(f)
            self.mark_unit_verdict(f.file, f.line, "findings")
            n += 1
        return n

    def confirmed(self) -> list[Finding]:
        return [f for f in self.findings.values() if f.form_ok and f.status != "disputed"]

    def disputed(self) -> list[Finding]:
        """形式审通过、但被 meta-review 抽查推翻的。单独统计，不混进误报率。"""
        return [f for f in self.findings.values() if f.form_ok and f.status == "disputed"]

    def rejected(self) -> list[Finding]:
        return [f for f in self.findings.values() if not f.form_ok]

    def out_of_diff(self) -> list[Finding]:
        """增量模式下报在未变更行上的问题（不算错，但要看一眼才知道收不收）。"""
        return [f for f in self.confirmed() if f.in_diff is False]

    def coverage(self) -> list[str]:
        """哪些被要求审查的文件，审查者一次都没读过。"""
        return [f for f in self.files if not any(k == f for _, k in self.reads)]

    def unaccounted(self) -> list[str]:
        """还没有结论的对象：增量模式按函数算，其它模式按文件算。

        「有结论」只认形式审通过的 finding 或清白声明 —— 被驳回的条目不构成
        结论，该文件仍然要重新看一遍或给出清白举证。
        """
        if self.strict_units and self.units:
            return [f"{u.file}:{u.qualname}({u.start}-{u.end})"
                    for u in self.uncovered_units()]
        done = {f.file for f in self.findings.values() if f.form_ok}
        return [f for f in self.scoped_files()
                if f not in done and f not in self.clean_claims]

    def declare_clean(self, agent: str, path: str, reason: str,
                      units: list | None = None) -> str:
        """登记「看过了、判定无问题」，必须带具体依据。

        `units` 为空 = 整个文件干净；给了名字 = 只这几个函数干净。
        后者是分批判审的前提：一个文件里报了 2 条问题、剩下 18 个函数没问题，
        模型必须能只把这 18 个函数判干净 —— 否则它只有两个选择：谎称整文件
        干净（把 2 条问题也盖掉），或者干脆不声明（覆盖闸门永远不放行）。
        """
        key = self.normalize(path)
        if key not in self.files:
            return f"声明失败：{key} 不在本次审查范围 {self.files} 内"
        if (agent, key) not in self.reads:
            return f"声明失败：本会话未读过 {key}，未读即判无问题不可信，请先 read_file"
        why = (reason or "").strip()
        if len(why) < 10:
            return ("声明被驳回：reason 太空泛。要写清检查了哪些函数/哪几类问题、"
                    "为什么判定不构成缺陷（≥10 字）")
        every = self.units.get(key, [])
        if units:
            names = {str(x).strip() for x in units if str(x).strip()}
            known = {u.qualname for u in every}
            unknown = sorted(names - known)
            if unknown:
                return (f"声明被驳回：{key} 里没有这些函数：{unknown}。"
                        f"可用的函数名：{sorted(known)}")
            chosen = [u for u in every if u.qualname in names]
        else:
            chosen = list(every)
        for u in chosen:
            self.unit_verdicts[(key, u.qualname)] = "clean"
        if not units:                        # 只有整文件声明才记「这个文件审过了」
            self.clean_claims[key] = why
            if self.store is not None:
                self.store.add_clean_claim(self.review_id, key, agent, why)
        return (f"已记录 {key} 的清白声明（覆盖 {len(chosen)} 个函数："
                f"{'、'.join(u.qualname for u in chosen[:8])}"
                f"{'…' if len(chosen) > 8 else ''}）。")

    def confirm(self, fid: str, verdict: str, note: str = "") -> Finding | None:
        """meta-review 结论：confirm / dispute。写内存 + 落库，两处必须同源。"""
        f = self.findings.get(fid)
        if f is None:
            return None
        f.status = "confirmed" if verdict == "confirm" else "disputed"
        f.critic_note = note or ""
        self._persist(f)
        return f

    def summary(self) -> str:
        ok, rej, dis = self.confirmed(), self.rejected(), self.disputed()
        head = (f"审查文件 {len(self.files)} 个｜有效 {len(ok)} 条 / "
                f"编造驳回 {len(rej)} 条 / 复核推翻 {len(dis)} 条")
        if self.units:
            head += f"｜函数 {sum(len(v) for v in self.units.values())} 个"
            gap = len(self.uncovered_units())
            if gap:
                head += f"（无结论 {gap} 个）"
        miss = self.coverage()
        if miss:
            head += f"｜未覆盖: {', '.join(miss)}"
        if self.clean_claims:
            head += f"｜清白声明 {len(self.clean_claims)} 个"
        cached = [f for f in self.confirmed() if f.cached]
        if cached:
            head += f"｜复用 {len(cached)} 条"
        odd = self.out_of_diff()
        if odd:
            head += f"｜未变更行 {len(odd)} 条"
        gap = self.unaccounted()
        if gap and not self.strict_units:
            head += f"｜无结论: {', '.join(gap)}"
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
        if self.clean_claims:
            lines.append("")
            lines.append("-- 清白声明（审过且判定无问题，附依据）--")
            for k, v in self.clean_claims.items():
                lines.append(f"       {k}: {v[:90]}")
        if self.rejected():
            lines.append("")
            lines.append("-- 形式审驳回（不计入报告，用于统计审查者幻觉率）--")
            for f in self.rejected():
                lines.append(f"       #{f.id} {f.file}:{f.line} {f.message[:40]} ← {'；'.join(f.reasons)}")
        return "\n".join(lines)

    # ---- 落缓存（P14）----
    def verdict_items(self) -> list[dict]:
        """本次实测得到的结论（不含复用来的），供写函数级缓存。"""
        return [{"file": f.file, "line": f.line, "severity": f.severity,
                 "category": f.category, "message": f.message,
                 "evidence": f.evidence, "suggestion": f.suggestion,
                 "status": f.status}
                for f in self.findings.values() if f.form_ok and not f.cached]

    def effective_verdicts(self) -> dict[tuple[str, str], str]:
        """落缓存用的函数级结论。

        增量模式（strict_units）只认显式结论：某个函数没被报问题、也没有
        清白声明，就是「没审」，不写缓存 —— 否则下次直接跳过它。

        非增量模式（整文件审查）允许把文件级结论外推到函数级：审查员读的是
        整个文件，完成条件也是文件级，此时「这个文件有问题，但 f() 没被报」
        和「f() 干净」在我们自己的口径里是同一件事。**两种口径不能混**，
        混了就会在增量模式下悄悄漏审函数。
        """
        verdicts = dict(self.unit_verdicts)
        if not self.strict_units:
            touched = {f.file for f in self.findings.values() if f.form_ok}
            for file, units in self.units.items():
                if file in self.clean_claims or file in touched:
                    for u in units:
                        verdicts.setdefault((file, u.qualname), "clean")
        return verdicts

    def flush_units(self, cache, review_id: str | None = None) -> int:
        """把函数级结论写进 `unit_cache`，返回写入条数。"""
        if cache is None or not self.units:
            return 0
        return cache.record_verdicts(review_id or self.review_id or "",
                                     self.units, self.effective_verdicts(),
                                     self.verdict_items())


# ---------------------------------------------------------------- 工具契约
def make_tools(session: ReviewSession) -> list[Tool]:
    """构造审查流水线的五个工具。每个都遵守「先校验、失败回填」的约定。"""

    def _read_file(ctx: dict, path: str, start_line: int | None = None,
                   end_line: int | None = None, **_) -> str:
        return session.read(ctx["agent"].name, path, start_line, end_line)

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
            extra = "" if f.in_diff is not False else \
                    "（注意：这行不在本次变更行上，会被标注为「未变更行」）"
            return (f"已记录 #{f.id}（形式审通过）。{extra}"
                    f"继续报下一条，全部报完输出 REQUEST_REVIEW。")
        return ("记录已被形式审驳回，这条不计入报告：" + "；".join(f.reasons) +
                "。请修正后重新提交，或放弃这条。")

    def _declare_clean(ctx: dict, file: str, reason: str, units=None, **_) -> str:
        """申报「看完了、没发现问题」，必须带具体依据。"""
        return session.declare_clean(ctx["agent"].name, file, reason, units=units)

    def _review_status(ctx: dict, **_) -> str:
        """审核者用的覆盖查证：还差哪些文件/函数没有结论。"""
        gap = session.unaccounted()
        head = session.summary()
        if not gap:
            return f"{head}\n所有待审对象都有结论，可以抽查 finding 了。"
        return (f"{head}\n**尚无结论的{'函数' if session.strict_units else '文件'}："
                f"{', '.join(gap)}** —— 这些既没有问题记录，也没有清白声明，产出不完整。")

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
        session.confirm(fid, v, note)
        target = "确认成立" if v == "confirm" else "判定不成立，已移除"
        return f"#{fid} {target}。{note}"

    return [
        Tool("read_file", "读取代码文件（带行号）。报问题前必须先读，否则视为编造。",
             {"type": "object",
              "properties": {
                  "path": {"type": "string", "description": "文件路径"},
                  "start_line": {"type": "integer",
                                 "description": "可选：只读这个行号之后（增量审查省上下文）"},
                  "end_line": {"type": "integer",
                               "description": "可选：只读到这个行号"}},
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
             _record_finding, mutates=True),
        Tool("declare_clean",
             "申报「看完了、未发现问题」。每个待审对象都必须有结论：要么 "
             "record_finding，要么 declare_clean。reason 要写清检查了哪些函数、"
             "哪几类问题、为什么判定不构成缺陷（空话会被驳回）。",
             {"type": "object",
              "properties": {
                  "file": {"type": "string", "description": "文件相对路径"},
                  "reason": {"type": "string",
                             "description": "逐项说明检查了什么、为什么判定无问题"},
                  "units": {"type": "array", "items": {"type": "string"},
                            "description": "可选：只把这些函数判为干净（增量审查"
                                           "分批时用）。不填 = 整个文件都干净"}},
              "required": ["file", "reason"]},
             _declare_clean, mutates=True),
        Tool("review_status",
             "查看本次审查的整体覆盖情况：已报问题、清白声明、尚无结论的文件/函数。"
             "审核产出前必须先调用它。",
             {"type": "object", "properties": {}},
             _review_status),
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
