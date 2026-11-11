"""P1 召回侧：记账（Q-8）、权重/置信因子、expand、explain。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from nebula.core.timeutil import fmt
from nebula.lifecycle import policy
from nebula.recall.detail import expand
from nebula.recall.explain import explain
from nebula.recall.search import recall
from nebula.store.write_service import WriteResult, WriteService

BASE = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def memories(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    assert row is not None
    return row


# ----------------------------------------------------------------------- 记账


def test_recall_records_last_recalled_and_count(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("记忆库用 SQLite 存储")
    before = memories(conn, item.id)
    assert before["recall_count"] == 0
    assert before["last_recalled"] is None

    result = recall(conn, "记忆库 SQLite")
    assert result.hits

    after = memories(conn, item.id)
    assert after["recall_count"] == 1
    assert after["last_recalled"] is not None


def test_recall_record_false_leaves_counters_alone(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("记忆库用 SQLite 存储")
    assert recall(conn, "记忆库 SQLite", record=False).hits
    assert memories(conn, item.id)["recall_count"] == 0


def test_recall_records_weak_hits_without_waking_them(
    conn: sqlite3.Connection,
    write: Callable[..., WriteResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """弱命中已注入给模型，要记账（Q-8）；但没到唤醒阈值，dormant 不该被拉起。

    审查 m-4 指出的正是这个混同：把"被注入"和"被真的想起来"绑成一条阈值，
    会让弱命中的记忆永远涨不了 recall_count，也就永远到不了 §9.2 的晋升线。
    """
    # 阈值设到 1 之上：覆盖率最大就是 1.0，这样"未达唤醒阈值"必然成立
    monkeypatch.setattr(policy, "WAKE_RELEVANCE", 1.01)
    item = write("记忆库用 SQLite 存储")
    conn.execute("UPDATE memories SET status = 'dormant' WHERE id = ?", (item.id,))

    assert recall(conn, "记忆库 SQLite").hits

    row = memories(conn, item.id)
    assert row["recall_count"] == 1
    assert row["last_recalled"] is not None
    assert row["status"] == "dormant"


def test_recall_wakes_dormant_on_hit(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("记忆库用 SQLite 存储")
    conn.execute("UPDATE memories SET status = 'dormant' WHERE id = ?", (item.id,))

    assert recall(conn, "记忆库 SQLite").hits
    assert memories(conn, item.id)["status"] == "active"


# ----------------------------------------------------------------------- 因子


def test_weight_factor_breaks_ties(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write("项目用 SQLite 存记忆")
    heavy = write("项目用 SQLite 做缓存")
    conn.execute("UPDATE memories SET weight = 3.0 WHERE id = ?", (heavy.id,))

    result = recall(conn, "项目 SQLite")
    assert next(hit.id for hit in result.hits) == heavy.id


def test_confidence_factor_breaks_ties(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    low = write("部署脚本放 scripts 目录")
    high = write("部署脚本放 deploy 目录")
    conn.execute("UPDATE memories SET confidence = 0.1 WHERE id = ?", (low.id,))
    conn.execute("UPDATE memories SET confidence = 1.0 WHERE id = ?", (high.id,))

    result = recall(conn, "部署脚本 scripts deploy")
    assert next(hit.id for hit in result.hits) == high.id


def test_normalized_weight_is_bounded() -> None:
    from nebula.core.scoring import normalize_weight

    assert normalize_weight(0.0) == 0.0
    assert normalize_weight(1.0) == pytest.approx(0.5)
    assert normalize_weight(1000.0) < 1.0


# ---------------------------------------------------------------------- expand


def test_expand_shows_history_and_links(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    first = write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        fact_key="api.method",
        now=BASE,
    )
    second = write(
        "接口方法改用 GET",
        assertion_type="decision",
        role="user",
        fact_key="api.method",
        now=BASE + timedelta(days=1),
    )

    result = expand(conn, first.id)
    assert result.memory.id == first.id
    assert [entry.id for entry in result.history] == [first.id, second.id]
    assert any(link.type == "supersedes" and link.dst == first.id for link in result.links)
    assert any(event.type == "memory.superseded" or event.type.startswith("memory.")
               for event in result.events)
    text = "\n".join(result.lines())
    assert "历代版本" in text
    assert "证据" in text


def test_expand_unknown_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="没有这条记忆"):
        expand(conn, "mem-404")


def test_expand_is_read_only(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("随便一条")
    before = memories(conn, item.id)
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    expand(conn, item.id)
    after = memories(conn, item.id)
    assert (before["recall_count"], before["heat"], before["status"]) == (
        after["recall_count"],
        after["heat"],
        after["status"],
    )
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events


# --------------------------------------------------------------------- explain


def test_explain_untouched_memory_is_recallable(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("记忆库用 SQLite 存储")
    result = explain(conn, item.id)
    assert result.recallable is True
    assert any("四道闸" in reason for reason in result.reasons)


def test_explain_reports_recycle_bin(
    conn: sqlite3.Connection, write: Callable[..., WriteResult], service: WriteService
) -> None:
    item = write("要被忘掉的")
    service.forget(item.id, now=BASE)
    result = explain(conn, item.id)
    assert result.recallable is False
    assert any("回收站" in reason for reason in result.reasons)


def test_explain_reports_archive_reason(
    conn: sqlite3.Connection, write: Callable[..., WriteResult], service: WriteService
) -> None:
    item = write("已经被埋起来的")
    service.archive(item.id, reason="decay", now=BASE)
    result = explain(conn, item.id)
    assert result.recallable is False
    assert any("已归档" in reason and "decay" in reason for reason in result.reasons)


def test_explain_reports_superseded_window(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    old = write(
        "构建命令用 maven",
        assertion_type="decision",
        role="user",
        fact_key="build.tool",
        now=BASE,
    )
    write(
        "构建命令改用 gradle",
        assertion_type="decision",
        role="user",
        fact_key="build.tool",
        now=BASE + timedelta(days=1),
    )
    result = explain(conn, old.id)
    assert result.recallable is False
    assert any("已失效" in reason for reason in result.reasons)


def test_explain_reports_private_partition(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write("私有片段：端口 9222", visibility="private", session_id="s-1")
    blocked = explain(conn, item.id)
    assert blocked.recallable is False
    assert any("私有分区" in reason for reason in blocked.reasons)

    visible = explain(conn, item.id, viewer_session="s-1")
    assert visible.recallable is True


def test_explain_reports_seat_taken(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """同角色写 claim 不触发仲裁，于是真要抢席位——这时 explain 必须拦。"""
    write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        fact_key="api.seat",
        now=BASE,
    )
    claim = write(
        "接口方法可能是 GET",
        assertion_type="claim",
        role="user",
        fact_key="api.seat",
        now=BASE,
    )
    result = explain(conn, claim.id)
    assert result.recallable is False
    assert any("占席" in reason for reason in result.reasons)


def test_explain_honours_double_injection_for_open_arbitration(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """open 仲裁的对子召回时双注入，explain 不能判它"被占席"。

    审查 M-2 的反例：同一份数据，recall 注入两条、explain 说一条不可召回。
    """
    write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        fact_key="api.dual",
        now=BASE,
    )
    challenge = write(
        "接口方法其实是 GET",
        assertion_type="observation",
        role="agent",
        fact_key="api.dual",
        now=BASE,
    )
    assert challenge.arbitration_id is not None

    result = explain(conn, challenge.id)
    assert result.recallable is True
    assert any("双注入" in reason for reason in result.reasons)

    hits = recall(conn, "接口方法", 5, record=False).hits
    assert challenge.id in {hit.id for hit in hits}, "explain 与 recall 必须同口径"


def test_explain_does_not_block_on_same_rank(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """同档席位靠综合分决定；explain 没有查询语境，不能硬判占席（M-2）。"""
    first = write(
        "接口可能用 GET",
        assertion_type="claim",
        role="agent",
        fact_key="seat.same",
        now=BASE,
    )
    write(
        "接口可能用 PUT",
        assertion_type="claim",
        role="agent",
        fact_key="seat.same",
        now=BASE,
    )
    result = explain(conn, first.id)
    assert result.recallable is True
    assert any("同档" in reason for reason in result.reasons)


def test_explain_unknown_id_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="没有这条记忆"):
        explain(conn, "mem-404")


def test_explain_now_parameter_is_used(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    item = write(
        "未来才生效的约定",
        fact_key="future.rule",
        valid_from=fmt(BASE + timedelta(days=30)),
        now=BASE,
    )
    assert explain(conn, item.id, now=fmt(BASE)).recallable is False
    assert explain(conn, item.id, now=fmt(BASE + timedelta(days=60))).recallable is True
