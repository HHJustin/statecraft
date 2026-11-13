"""答案生成：把查询结果变成一句能溯源的话。

三条硬约束，每条都对应一个真实会发生的失效：

1. **检索结果带编号进上下文**（`[F1] [F2] ...`），模型只能引用这些编号。
   没有编号，模型就会把「我记得 Python 里空列表会除零」这种通用知识
   混进答案 —— 那已经不是「查审查记录」，是「让模型聊聊代码」。

2. **引用必须回 DB 复核**。模型完全可以编一个 `[F7]`，而上下文里只有 5 条。
   这里不是警告一下就完事：编号越界记为 `unknown_citations`，指向的记录
   还要再查一次库（可能已被删/被驳回），查不到就剔除。**答案里的每条引用
   都能落到一行数据库记录**，这是可断言的事实，不是承诺。

3. **没有命中就直接答「没有」，不调模型**。库是空的时候让模型去「根据上下文
   回答」，它只会把上下文之外的东西倒出来。空结果走确定性回答，成本为零、
   幻觉为零。`empty=True` 让前端能区分「没查到」和「查到了但没结论」。

另外两个刻意的设计：

- **聚合类问题不经过模型**。问「哪个文件问题最多」，SQL 已经算出数字了，
  让模型复述一遍只会引入说错数字的风险。数字必须来自 SQL。
  （`deterministic=True`，前端可以标「精确统计」。）
- **模型没给任何有效引用时不丢掉它的回答，而是补上原始清单**。回答可能
  说对了但忘了标号，直接替换太粗暴；但也不能让用户面对一段无法溯源的话，
  所以追加「以下为原始记录，供核对」并把 `unsourced=True` 抛出去。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..providers.base import ProviderError
from ..storage.findings import ReviewStore
from . import router
from .query import MAX_ROWS, QueryEngine

# 模型可能写 [F1] / 【F1】 / 全角括号，都认
_CITE_RE = re.compile(r"[\[【]\s*F\s*(\d+)\s*[\]】]", re.I)

SYSTEM_PROMPT = """你是代码审查记录的问答助手。你的唯一信息来源是下面给出的审查记录。

硬性规则（违反任何一条，这个回答就作废）：
1. 只能使用记录里明确写出的信息，不得补充通用编程知识、不得推断、不得假设。
2. 每个结论后面必须标注引用编号，格式为 [F1]；一句话涉及多条就都标上，如 [F1][F3]。
3. 记录中没有的内容，直接回答「现有审查记录中没有相关结论」，不要猜、不要给建议、不要安慰。
4. 不要复述记录原文全文，用一两句话回答即可；用户要清单时再逐条列。

回答用中文。"""


@dataclass
class Citation:
    label: str            # F1
    finding_id: str
    file: str
    line: int
    severity: str
    review_id: str
    message: str = ""


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    route: str = "structured"
    empty: bool = False              # 库里没有任何相关记录
    # 答案来源：sql（聚合）/ records（清单，未过模型）/ llm / none（没查库）
    # 为什么要区分 sql 和 records：前者是「算出来的数字」，后者是「列出来的记录」，
    # 前端对这两者的可信度预期完全不同，混成一个 deterministic 布尔就丢了信息。
    source: str = "sql"
    deterministic: bool = False      # 未经过模型（source != "llm"）
    unsourced: bool = False          # 模型回答了但没给任何有效引用
    unknown_citations: list[str] = field(default_factory=list)   # 模型编的编号
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        tags = []
        if self.empty:
            tags.append("无记录")
        if self.source == "sql":
            tags.append("精确统计")
        elif self.source == "records":
            tags.append("未经模型")
        if self.unsourced:
            tags.append("未引用")
        if self.unknown_citations:
            tags.append(f"越界引用 {self.unknown_citations}")
        tag_text = "|".join(tags)
        head = f"[{self.route}]" + (f"[{tag_text}]" if tags else "")
        body = self.text
        if self.citations:
            body += "\n\n引用：\n" + "\n".join(
                f"  {c.label} → {c.file}:{c.line} [{c.severity}] 批次 {c.review_id}"
                for c in self.citations)
        return f"{head}\n{body}"

    def to_dict(self) -> dict:
        return {
            "text": self.text, "route": self.route, "empty": self.empty,
            "source": self.source,
            "deterministic": self.deterministic, "unsourced": self.unsourced,
            "unknown_citations": self.unknown_citations, "notes": self.notes,
            "citations": [c.__dict__ for c in self.citations],
        }


def _run_coro(coro):
    """把 async 调用跑完。已在事件循环里时开个线程跑 —— 问答是同步接口，
    但可能被 async 代码调用（FastAPI handler 里），两处都得能跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def build_context(rows: list[dict]) -> str:
    """把 finding 行编成带编号的上下文。编号顺序 = 引用解析顺序，必须一致。"""
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"[F{i}] {r['file']}:{r['line']} [{r['severity']}/{r['category']}] "
                     f"{r['message']}")
        lines.append(f"     证据：{r['evidence']}")
        if r.get("suggestion"):
            lines.append(f"     建议：{r['suggestion']}")
        tail = f"     复核状态：{r['status']}"
        if r.get("critic_note"):
            tail += f"（{r['critic_note']}）"
        lines.append(f"{tail}｜批次 {r['review_id']}")
    return "\n".join(lines)


class CodebaseQA:
    """审查记录的问答门面。

        qa = CodebaseQA("reviews.db", llm=make_llm())
        print(qa.ask("sample.py 有哪些高危问题？"))
        print(qa.ask("哪个文件问题最多？"))

    不传 llm 也能用：列表/聚合类问题本来就不需要模型，语义召回也能给出清单
    （只是没有归纳性的措辞）。这条降级路径是故意的 —— 问答不该因为模型挂了
    就完全不可用。
    """

    def __init__(self, store: ReviewStore | str, llm=None, memory=None):
        self._owns = isinstance(store, (str, bytes, Path))
        self.store = ReviewStore(store) if self._owns else store
        self.llm = llm
        self.memory = memory
        self.engine = QueryEngine(self.store, memory=memory)

    def close(self) -> None:
        if self._owns:
            self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 记忆索引
    def index_findings(self, review_id: str | None = None) -> int:
        """把 findings 灌进记忆层，供三路召回（词面/向量/代码图）。

        metadata 里带 `finding_id` 是硬要求：语义召回只负责「想起来」，
        答案一律回 DB 取 —— 索引里的文本可能已经过期，DB 才是真相源。
        """
        if self.memory is None:
            raise RuntimeError("没有接入记忆层，无法建索引")
        rows = self.store.findings(review_id, form_ok=True,
                                   limit=1_000_000)   # 建索引要全量
        for r in rows:
            self.memory.add(
                f"[{r['id']}] {r['file']}:{r['line']} [{r['severity']}/{r['category']}] "
                f"{r['message']} 证据：{r['evidence']}",
                metadata={"type": "finding", "finding_id": r["id"],
                          "review_id": r["review_id"], "file": r["file"]})
        return len(rows)

    # ------------------------------------------------------------ 主入口
    def ask(self, question: str, review_id: str | None = None) -> Answer:
        greeting = _smalltalk_reply(question)
        if greeting is not None:
            return greeting
        decision = router.decide(question)
        if decision["route"] == router.SEMANTIC:
            res = self.engine.semantic(question)
        else:
            res = self.engine.structured(question, review_id=review_id)
        return self._compose(question, res, decision)

    async def aask(self, question: str, review_id: str | None = None) -> Answer:
        """异步版：自己跑查询，只把 LLM 调用 await 掉。"""
        greeting = _smalltalk_reply(question)
        if greeting is not None:
            return greeting
        decision = router.decide(question)
        if decision["route"] == router.SEMANTIC:
            res = self.engine.semantic(question)
        else:
            res = self.engine.structured(question, review_id=review_id)
        return await self._compose_async(question, res, decision)

    # ------------------------------------------------------------ 组装
    def _compose(self, question: str, res, decision: dict) -> Answer:
        if res.empty:
            return Answer(text="现有审查记录中没有相关结论。", route=res.route,
                          empty=True, notes=res.notes + [f"路由：{decision['reason']}"])
        if res.stats is not None and not res.rows:
            return Answer(text=_format_stats(res.stats), route=res.route,
                          source="sql", deterministic=True,
                          notes=res.notes + [f"路由：{decision['reason']}"])
        if self.llm is None:
            return self._fallback(res, decision, "未接入模型：直接给出确定性清单")
        try:
            reply = _run_coro(self.llm.chat(self._messages(question, res.rows)))
        except ProviderError as exc:
            return self._fallback(res, decision, f"模型调用失败，已降级：{exc}")
        except Exception as exc:                      # noqa: BLE001 兜底不能打穿问答
            return self._fallback(res, decision,
                                 f"模型异常（{type(exc).__name__}: {exc}），已降级")
        return self._from_reply(reply.content, res, decision)

    async def _compose_async(self, question: str, res, decision: dict) -> Answer:
        if res.empty:
            return Answer(text="现有审查记录中没有相关结论。", route=res.route,
                          empty=True, notes=res.notes + [f"路由：{decision['reason']}"])
        if res.stats is not None and not res.rows:
            return Answer(text=_format_stats(res.stats), route=res.route,
                          source="sql", deterministic=True,
                          notes=res.notes + [f"路由：{decision['reason']}"])
        if self.llm is None:
            return self._fallback(res, decision, "未接入模型：直接给出确定性清单")
        try:
            reply = await self.llm.chat(self._messages(question, res.rows))
        except ProviderError as exc:
            return self._fallback(res, decision, f"模型调用失败，已降级：{exc}")
        return self._from_reply(reply.content, res, decision)

    def _messages(self, question: str, rows: list[dict]) -> list[dict]:
        # 范围说明（哪个批次/是否跨批次）留在 notes，不进 prompt：
        # 模型拿它没用，反而可能把「批次 id」当结论写进答案。
        user = (f"审查记录（共 {len(rows)} 条）：\n{build_context(rows)}\n\n"
                f"问题：{question}")
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]

    def _from_reply(self, text: str, res, decision: dict) -> Answer:
        cites, unknown = self._resolve_citations(text, res.rows)
        if unknown:
            # 保留模型原文会误导：用户看到 [F7] 会以为真有第 7 条。
            # 就地标注，而不是悄悄删掉 —— 删掉等于替模型掩盖了一次幻觉。
            bad = set(unknown)
            text = _CITE_RE.sub(
                lambda m: (f"[F{m.group(1)}·查无此记录]"
                           if f"[F{m.group(1)}]" in bad else m.group(0)), text)
        notes = res.notes + [f"路由：{decision['reason']}"]
        if unknown:
            notes.append(f"模型引用了不存在的编号 {unknown}，已剔除（这是可确定性发现的幻觉）")
        if not cites:
            notes.append("回答未引用任何有效编号 → 判定为无出处，已附原始记录供核对")
            return Answer(text=f"{text}\n\n以下为原始记录，供核对：\n"
                               f"{_format_rows(res.rows)}",
                          citations=self._citations_from(res.rows), route=res.route,
                          source="llm", unsourced=True,
                          unknown_citations=unknown, notes=notes)
        return Answer(text=text, citations=cites, route=res.route, source="llm",
                      unknown_citations=unknown, notes=notes)

    def _fallback(self, res, decision: dict, why: str) -> Answer:
        return Answer(text=_format_rows(res.rows), route=res.route,
                      citations=self._citations_from(res.rows),
                      source="records", deterministic=True,
                      notes=res.notes + [f"路由：{decision['reason']}", why])

    def _citations_from(self, rows: list[dict]) -> list[Citation]:
        """按上下文编号构造引用。清单里印着 [F1]，就必须有一条记录对得上。"""
        return [Citation(label=f"F{i}", finding_id=r["id"], file=r["file"],
                         line=r["line"], severity=r["severity"],
                         review_id=r["review_id"], message=r["message"])
                for i, r in enumerate(rows, 1)]

    def _resolve_citations(self, text: str, rows: list[dict]) -> tuple[list[Citation], list[str]]:
        """编号 → 记录 → 回 DB 复核。查不到的一律剔除。"""
        by_label = {f"F{i}": r for i, r in enumerate(rows, 1)}
        cites: dict[str, Citation] = {}
        unknown: list[str] = []
        for m in _CITE_RE.finditer(text or ""):
            label = f"F{m.group(1)}"
            row = by_label.get(label)
            if row is None:
                unknown.append(f"[{label}]")
                continue
            if label in cites:
                continue
            db_row = self.store.finding(row["id"])
            if db_row is None:                 # 上下文里有、库里没了（并发删除）
                unknown.append(f"[{label}]")
                continue
            cites[label] = Citation(
                label=label, finding_id=db_row["id"], file=db_row["file"],
                line=db_row["line"], severity=db_row["severity"],
                review_id=db_row["review_id"], message=db_row["message"])
        return list(cites.values()), unknown


def _smalltalk_reply(question: str) -> Answer | None:
    """寒暄/空输入不进查询。

    不加这一层，「你好」会被路由成「结构化、无过滤条件」→ 返回全库问题清单。
    技术上没算错，但用户第一句问候收到 5 条缺陷，属于典型的「系统比我更认真」。
    复用分拣器现成的寒暄规则，不额外维护一份正则。
    """
    from ..core.triage import RuleTriage

    if RuleTriage().classify(question)["intent"] != "chitchat":
        return None
    return Answer(
        text="我是代码审查记录的查询助手，可以问：\n"
             "  · sample.py 有哪些高危问题？\n"
             "  · 哪个文件问题最多？\n"
             "  · 有没有处理过类似的除零问题？",
        route="structured", empty=True, source="none",
        notes=["寒暄输入，未查询数据库"])


# ---------------------------------------------------------------- 确定性文本
_ORDER = ("high", "medium", "low")
_CN = {"high": "高危", "medium": "中危", "low": "低危"}


def _dist_text(dist: dict) -> str:
    parts = [f"{_CN.get(k, k)} {dist[k]} 条" for k in _ORDER if dist.get(k)]
    return "、".join(parts) or "无"


def _format_stats(stats: dict) -> str:
    """聚合答案。数字全部来自 SQL，一个字都不经过模型。"""
    kind = stats["kind"]
    if kind == "count":
        where = []
        if stats.get("severity"):
            where.append(f"严重度为{_CN.get(stats['severity'], stats['severity'])}")
        if stats.get("file"):
            where.append(f"文件 {stats['file']}")
        scope = "（" + "、".join(where) + "）" if where else ""
        n = stats["n"]
        head = f"共 {n} 条问题{scope}。"
        dist = stats.get("dist") or {}
        return head + (f"其中 {_dist_text(dist)}。" if n else "")

    if kind == "by_severity":
        dist = stats["dist"] or {}
        total = sum(dist.values())
        return f"共 {total} 条问题：{_dist_text(dist)}。"

    if kind == "by_file":
        rows = stats["rows"]
        if not rows:
            return "现有审查记录中没有相关结论。"
        only = f"（仅{_CN.get(stats['severity'], stats['severity'])}）" if stats.get("severity") else ""
        lines = [f"问题最多的文件{only}："]
        for i, r in enumerate(rows[:5], 1):
            lines.append(f"  {i}. {r['file']} —— {r['n']} 条")
        if len(rows) > 1:
            lines.append(f"共涉及 {len(rows)} 个文件。")
        return "\n".join(lines)

    return "（未知聚合类型）"


def _format_rows(rows: list[dict]) -> str:
    """清单式回答：不归纳，只列事实。降级路径与「供核对」附录共用。"""
    if not rows:
        return "现有审查记录中没有相关结论。"
    dist: dict[str, int] = {}
    for r in rows:
        dist[r["severity"]] = dist.get(r["severity"], 0) + 1
    lines = [f"共 {len(rows)} 条（{_dist_text(dist)}）："]
    for i, r in enumerate(rows, 1):
        ev = (r.get("evidence") or "").strip().replace("\n", " ")[:120]
        lines.append(f"[F{i}] {r['file']}:{r['line']} [{r['severity']}/{r['category']}] "
                     f"{r['message']}（证据：{ev}）")
    return "\n".join(lines)


__all__ = ["Answer", "Citation", "CodebaseQA", "build_context", "MAX_ROWS"]
