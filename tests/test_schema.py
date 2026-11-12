"""P0-1：建表、schema 版本、幂等，以及库层兜底的不变量触发器。

这一组用例直接走 SQL：它测的正是"绕过 write_service 也会被拦住"的库层约束，
用 write_service 来准备数据反而测不到触发器的独立性。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nebula.store.db import connect, migrate, open_database, schema_version
from nebula.store.schema import CURRENT_SCHEMA_VERSION

P0_TABLES = ("memories", "memory_links", "episodes", "events", "arbitration", "counter")
STAMP = "2026-05-01T09:00:00+00:00"


def insert_memory(
    conn: sqlite3.Connection,
    seq: int,
    *,
    content: str = "样本内容",
    layer: str = "semantic",
    assertion_type: str = "observation",
    scope: str = "global",
    project_id: str | None = None,
) -> str:
    memory_id = f"mem-{seq}"
    conn.execute(
        """
        INSERT INTO memories (
            id, scope, project_id, visibility, layer, kind, assertion_type, asserted_by, content,
            status, valid_from, created_at, seq, evidence, content_hash
        ) VALUES (?, ?, ?, 'shared', ?, 'fact', ?, '{"role":"agent"}', ?, 'active',
                  ?, ?, ?, '["ep-1"]', ?)
        """,
        (
            memory_id,
            scope,
            project_id,
            layer,
            assertion_type,
            content,
            STAMP,
            STAMP,
            seq,
            f"hash-{seq}",
        ),
    )
    return memory_id


def test_init_creates_p0_tables(conn: sqlite3.Connection) -> None:
    names = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for expected in P0_TABLES:
        assert expected in names
    assert "schema_meta" in names
    assert "memories_fts" in names


def test_schema_version_readable(conn: sqlite3.Connection) -> None:
    assert schema_version(conn) == CURRENT_SCHEMA_VERSION


def test_migrate_twice_is_idempotent(conn: sqlite3.Connection) -> None:
    assert migrate(conn) == []
    assert schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_open_database_on_existing_file_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    first = open_database(str(path))
    try:
        insert_memory(first, 1, content="重开库之后这条还在")
    finally:
        first.close()

    second = open_database(str(path))
    try:
        assert schema_version(second) == CURRENT_SCHEMA_VERSION
        assert second.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        second.close()


def test_expired_at_is_immutable(conn: sqlite3.Connection) -> None:
    memory_id = insert_memory(conn, 1, content="旧结论")
    conn.execute("UPDATE memories SET expired_at = ? WHERE id = ?", (STAMP, memory_id))
    with pytest.raises(sqlite3.IntegrityError, match="INV3"):
        conn.execute(
            "UPDATE memories SET expired_at = ? WHERE id = ?",
            ("2026-06-01T00:00:00+00:00", memory_id),
        )
    # 行还在，没有被静默删掉
    remaining = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()[0]
    assert remaining == 1


def test_migrate_v1_to_v2_backfills_arbitration_slot(tmp_path: Path) -> None:
    """旧库升级：v1 的仲裁行没有槽位字段，v2 迁移要从对子左侧那条回填。"""
    path = tmp_path / "v1.db"
    conn = connect(path)
    try:
        assert migrate(conn, target=1) == [1]
        assert schema_version(conn) == 1
        insert_memory(conn, 1, scope="project", project_id="p1")
        insert_memory(conn, 2, scope="project", project_id="p1")
        conn.execute(
            "INSERT INTO arbitration (id, created_at, fact_key, left_id, right_id, status) "
            "VALUES ('arb-1', ?, 'api', 'mem-1', 'mem-2', 'open')",
            (STAMP,),
        )

        assert migrate(conn) == [2, 3, 4]
        assert schema_version(conn) == CURRENT_SCHEMA_VERSION
        row = conn.execute(
            "SELECT scope, project_id, visibility, session_id FROM arbitration WHERE id = 'arb-1'"
        ).fetchone()
        assert row["scope"] == "project"
        assert row["project_id"] == "p1"
        assert row["visibility"] == "shared"
        assert row["session_id"] == ""
        assert migrate(conn) == []
    finally:
        conn.close()


def test_migrate_v2_to_v3_adds_lifecycle_and_backfills_events(tmp_path: Path) -> None:
    """旧库升级：v3 加生命周期时间戳，并把审计事件按 target 回填分区列（Q-6）。"""
    path = tmp_path / "v2.db"
    conn = connect(path)
    try:
        assert migrate(conn, target=2) == [1, 2]
        memory_id = insert_memory(conn, 1, scope="project", project_id="p1", content="私有片段")
        conn.execute(
            "UPDATE memories SET visibility = 'private', session_id = 's1' WHERE id = ?",
            (memory_id,),
        )
        conn.execute(
            "INSERT INTO events (seq, ts, type, target) VALUES (1, ?, 'memory.created', ?)",
            (STAMP, memory_id),
        )
        event_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(events)")}
        assert "scope" not in event_columns

        assert migrate(conn) == [3, 4]
        row = conn.execute(
            "SELECT scope, visibility FROM events WHERE target = ?", (memory_id,)
        ).fetchone()
        assert row["scope"] == "project"
        assert row["visibility"] == "private"

        memory_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(memories)")}
        assert {"archived_at", "deleted_at", "decayed_at"} <= memory_columns
        fresh = conn.execute(
            "SELECT archived_at, deleted_at, decayed_at FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        assert fresh["archived_at"] is None
        assert fresh["deleted_at"] is None
        assert fresh["decayed_at"] is None

        # 回填只该在迁移事务里发生一次，装回触发器后 events 仍然 append-only
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE events SET type = 'tampered'")
    finally:
        conn.close()


def test_migrate_v3_to_v4_adds_interface_tables(tmp_path: Path) -> None:
    """旧库升级：v4 只加两表，不动已有行；升级后两表立即可用。"""
    path = tmp_path / "v3.db"
    conn = connect(path)
    try:
        assert migrate(conn, target=3) == [1, 2, 3]
        memory_id = insert_memory(conn, 1, content="升级前的条目")
        names = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "injection_log" not in names
        assert "feedback" not in names

        assert migrate(conn) == [4]
        names = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"injection_log", "feedback"} <= names

        row = conn.execute("SELECT content FROM memories WHERE id = ?", (memory_id,)).fetchone()
        assert row["content"] == "升级前的条目"

        conn.execute(
            "INSERT INTO injection_log (id, ts, query) VALUES ('inj-1', ?, 'q')", (STAMP,)
        )
        # kind 白名单在库层兜底：指标只认这三类，多一个值会让分母口径漂
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO feedback (id, ts, memory_id, kind, source) "
                "VALUES ('fb-1', ?, ?, 'maybe', 'tool')",
                (STAMP, memory_id),
            )
        assert migrate(conn) == []
    finally:
        conn.close()


def test_semantic_memory_cannot_be_hard_deleted(conn: sqlite3.Connection) -> None:
    memory_id = insert_memory(conn, 1, content="不许硬删")
    with pytest.raises(sqlite3.IntegrityError, match="INV3"):
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))


def test_deleted_semantic_memory_can_leave_the_recycle_bin(conn: sqlite3.Connection) -> None:
    """INV3 只保"不硬删"；已进回收站（status=deleted）的允许真删。"""
    memory_id = insert_memory(conn, 1, content="回收站里的条目")
    conn.execute("UPDATE memories SET status = 'deleted' WHERE id = ?", (memory_id,))
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_events_are_append_only(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO events (seq, ts, type, target) VALUES (1, ?, 'memory.created', 'mem-1')",
        (STAMP,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET type = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events")


def test_seq_is_unique(conn: sqlite3.Connection) -> None:
    insert_memory(conn, 1)
    with pytest.raises(sqlite3.IntegrityError):
        insert_memory(conn, 1, content="重复的 seq")


def test_global_scope_rejects_project_id(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO memories (
                id, scope, project_id, visibility, layer, kind, assertion_type, asserted_by,
                content, status, created_at, seq, evidence, content_hash
            ) VALUES ('mem-bad', 'global', 'p1', 'shared', 'semantic', 'fact', 'observation',
                      '{"role":"agent"}', 'x', 'active', ?, 9, '[]', 'h')
            """,
            (STAMP,),
        )
