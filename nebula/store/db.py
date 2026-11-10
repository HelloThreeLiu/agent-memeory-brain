"""连接、路径解析与迁移执行器。

连接约定：``isolation_level=None``（自动提交）+ 显式 ``BEGIN IMMEDIATE``。
写入路径必须显式开事务，这样"部分写入不留脏数据"是可验证的，而不是依赖
sqlite3 模块的隐式行为。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from nebula.store.schema import CURRENT_SCHEMA_VERSION, MIGRATIONS

DB_PATH_ENV = "NEBULA_DB"


def resolve_db_path(explicit: str | None = None) -> Path:
    """库路径优先级：显式参数 > ``NEBULA_DB`` 环境变量 > ``~/.nebula/memory.db``。"""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(DB_PATH_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".nebula" / "memory.db"


def connect(path: str | Path) -> sqlite3.Connection:
    target = str(path)
    if target != ":memory:":
        Path(target).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """当前库的 schema 版本；未初始化过返回 0。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row is not None else 0


def migrate(conn: sqlite3.Connection, target: int = CURRENT_SCHEMA_VERSION) -> list[int]:
    """把库升到 ``target``，返回本次实际应用的版本号列表。

    幂等：已应用的版本会跳过；DDL 自身也是 ``IF NOT EXISTS``，重复执行不报错。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    current = schema_version(conn)
    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version <= current or migration.version > target:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(migration.version),),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(migration.version)
    return applied


def open_database(path: str | None = None) -> sqlite3.Connection:
    """打开（必要时初始化）一个库，保证 schema 已就位。"""
    conn = connect(resolve_db_path(path))
    migrate(conn)
    return conn


def initialization_report(path: str | None = None) -> dict[str, object]:
    """``nebula init`` 用的自检报告。"""
    resolved = resolve_db_path(path)
    conn = open_database(path)
    try:
        applied_tables = [
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            "path": str(resolved),
            "schema_version": schema_version(conn),
            "tables": applied_tables,
        }
    finally:
        conn.close()
