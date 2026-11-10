"""测试夹具：每个用例一个独立库文件，不共享状态。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from nebula.store.db import connect, migrate
from nebula.store.write_service import WriteResult, WriteService


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture()
def service(conn: sqlite3.Connection) -> WriteService:
    return WriteService(conn)


@pytest.fixture()
def write(service: WriteService) -> Callable[..., WriteResult]:
    """默认补一条 evidence 的写入助手（INV4 要求 semantic 带证据）。"""

    def _write(content: str, **kwargs: object) -> WriteResult:
        kwargs.setdefault("evidence", ("ep-1",))
        return service.remember(content, **kwargs)  # type: ignore[arg-type]

    return _write
