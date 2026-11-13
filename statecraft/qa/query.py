"""查询执行：把一句自然语言变成「SQL 过滤」或「召回结果」。

这里最容易做错的一件事是**跨批次查询**。同一个问题在 3 个批次里都存在，
`SELECT ... ` 全表扫一遍就会报「3 条」，用户看到的却是同一处缺陷说了三遍。
所以：

- **结构化查询默认只查最新一批**（`latest_review()`）。问「有哪些高危问题」
  是在问「现在这套代码怎么样」，历史批次是版本演进，混在一起没有意义。
- **语义查询默认跨批次**。「以前修过类似的吗」问的就是历史，只查最新批次
  等于把问题本身否掉了。

两个口径都在 `notes` 里写出来 —— 用户有权知道自己看到的是哪个范围。

另一条纪律：**memory 只是召回器，不是答案来源**。三路召回（词面/向量/代码图）
返回的是文本，可能是几天前索引的、可能与库里对不上。所以每条召回都要拿
`metadata["finding_id"]` 回 DB 复核，库里没有就丢掉并计入 `dropped` —— 宁可
少给一条，也不给一条查无实据的。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..storage.findings import ReviewStore

# 用户嘴里的文件名：带常见源码后缀即可，路径前缀不强求（auth.py / src/auth.py 都得认）
_FILE_RE = re.compile(
    r"[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|go|java|rb|rs|c|h|cc|cpp|sql|md|sh|ya?ml)",
    re.I)

# 严重度词表。注意「严重度」是聚合词不是过滤词 —— 先把它替换掉再匹配，
# 否则「严重度分布」会被当成 severity='high' 的过滤条件，返回 0 条。
_SEVERITY_WORDS = (
    ("high", ("高危", "严重", "致命", "紧急", "critical", "blocker", "high")),
    ("medium", ("中危", "中等", "一般", "major", "medium")),
    ("low", ("低危", "轻微", "次要", "minor", "low")),
)

MAX_ROWS = 20          # 进上下文的条数上限：再多模型也读不完，还会稀释注意力

# 疑问词/虚词：留在查询里只会稀释召回。
# 为什么必须做这一步：`ReviewStore.search` 的 FTS 只切 ASCII 词，中文走 LIKE
# 兜底时是**整句**匹配 ——「有没有处理过类似的除零问题」去 LIKE `%整句%`，
# 库里永远没有这一行。中文环境里这不是边缘情况，是主路径。
_ZH_STOP = (
    "有没有", "是否", "处理过", "处理", "以前", "之前", "曾经", "过去", "历史上",
    "类似", "相似", "相近", "同类", "相同", "一样",
    "问题", "毛病", "缺陷", "隐患", "情况", "记录", "审查", "检查",
    "怎么", "如何", "怎样", "什么", "为什么", "哪些", "哪个", "多少", "几个",
    "我们", "你们", "他们", "这个", "那个", "这些", "那些", "现在", "目前",
    "代码", "项目", "文件", "帮我", "看看", "查查", "一下", "还有",
)
_ZH_CHAR_STOP = "的了吗呢呀啊吧嘛哦么"

_KW_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*|[\u4e00-\u9fff]{2,}")


def keywords(question: str, limit: int = 5) -> list[str]:
    """把问句切成可检索的关键词。英文标识符优先级最高（`average`、`None`）。

    这是查询改写的最小实现，不引入模型：问答对延迟敏感，而且改写错了
    会静默降低召回 —— 规则至少能复现、能单测。
    """
    q = (question or "")
    rest = q
    for w in _ZH_STOP:
        rest = rest.replace(w, " ")
    for ch in _ZH_CHAR_STOP:
        rest = rest.replace(ch, " ")
    out: list[str] = []
    for m in _KW_RE.finditer(rest):
        kw = m.group(0).strip("._")
        if len(kw) < 2 or kw in out:
            continue
        out.append(kw)
    if not out:
        # 关键词被剥光了（例如「你好」）：退回原句，让上层自然返回空
        return [q.strip()] if q.strip() else []
    # 英文标识符更精确，排前面
    out.sort(key=lambda w: (0 if w.isascii() else 1))
    return out[:limit]


@dataclass
class Slots:
    """从问题里抽出来的过滤条件。抽不到就是 None，绝不猜默认值。"""
    files: list[str] = field(default_factory=list)
    severity: str | None = None
    aggregate: str | None = None      # by_file / by_severity / count
    review_id: str | None = None

    def describe(self) -> str:
        bits = []
        if self.files:
            bits.append(f"文件={self.files}")
        if self.severity:
            bits.append(f"严重度={self.severity}")
        if self.aggregate:
            bits.append(f"聚合={self.aggregate}")
        return "，".join(bits) or "无过滤条件"


@dataclass
class QueryResult:
    route: str
    rows: list[dict] = field(default_factory=list)     # finding 行（form_ok=1）
    stats: dict | None = None                          # 聚合结果
    review_id: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows and self.stats is None


def extract_slots(question: str) -> Slots:
    """槽位抽取：只认确定的字面特征，不做语义猜。"""
    q = (question or "").lower()
    slots = Slots()

    slots.files = [m.group(0).replace("\\", "/") for m in _FILE_RE.finditer(q)]

    # 「严重度」先剔掉，避免它被当成 high 的证据
    sev_text = q.replace("严重度", "").replace("严重程度", "")
    for level, words in _SEVERITY_WORDS:
        if any(w in sev_text for w in words):
            slots.severity = level
            break

    slots.aggregate = _aggregate(q)
    # 「严重度分布」问的是分布本身，此时再按 severity 过滤就把语义变了
    #（「按严重度分布」变成「高危的分布」）。但「哪个文件高危最多」要保留过滤。
    if slots.aggregate == "by_severity":
        slots.severity = None
    return slots


def _aggregate(q: str) -> str | None:
    """判定聚合类型。顺序有讲究，先具体后宽泛。"""
    if "严重度" in q or "严重程度" in q or "按等级" in q or "按严重" in q:
        return "by_severity"
    if any(k in q for k in ("最多", "最少", "排行", "排名", "top", "哪个文件",
                            "哪些文件", "哪个模块")):
        return "by_file"
    if "分布" in q or "统计" in q or "占比" in q:
        return "by_file"
    if any(k in q for k in ("多少", "几个", "几条", "总数", "一共", "总共", "合计", "数量")):
        return "count"
    return None


class QueryEngine:
    """执行层：只负责「拿到数据」，不做自然语言包装（那是 answer.py 的事）。"""

    def __init__(self, store: ReviewStore, memory=None):
        self.store = store
        self.memory = memory

    # ------------------------------------------------------------ 结构化
    def structured(self, question: str, review_id: str | None = None) -> QueryResult:
        slots = extract_slots(question)
        rid, note = self._scope(review_id)
        notes = [note]

        if slots.aggregate == "by_file":
            rows = self.store.counts_by_file(review_id=rid, severity=slots.severity)
            if not rows:
                return QueryResult("structured", review_id=rid, notes=notes)
            # 文件过滤也要生效：「auth.py 和 parser.py 谁的问题多」应该只在这两个里比
            if slots.files:
                keep = set(self._resolve_files(slots.files, rid))
                filtered = [r for r in rows if r["file"] in keep]
                if filtered:
                    rows = filtered
                    notes.append(f"只在 {sorted(keep)} 里比较")
            return QueryResult("structured", stats={"kind": "by_file", "rows": rows,
                                                   "severity": slots.severity},
                               review_id=rid, notes=notes)

        if slots.aggregate == "by_severity":
            dist = self.store.counts_by_severity(review_id=rid)
            return QueryResult("structured",
                               stats={"kind": "by_severity", "dist": dist},
                               review_id=rid, notes=notes)

        one_file = None if not slots.files else self._one_file(slots.files, rid, notes)

        if slots.aggregate == "count":
            # 计数必须走 SQL COUNT：用 len(rows) 会被 MAX_ROWS 截断，
            # 30 条问题会答成「20 条」—— 数字错比不给数字更糟。
            n = self.store.count(rid, severity=slots.severity,
                                 file=one_file, form_ok=True)
            dist = self.store.counts_by_severity(review_id=rid, severity=slots.severity,
                                                 file=one_file)
            return QueryResult("structured",
                               stats={"kind": "count", "n": n, "dist": dist,
                                      "severity": slots.severity,
                                      "file": one_file or (slots.files[0] if slots.files else None)},
                               review_id=rid, notes=notes)

        rows = self.store.findings(rid, severity=slots.severity, file=one_file,
                                   form_ok=True, limit=MAX_ROWS)
        return QueryResult("structured", rows=rows, review_id=rid, notes=notes)

    # ------------------------------------------------------------ 语义
    def semantic(self, question: str, limit: int = MAX_ROWS) -> QueryResult:
        notes: list[str] = ["范围：全部历史批次（问『以前』就不能只看最新一批）"]
        kws = keywords(question)
        rows: list[dict] = []
        seen: set = set()
        for kw in kws:
            for row in self.store.search(kw, limit=limit):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                rows.append(row)
        if kws:
            notes.append(f"查询改写：{' + '.join(kws)}")
        if rows:
            notes.append(f"词面召回（FTS5/LIKE）命中 {len(rows)} 条")

        dropped = 0
        if self.memory is not None:
            got, drop = self._recall_from_memory(question, seen, limit)
            dropped = drop
            if got:
                rows.extend(got)
                notes.append(f"混合召回补充 {len(got)} 条")
            if drop:
                # 这条数字本身就是「索引不可信」的证据，必须露出来
                notes.append(f"混合召回有 {drop} 条在库里查无实据，已丢弃")
        if self.memory is None:
            notes.append("未接入记忆层：只走词面召回，语义相近但用词不同的条目会漏")
        return QueryResult("semantic", rows=rows[:limit], notes=notes)

    def _recall_from_memory(self, question: str, seen: set, limit: int) -> tuple[list, int]:
        """memory 召回 → 回 DB 复核。查无实据的一律丢弃。"""
        try:
            items = self.memory.search(question, limit=limit)
        except Exception:                             # 召回故障不能拖垮问答
            return [], 0
        out, dropped = [], 0
        for it in items:
            fid = (getattr(it, "metadata", None) or {}).get("finding_id")
            if not fid or fid in seen:
                continue
            row = self.store.finding(fid)              # DB 是真相源
            if row is None or not row.get("form_ok"):
                dropped += 1
                continue
            out.append(row)
            seen.add(fid)
        return out, dropped

    # ------------------------------------------------------------ 内部工具
    def _scope(self, review_id: str | None) -> tuple[str | None, str]:
        """结构化查询的范围：显式批次 > 最新已完成批次。"""
        if review_id:
            return review_id, f"范围：指定批次 {review_id}"
        latest = self.store.latest_review()
        if latest is None:
            return None, "范围：库里没有已完成的批次，退回全库"
        return latest["id"], (f"范围：最新批次 {latest['id']}"
                              f"（跨批次会重复计数，需要时用 --review 指定）")

    def _resolve_files(self, names: list[str], review_id: str | None) -> list[str]:
        """把 `auth.py` 映射到库里真实的 `src/auth.py`（精确优先，再按后缀）。"""
        known = self.store.files(review_id)
        out: list[str] = []
        for n in names:
            if n in known:
                out.append(n)
                continue
            tail = "/" + n.lstrip("/")
            out.extend(k for k in known if k.endswith(tail) and k not in out)
        return out

    def _one_file(self, names: list[str], review_id: str | None,
                  notes: list[str]) -> str | None:
        """单文件过滤。多个文件时只用第一个 —— 明细查询一次只看一个，
        多文件的比较属于聚合问题（by_file），走上面那条分支。"""
        resolved = self._resolve_files(names, review_id)
        if not resolved:
            notes.append(f"库中没有文件 {names}（可能不在本批次审查范围内）")
            return names[0]           # 返回原名，让查询自然返回 0 条
        if len(resolved) > 1:
            notes.append(f"{names} 命中多个文件 {resolved}，明细只取 {resolved[0]}")
        return resolved[0]
