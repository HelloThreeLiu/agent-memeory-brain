"""P1.5a：注入日志与反馈写口。

这两张表是三项指标的**唯一**数据源，所以断言要盯三件事：记账完整、错误可见、
不污染记忆的 seq（否则 P0 的「seq 连续无洞」当场破）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import pytest

from nebula.store.write_service import WriteResult, WriteService


def _injection_row(conn: sqlite3.Connection, injection_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM injection_log WHERE id = ?", (injection_id,)).fetchone()
    assert row is not None
    return row


def test_log_injection_records_ids_and_chars(
    service: WriteService, conn: sqlite3.Connection
) -> None:
    injection_id = service.log_injection(
        query="构建命令是什么",
        memory_ids=("mem-1", "mem-2", "mem-1"),
        chars=48,
        top_k=5,
        host="claude-code",
        session_id="s1",
        project_id="p1",
        elapsed_ms=1.7,
    )

    assert injection_id.startswith("inj-")
    row = _injection_row(conn, injection_id)
    assert row["query"] == "构建命令是什么"
    assert json.loads(row["memory_ids"]) == ["mem-1", "mem-2"]
    assert row["chars"] == 48
    assert row["top_k"] == 5
    assert row["host"] == "claude-code"
    assert row["session"] == "s1"
    assert row["project_id"] == "p1"
    assert row["degraded_level"] == 0
    assert row["elapsed_ms"] == pytest.approx(1.7)


def test_log_injection_rejects_impossible_numbers(service: WriteService) -> None:
    with pytest.raises(ValueError, match="top_k"):
        service.log_injection(query="q", memory_ids=(), chars=0, top_k=0)
    with pytest.raises(ValueError, match="chars"):
        service.log_injection(query="q", memory_ids=(), chars=-1, top_k=5)
    with pytest.raises(ValueError, match="degraded_level"):
        service.log_injection(
            query="q", memory_ids=(), chars=0, top_k=5, degraded_level=-1
        )


def test_record_feedback_writes_row_and_event(
    service: WriteService, conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("接口用 POST", role="user", assertion_type="decision")
    feedback = service.record_feedback(
        result.id, "wrong", source="mcp", note="其实是 PUT", host="claude-code"
    )

    assert feedback.id.startswith("fb-")
    row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback.id,)).fetchone()
    assert row["memory_id"] == result.id
    assert row["kind"] == "wrong"
    assert row["source"] == "mcp"
    assert row["note"] == "其实是 PUT"

    event = conn.execute(
        "SELECT type, target, payload, scope, visibility FROM events "
        "WHERE type = 'feedback.recorded'"
    ).fetchone()
    assert event is not None
    assert event["target"] == result.id
    assert json.loads(event["payload"])["kind"] == "wrong"
    # 审计事件要能被 expand 摊给别的分区看，note 与正文摘要都不能进 payload
    assert "其实是 PUT" not in event["payload"]


def test_record_feedback_rejects_unknown_memory(
    service: WriteService, conn: sqlite3.Connection
) -> None:
    with pytest.raises(ValueError, match="没有这条记忆"):
        service.record_feedback("mem-不存在", "useless")
    # 报错必须整体回滚：留下半条反馈会让负反馈率分母漂
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE type = 'feedback.recorded'").fetchone()[0]
        == 0
    )


def test_record_feedback_rejects_unknown_kind(
    service: WriteService, write: Callable[..., WriteResult]
) -> None:
    result = write("随便一条")
    with pytest.raises(ValueError, match="未知的反馈类型"):
        service.record_feedback(result.id, "maybe")


def test_record_feedback_checks_injection_reference(
    service: WriteService, write: Callable[..., WriteResult]
) -> None:
    result = write("随便一条")
    injection_id = service.log_injection(
        query="q", memory_ids=(result.id,), chars=10, top_k=5
    )
    ok = service.record_feedback(result.id, "useful", injection_id=injection_id)
    assert ok.kind == "useful"

    with pytest.raises(ValueError, match="没有这次注入记录"):
        service.record_feedback(result.id, "useful", injection_id="inj-nope")


def test_interface_records_do_not_consume_memory_seq(
    service: WriteService, conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    first = write("第一条")
    second = write("第二条")
    assert (first.seq, second.seq) == (1, 2)

    injection_id = service.log_injection(query="q", memory_ids=(first.id,), chars=5, top_k=1)
    service.record_feedback(first.id, "useful", injection_id=injection_id)
    third = write("第三条")

    # seq 是记忆写入顺序（INV2），接入面记账不许在它上面留洞
    assert third.seq == 3
