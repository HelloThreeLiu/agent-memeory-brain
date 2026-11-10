"""P0-1：建表、schema 版本、幂等，以及库层兜底的不变量触发器。

这一组用例直接走 SQL：它测的正是"绕过 write_service 也会被拦住"的库层约束，
用 write_service 来准备数据反而测不到触发器的独立性。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nebula.store.db import migrate, open_database, schema_version
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
) -> str:
    memory_id = f"mem-{seq}"
    conn.execute(
        """
        INSERT INTO memories (
            id, scope, visibility, layer, kind, assertion_type, asserted_by, content,
            status, valid_from, created_at, seq, evidence, content_hash
        ) VALUES (?, 'global', 'shared', ?, 'fact', ?, '{"role":"agent"}', ?, 'active',
                  ?, ?, ?, '["ep-1"]', ?)
        """,
        (memory_id, layer, assertion_type, content, STAMP, STAMP, seq, f"hash-{seq}"),
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
