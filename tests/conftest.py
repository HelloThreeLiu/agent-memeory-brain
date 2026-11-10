"""测试夹具：每个用例一个独立库文件，不共享状态。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from nebula.store.db import connect, migrate


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture()
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    migrate(connection)
    yield connection
    connection.close()
