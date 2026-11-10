"""P0-6：设计文档 §8.1.1 的 DDL 必须与 schema.py 逐条一致。

文档与代码各写一份 DDL，迟早会漂。这里把"记得同步"变成一道会红的门禁：
改 schema 不改文档，这条测试就挂。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from nebula.store.schema import MIGRATIONS

ROOT = Path(__file__).resolve().parents[1]
DESIGN_DOC = ROOT / "docs" / "记忆系统调研与设计.md"


def _normalized(text: str) -> str:
    return " ".join(textwrap.dedent(text).split())


def _documented_ddl() -> str:
    text = DESIGN_DOC.read_text(encoding="utf-8")
    start = text.index("### 8.1.1")
    end = text.index("### 8.2", start)
    return _normalized(text[start:end])


def test_every_migration_statement_is_documented() -> None:
    documented = _documented_ddl()
    missing = [
        statement
        for statement in (_normalized(s) for s in MIGRATIONS[0].statements)
        if statement not in documented
    ]
    assert missing == [], (
        f"设计文档 §8.1.1 与 schema.py 不一致（{len(missing)} 条缺失），"
        f"第一条：{missing[0][:80] if missing else ''}"
    )
