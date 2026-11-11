"""P1 生命周期写口：记账、置顶、归档、回收站、裁决。

每个动作都要证明两件事：状态真的变了，且 FTS / 审计事件跟着变了。
只断言 status 会漏掉"库与索引不一致"这类静默问题。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from nebula.core.timeutil import fmt, parse_ts
from nebula.store.write_service import WriteResult, WriteService

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def at(days: int = 0) -> datetime:
    return T0 + timedelta(days=days)


def fts_rows(conn: sqlite3.Connection, memory_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    return int(row[0])


def memory_row(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    assert row is not None
    return row


def event_types(conn: sqlite3.Connection, target: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT type FROM events WHERE target = ? ORDER BY id", (target,)
        )
    ]


# --------------------------------------------------------------------- 召回记账


def test_touch_recall_updates_counter_and_timestamp(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("服务器用 SQLite 存记忆", now=at(0))

    assert write("服务器用 SQLite 存记忆", now=at(0))  # 同一条已存在

    service = WriteService(conn)
    assert service.touch_recall([result.id], now=at(1)) == 1

    row = memory_row(conn, result.id)
    assert row["recall_count"] == 1
    assert row["last_recalled"] == fmt(at(1))
    assert row["status"] == "active"


def test_touch_recall_wakes_dormant_but_not_archived(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    dormant = write("休眠中的偏好", now=at(0))
    archived = write("已归档的旧结论", now=at(0))
    conn.execute("UPDATE memories SET status = 'dormant' WHERE id = ?", (dormant.id,))
    conn.execute("UPDATE memories SET status = 'archived' WHERE id = ?", (archived.id,))

    service = WriteService(conn)
    touched = service.touch_recall([dormant.id, archived.id], now=at(2))

    assert touched == 1
    assert memory_row(conn, dormant.id)["status"] == "active"
    assert memory_row(conn, archived.id)["status"] == "archived"


def test_touch_recall_records_partitioned_event(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("私有片段", visibility="private", session_id="s1", now=at(0))

    WriteService(conn).touch_recall([result.id], session_id="s1", now=at(1))

    row = conn.execute(
        "SELECT scope, visibility FROM events WHERE target = ? AND type = 'memory.recalled'",
        (result.id,),
    ).fetchone()
    assert row is not None
    assert row["visibility"] == "private"


# ------------------------------------------------------------------------- 置顶


def test_pin_toggles_and_is_idempotent(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("不要动这条", now=at(0))
    service = WriteService(conn)

    assert service.pin(result.id, now=at(0)) is True
    assert memory_row(conn, result.id)["pinned"] == 1
    # 同样的状态再置顶一次：不算改动，也不该重复记事件
    assert service.pin(result.id, now=at(0)) is False
    assert service.pin(result.id, pinned=False, now=at(1)) is True
    assert memory_row(conn, result.id)["pinned"] == 0


def test_pin_unknown_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="没有这条记忆"):
        WriteService(conn).pin("mem-999")


# ------------------------------------------------------------------------- 归档


def test_archive_leaves_fts_and_unarchive_restores_it(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("构建用 maven 打包", now=at(0))
    service = WriteService(conn)
    assert fts_rows(conn, result.id) == 1

    assert service.archive(result.id, reason="decay", now=at(10)) is True
    row = memory_row(conn, result.id)
    assert row["status"] == "archived"
    assert row["archived_reason"] == "decay"
    assert row["archived_at"] == fmt(at(10))
    # Q-5：归档条目不许继续占索引
    assert fts_rows(conn, result.id) == 0

    assert service.unarchive(result.id, now=at(11)) is True
    restored = memory_row(conn, result.id)
    assert restored["status"] == "active"
    assert restored["archived_reason"] is None
    assert fts_rows(conn, result.id) == 1


def test_archive_rejects_unknown_reason(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("随便一条", now=at(0))
    with pytest.raises(ValueError, match="未知的归档原因"):
        WriteService(conn).archive(result.id, reason="because")


# ----------------------------------------------------------------------- 回收站


def test_forget_and_restore_within_window(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("要被忘掉的事实", now=at(0))
    service = WriteService(conn)

    assert service.forget(result.id, now=at(1)) is True
    row = memory_row(conn, result.id)
    assert row["status"] == "deleted"
    assert row["deleted_at"] == fmt(at(1))
    assert fts_rows(conn, result.id) == 0

    assert service.restore(result.id, now=at(20)) is True
    back = memory_row(conn, result.id)
    assert back["status"] == "active"
    assert back["deleted_at"] is None
    assert fts_rows(conn, result.id) == 1


def test_restore_refuses_after_30_days(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    result = write("过期就回不来了", now=at(0))
    service = WriteService(conn)
    service.forget(result.id, now=at(0))

    with pytest.raises(ValueError, match="超过回收站 30 天窗口"):
        service.restore(result.id, now=at(31))
    assert memory_row(conn, result.id)["status"] == "deleted"


def test_purge_only_removes_expired_recycle_bin_rows(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    old = write("三十天前忘掉的", now=at(0))
    recent = write("刚忘掉的", now=at(30))
    kept = write("还好好的", now=at(30))
    service = WriteService(conn)
    service.forget(old.id, now=at(0))
    service.forget(recent.id, now=at(30))

    assert service.purge_expired(now=at(29)) == 0
    assert service.purge_expired(now=at(31)) == 1

    remaining = {str(row[0]) for row in conn.execute("SELECT id FROM memories")}
    assert old.id not in remaining
    assert recent.id in remaining
    assert kept.id in remaining
    # 再过 30 天，第二条也到窗口外
    assert service.purge_expired(now=at(61)) == 1
    assert recent.id not in {
        str(row[0]) for row in conn.execute("SELECT id FROM memories")
    }


def test_purge_clears_links_and_arbitration_first(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """外键顺序：先清 links / arbitration 再删主体，否则整批回滚。"""
    old = write("要被彻底删掉", now=at(0))
    other = write("还活着的条目", now=at(0))
    conn.execute(
        "INSERT INTO memory_links (src, dst, type) VALUES (?, ?, 'supports')",
        (other.id, old.id),
    )
    WriteService(conn).forget(old.id, now=at(0))

    assert WriteService(conn).purge_expired(now=at(31)) == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?", (old.id,)).fetchone()[0] == 0


# ------------------------------------------------------------------------- 裁决


def _arbitrated_pair(
    service: WriteService, fact_key: str = "db.engine"
) -> tuple[WriteResult, WriteResult]:
    """decision（user，高档）被 observation（agent，低档）挑战 → C 态开仲裁。"""
    keeper = service.remember(
        "数据库用 SQLite",
        fact_key=fact_key,
        assertion_type="decision",
        role="user",
        evidence=("ep-1",),
        now=at(0),
    )
    challenger = service.remember(
        "数据库用 PostgreSQL",
        fact_key=fact_key,
        assertion_type="observation",
        role="agent",
        evidence=("ep-2",),
        now=at(1),
    )
    assert challenger.arbitration_id is not None
    return keeper, challenger


def test_resolve_left_retires_the_challenger(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    keeper, challenger = _arbitrated_pair(service)

    winner = service.resolve_arbitration(
        challenger.arbitration_id or "", "left", now=at(2)
    )

    assert winner == keeper.id
    loser = memory_row(conn, challenger.id)
    # 赢家更早生效（旧条胜出）：输家从自己生效撑到被裁决为止，
    # 而不是倒挂成"从未成立"（审查 M-1）
    assert loser["valid_to"] == fmt(at(2))
    assert parse_ts(loser["valid_to"]) >= parse_ts(loser["valid_from"])
    assert loser["expired_at"] == fmt(at(2))
    assert loser["superseded_by"] == keeper.id
    assert loser["status"] == "active"  # observation 输掉不归档，只是退出当前区间
    arb = conn.execute(
        "SELECT status, resolution, resolved_by FROM arbitration WHERE id = ?",
        (challenger.arbitration_id,),
    ).fetchone()
    assert arb["status"] == "resolved"
    assert arb["resolution"] == "left"
    assert arb["resolved_by"] == "user"


def test_resolve_right_truncates_at_the_winner_start(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    """claim 胜出（新压旧）：旧条止于新条生效时刻，区间同样不许倒挂。"""
    keeper = service.remember(
        "数据库用 SQLite",
        fact_key="db.engine.claimwin",
        assertion_type="decision",
        role="user",
        evidence=("ep-1",),
        now=at(0),
    )
    claim = service.remember(
        "数据库改用 PostgreSQL",
        fact_key="db.engine.claimwin",
        assertion_type="claim",
        role="agent",
        evidence=("ep-2",),
        now=at(1),
    )
    assert claim.arbitration_id is not None

    assert service.resolve_arbitration(claim.arbitration_id, "right", now=at(2)) == claim.id

    loser = memory_row(conn, keeper.id)
    assert loser["valid_to"] == fmt(at(1))
    assert parse_ts(loser["valid_to"]) >= parse_ts(loser["valid_from"])
    # 输家是 decision，不是 claim，所以只退场不归档
    assert loser["status"] == "active"


def test_resolve_refutes_a_losing_claim(conn: sqlite3.Connection, service: WriteService) -> None:
    keeper = service.remember(
        "数据库用 SQLite",
        fact_key="db.engine",
        assertion_type="decision",
        role="user",
        evidence=("ep-1",),
        now=at(0),
    )
    claim = service.remember(
        "数据库改用 PostgreSQL",
        fact_key="db.engine",
        assertion_type="claim",
        role="agent",
        evidence=("ep-2",),
        now=at(1),
    )
    assert claim.arbitration_id is not None

    service.resolve_arbitration(claim.arbitration_id, "left", now=at(2))

    loser = memory_row(conn, claim.id)
    assert loser["status"] == "archived"
    assert loser["archived_reason"] == "refuted"
    assert fts_rows(conn, claim.id) == 0
    assert memory_row(conn, keeper.id)["status"] == "active"


def test_resolve_both_keeps_every_row_current(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    keeper, challenger = _arbitrated_pair(service)

    assert service.resolve_arbitration(challenger.arbitration_id or "", "both", now=at(2)) == ""

    for memory_id in (keeper.id, challenger.id):
        row = memory_row(conn, memory_id)
        assert row["status"] == "active"
        assert row["expired_at"] is None
        assert row["valid_to"] is None


def test_resolve_rejects_merged_until_design_defines_it(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    _, challenger = _arbitrated_pair(service)
    with pytest.raises(ValueError, match="不支持的裁决结论"):
        service.resolve_arbitration(challenger.arbitration_id or "", "merged", now=at(2))
    arb = conn.execute(
        "SELECT status FROM arbitration WHERE id = ?", (challenger.arbitration_id,)
    ).fetchone()
    assert arb["status"] == "open"


def test_resolve_twice_is_rejected(conn: sqlite3.Connection, service: WriteService) -> None:
    _, challenger = _arbitrated_pair(service)
    service.resolve_arbitration(challenger.arbitration_id or "", "left", now=at(2))

    with pytest.raises(ValueError, match="已经裁决过"):
        service.resolve_arbitration(challenger.arbitration_id or "", "left", now=at(3))


def test_resolve_tolerates_already_expired_loser(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    """输家已被 B 路由失效过：不能再写 expired_at（INV3 触发器会 ABORT）。"""
    _, challenger = _arbitrated_pair(service)
    conn.execute(
        "UPDATE memories SET expired_at = ?, valid_to = ? WHERE id = ?",
        (fmt(at(1)), fmt(at(1)), challenger.id),
    )

    assert service.resolve_arbitration(challenger.arbitration_id or "", "left", now=at(2))
    assert "arbitration.loser_already_expired" in event_types(conn, challenger.id)


def test_resolve_refuses_unknown_arbitration(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="没有这条仲裁"):
        WriteService(conn).resolve_arbitration("arb-404", "left")


def test_sweep_status_moves_rows_to_the_recycle_bin(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """批量 deleted 分支也要真的能用（审查 n-7：原本无调用也无用例）。"""
    first = write("批量遗忘 A", now=at(0))
    second = write("批量遗忘 B", now=at(0))
    service = WriteService(conn)

    assert service.sweep_status([first.id, second.id], "deleted", now=at(1)) == 2

    for memory_id in (first.id, second.id):
        row = memory_row(conn, memory_id)
        assert row["status"] == "deleted"
        assert row["deleted_at"] == fmt(at(1))
        assert fts_rows(conn, memory_id) == 0


def test_sweep_status_rejects_targets_outside_the_whitelist(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("不该被批量复活", now=at(0))
    with pytest.raises(ValueError, match="不支持目标状态"):
        WriteService(conn).sweep_status([item.id], "active")
    assert memory_row(conn, item.id)["status"] == "active"


def test_resolve_same_moment_uses_the_decision_time(
    conn: sqlite3.Connection, service: WriteService
) -> None:
    """同刻对子裁 left：输家撑到裁决时刻，不是零宽区间（复查 m-9）。

    §9.3 表第三行与实现曾经打架：`>=` 让同刻也走"赢家生效时刻"分支，
    得到 `[t, t)` —— 不违反硬约束，但把"对子存续过、被注入过"的记录抹掉了。
    """
    keeper = service.remember(
        "部署用 docker",
        fact_key="deploy.same",
        assertion_type="decision",
        role="user",
        evidence=("ep-1",),
        now=at(0),
    )
    challenger = service.remember(
        "部署改用 podman",
        fact_key="deploy.same",
        assertion_type="observation",
        role="agent",
        evidence=("ep-2",),
        now=at(0),
    )
    assert challenger.arbitration_id is not None

    service.resolve_arbitration(challenger.arbitration_id, "left", now=at(2))

    loser = memory_row(conn, challenger.id)
    assert loser["valid_from"] == fmt(at(0))
    assert loser["valid_to"] == fmt(at(2))
    assert parse_ts(loser["valid_to"]) > parse_ts(loser["valid_from"])
    assert memory_row(conn, keeper.id)["status"] == "active"


class _RestoreAfterScan:
    """第一次读到回收站候选之后，立刻把它复原。

    真实并发在 ``BEGIN IMMEDIATE`` 之后插不进来（写锁在事务手里），所以这里
    在同一个连接、同一个事务里注入——测的是"扫描结果不能直接当成删除目标"
    这条规则本身，而不是某种调度时序。
    """

    def __init__(self, real: sqlite3.Connection, memory_id: str) -> None:
        self._real = real
        self._memory_id = memory_id
        self.injected = False

    @property
    def in_transaction(self) -> bool:
        return self._real.in_transaction

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        cursor = self._real.execute(sql, *args)
        if (
            not self.injected
            and sql.strip().startswith("SELECT id FROM memories")
            and "deleted_at IS NOT NULL" in sql
        ):
            self.injected = True
            WriteService(self._real).restore(self._memory_id, now=at(1))
        return cursor


def test_purge_skips_rows_restored_after_the_scan(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """扫描之后被复原的条目，主体、索引、链接都不能动（复查 m-10）。

    少一道"二次确认"，按旧 id 清 links / FTS 会留下"记忆还在、却搜不到也
    没有关联"的不一致——比多留一条更糟。
    """
    target = write("三十天前忘掉的", now=at(0))
    other = write("还活着的关联条目", now=at(0))
    conn.execute(
        "INSERT INTO memory_links (src, dst, type) VALUES (?, ?, 'supports')",
        (other.id, target.id),
    )
    WriteService(conn).forget(target.id, now=at(0))
    assert fts_rows(conn, target.id) == 0

    probe = _RestoreAfterScan(conn, target.id)
    purged = WriteService(probe).purge_expired(now=at(31))  # type: ignore[arg-type]

    assert probe.injected is True, "注入没生效，这条用例什么都没测到"
    assert purged == 0
    row = memory_row(conn, target.id)
    assert row["status"] == "active"
    assert row["deleted_at"] is None
    assert fts_rows(conn, target.id) == 1, "复原后的条目必须重新可搜"
    assert conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 1

