"""statecraft.eval —— 黄金集评估体系（P9 轻量版）。

三层评估对象（对应路线图第 6 节）：
- L1 检索质量：Recall@K / MRR / Hit@K
- L2 审核质量：混淆矩阵 Precision / Recall / F1（reject 为正类）
- L3 端到端：完成率 / 必经状态覆盖
黄金集为 JSONL（随代码入库、可版本化、可一键回归）：
    python -m statecraft.eval.runner
"""
