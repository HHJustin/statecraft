"""问答层（P13）：审查结果 → 可溯源的一句话答案。

三层，各管一件事：

    router.py   这句话该走 SQL 还是走召回（规则，不需要模型）
    query.py    执行查询（结构化走 SQL 聚合，语义走词面 + 可选三路召回）
    answer.py   组装答案（强制引用、引用回查、空结果不调模型）

分层的用处不是「好看」，是**每一层都能单独测**：路由错了不用启动数据库，
SQL 错了不用调模型，引用校验错了不用造一份完美回答。
"""
from .answer import Answer, Citation, CodebaseQA
from .query import QueryEngine, QueryResult, extract_slots
from .router import decide, route

__all__ = ["Answer", "Citation", "CodebaseQA", "QueryEngine", "QueryResult",
           "extract_slots", "decide", "route"]
