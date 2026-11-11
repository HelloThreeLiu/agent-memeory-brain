"""500 轮连续使用的膨胀测试（P1 验收）。

真实用法不是"写满 2000 条然后看"，而是"每天写几条、偶尔查一下、定期维护"。
这个用例就按那个节奏跑：500 轮 × 每轮 3 条写入 + 1 次召回，每 50 轮一次睡眠。

把 ``MAX_ACTIVE_MEMORIES`` 压到 300 是为了在 1500 条规模上就能触发容量归档，
不必真的写满 2000 条。压小的是水位，被验证的机制完全一样。

三条验收：
1. 活跃条数被钉在软上限附近，不随轮次增长；
2. 总量一条不丢（只归档，不硬删，INV3）；
3. 后段的召回耗时与注入规模与前段持平。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nebula.core.timeutil import fmt
from nebula.lifecycle import policy
from nebula.lifecycle.sleep import run_sleep
from nebula.recall.search import recall
from nebula.store.write_service import WriteService

ROUNDS = 500
DAYS_PER_ROUND = 3
ROWS_PER_ROUND = 3
BASE = datetime(2026, 1, 1, tzinfo=UTC)
SOFT_CAP = 300
CAPACITY_TARGET = 270


def round_time(index: int) -> datetime:
    return BASE + timedelta(days=index * DAYS_PER_ROUND)


def write_round(service: WriteService, index: int) -> None:
    now = round_time(index)
    service.remember(
        f"第 {index} 轮：模块 mod{index} 使用 SQLite 存储",
        fact_key=f"mod.{index}.db",
        kind="fact",
        assertion_type="observation",
        role="agent",
        evidence=(f"ep-{index}",),
        now=now,
    )
    service.remember(
        f"第 {index} 轮：接口 api{index} 定为 POST",
        fact_key=f"api.{index}.method",
        kind="decision",
        assertion_type="decision",
        role="user",
        evidence=(f"ep-{index}",),
        now=now,
    )
    service.remember(
        f"第 {index} 轮：构建 build{index} 耗时 {index % 60} 秒",
        fact_key=f"build.{index}.time",
        kind="fact",
        assertion_type="observation",
        role="agent",
        evidence=(f"ep-{index}",),
        now=now,
    )


def test_five_hundred_rounds_keep_the_library_bounded(
    conn: sqlite3.Connection,
    service: WriteService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "MAX_ACTIVE_MEMORIES", SOFT_CAP)
    monkeypatch.setattr(policy, "CAPACITY_TARGET", CAPACITY_TARGET)

    injected: list[int] = []
    timings: list[float] = []
    for index in range(ROUNDS):
        write_round(service, index)
        result = recall(conn, f"模块 mod{index}", 5)
        injected.append(sum(len(hit.content) for hit in result.hits))
        timings.append(result.elapsed_ms)
        assert len(result.hits) <= 5
        if (index + 1) % 50 == 0:
            run_sleep(conn, now=round_time(index))

    total = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
    assert total == ROUNDS * ROWS_PER_ROUND, "只许归档，不许硬删（INV3）"

    by_status = {
        str(row["status"]): int(row["n"])
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM memories GROUP BY status")
    }
    live = by_status.get("active", 0) + by_status.get("dormant", 0)
    assert live <= SOFT_CAP * 1.2, f"活跃 {live} 条，软上限 {SOFT_CAP} 没兜住：{by_status}"
    assert by_status.get("archived", 0) > 0, "500 轮之后一条都没归档，遗忘阶梯没跑起来"

    # 归档必须带原因，否则以后没人知道它为什么被埋
    missing_reason = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status = 'archived' AND archived_reason IS NULL"
    ).fetchone()[0]
    assert missing_reason == 0

    # 每条仍在用的 semantic 记忆都必须有证据（P1 验收第三条）
    no_evidence = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE layer = 'semantic' "
        "AND status IN ('active', 'dormant') AND evidence = '[]'"
    ).fetchone()[0]
    assert no_evidence == 0

    # Q-5：归档条目不许继续占 FTS
    fts_rows = int(conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0])
    assert fts_rows == live


def test_late_rounds_do_not_cost_more_than_early_rounds(
    conn: sqlite3.Connection,
    service: WriteService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库变大之后，每轮的召回耗时与注入规模都不该跟着涨。"""
    monkeypatch.setattr(policy, "MAX_ACTIVE_MEMORIES", SOFT_CAP)
    monkeypatch.setattr(policy, "CAPACITY_TARGET", CAPACITY_TARGET)

    injected: list[int] = []
    timings: list[float] = []
    for index in range(ROUNDS):
        write_round(service, index)
        result = recall(conn, f"模块 mod{index}", 5)
        injected.append(sum(len(hit.content) for hit in result.hits))
        timings.append(result.elapsed_ms)
        if (index + 1) % 50 == 0:
            run_sleep(conn, now=round_time(index))

    def mean(values: list[int] | list[float]) -> float:
        return sum(values) / len(values)

    early = slice(50, 150)
    late = slice(ROUNDS - 100, ROUNDS)
    assert mean(injected[late]) <= mean(injected[early]) * 1.5, (
        f"后段注入 {mean(injected[late]):.0f} 字 vs 前段 {mean(injected[early]):.0f} 字"
    )
    late_p95 = sorted(timings[late])[int(len(timings[late]) * 0.95) - 1]
    assert late_p95 < 100.0, f"后段 p95 = {late_p95:.1f}ms"


def test_soak_leaves_audit_trail_consistent(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    """短程抽查：事件都带分区、seq 单调、与记忆条数对得上。"""
    for index in range(20):
        write_round(service, index)
    run_sleep(conn, now=round_time(19))

    rows = conn.execute(
        "SELECT seq, scope, visibility FROM events WHERE type = 'memory.created'"
    ).fetchall()
    assert len(rows) == 20 * ROWS_PER_ROUND
    assert all(str(row["scope"]) == "global" for row in rows)
    assert all(str(row["visibility"]) == "shared" for row in rows)
    stamps = [str(row["seq"]) for row in rows]
    assert stamps == sorted(stamps, key=lambda value: int(value))

    unpaired = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status = 'active' AND valid_from IS NOT NULL "
        "AND valid_from > ?",
        (fmt(round_time(19)),),
    ).fetchone()[0]
    assert unpaired == 0
