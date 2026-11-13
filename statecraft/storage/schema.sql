-- Statecraft 持久层 schema（P12）
--
-- 三条纪律写在这里，后面所有代码都遵守：
-- 1. reviews / findings / clean_claims 是**真相源**，其余（findings_fts）是派生视图；
-- 2. 派生视图任何时候都能从真相源重建，不做增量修复；
-- 3. 主键一律显式给出（内容哈希），不依赖 AUTOINCREMENT —— 幂等是设计出来的，
--    不是靠调用方保证的。

-- ---------------------------------------------------------------- 审查批次
CREATE TABLE IF NOT EXISTS reviews (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    repo_root          TEXT NOT NULL,
    scope              TEXT NOT NULL,                 -- JSON 数组：本次审了哪些文件
    plan               TEXT,                          -- 规划者原文（可空：Blocked 时可能没写完）
    status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','executing','reviewed','done','blocked')),
    -- 三个统计列是派生值，finish_review() 时按 findings 重算。
    -- 落列是为了「列表页不聚合」，代价是必须记得重算 —— recount() 负责。
    findings_total     INTEGER NOT NULL DEFAULT 0,
    findings_confirmed INTEGER NOT NULL DEFAULT 0,
    findings_disputed  INTEGER NOT NULL DEFAULT 0,
    review_model       TEXT,                          -- 用的哪个模型（换模型后可比）
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);

-- ---------------------------------------------------------------- 单条问题
CREATE TABLE IF NOT EXISTS findings (
    id          TEXT PRIMARY KEY,                     -- sha1(review_id|file|line)[:16]
    review_id   TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file        TEXT NOT NULL,
    line        INTEGER NOT NULL CHECK (line > 0),
    severity    TEXT NOT NULL CHECK (severity IN ('high','medium','low')),
    category    TEXT NOT NULL,
    message     TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    suggestion  TEXT,
    status      TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','confirmed','disputed','dismissed')),
    form_ok     INTEGER NOT NULL DEFAULT 0,           -- 六条形式审是否全过
    evidence_ok INTEGER NOT NULL DEFAULT 0,           -- 证据逐字符核对结果
    audit_ok    INTEGER NOT NULL DEFAULT 0,           -- 审计日志查证结果
    critic_note TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file, status);
CREATE INDEX IF NOT EXISTS idx_findings_sev  ON findings(severity, status);
CREATE INDEX IF NOT EXISTS idx_findings_rev  ON findings(review_id);

-- ---------------------------------------------------------------- 清白声明
-- 「审过且判定无问题」必须留证据，否则「没报」和「没看」无法区分。
-- 真机实测里正是靠它才把「读完整份 Flask 一条不报」变成可追问的对象。
CREATE TABLE IF NOT EXISTS clean_claims (
    review_id  TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    file       TEXT NOT NULL,
    agent      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (review_id, file)
);

-- ---------------------------------------------------------------- 派生检索索引
-- 代码审查的典型查询是按符号名（average / process / Session），英文词面就够用，
-- 所以 FTS5 不折腾 CJK 分词：中文查询走 LIKE 兜底，见 ReviewStore.search()。
CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(
    finding_id UNINDEXED,
    text
);

-- ---------------------------------------------------------------- 通用记忆（SqliteMemory）
CREATE TABLE IF NOT EXISTS memory_items (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'shared',        -- shared / agent / task
    task_id    TEXT,
    metadata   TEXT NOT NULL DEFAULT '{}',            -- JSON：检索前置过滤就在这里
    hash       TEXT NOT NULL,                         -- md5(text)：确定性去重
    deleted    INTEGER NOT NULL DEFAULT 0,            -- 软删，物理数据保留
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_hash ON memory_items(hash, deleted);

CREATE TABLE IF NOT EXISTS memory_history (
    memory_id TEXT NOT NULL,
    event     TEXT NOT NULL,                          -- ADD / UPDATE / DELETE
    old       TEXT,
    new       TEXT,
    at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_mid ON memory_history(memory_id);
