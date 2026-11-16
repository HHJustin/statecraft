"""把「第几行变了」翻译成「哪个函数变了」—— P14 的核心那一步。

审查的天然单位是函数，不是行：模型看到半截函数会误判（缺上下文），
报告挂在半截函数上也没法路由给负责人。所以 diff 的行区间必须先映射到
AST 给的结构单位上。

四个决定，每个都有具体理由：

1. **嵌套单位一律取「最内层」**：改了 `class A: def f:` 里的第 10 行，
   单位是 `A.f`，不是 `A`。返回到最外层会把「改了一行」放大成「审整个类」，
   增量审查就退化成全文件审查。

2. **函数体的 `start` 要把装饰器算进去**：`ast` 的 `FunctionDef.lineno`
   指向 `def` 那一行，装饰器在它上面 —— 只改装饰器（比如去掉 `@lru_cache`）
   会映射到别的单位上去。用 `min(lineno, *decorator.lineno)`。

3. **哈希只覆盖函数体，且忽略行尾空白与空行**：函数往上挪 100 行、
   整体多两个空行，语义没变，不该重审。但**不忽略注释和缩进** ——
   注释是审查对象，缩进在 Python 里是语义。

4. **`<module>` 单位永远存在**：import / 全局常量 / 类属性这类改动不属于
   任何函数，硬塞给相邻函数是错的。它们归 `<module>`，照审。

解析失败（SyntaxError）不抛异常，退化成「整个文件一个单位」——
改坏语法的提交恰恰最该被审，这时说「解析不了，跳过」是最坏的选择。
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

MODULE_QUALNAME = "<module>"


@dataclass(frozen=True)
class CodeUnit:
    """一个可审查的单位（函数/方法/类/模块）。"""

    file: str                                   # 相对审查根目录的 posix 路径
    qualname: str
    kind: str                                  # function/async_function/method/class/module
    start: int                                 # 1-based 闭区间，含装饰器
    end: int
    body_hash: str
    header: str = ""                           # `def f(a, b):` 首行，给 prompt 用

    @property
    def span(self) -> tuple[int, int]:
        return self.start, self.end

    def contains(self, line: int) -> bool:
        return self.start <= line <= self.end

    def intersects(self, start: int, end: int) -> bool:
        return self.start <= end and self.end >= start

    def describe(self) -> str:
        tag = f"{self.kind} " if self.kind not in ("function", "method") else ""
        return f"{self.qualname}（{tag}{self.start}-{self.end}）"

    def as_dict(self) -> dict:
        return {"file": self.file, "qualname": self.qualname, "kind": self.kind,
                "start": self.start, "end": self.end, "hash": self.body_hash,
                "header": self.header}


def normalize_body(lines: list[str], start: int, end: int) -> str:
    """取 [start, end] 行（1-based）做归一化：去行尾空白、丢空行。"""
    chunk = lines[max(0, start - 1):end]
    return "\n".join(ln.rstrip() for ln in chunk if ln.strip())


def body_hash(qualname: str, lines: list[str], start: int, end: int) -> str:
    """`sha1(qualname + 归一化函数体)`。

    为什么把 qualname 也算进去：函数改名后函数体一字未变，但「这个名字
    的函数审过没有」的答案应该是「没有」—— 改名本身是一次变更。
    """
    raw = f"{qualname}\n{normalize_body(lines, start, end)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _end_of(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None)
    if end:
        return int(end)
    return max((getattr(n, "lineno", 1) for n in ast.walk(node)), default=1)


def _start_of(node: ast.AST) -> int:
    lineno = getattr(node, "lineno", 1)
    decos = [d.lineno for d in getattr(node, "decorator_list", [])]
    return min([lineno, *decos])


def units_of_source(source: str, file: str) -> list[CodeUnit]:
    """解析一份源码里的全部结构单位（含 `<module>`）。"""
    lines = source.splitlines()
    total = max(1, len(lines))
    module = CodeUnit(file=file, qualname=MODULE_QUALNAME, kind="module",
                      start=1, end=total,
                      body_hash=body_hash(MODULE_QUALNAME, lines, 1, total))
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 语法错误时不假装能切分：整个文件就是一个单位，让人去改
        return [module]

    out: list[CodeUnit] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start, end = _start_of(child), _end_of(child)
                if isinstance(child, ast.ClassDef):
                    kind = "class"
                elif prefix:
                    kind = "method"
                elif isinstance(child, ast.AsyncFunctionDef):
                    kind = "async_function"
                else:
                    kind = "function"
                out.append(CodeUnit(
                    file=file, qualname=name, kind=kind, start=start, end=end,
                    body_hash=body_hash(name, lines, start, end),
                    # header 取 `def` 那一行而不是 start：装饰器行对读者没信息量
                    header=lines[child.lineno - 1].strip()[:100] if lines else ""))
                walk(child, name + ".")
            else:
                walk(child, prefix)               # if/try 里的顶层函数也算

    walk(tree, "")
    return [module, *out]


def read_units(path: str | Path, file: str) -> list[CodeUnit]:
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return units_of_source(source, file)


def unit_at(units: list[CodeUnit], line: int) -> CodeUnit | None:
    """包含该行的最内层单位。"""
    hits = [u for u in units if u.contains(line)]
    if not hits:
        return None
    return min(hits, key=lambda u: (u.end - u.start, u.start))


_KIND_DEPTH = {"module": 0, "class": 1, "function": 2, "method": 2}


def _depth(u: CodeUnit) -> tuple[int, int]:
    """「谁在里面」的兜底判据，只在两个单位区间**完全相同**时才用得上。"""
    return (_KIND_DEPTH.get(u.kind, 2), u.qualname.count("."))


def _inside(inner: CodeUnit, outer: CodeUnit) -> bool:
    """inner 是否比 outer 更内层。区间相同时比种类深度，否则比包含。"""
    if inner is outer:
        return False
    if not (inner.start >= outer.start and inner.end <= outer.end):
        return False
    if (inner.start, inner.end) != (outer.start, outer.end):
        return True
    # 例子：`def inner(): return 1` 这种单行函数，模块与函数的区间都是 1..N。
    # 只按区间判断会把两者都选上 —— 同一段代码审两遍、结论重复入库。
    return _depth(inner) > _depth(outer)


def select_units(units: list[CodeUnit],
                 ranges: list[tuple[int, int]]) -> list[CodeUnit]:
    """把变更区间映射到单位，返回「最内层」的那些（去重、按位置排序）。

    判定「最内层」用包含关系而不是比区间大小：一段 diff 可能横跨两个函数，
    这时两个都是最内层；而一段 diff 落在方法里时，只有方法是最内层
    （类和模块都包含它，被排除）。
    """
    picked: dict[tuple[str, str], CodeUnit] = {}
    for start, end in ranges:
        if end < start:
            start, end = end, start
        covered = [u for u in units if u.intersects(start, end)]
        for u in covered:
            nested = any(_inside(v, u) for v in covered)
            if not nested:                        # 里面没有更细的单位 → 就是它
                picked[(u.file, u.qualname)] = u
    return sorted(picked.values(), key=lambda u: (u.file, u.start))


def changed_units(root: str | Path, ranges_by_file: dict[str, list[tuple[int, int]]]
                  ) -> dict[str, list[CodeUnit]]:
    """对每个有变更的文件算「要审哪些单位」。文件不存在（已删除）的直接跳过。"""
    root = Path(root)
    out: dict[str, list[CodeUnit]] = {}
    for file, ranges in ranges_by_file.items():
        units = read_units(root / file, file)
        if not units:
            continue
        picked = select_units(units, ranges) or [units[0]]
        out[file] = picked
    return out


def iter_python_files(root: str | Path,
                      patterns: tuple[str, ...] = ("*.py",)) -> list[str]:
    """全仓模式的候选文件：按 pattern 递归找，跳过明显的非源码目录。

    不用 .gitignore 解析：多引一个依赖不值当，而 `venv/`、`node_modules/`
    这些坑是固定的几个名字，写死比解析规则更可预测。
    """
    root = Path(root)
    skip = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
            "node_modules", "build", "dist", ".mypy_cache", ".pytest_cache",
            ".tox", "site-packages", ".workbuddy"}
    found: set[Path] = set()
    for pat in patterns or ("*.py",):
        for p in root.rglob(pat):
            parts = p.relative_to(root).parts[:-1]
            if any(part in skip or part.startswith(".") for part in parts):
                continue
            if p.is_file():
                found.add(p)
    return sorted(p.relative_to(root).as_posix() for p in found)
