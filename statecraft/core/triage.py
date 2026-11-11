"""意图识别 / 分拣节点（源自 Edict 的「太子分拣」：区分闲聊与任务）。

设计纪律（呼应路线图「能确定性就别用 LLM」原则）：
- 默认 RuleTriage：纯正则 + 关键词，零成本、可离线、可代码断言
- 保留 TriageBase 协议：复杂语义分拣可换 LLM/小模型实现，构造时注入即可
- 保守策略：拿不准一律按「任务」处理 —— 误杀真任务的代价
  远大于让流水线多跑一次（漏放 < 误杀）
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
    """分拣器协议：实现 classify 即可替换（LLM 分拣 / 小模型分类器等）。"""

    @abstractmethod
    def classify(self, text: str) -> dict:
        """返回 {"intent": "task" | "chitchat", "reason": str}。"""
        raise NotImplementedError


class RuleTriage(TriageBase):
    """规则分拣器（默认）：确定性、零依赖、离线可测。"""

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
