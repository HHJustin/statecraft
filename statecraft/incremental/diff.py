"""git diff 解析 —— 增量审查的入口（P14）。

为什么不用 `git diff --numstat` 或 `git status`：
它们只说「哪个文件变了」，不说「第几行变了」。审查要落到行，就必须解析 hunk。

**为什么是 `--unified=0`**：默认的 3 行上下文会把相邻函数一起拖进来 ——
「改了第 40 行」被放大成「改了 37~43 行」，跨到上一个函数就白审了。
代价是纯删除的 hunk 会变成 `@@ -12,2 +11,0 @@`（新侧长度为 0），
这种 hunk 在新文件里没有对应行，见 `FileChange.new_ranges` 的注释。

三个真实会踩的坑（都有用例）：
1. `@@ -1 +1 @@`：count 省略时默认是 **1**，不是 0。按 0 算会把单行改动
   解析成空区间，整条 diff 静默丢失。
2. `new file mode` / `deleted file mode` / `rename from ...` / `rename to ...`：
   文件名不听 `---`/`+++` 的（分别是 `/dev/null` 和旧名）。只从 `+++` 取路径，
   重命名会被记成旧路径，之后 read_file 直接 FileNotFound。
3. 中文/空格文件名会被 git 加引号转义（`core.quotepath=true` 的默认行为），
   所以调 git 时显式关掉它，解析端再兜一层 unquote —— 两层都做，
   因为关掉 quotepath 只影响「输出中文」，空格仍然会被引号包起来。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_ESCAPE = {"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\", "r": b"\r"}


class GitError(RuntimeError):
    """git 不可用 / 不是仓库 / 版本号不存在。CLI 直接把它打成一句人话。"""


def _unquote(path: str) -> str:
    """还原 git 的 C 风格转义（`"a/\\346\\226\\207.py"` → `a/文.py`）。"""
    p = (path or "").strip()
    if len(p) < 2 or not (p.startswith('"') and p.endswith('"')):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":                     # \346 八进制字节
                digits = body[i + 1:i + 4]
                out += bytes([int(digits, 8) & 0xFF])
                i += 1 + len(digits)
                continue
            out += _ESCAPE.get(nxt, nxt.encode("utf-8"))
            i += 2
            continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _strip_ab(path: str) -> str:
    p = _unquote(path.split("\t")[0])                # `--- a/x\t(时间戳)`
    for pre in ("a/", "b/"):
        if p.startswith(pre):
            return p[len(pre):]
    return p


@dataclass(frozen=True)
class Hunk:
    """一段连续变更。行号都是**新文件**侧（旧侧的只留作排查用）。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str = ""                                # `@@` 后面的函数上下文，仅参考

    @property
    def is_pure_deletion(self) -> bool:
        return self.new_count == 0


@dataclass
class FileChange:
    path: str = ""                                  # 新侧路径；删除的文件退回旧路径
    old_path: str = ""
    status: str = "modified"                        # added/modified/deleted/renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def reviewable(self) -> bool:
        """能进审查的：新增/修改/重命名，且不是二进制。

        删除的文件不审 —— 没有内容可读，硬审只会得到 FileNotFound。
        """
        return self.status != "deleted" and not self.binary and bool(self.path)

    @property
    def new_ranges(self) -> list[tuple[int, int]]:
        """新侧的变更行区间（闭区间，1-based）。

        纯删除的 hunk 在新文件里没有行，只能取删除点作为探针：
        `@@ -12,2 +11,0 @@` 表示「新文件第 11 行之后删了 2 行」，
        探针取第 11 行 —— 也就是删除点上方的那个函数。
        这一定不总是正确答案（整个函数被删时，上方已经是另一个函数了），
        所以调用方要把「纯删除 hunk」单独报出来，不能假装映射是精确的。
        """
        out: list[tuple[int, int]] = []
        for h in self.hunks:
            if h.new_count > 0:
                out.append((h.new_start, h.new_start + h.new_count - 1))
            else:
                probe = max(1, h.new_start)
                out.append((probe, probe))
        return out

    @property
    def pure_deletions(self) -> list[Hunk]:
        return [h for h in self.hunks if h.is_pure_deletion]

    def describe(self) -> str:
        tag = {"added": "新增", "deleted": "删除", "renamed": "重命名"}.get(
            self.status, "修改")
        extra = "（二进制，跳过）" if self.binary else ""
        return f"[{tag}] {self.path}｜{len(self.hunks)} 段变更{extra}"


def parse_diff(text: str) -> list[FileChange]:
    """把 `git diff --unified=0` 的文本解析成 FileChange 列表。纯函数，好测。"""
    changes: list[FileChange] = []
    cur: FileChange | None = None

    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            idx = rest.rfind(" b/")
            old = _strip_ab(rest[:idx]) if idx > 0 else ""
            new = _strip_ab(rest[idx + 1:]) if idx > 0 else ""
            cur = FileChange(path=new, old_path=old)
            changes.append(cur)
            continue
        if cur is None:                             # 文件头之前的杂项
            continue

        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
            cur.path = cur.path or cur.old_path
        elif line.startswith("rename from "):
            cur.old_path = _unquote(line[len("rename from "):])
            cur.status = "renamed"
        elif line.startswith("rename to "):
            cur.path = _unquote(line[len("rename to "):])
            cur.status = "renamed"
        elif line.startswith("--- "):
            v = line[4:].strip()
            if v != "/dev/null" and not cur.old_path:
                cur.old_path = _strip_ab(v)
        elif line.startswith("+++ "):
            v = line[4:].strip()
            if v == "/dev/null":
                cur.status = "deleted"
                cur.path = cur.old_path or cur.path
            else:
                cur.path = _strip_ab(v)
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith("@@ "):
            m = _HUNK_RE.match(line)
            if m:
                cur.hunks.append(Hunk(
                    old_start=int(m.group(1)),
                    old_count=int(m.group(2)) if m.group(2) is not None else 1,
                    new_start=int(m.group(3)),
                    new_count=int(m.group(4)) if m.group(4) is not None else 1,
                    header=(m.group(5) or "").strip(),
                ))
    # 删除的文件：path 最终退回旧名，便于报告里说清「删了哪个文件」
    for c in changes:
        if c.status == "deleted" and not c.path:
            c.path = c.old_path
    return changes


def _run_git(repo, args: list[str], *, timeout: float = 120.0) -> str:
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise GitError("找不到 git 命令：增量审查需要本机安装 git") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git 超时（{timeout:.0f}s）：{' '.join(args)}") from e
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise GitError(f"git {' '.join(args)} 失败：{msg[0] if msg else '未知错误'}")
    return proc.stdout


def _untracked(repo: str | Path, paths: tuple[str, ...]) -> list[FileChange]:
    """未跟踪的新文件 —— `git diff` **永远不会**显示它们。

    这是增量审查最容易静默失效的地方：`git init` 之后新建一个文件、
    还没 `git add`，`git diff HEAD` 是空的，审查于是报「没有可审的变更」。
    用户看到的是「我明明写了两百行，它说什么都没有」，而且没有任何报错。

    所以新文件单独走 `ls-files --others`，并合成一段「整文件新增」的 hunk。
    """
    root = Path(repo)
    out = _run_git(repo, ["ls-files", "--others", "--exclude-standard",
                          "--", *(paths or ("*.py",))])
    changes: list[FileChange] = []
    for rel in (x for x in out.splitlines() if x.strip()):
        f = root / rel
        if not f.is_file():
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        ch = FileChange(path=rel, old_path=rel, status="added")
        if b"\x00" in data[:4096]:                  # 二进制（pyc、图片）不审
            ch.binary = True
            changes.append(ch)
            continue
        n = len(data.decode("utf-8", "replace").splitlines()) or 1
        ch.hunks.append(Hunk(old_start=0, old_count=0, new_start=1,
                             new_count=n, header="（未跟踪的新文件）"))
        changes.append(ch)
    return changes


def collect_diff(repo: str | Path = ".", base: str = "HEAD", head: str | None = None,
                 paths: tuple[str, ...] = ("*.py",),
                 three_dot: bool = False) -> list[FileChange]:
    """调 git 拿变更并解析。

    - `base="HEAD"`（默认）：工作区 + 暂存区相对 HEAD 的改动（本地开发最常用）；
    - `head` 给值：两个提交之间比，如 `base="HEAD~3", head="HEAD"`；
    - `three_dot=True`：`A...B`，按 merge-base 比 —— CI 上比较分支时这才是
      想要的语义，否则主干后来的提交会被算成「你改的」。
    """
    if head is None:
        spec = [base]
    elif three_dot:
        spec = [f"{base}...{head}"]
    else:
        spec = [base, head]
    args = ["diff", "--unified=0", "--no-color", "--no-ext-diff",
            "--find-renames", *spec, "--", *(paths or ("*.py",))]
    changes = parse_diff(_run_git(repo, args))
    if head is None:
        # 只看「工作区 vs base」时才补未跟踪文件；两个提交之间比没有这个概念。
        seen = {c.path for c in changes}
        changes += [c for c in _untracked(repo, paths) if c.path not in seen]
    return changes


def worktree_differs(repo: str | Path, ref: str,
                    paths: tuple[str, ...] = ("*.py",)) -> bool:
    """工作区与 `ref` 是否有差异（`git diff --quiet` 的退出码语义）。

    用在「审两个历史提交」的场景：行区间来自提交 B，而函数是按**工作区文件**
    解析的。两者不一致时行号会错位，映射到错误的函数 —— 静默错比报错更糟。
    """
    cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false",
           "diff", "--quiet", "--no-ext-diff", ref, "--", *(paths or ("*.py",))]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120).returncode == 1
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def is_git_repo(path: str | Path = ".") -> bool:
    try:
        out = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    except GitError:
        return False
    return out.strip() == "true"


def repo_root(path: str | Path = ".") -> Path:
    """变更路径是相对仓库根的 —— 审查根目录必须对齐，否则所有相对路径都错。"""
    return Path(_run_git(path, ["rev-parse", "--show-toplevel"]).strip()).resolve()
