"""schema 定义与迁移清单。

P0 只建"能存、能召回、能断言"真正用到的表：``memories`` / ``memory_links`` /
``episodes`` / ``events`` / ``arbitration``，加上 ``counter`` 与 ``schema_meta``。
设计文档 §8.1 里其余表（projects / sessions / leases / injection_log / feedback /
embeddings / snapshots / core_versions）等到 P1 / P4 用增量迁移加入——加表是纯增量，
不会牵动已有表结构。

不变量在这里用**数据库触发器**兜底（而不是只靠 Python 自觉）：绕开
``write_service`` 直接写库也会被拒，P0-2 的"断言会真红"才是真的。
"""

from __future__ import annotations

from dataclasses import dataclass

CURRENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Migration:
    """一条迁移：版本号 + 名字 + 幂等的 DDL 语句。"""

    version: int
    name: str
    statements: tuple[str, ...]


_P0_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # seq 分配器：与写入同事务自增，不依赖时间戳（§8.1 修正 2）
    """
    CREATE TABLE IF NOT EXISTS counter (
        name  TEXT PRIMARY KEY,
        value INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id             TEXT PRIMARY KEY,
        scope          TEXT NOT NULL CHECK (scope IN ('global', 'project', 'host')),
        project_id     TEXT,
        host           TEXT,
        session_id     TEXT,
        visibility     TEXT NOT NULL CHECK (visibility IN ('private', 'shared')),
        layer          TEXT NOT NULL CHECK (layer IN ('core', 'semantic', 'episodic')),
        kind           TEXT NOT NULL CHECK (
                           kind IN ('fact', 'preference', 'decision', 'relation',
                                    'procedure', 'event', 'handoff')),
        channel        TEXT,
        assertion_type TEXT NOT NULL CHECK (
                           assertion_type IN ('observation', 'decision', 'claim', 'hypothesis')),
        asserted_by    TEXT NOT NULL,
        stance         TEXT CHECK (stance IS NULL OR stance IN ('support', 'objection', 'neutral')),
        fact_key       TEXT,
        content        TEXT NOT NULL,
        importance     REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
        weight         REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
        confidence     REAL NOT NULL DEFAULT 0.6 CHECK (confidence BETWEEN 0 AND 1),
        status         TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'dormant', 'archived', 'deleted')),
        archived_reason TEXT CHECK (
                           archived_reason IS NULL
                           OR archived_reason IN ('decay', 'capacity', 'refuted')),
        valid_from     TEXT,
        valid_to       TEXT,
        created_at     TEXT NOT NULL,
        expired_at     TEXT,
        superseded_by  TEXT,
        seq            INTEGER NOT NULL,
        pinned         INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
        half_life_days REAL,
        heat           REAL NOT NULL DEFAULT 1.0,
        evidence       TEXT NOT NULL DEFAULT '[]',
        content_hash   TEXT NOT NULL,
        last_recalled  TEXT,
        recall_count   INTEGER NOT NULL DEFAULT 0,
        -- scope=global 时不应带 project_id（§8.1 作用域约定）
        CHECK (scope <> 'global' OR project_id IS NULL)
    )
    """,
    # INV2：seq 唯一且是唯一排序依据；INV1 的写入顺序在库层不可回退
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_memories_seq ON memories (seq)",
    "CREATE INDEX IF NOT EXISTS ix_memories_slot ON memories (scope, project_id, fact_key, status)",
    "CREATE INDEX IF NOT EXISTS ix_memories_dedup ON memories (content_hash, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_memories_pool ON memories (status, valid_to)",
    # INV3：expired_at 写入后不可改
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_expired_at_immutable
    BEFORE UPDATE OF expired_at ON memories
    FOR EACH ROW WHEN OLD.expired_at IS NOT NULL AND NEW.expired_at IS NOT OLD.expired_at
    BEGIN
        SELECT RAISE(ABORT, 'INV3: expired_at 写入后不可修改');
    END
    """,
    # INV3：语义事实不硬删（只有已进回收站 status=deleted 的才允许物理删除）
    """
    CREATE TRIGGER IF NOT EXISTS trg_memories_no_hard_delete
    BEFORE DELETE ON memories
    FOR EACH ROW WHEN OLD.layer = 'semantic' AND OLD.status <> 'deleted'
    BEGIN
        SELECT RAISE(ABORT, 'INV3: 语义事实不硬删，请先归档或进回收站');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_links (
        src          TEXT NOT NULL REFERENCES memories (id),
        dst          TEXT NOT NULL REFERENCES memories (id),
        type         TEXT NOT NULL CHECK (
                         type IN ('synapse', 'derived_from', 'contradicts',
                                  'supports', 'supersedes', 'part_of')),
        weight       REAL NOT NULL DEFAULT 1.0,
        co_activation REAL NOT NULL DEFAULT 0.0,
        last_fired   TEXT,
        PRIMARY KEY (src, dst, type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_links_dst ON memory_links (dst, type)",
    """
    CREATE TABLE IF NOT EXISTS episodes (
        id         TEXT PRIMARY KEY,
        session_id TEXT,
        topic      TEXT,
        started_at TEXT NOT NULL,
        ended_at   TEXT,
        heat       REAL NOT NULL DEFAULT 1.0,
        summary    TEXT,
        visibility TEXT NOT NULL DEFAULT 'shared'
                       CHECK (visibility IN ('private', 'shared')),
        role       TEXT
    )
    """,
    # events 是 append-only 审计底账（M6）
    """
    CREATE TABLE IF NOT EXISTS events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        seq     INTEGER NOT NULL,
        ts      TEXT NOT NULL,
        host    TEXT,
        session TEXT,
        type    TEXT NOT NULL,
        target  TEXT,
        payload TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_events_seq ON events (seq)",
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_no_update
    BEFORE UPDATE ON events
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'events 是 append-only，不允许 UPDATE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
    BEFORE DELETE ON events
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'events 是 append-only，不允许 DELETE');
    END
    """,
    # 仲裁队列（INV5 的 C 态需要它落库）
    """
    CREATE TABLE IF NOT EXISTS arbitration (
        created_at  TEXT NOT NULL,
        fact_key    TEXT NOT NULL,
        id          TEXT PRIMARY KEY,
        left_id     TEXT NOT NULL REFERENCES memories (id),
        right_id    TEXT NOT NULL REFERENCES memories (id),
        status      TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
        resolution  TEXT CHECK (
                        resolution IS NULL OR resolution IN ('left', 'right', 'both', 'merged')),
        resolved_by TEXT,
        resolved_at TEXT,
        CHECK (status <> 'resolved' OR resolution IS NOT NULL)
    )
    """,
    # 同一 fact_key 同时最多一个 open 仲裁：防止"仲裁泛滥"（INV5 四态互斥）
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_arbitration_open ON arbitration (fact_key) "
    "WHERE status = 'open'",
    # 中文 2-gram 倒排索引：content_2gram 是空格分隔的 2-gram 词串（见 core/tokenize.py）
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5 (
        memory_id UNINDEXED,
        content_2gram,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
)

MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="p0-base", statements=_P0_STATEMENTS),
)
