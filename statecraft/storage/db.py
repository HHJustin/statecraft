"""SQLite 连接与建表 —— 持久层的地基。

为什么是 SQLite（而不是 JSONL / Postgres / 向量库），见路线图 §4.1：
标准库自带、单文件、能 SQL 聚合、能 FTS5。这个项目要的是「能查」，
不是「高并发」；上 Postgres 会为一个单机工具引入运维成本。

三个实现上的取舍：
1. **每连接设 PRAGMA**：`foreign_keys` 是连接级开关，不设等于没有外键；
   WAL 是库级但也要每连接确认一次。
2. **用 `PRAGMA user_version` 做版本号**，不额外建 migrations 表 ——
   这么小的 schema 不需要 Alembic，但必须能识别「库比代码旧/新」。
3. **只自动执行「纯新增」的迁移**：每个版本必须在 `_ADDITIVE_STEPS` 里
   登记一句「这一步加了什么、为什么是纯新增」。没登记的版本一律拒绝，
   因为写在半路的改列/搬数据迁移会让库停在既不是旧版也不是新版的中间态
   —— 那种情况必须交给人，代码不许自作聪明。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

# 版本 → (这一步新增了什么, 是否纯新增)。
# 「纯新增」= 只 CREATE TABLE/INDEX IF NOT EXISTS，重跑 schema.sql 对已有
# 数据零副作用。任何需要改列、搬数据、收紧约束的版本都不许登记成 True。
_ADDITIVE_STEPS: dict[int, tuple[str, bool]] = {
    2: ("unit_cache：增量审查的函数体哈希缓存（纯新增表）", True),
}
DEFAULT_DB = "statecraft.db"

_SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# 已建过表的库（进程内缓存，省掉每次连接都跑一遍 DDL）。
# 只对文件库有效：:memory: 每次连接都是新库，必须重建。
_initialized: set[str] = set()


def _resolve(db_path) -> str:
    """`":memory:"` 原样返回；文件路径转**规范**绝对路径并建父目录。

    必须无条件 `resolve()`：Windows 的 `%TEMP%` 常是 8.3 短名
    （`C://Users//CHENGX~1//...`），同一个文件于是有两个字符串写法。
    用它当 `_initialized` 的键，就会出现「测试拿长名去清缓存清不掉、
    生产里换一种写法打开就重跑一遍 DDL」这类只在特定机器上复现的问题。
    """
    if str(db_path) == ":memory:":
        return ":memory:"
    p = Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p.resolve())


def connect(db_path=DEFAULT_DB, *, init: bool = True) -> sqlite3.Connection:
    """打开（必要时创建）数据库。

    默认 `check_same_thread=True`：审查是串行的，工具处理器都在同一线程里跑。
    真要从别的线程用，会得到一个明确的 ProgrammingError，而不是偶发的
    数据损坏或 "database is locked" —— 报错比静默更安全。
    """
    path = _resolve(db_path)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")     # 读写不互相阻塞
        conn.execute("PRAGMA synchronous = NORMAL")   # WAL 下够安全，且快
    if init and (path == ":memory:" or path not in _initialized
                 or conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
        # 多读一次 `PRAGMA user_version` 才敢跳过建表：`_initialized` 只是
        # 进程内记忆，管不了「这个文件在这期间被换掉/降级了」。一次 PRAGMA
        # 的代价换掉一整类「库看着是新的、其实还是旧表结构」的错。
        initialize(conn)
        if path != ":memory:":
            _initialized.add(path)
    return conn


def initialize(conn: sqlite3.Connection) -> int:
    """建表并返回当前 schema 版本。可重复调用（全部 IF NOT EXISTS）。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return SCHEMA_VERSION
    if version < SCHEMA_VERSION:
        return _migrate(conn, version)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库 schema 版本比代码新（{version} > {SCHEMA_VERSION}），"
            f"请升级 statecraft 而不是继续用旧代码写这个库"
        )
    return version


def _migrate(conn: sqlite3.Connection, from_version: int) -> int:
    """把旧库抬到当前版本，只允许纯新增。

    唯一动作是「重跑一遍 schema.sql + 抬版本号」。之所以敢这么做，是因为
    schema.sql 里每一条 DDL 都带 IF NOT EXISTS —— 这是可验证的，不是信仰：
    tests/test_storage.py 里有一条用例用 v1 库跑迁移，断言老数据一条不少。
    """
    for v in range(from_version + 1, SCHEMA_VERSION + 1):
        step = _ADDITIVE_STEPS.get(v)
        if not step or not step[1]:
            raise RuntimeError(
                f"数据库 schema 版本过旧（{from_version} < {SCHEMA_VERSION}），"
                f"但第 {v} 版没有登记成「纯新增」，不能自动迁移。"
                f"请手工迁移或删库重建。当前文件: {_db_of(conn)}"
            )
    conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return SCHEMA_VERSION


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """从 `findings` 重建 FTS 索引，返回重建条数。

    索引坏了不修，直接重建 —— 修复逻辑一定会有 bug，重建逻辑只有一条路径。
    """
    from .findings import SEARCH_TEXT_SQL

    conn.execute("DELETE FROM findings_fts")
    conn.execute(
        f"INSERT INTO findings_fts(finding_id, text) "
        f"SELECT id, {SEARCH_TEXT_SQL} FROM findings"
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM findings_fts").fetchone()[0]


def _db_of(conn: sqlite3.Connection) -> str:
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2] or ":memory:"
    except sqlite3.Error:
        pass
    return "?"
