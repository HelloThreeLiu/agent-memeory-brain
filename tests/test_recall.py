"""P0-4：候选池谓词、一席制、档位优先、双注入例外与固定查询集回归。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from nebula.recall.search import recall
from nebula.store.write_service import WriteResult, WriteService
from tests.seed import QUERIES, seeded_contents

BASE = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


@pytest.fixture()
def seeded(service: WriteService) -> int:
    for _slug, content in seeded_contents():
        service.remember(content, evidence=("ep-seed",), importance=0.5)
    return len(seeded_contents())


def test_seed_set_has_two_hundred_rows(seeded: int) -> None:
    assert seeded == 200


def test_fixed_query_set_hit_rate_at_least_seventy_percent(
    conn: sqlite3.Connection, seeded: int
) -> None:
    """20 个查询、top-5 里出现目标主题即算命中。合成数据基线，不是真实场景结论。"""
    hits = 0
    misses: list[str] = []
    for query, slug in QUERIES:
        result = recall(conn, query, 5)
        if any(f"【{slug}】" in hit.content for hit in result.hits):
            hits += 1
        else:
            misses.append(f"{query} → {slug}")
    assert hits / len(QUERIES) >= 0.70, f"命中 {hits}/{len(QUERIES)}；未命中：{misses}"


def test_recall_is_fast_enough(conn: sqlite3.Connection, seeded: int) -> None:
    timings: list[float] = []
    for query, _slug in QUERIES:
        timings.append(recall(conn, query, 5).elapsed_ms)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < 100.0, f"p95 = {p95:.1f}ms"


def test_empty_query_returns_nothing(conn: sqlite3.Connection, seeded: int) -> None:
    assert recall(conn, "  ").hits == ()


def test_expired_version_is_not_in_candidate_pool(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        fact_key="pool.api",
        now=BASE,
    )
    write(
        "接口方法改用 GET",
        assertion_type="decision",
        role="user",
        fact_key="pool.api",
        now=BASE + timedelta(days=1),
    )
    result = recall(conn, "接口方法", 5)
    assert [hit.content for hit in result.hits] == ["接口方法改用 GET"]


def test_future_valid_from_is_not_in_candidate_pool(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write(
        "未来才生效的约定：接口用 PATCH",
        fact_key="pool.future",
        valid_from="2030-01-01T00:00:00+00:00",
    )
    assert recall(conn, "接口用 PATCH").hits == ()


def test_archived_is_not_in_candidate_pool(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("已经归档的旧约定：接口用 GET", fact_key="pool.archived")
    conn.execute("UPDATE memories SET status = 'archived' WHERE id = ?", (result.id,))
    assert recall(conn, "已归档的接口约定").hits == ()


def test_same_fact_key_takes_one_seat_and_reports_objection(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write(
        "构建命令是 npm run build",
        assertion_type="observation",
        role="agent",
        fact_key="seat.build",
    )
    write(
        "构建命令其实是 yarn build",
        assertion_type="claim",
        role="agent",
        fact_key="seat.build",
    )
    result = recall(conn, "构建命令是什么", 5)
    assert len(result.hits) == 1
    assert result.hits[0].assertion_type == "observation"
    assert result.hits[0].pointer_count == 1
    assert any("未验证异议" in line for line in result.lines())


def test_seat_goes_to_higher_assertion_type_even_if_score_is_lower(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write(
        "部署脚本放在 scripts/deploy",
        assertion_type="observation",
        role="agent",
        fact_key="seat.rank",
        importance=0.1,
    )
    write(
        "部署脚本好像不在 scripts/deploy",
        assertion_type="claim",
        role="agent",
        fact_key="seat.rank",
        importance=1.0,
    )
    result = recall(conn, "部署脚本放在哪里", 5)
    assert result.hits[0].assertion_type == "observation"


def test_open_arbitration_pair_is_injected_together(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write("接口方法定为 POST", assertion_type="decision", role="user", fact_key="arb.inject")
    write("接口方法其实是 GET", assertion_type="observation", role="agent", fact_key="arb.inject")
    result = recall(conn, "接口方法", 5)
    assert len(result.hits) == 2
    assert {hit.assertion_type for hit in result.hits} == {"decision", "observation"}


def test_private_rows_are_filtered_in_the_pool(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write(
        "私有片段：我的本地调试端口是 9222",
        visibility="private",
        session_id="s-1",
    )
    assert recall(conn, "本地调试端口").hits == ()
    visible = recall(conn, "本地调试端口", viewer_session="s-1")
    assert len(visible.hits) == 1
