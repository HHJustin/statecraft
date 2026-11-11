"""意图识别 / 分拣节点（源自 Edict 的「太子分拣」）。

两个层次，按场景选：

1. `RuleTriage` —— 二分类（闲聊 / 任务），框架默认
   聊天入口只有「这事要不要进流水线」一个问题，二分够用。

2. `IntentRouter` —— 多分类路由（如 codedoc 的 补注释 / 审查代码 / 解释代码 / 闲聊）
   同一个入口进来四种活，路由错了整条流水线白跑 —— 这时才需要多分类。

`IntentRouter` 的两级漏斗（不做小模型的理由见文末）：

    L1 规则（确定性）：寒暄整句匹配 → chitchat
                      路由关键词打分 → 命中且不僵持就定
    L2 LLM 兜底（可选注入）：规则没命中 / 多路僵持时才调，返回 JSON
    兜底：默认路由（保守）

设计纪律（呼应路线图「能确定性就别用 LLM」）：
- 规则优先：零成本、可离线、可代码断言，覆盖绝大多数明确输入
- LLM 只处理灰区：调用量与成本压到最低，且**异常即降级**，绝不阻断主流程
- 保守策略：拿不准按任务处理 —— 误杀真任务的代价远大于多跑一次流水线

为什么不做小模型那一级：小模型的价值是用低延迟挡住 80% 流量省钱，
只有「日均数千次触发、成本敏感」的场景才划算。codedoc 是「用户点一下」
或「PR 触发一次」的稀疏流量，加第三级是过度设计 —— 规则 + LLM 已经够。
"""
import re
from abc import ABC, abstractmethod

# 寒暄/客套模式（整句匹配，避免误伤「你好，帮我写周报」这类混合输入）
_CHITCHAT_PATTERNS = (
    r"^(你好|您好|hi|hello|嗨|哈喽|在吗|在么|在不在)[,，。!！~?\uff1f\s]*$",
    r"^(谢谢|多谢|感谢|thanks|thank you|辛苦了)[^。！!]{0,8}[。!！]*$",
    r"^(再见|拜拜|晚安|早安|下午好|bye).{0,6}$",
    r"^(哈哈+|嘻嘻|呵呵|嘿嘿|666+|233+|厉害|不错|棒|赞)[\s!！。~\d]*$",
)

# 行动信号：出现任一即视为任务（动词 / 领域名词 / 英文动作词）
_ACTION_KEYWORDS = (
    # 中文动词
    "写", "做", "生成", "整理", "分析", "修复", "实现", "部署", "检查", "评审",
    "计划", "调研", "测试", "执行", "处理", "开发", "修改", "更新", "删除",
    "查询", "回答", "翻译", "总结", "排查", "补充", "审查", "重构", "上传",
    "下载", "导出", "导入", "同步", "计算", "发送", "创建", "规划",
    # 领域名词
    "周报", "日报", "工单", "需求", "bug", "issue", "代码", "函数", "接口",
    "文档", "注释", "docstring", "任务", "项目", "版本", "发布", "数据",
    "报表", "配置", "脚本", "数据库", "服务",
    # 英文动作
    "create", "fix", "implement", "deploy", "refactor", "review", "test",
    "update", "delete", "add", "generate", "write", "build", "run", "check",
    "query", "parse", "convert",
)

_VALID_CHAR = re.compile(r"[\w\u4e00-\u9fff]")


class TriageBase(ABC):
    """分拣器协议：实现 classify 即可替换（LLM 分拣 / 小模型分类器等）。

    classify 返回的 dict 至少含：
        intent: "task" | "chitchat"   —— 编排器只认这个（决定是否进流水线）
        reason: str                   —— 判定理由（落日志/事件，便于审计）
    """

    @abstractmethod
    def classify(self, text: str) -> dict:
        raise NotImplementedError


class RuleTriage(TriageBase):
    """规则分拣器（默认）：二分类，确定性、零依赖、离线可测。"""

    def classify(self, text: str) -> dict:
        t = (text or "").strip()
        low = t.lower()

        # ① 空输入 / 无有效字符 → 闲聊（不进流水线）
        if not low or not _VALID_CHAR.search(low):
            return {"intent": "chitchat", "reason": "无有效内容"}

        # ② 含行动信号 → 任务（先判行动，避免「你好，帮我写周报」被误杀）
        if any(k in low for k in _ACTION_KEYWORDS):
            return {"intent": "task", "reason": "含行动信号"}

        # ③ 命中寒暄模式 → 闲聊
        for p in _CHITCHAT_PATTERNS:
            if re.match(p, low):
                return {"intent": "chitchat", "reason": f"命中寒暄模式: {p}"}

        # ④ 保守兜底：拿不准按任务处理（漏放代价 < 误杀代价）
        return {"intent": "task", "reason": "保守兜底：默认按任务处理"}


class IntentRouter(TriageBase):
    """多分类意图路由：规则打分优先，LLM 只处理灰区。

    routes 支持两种写法（可混用）：
        {"doc": ("补注释", "写注释", "docstring")}          # 等权
        {"doc": {"补注释": 3, "docstring": 2, "注释": 1}}   # 带权（推荐）

    llm_classifier：可调用对象 (text, labels) -> dict|str
        dict 形如 {"route": "doc", "confidence": 0.8}；str 直接当路由名。
        抛异常 = 降级到默认路由（绝不阻断主流程）。
    """

    RULE_CONFIDENCE = 0.6      # 最高分占比低于此值且有多路竞争 → 交给 LLM

    def __init__(self, routes: dict, *, default_route: str | None = None,
                 llm_classifier=None, rule_confidence: float | None = None):
        if not routes:
            raise ValueError("IntentRouter 至少需要一个路由")
        self.routes = {k: self._normalize(v) for k, v in routes.items()}
        self.default_route = default_route or next(iter(self.routes))
        if self.default_route not in self.routes:
            raise ValueError(f"default_route {self.default_route!r} 不在路由表中")
        self.llm_classifier = llm_classifier
        self.rule_conf = (self.RULE_CONFIDENCE if rule_confidence is None
                          else rule_confidence)

    @staticmethod
    def _normalize(spec) -> dict[str, float]:
        if isinstance(spec, dict):
            return {str(k): float(v) for k, v in spec.items()}
        return {str(k): 1.0 for k in spec}

    # ---- 主入口 ----
    def classify(self, text: str) -> dict:
        t = (text or "").strip()
        low = t.lower()

        # ① 空输入 → 闲聊
        if not low or not _VALID_CHAR.search(low):
            return self._verdict("chitchat", 1.0, "无有效内容", "rule")

        # ② 寒暄整句 → 闲聊（先于路由打分，避免「你好」被默认路由吃掉）
        for p in _CHITCHAT_PATTERNS:
            if re.match(p, low):
                return self._verdict("chitchat", 0.9, f"命中寒暄模式: {p}", "rule")

        # ③ 路由关键词打分（长词/高权重词更能代表意图）
        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        for route, words in self.routes.items():
            matched = [(w, wt) for w, wt in words.items() if w in low]
            if matched:
                scores[route] = sum(wt for _, wt in matched)
                hits[route] = [w for w, _ in matched]

        if scores:
            best = max(scores, key=lambda r: (scores[r], -list(self.routes).index(r)))
            total = sum(scores.values()) or 1.0
            share = scores[best] / total
            reason = f"关键词命中 {hits[best]}（{scores[best]:g}/{total:g}）"
            if share >= self.rule_conf or len(scores) == 1:
                return self._verdict(best, share, reason, "rule")
            # 多路僵持：有 LLM 就让它定，没有就按最高分（注册顺序兜底）
            if self.llm_classifier is None:
                return self._verdict(best, share, reason + "｜多路僵持，按最高分", "rule")
            llm = self._ask_llm(t)
            if llm is not None:
                return llm
            return self._verdict(best, share, reason + "｜LLM 不可用，按最高分", "rule")

        # ④ 规则无命中 → LLM 兜底
        if self.llm_classifier is not None:
            llm = self._ask_llm(t)
            if llm is not None:
                return llm

        # ⑤ 保守兜底：默认路由（多数场景 = 主线任务），不猜
        return self._verdict(self.default_route, 0.3,
                             "规则未命中 → 默认路由（保守兜底）", "fallback")

    # ---- LLM 兜底：异常即降级 ----
    def _ask_llm(self, text: str) -> dict | None:
        labels = list(self.routes) + ["chitchat"]
        try:
            out = self.llm_classifier(text, labels)
        except Exception as exc:                    # 分拣器故障不能拖垮主流程
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if isinstance(out, dict):
            route = out.get("route") or out.get("intent")
            conf = float(out.get("confidence", 0.6))
            reason = out.get("reason", "LLM 分拣")
        else:
            route, conf, reason = out, 0.6, "LLM 分拣"
        if route not in labels:
            self.last_error = f"LLM 返回未知路由: {route!r}"
            return None
        return self._verdict(str(route), conf, f"{reason}（{route}）", "llm")

    # ---- 统一返回结构：intent 保持 task/chitchat 语义，兼容编排器分拣直通 ----
    def _verdict(self, route: str, confidence: float,
                 reason: str, level: str) -> dict:
        return {
            "intent": "chitchat" if route == "chitchat" else "task",
            "route": route,
            "confidence": round(float(confidence), 3),
            "reason": reason,
            "level": level,          # rule | llm | fallback —— 便于统计规则覆盖率
        }

    # ---- 调试辅助 ----
    def describe(self) -> str:
        lines = [f"IntentRouter（默认路由 {self.default_route}，"
                 f"规则置信阈值 {self.rule_conf:g}，"
                 f"LLM 兜底 {'已启用' if self.llm_classifier else '未启用'}）"]
        for route, words in self.routes.items():
            top = sorted(words.items(), key=lambda kv: -kv[1])[:5]
            lines.append(f"  - {route}: " + "、".join(w for w, _ in top))
        return "\n".join(lines)
