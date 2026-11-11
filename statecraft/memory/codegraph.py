"""代码符号图 —— 混合检索的第三路（结构关系加成）。

为什么代码场景的第三路不该照搬「通用实体图」：
- 代码的结构是**显式**的：函数、类、参数、异常、调用关系 —— 标准库 `ast`
  直接给出来，不需要用 NLP 去猜（Mem0 抽业务实体还得靠规则/LLM）
- 注释生成最需要的信息恰恰是结构关系：
    被谁调用   → 注释该写清前置条件
    调用了谁   → 注释该写清副作用 / 异常来源
    属于哪个模块 → 注释该写清职责边界

三个部件：
- `SymbolNode` ：符号节点（名称/类型/参数/docstring/异常/出入边）
- `CodeGraph`  ：ast 解析 + 建图 + 生成可入库的「符号上下文」文本
- `SymbolIndex`：符号 → 记忆条目 反查表，给混合检索提供第三路分数
    直接命中符号 = 1.0
    命中邻居（caller / callee）= 0.5
    否则 = 0

用法：
    graph = CodeGraph()
    graph.add_source(source, module="sample.py")
    entries = graph.to_memory_entries()          # [(text, metadata)]
    mem = HybridMemory(embedder=..., graph_index=SymbolIndex(graph))
"""
import ast
import re
from dataclasses import dataclass, field

_KIND_TAG = {"class": "[类] ", "async_function": "[async] "}


@dataclass
class SymbolNode:
    """一个代码符号（函数/方法/类）及其结构关系。"""

    name: str                       # 全名，如 "order_flow.apply_coupon"
    kind: str                       # function / async_function / method / class
    module: str
    lineno: int
    args: list[str] = field(default_factory=list)
    docstring: str | None = None
    raises: list[str] = field(default_factory=list)     # 可能抛出的异常类型
    callees: list[str] = field(default_factory=list)    # 它调用了谁（出边）
    callers: list[str] = field(default_factory=list)    # 谁调用了它（入边，建图后回填）

    @property
    def has_doc(self) -> bool:
        return bool((self.docstring or "").strip())

    @property
    def short(self) -> str:
        return self.name.rsplit(".", 1)[-1]

    def to_text(self, with_edges: bool = False) -> str:
        """生成可入库的符号上下文文本。

        默认 **不写入调用关系**，这是刻意的职责分工：
            文本管「我是谁」—— 签名、位置、docstring、可能抛出的异常
            图管「我和谁有关系」—— callers / callees 边
        把关系塞进文本会让词面/向量检索「假装」具备关系能力，
        反而掩盖第三路的价值；分开之后关系型查询只能由图回答。

        with_edges=True 仅供人工阅读/调试（会带上出入边）。
        """
        head = self.name
        if self.args:
            head = f"{head}({', '.join(self.args)})"
        tag = _KIND_TAG.get(self.kind, "")
        parts = [f"{tag}{head}｜{self.module}:{self.lineno}"]
        parts.append("无 docstring" if not self.has_doc
                     else f"docstring: {self.docstring.strip()[:120]}")
        if self.raises:
            parts.append(f"可能抛出 {', '.join(self.raises)}")
        if with_edges:
            if self.callees:
                parts.append(f"调用 {', '.join(self.callees)}")
            if self.callers:
                parts.append(f"被 {', '.join(self.callers)} 调用")
        return "｜".join(parts)


def _calls_of(node) -> set[str]:
    """函数体内所有调用的名字（Name.id / Attribute.attr）。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


def _raises_of(node) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            exc = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
            if isinstance(exc, ast.Name):
                out.add(exc.id)
            elif isinstance(exc, ast.Attribute):
                out.add(exc.attr)
    return out


def _args_of(node) -> list[str]:
    return [a.arg for a in node.args.args if a.arg not in ("self", "cls")]


class CodeGraph:
    """多文件代码符号图（ast 建图，零依赖）。"""

    def __init__(self):
        self.symbols: dict[str, SymbolNode] = {}
        self.modules: list[str] = []

    # ---- 建图 ----
    def add_source(self, source: str, module: str = "<src>") -> "CodeGraph":
        tree = ast.parse(source)
        self.modules.append(module)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                self._add_symbol(node, module, kind="class", name=node.name)
                for sub in node.body:                     # 类内方法（一层）
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._add_symbol(sub, module, kind="method",
                                         name=f"{node.name}.{sub.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = ("async_function"
                        if isinstance(node, ast.AsyncFunctionDef) else "function")
                self._add_symbol(node, module, kind=kind, name=node.name)
        self._link_callers()
        return self

    def _add_symbol(self, node, module: str, kind: str, name: str) -> None:
        self.symbols[f"{module}::{name}"] = SymbolNode(
            name=name, kind=kind, module=module, lineno=node.lineno,
            args=_args_of(node), docstring=ast.get_docstring(node),
            raises=sorted(_raises_of(node)), callees=sorted(_calls_of(node)),
        )

    def _lookup(self, short_name: str) -> SymbolNode | None:
        for node in self.symbols.values():
            if node.name == short_name or node.short == short_name:
                return node
        return None

    def _link_callers(self) -> None:
        """回填入边：只保留指向本图内已知符号的调用（滤掉 print/split 等）。"""
        known = {n.short: n for n in self.symbols.values()}
        for src in self.symbols.values():
            kept = []
            for callee in src.callees:
                short = callee.rsplit(".", 1)[-1]
                if short in known:
                    kept.append(known[short].name)
                    if src.name not in known[short].callers:
                        known[short].callers.append(src.name)
            src.callees = sorted(set(kept))
            src.callers = sorted(set(src.callers))

    # ---- 查询 ----
    def symbol_names(self) -> set[str]:
        return {n.name for n in self.symbols.values()}

    def get(self, name: str) -> SymbolNode | None:
        return self._lookup(name)

    def neighbors(self, name: str) -> set[str]:
        """一跳邻居：callers ∪ callees（图的加成就是靠它扩散）。"""
        node = self._lookup(name)
        if node is None:
            return set()
        return set(node.callers) | set(node.callees)

    def missing_doc(self, module: str | None = None) -> list[SymbolNode]:
        """缺注释的函数/方法（planner 做计划的输入，比让 LLM 自己扫文件更可靠）。

        module 不为空时只统计该文件的符号（分批处理大项目时用）。
        """
        return [n for n in self.symbols.values()
                if n.kind != "class" and not n.has_doc
                and (module is None or n.module == module)]

    def to_memory_entries(self) -> list[tuple[str, dict]]:
        """每个符号生成一条可入库的记忆（metadata 带 symbol，供 SymbolIndex 反查）。"""
        return [(n.to_text(), {"type": "code_symbol", "symbol": n.name,
                               "module": n.module, "kind": n.kind,
                               "has_doc": n.has_doc})
                for n in self.symbols.values()]

    def summary(self) -> str:
        total = len(self.symbols)
        miss = len(self.missing_doc())
        return f"{len(self.modules)} 个文件 / {total} 个符号 / {miss} 个缺注释"


class SymbolIndex:
    """符号 → 记忆条目 反查表：混合检索的第三路（图加成）。"""

    def __init__(self, graph: CodeGraph):
        self.graph = graph
        self._links: dict[str, set[str]] = {}     # symbol -> {memory_id}

    # ---- 建链 ----
    def link(self, memory_id: str, symbols) -> None:
        for sym in symbols:
            sym = (sym or "").strip()
            if sym:
                self._links.setdefault(sym, set()).add(memory_id)

    def link_from_metadata(self, memory_id: str, metadata: dict | None) -> None:
        md = metadata or {}
        for key in ("symbol", "symbols"):
            val = md.get(key)
            if isinstance(val, str):
                self.link(memory_id, [val])
            elif isinstance(val, (list, tuple, set)):
                self.link(memory_id, val)

    # ---- 查询侧 ----
    def query_symbols(self, query: str) -> set[str]:
        """从 query 里抽出命中的已知符号。

        用 ASCII 词边界（`(?<![A-Za-z0-9_])`）而非 `\\b`：
        中文与标识符相邻时 `\\b` 不成立（`\\w` 含中文），会漏命中。
        """
        q = query or ""
        if not q:
            return set()
        hits: set[str] = set()
        for name in self.graph.symbol_names():
            for cand in {name, name.rsplit(".", 1)[-1]}:
                pattern = rf"(?<![A-Za-z0-9_]){re.escape(cand)}(?![A-Za-z0-9_])"
                if re.search(pattern, q):
                    hits.add(name)
                    break
        return hits

    def score(self, query_symbols: set[str], memory_id: str) -> float:
        """第三路分数：直接命中 = 1.0；命中一跳邻居 = 0.5；否则 0。"""
        if not query_symbols or not self._links:
            return 0.0
        best = 0.0
        for sym in query_symbols:
            if memory_id in self._links.get(sym, ()):
                return 1.0
            for nb in self.graph.neighbors(sym):
                if memory_id in self._links.get(nb, ()):
                    best = 0.5
        return best
