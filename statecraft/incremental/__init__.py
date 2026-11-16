"""incremental —— 增量审查（P14）：只审改动的函数，没改的不审。

三个部件，职责单一：

    diff.py    git diff --unified=0 的解析（行区间从哪来）
    units.py   AST 映射 + 函数体哈希（行区间翻译成「哪个函数」）
    cache.py   函数级结论缓存（同样的函数体不审第二遍）
    plan.py    上面三者的编排，产出「这次审什么」的 ReviewPlan

为什么不做「按文件跳过」：一个 800 行的文件里改了一行，按文件跳过等于
要么全审（浪费 799 行）要么全不审（漏掉那行）。按函数才是审查的天然粒度。
"""
from __future__ import annotations

from .cache import CachedUnit, UnitCache
from .diff import FileChange, GitError, Hunk, collect_diff, is_git_repo, parse_diff, repo_root
from .plan import ReviewPlan, plan_for_files, plan_from_diff, plan_full
from .units import (CodeUnit, body_hash, changed_units, iter_python_files,
                    read_units, select_units, unit_at, units_of_source)

__all__ = [
    "Hunk", "FileChange", "GitError", "parse_diff", "collect_diff", "is_git_repo",
    "repo_root", "CodeUnit", "body_hash", "units_of_source", "select_units",
    "unit_at", "changed_units", "read_units", "iter_python_files",
    "UnitCache", "CachedUnit", "ReviewPlan", "plan_from_diff", "plan_for_files",
    "plan_full",
]
