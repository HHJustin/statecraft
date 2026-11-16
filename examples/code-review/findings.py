"""兼容壳 —— 实现已经搬进包里（`statecraft.review.findings`）。

为什么保留这个文件：示例目录里 `import findings` 的写法（README、测试、
以及直接 `python review.py` 的人）不能一夜之间失效。但它只是 re-export，
**不要在这里加新逻辑** —— 示例和 CLI 共用一份实现是刻意的：
审查的正确性（形式审、覆盖闸门、落库时机）只能有一个出处。

新代码请直接 `from statecraft.review.findings import ...`。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:                 # 允许 python findings.py 单独跑
    sys.path.insert(0, str(_ROOT))

from statecraft.review.findings import (  # noqa: E402,F401
    CATEGORIES, CHECK_NAMES, SEVERITIES, Finding, ReviewSession, _norm,
    audit_ok, make_tools,
)

__all__ = ["Finding", "ReviewSession", "make_tools", "audit_ok",
           "SEVERITIES", "CATEGORIES", "CHECK_NAMES"]
