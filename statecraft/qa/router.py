"""问答路由：结构化 or 语义（两类，规则判定，不调模型）。

为什么要分两类，看两个问题就明白：

    「auth.py 有哪些高危问题？」  → 这是**过滤条件**，SQL 一条 WHERE 就完了
    「这个 bug 我们以前修过吗？」  → 这是**相似度**，只有向量/词面召回能做

用向量去回答第一类问题是灾难：它会把「高危」当成一个语义概念，返回一堆
「看起来像高危」的条目，而用户要的是「高危的那些，一条不许少」。反过来用
SQL 回答第二类问题也不成立 —— 「修过吗」根本没有可过滤的字段。

三个刻意的决定：

1. **不引入 LLM 兜底。** 项目里 `IntentRouter` 是「规则打分 + LLM 兜底」，
   但那是入口分拣，走错的代价是多跑一次流水线。问答不一样：走错会当场
   给用户一个答非所问的结果，而且问答对延迟敏感（多一次模型往返就是几秒）。
   规则错判了可以改词表，模型错判了没法复现。
2. **平局按「有没有结构化锚点」判，不无条件偏向一边。** 「哪些问题跟这个类似」
   同时命中「哪些」(结构) 和「类似」(语义)，纯比分数是平局。这时看问题里
   有没有文件名 / severity 词 / 聚合词 —— 有锚点说明用户想过滤，没有说明
   他想找相似的。这一条把最容易错判的句式救回来了。
3. **一句都没命中就走结构化。** SQL 的结果可复现、可解释、可断言；语义召回
   的结果依赖 embedder 实现，用户追问「为什么它算相似」时答不上来。保守。
"""
from __future__ import annotations

import re

# 结构化信号：过滤 / 聚合 / 计数都算。用户想要「确定的那几条」。
_STRUCTURED_CUES = (
    "哪些", "哪几个", "哪个", "列出", "清单", "明细", "逐条",
    "多少", "几个", "几条", "数量", "总数", "一共", "总共", "合计",
    "最多", "最少", "排行", "排名", "top", "分布", "统计", "占比",
)
# 注意「列表」不在这里：它在 Python 语境里指 list（「空列表会怎样」），
# 放进来会把「空列表相除会怎样」判成「列个清单给我」，实测踩过。

# 语义信号：用户在问「有没有相似的 / 以前怎么处理的」，不是问精确集合。
_SEMANTIC_CUES = (
    "类似", "相似", "相近", "同类",
    "有没有", "是否出现", "出现过", "以前", "之前", "曾经", "过去", "历史上",
    "怎么处理", "如何处理", "怎么办", "相同问题", "重复问题",
    # 「X 处理过吗 / 修过吗」是最常见的问法，但它一个结构化词都不含：
    # 漏了这三条，「内存泄漏处理过吗」会走结构化并返回全库清单（实测踩到）。
    "处理过", "修过", "遇到过", "见过", "查到过", "是否有",
    # 解释型问题（「空列表相除会怎样」）既不是过滤也不是「找相似的」，
    # 但它需要匹配**具体代码描述** —— 语义召回只给相关的几条，
    # 结构化只能返回全库清单。按召回处理更有用。
    "会怎样", "会出什么", "有什么影响", "为什么", "怎么回事", "会导致", "有什么后果",
)

# 结构化锚点：出现这些说明问题里有可过滤的具体对象。
_ANCHOR_FILE = re.compile(
    r"[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|go|java|rb|rs|c|h|cc|cpp|sql|md|sh|ya?ml)",
    re.I)
_ANCHOR_SEVERITY = ("高危", "严重", "致命", "紧急", "中危", "低危", "轻微",
                    "high", "medium", "low", "critical")
_ANCHOR_AGG = ("最多", "最少", "排行", "排名", "分布", "统计", "一共", "总共")

STRUCTURED = "structured"
SEMANTIC = "semantic"


def score(question: str) -> dict:
    """给出打分明细（对外暴露，便于日志观察与单测断言）。"""
    q = (question or "").lower()
    s_hits = [c for c in _STRUCTURED_CUES if c in q]
    v_hits = [c for c in _SEMANTIC_CUES if c in q]
    anchors: list[str] = []
    if _ANCHOR_FILE.search(q):
        anchors.append("file")
    if any(a in q for a in _ANCHOR_SEVERITY):
        anchors.append("severity")
    if any(a in q for a in _ANCHOR_AGG):
        anchors.append("aggregate")
    return {
        "structured_score": 2 * len(s_hits),
        "semantic_score": 2 * len(v_hits),
        "structured_hits": s_hits,
        "semantic_hits": v_hits,
        "anchors": anchors,
    }


def route(question: str) -> str:
    """返回 "structured" 或 "semantic"。"""
    return decide(question)["route"]


def decide(question: str) -> dict:
    """返回路由结果 + 判定依据（理由要能被打印出来，否则线上没法调）。"""
    d = score(question)
    s, v = d["structured_score"], d["semantic_score"]

    if v > s:
        verdict, reason = SEMANTIC, f"语义信号更强（{v} > {s}）"
    elif s > v:
        verdict, reason = STRUCTURED, f"结构化信号更强（{s} > {v}）"
    elif d["anchors"]:
        # 平局：有具体锚点说明用户要的是「这些对象里的哪些」，按过滤处理
        verdict = STRUCTURED
        reason = f"平局但含结构化锚点 {d['anchors']}，按过滤处理"
    elif v == 0 and s == 0:
        verdict, reason = STRUCTURED, "无信号命中 → 保守走结构化（结果可复现）"
    else:
        verdict, reason = SEMANTIC, "平局且无结构化锚点，按语义召回处理"

    d.update({"route": verdict, "reason": reason})
    return d
