"""评估指标（纯函数，可独立单测）。"""


def hit_at_k(ranked: list, relevant: set, k: int) -> float:
    """Hit@K：前 K 个结果里是否命中任一相关项。"""
    return 1.0 if any(x in relevant for x in ranked[:k]) else 0.0


def recall_at_k(ranked: list, relevant: set, k: int) -> float:
    """Recall@K：相关项被召回的比例（L1 核心指标）。"""
    if not relevant:
        return 1.0
    return len(relevant & set(ranked[:k])) / len(relevant)


def mrr(ranked: list, relevant: set) -> float:
    """MRR：第一个相关结果排名倒数的均值分量。"""
    for i, x in enumerate(ranked, 1):
        if x in relevant:
            return 1.0 / i
    return 0.0


def confusion(y_true: list[str], y_pred: list[str], positive: str = "reject") -> dict:
    """混淆矩阵 + Precision/Recall/F1（审核场景：reject 为正类）。

    - fp（误杀）：应 approve 却被 reject
    - fn（漏放）：应 reject 却被 approve —— 审核场景更危险
    """
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == positive and p == positive)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t != positive and p == positive)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == positive and p != positive)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t != positive and p != positive)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1,
            "false_kill_rate": fp / (fp + tn) if (fp + tn) else 0.0,
            "false_pass_rate": fn / (fn + tp) if (fn + tp) else 0.0}
