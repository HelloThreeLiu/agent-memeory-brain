"""P0-2：把 INV1 / INV2 / INV3 / INV4 / INV6 / INV12 落成会红的断言。

INV1 是"没有旁路写入"，做法是静态扫描 + 行为断言：既证明写入口唯一，
也证明每次写入都留下了 events 足迹。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import nebula
from nebula.core.assertion import capped_confidence, promotion_target
from nebula.store.write_service import InvariantViolation, WriteResult, WriteService

#: 这些语句只允许出现在 write_service.py 里
WRITE_PATTERNS = (
    "INSERT INTO memories",
    "UPDATE memories",
    "DELETE FROM memories",
    "INSERT INTO memory_links",
    "UPDATE memory_links",
    "DELETE FROM memory_links",
    "INSERT INTO events",
    "INSERT INTO arbitration",
    "UPDATE arbitration",
)


def test_inv1_only_write_service_issues_writes() -> None:
    package_root = Path(nebula.__file__).parent
    offenders: list[str] = []
    for path in package_root.rglob("*.py"):
        if path.name == "write_service.py":
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in WRITE_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.relative_to(package_root)}: {pattern}")
    assert offenders == [], f"INV1: 出现旁路写入 → {offenders}"


def test_inv1_every_write_leaves_an_event(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("记一笔就留一条事件", fact_key="inv1.event")
    events = conn.execute("SELECT seq, type, target FROM events ORDER BY id").fetchall()
    assert len(events) >= 1
    assert events[-1]["type"] == "memory.created"
    assert events[-1]["target"] == result.id
    assert int(events[-1]["seq"]) == result.seq


def test_inv2_seq_is_monotonic_without_holes(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    for index in range(5):
        write(f"第 {index} 条", fact_key=f"inv2.{index}")
    seqs = [int(row["seq"]) for row in conn.execute("SELECT seq FROM memories ORDER BY seq")]
    assert seqs == [1, 2, 3, 4, 5]


def test_inv3_superseded_row_survives_and_is_queryable(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    first = write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        fact_key="inv3.api",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = write(
        "接口方法改用 GET",
        assertion_type="decision",
        role="user",
        fact_key="inv3.api",
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert second.action == "supersede_old"
    row = conn.execute(
        "SELECT valid_to, expired_at, superseded_by, status FROM memories WHERE id = ?",
        (first.id,),
    ).fetchone()
    assert row is not None, "INV3: 被取代的行被删掉了"
    assert row["valid_to"] == "2026-01-02T00:00:00+00:00"
    assert row["expired_at"] is not None
    assert row["superseded_by"] == second.id
    # 有效性看 valid_to，不看 status（§9.3）
    assert row["status"] == "active"


def test_inv4_semantic_without_evidence_is_rejected(service: WriteService) -> None:
    with pytest.raises(InvariantViolation, match="INV4"):
        service.remember("没有证据的事实")


def test_inv4_episodic_needs_no_evidence(service: WriteService) -> None:
    result = service.remember("一句对话原文", layer="episodic", kind="event")
    assert result.action == "created"


def test_inv4_user_decision_needs_no_evidence(service: WriteService) -> None:
    result = service.remember("接口统一用 POST", assertion_type="decision", role="user")
    assert result.action == "created"


def test_inv6_agent_cannot_write_decision(service: WriteService) -> None:
    with pytest.raises(InvariantViolation, match="INV6"):
        service.remember(
            "agent 替人拍板", assertion_type="decision", role="agent", evidence=("ep-1",)
        )


def test_promotion_rules_follow_inv6() -> None:
    assert promotion_target("claim", "agent") == "observation"
    assert promotion_target("hypothesis", "agent") == "observation"
    assert promotion_target("observation", "agent") == "observation"
    assert promotion_target("claim", "user") == "decision"
    assert capped_confidence(1.0, "agent") == pytest.approx(0.85)
    assert capped_confidence(1.0, "user") == pytest.approx(1.0)


def test_inv12_claim_never_expires_others(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    old = write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        fact_key="inv12.api",
    )
    claim = write(
        "接口方法应该是 PUT",
        assertion_type="claim",
        role="reviewer",
        fact_key="inv12.api",
    )
    row = conn.execute(
        "SELECT valid_to, status, expired_at FROM memories WHERE id = ?", (old.id,)
    ).fetchone()
    assert row["valid_to"] is None
    assert row["status"] == "active"
    assert row["expired_at"] is None

    edge = conn.execute(
        "SELECT 1 FROM memory_links WHERE src = ? AND dst = ? AND type = 'contradicts'",
        (claim.id, old.id),
    ).fetchone()
    assert edge is not None


def test_inv5_claim_against_same_role_does_not_open_arbitration(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write("构建命令是 npm run build", assertion_type="observation", role="agent", fact_key="inv5.b")
    write("构建命令其实不是 npm run build", assertion_type="claim", role="agent", fact_key="inv5.b")
    assert conn.execute("SELECT COUNT(*) FROM arbitration").fetchone()[0] == 0


def test_inv5_claim_across_roles_opens_arbitration(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write(
        "构建命令是 npm run build", assertion_type="observation", role="writer", fact_key="inv5.c"
    )
    claim = write(
        "构建命令其实不是 npm run build",
        assertion_type="claim",
        role="reviewer",
        fact_key="inv5.c",
    )
    assert claim.arbitration_id is not None
    row = conn.execute(
        "SELECT status, resolution FROM arbitration WHERE id = ?", (claim.arbitration_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["resolution"] is None
