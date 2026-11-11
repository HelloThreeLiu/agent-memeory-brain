"""衰减与遗忘阶梯（§9.5）。

大部分断言打的是 ``plan()``：它是只读的，能把"该动谁"单独钉住，
不用每验证一条规则就真的跑一次睡眠周期。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from nebula.core.timeutil import fmt
from nebula.lifecycle import decay, policy
from nebula.lifecycle.decay import decayed_heat, half_life_for, percentile, plan
from nebula.lifecycle.sleep import run_sleep
from nebula.store.write_service import WriteResult, WriteService

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def at(days: int) -> datetime:
    return T0 + timedelta(days=days)


def age(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    heat: float,
    last_recalled: datetime,
    decayed_at: datetime | None = None,
) -> None:
    """把一条记忆的时间/热度调到指定状态（只为构造场景，不代表真实路径）。"""
    conn.execute(
        "UPDATE memories SET heat = ?, last_recalled = ?, decayed_at = ? WHERE id = ?",
        (heat, fmt(last_recalled), fmt(decayed_at or last_recalled), memory_id),
    )


def test_decayed_heat_halves_each_half_life() -> None:
    assert decayed_heat(1.0, 100.0, 100.0) == pytest.approx(0.5)
    assert decayed_heat(1.0, 100.0, 200.0) == pytest.approx(0.25)
    assert decayed_heat(0.8, 100.0, 0.0) == pytest.approx(0.8)


def test_decayed_heat_never_grows_on_clock_skew() -> None:
    """elapsed 为负（时钟回拨）不能把 heat 抬高——否则改系统时间就是刷权重。"""
    assert decayed_heat(0.4, 30.0, -50.0) == pytest.approx(0.4)


def test_half_life_prefers_stored_value() -> None:
    assert half_life_for("decision", None) == policy.HALF_LIFE_DAYS["decision"]
    assert half_life_for("unknown-kind", None) == policy.DEFAULT_HALF_LIFE_DAYS
    assert half_life_for("fact", 7.0) == 7.0
    # 0 或负数不合法，退回先验值而不是让衰减除零
    assert half_life_for("fact", 0.0) == policy.HALF_LIFE_DAYS["fact"]


def test_percentile_edges() -> None:
    assert percentile([], 0.1) == 0.0
    assert percentile([5.0], 0.1) == 5.0
    assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert percentile([0.0, 10.0], 0.0) == 0.0
    assert percentile([0.0, 10.0], 1.0) == 10.0


def test_plan_settles_heat_from_last_settlement(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("记忆用 SQLite", kind="fact", now=at(0))

    first = plan(conn, now=at(365))
    step = next(item for item in first.heat if item.memory_id == result.id)
    assert step.before == pytest.approx(1.0)
    assert step.after == pytest.approx(0.5, abs=1e-6)

    # 结算过之后，同一时刻再算不该继续衰减（decayed_at 已经推进）
    WriteService(conn).apply_heat([(step.memory_id, step.after)], now=at(365))
    again = plan(conn, now=at(365))
    assert all(item.memory_id != result.id for item in again.heat)


def test_plan_dormants_cold_and_idle_rows(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    cold = write("冷了的东西", now=at(0))
    fresh = write("刚被想起的东西", now=at(0))
    age(conn, cold.id, heat=0.1, last_recalled=at(0))
    age(conn, fresh.id, heat=0.1, last_recalled=at(39))

    result = plan(conn, now=at(40))
    assert cold.id in result.to_dormant
    assert fresh.id not in result.to_dormant


def test_plan_never_touches_pinned_rows(
    conn: sqlite3.Connection, write: Callable[..., WriteResult], service: WriteService
) -> None:
    pinned = write("别动这条", now=at(0))
    age(conn, pinned.id, heat=0.001, last_recalled=at(0))
    service.pin(pinned.id, now=at(0))

    result = plan(conn, now=at(400))
    assert pinned.id not in result.to_dormant
    assert pinned.id not in result.to_archive
    # heat 仍然照常结算：置顶只豁免"因冷而被埋"，不豁免记录真实冷度。
    # 否则取消置顶的那一刻它会带着过期热度突然掉进归档。
    assert any(step.memory_id == pinned.id for step in result.heat)


def test_plan_archives_idle_rows_below_threshold(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    cold = write("半年前的东西", now=at(0))
    warm = write("一直在用的东西", now=at(0))
    age(conn, cold.id, heat=0.01, last_recalled=at(0))
    age(conn, warm.id, heat=1.0, last_recalled=at(199))

    result = plan(conn, now=at(200))
    assert cold.id in result.to_archive
    assert warm.id not in result.to_archive


def test_capacity_pressure_archives_lowest_heat_first(
    conn: sqlite3.Connection,
    write: Callable[..., WriteResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy, "MAX_ACTIVE_MEMORIES", 3)
    monkeypatch.setattr(policy, "CAPACITY_TARGET", 2)
    ids = [write(f"条目 {index}", now=at(0)).id for index in range(5)]
    for index, memory_id in enumerate(ids):
        age(conn, memory_id, heat=1.0 - index * 0.1, last_recalled=at(200))

    result = plan(conn, now=at(200))
    # 5 条 active 超过上限 3，降到目标水位 2 → 归档 3 条，且是最冷的三条
    assert len(result.to_archive_capacity) == 3
    assert set(result.to_archive_capacity) == {ids[2], ids[3], ids[4]}


def test_capacity_only_buries_active_rows_not_dormant(
    conn: sqlite3.Connection,
    write: Callable[..., WriteResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容量压力来自 active 数量，候选也只能是 active（审查 m-5）。

    旧实现从 active+dormant 里按 heat 挑，而最冷的几乎总是 dormant：
    结果是把还能被唤醒的条目先埋掉，active 数一点没降，下一轮继续触顶。
    """
    monkeypatch.setattr(policy, "MAX_ACTIVE_MEMORIES", 3)
    monkeypatch.setattr(policy, "CAPACITY_TARGET", 2)
    actives = [write(f"活跃条目 {index}", now=at(0)).id for index in range(5)]
    dormants = [write(f"休眠条目 {index}", now=at(0)).id for index in range(3)]
    for index, memory_id in enumerate(actives):
        age(conn, memory_id, heat=0.9 - index * 0.1, last_recalled=at(200))
    for memory_id in dormants:
        # 比任何 active 都冷，旧逻辑下必然先被选中
        age(conn, memory_id, heat=0.001, last_recalled=at(200))
        conn.execute("UPDATE memories SET status = 'dormant' WHERE id = ?", (memory_id,))

    result = plan(conn, now=at(200))
    assert len(result.to_archive_capacity) == 3
    assert set(result.to_archive_capacity) <= set(actives)
    assert set(result.to_archive_capacity).isdisjoint(dormants)


def test_plan_counts_purgeable_rows(
    conn: sqlite3.Connection, write: Callable[..., WriteResult], service: WriteService
) -> None:
    result = write("要清掉的", now=at(0))
    service.forget(result.id, now=at(0))

    assert plan(conn, now=at(29)).purgeable == 0
    assert plan(conn, now=at(31)).purgeable == 1


def test_sleep_applies_the_plan(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    cold = write("冷了的东西", now=at(0))
    age(conn, cold.id, heat=0.02, last_recalled=at(0))

    report = run_sleep(conn, now=at(200))

    assert report.dry_run is False
    assert report.heat_settled >= 1
    row = conn.execute("SELECT status, heat FROM memories WHERE id = ?", (cold.id,)).fetchone()
    assert row["status"] in ("dormant", "archived")
    assert float(row["heat"]) < 0.02


def test_sleep_dry_run_writes_nothing(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    cold = write("冷了的东西", now=at(0))
    age(conn, cold.id, heat=0.02, last_recalled=at(0))
    before = conn.execute(
        "SELECT status, heat, decayed_at FROM memories WHERE id = ?", (cold.id,)
    ).fetchone()
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    report = run_sleep(conn, now=at(200), dry_run=True)

    after = conn.execute(
        "SELECT status, heat, decayed_at FROM memories WHERE id = ?", (cold.id,)
    ).fetchone()
    assert report.dry_run is True
    assert (before["status"], before["heat"], before["decayed_at"]) == (
        after["status"],
        after["heat"],
        after["decayed_at"],
    )
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


def test_sleep_rolls_back_heat_when_a_later_step_fails(
    conn: sqlite3.Connection,
    write: Callable[..., WriteResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一轮维护要么全落要么全不落：heat 已改而归档失败是最难排查的中间态。"""
    cold = write("冷了的东西", now=at(0))
    age(conn, cold.id, heat=0.02, last_recalled=at(0))
    before = float(
        conn.execute("SELECT heat FROM memories WHERE id = ?", (cold.id,)).fetchone()[0]
    )

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("归档失败")

    monkeypatch.setattr(WriteService, "sweep_status", boom)
    with pytest.raises(RuntimeError, match="归档失败"):
        run_sleep(conn, now=at(200))

    after = float(
        conn.execute("SELECT heat FROM memories WHERE id = ?", (cold.id,)).fetchone()[0]
    )
    assert after == pytest.approx(before)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'memory.heat_settled'"
        ).fetchone()[0]
        == 0
    )


def test_sleep_is_idempotent_on_second_run(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    cold = write("冷了的东西", now=at(0))
    age(conn, cold.id, heat=0.02, last_recalled=at(0))

    first = run_sleep(conn, now=at(200))
    assert first.dormant + first.archived + first.archived_capacity >= 1
    status_after_first = conn.execute(
        "SELECT status FROM memories WHERE id = ?", (cold.id,)
    ).fetchone()[0]

    second = run_sleep(conn, now=at(200))
    assert second.dormant == 0
    assert second.archived == 0
    assert second.archived_capacity == 0
    assert conn.execute("SELECT status FROM memories WHERE id = ?", (cold.id,)).fetchone()[0] == (
        status_after_first
    )


def test_decay_module_plan_path_is_read_only(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write("随便一条", now=at(0))
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    decay.plan(conn, now=at(400))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
