"""P0-3：写入口的事务、seq 分配、幂等与跨进程并发。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nebula.recall.search import recall
from nebula.store.db import open_database
from nebula.store.write_service import WriteResult, WriteService, content_hash

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)

WORKER = """
import sys
sys.path.insert(0, {root!r})
from nebula.store.db import open_database
from nebula.store.write_service import WriteService

conn = open_database({db!r})
service = WriteService(conn)
for index in range(100):
    service.remember(
        "worker " + {tag!r} + " 第 " + str(index) + " 条内容",
        evidence=("ep-conc",),
        role="agent",
    )
conn.close()
"""


def test_first_write_is_created(write: Callable[..., WriteResult]) -> None:
    result = write("第一条记忆")
    assert result.action == "created"
    assert result.seq == 1
    assert result.id == "mem-1"


def test_dedup_within_window_merges_evidence(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    first = write("接口统一用 POST", fact_key="dup.key", now=BASE)
    second = write(
        "接口统一用 POST",
        fact_key="dup.key",
        evidence=("ep-2",),
        now=BASE + timedelta(seconds=30),
    )
    assert second.action == "deduplicated"
    assert second.id == first.id
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    evidence = json.loads(
        conn.execute("SELECT evidence FROM memories WHERE id = ?", (first.id,)).fetchone()[
            "evidence"
        ]
    )
    assert evidence == ["ep-1", "ep-2"]


def test_outside_window_is_not_treated_as_idempotent_write(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """60 秒窗口只管"幂等"，过了窗口即使内容相同也走 9.3 的 P2 merge（会强化权重）。"""
    first = write("接口统一用 POST", fact_key="dup.window", now=BASE)
    second = write("接口统一用 POST", fact_key="dup.window", now=BASE + timedelta(seconds=120))
    assert second.action == "merged"
    assert second.id == first.id
    weight = conn.execute("SELECT weight FROM memories WHERE id = ?", (first.id,)).fetchone()[
        "weight"
    ]
    assert weight > 1.0


def test_dedup_requires_fact_key(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write("没有键的重复内容")
    write("没有键的重复内容")
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_containment_merges_into_existing_row(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    old = write("接口统一用 POST", assertion_type="decision", role="user", fact_key="merge.key")
    new = write(
        "接口统一用 POST 方法",
        assertion_type="decision",
        role="user",
        fact_key="merge.key",
    )
    assert new.action == "merged"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    row = conn.execute("SELECT weight FROM memories WHERE id = ?", (old.id,)).fetchone()
    assert row["weight"] > 1.0


def test_supersede_sets_valid_to_and_keeps_row(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    old = write("接口方法定为 POST", assertion_type="decision", role="user", fact_key="sup.key")
    new = write("接口方法改用 GET", assertion_type="decision", role="user", fact_key="sup.key")
    row = conn.execute(
        "SELECT valid_to, expired_at, superseded_by FROM memories WHERE id = ?", (old.id,)
    ).fetchone()
    assert row["valid_to"] is not None
    assert row["expired_at"] is not None
    assert row["superseded_by"] == new.id
    link = conn.execute(
        "SELECT 1 FROM memory_links WHERE src = ? AND dst = ? AND type = 'supersedes'",
        (new.id, old.id),
    ).fetchone()
    assert link is not None


def test_backfilled_history_does_not_override_current(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    current = write(
        "现在的构建命令是 npm run build",
        assertion_type="decision",
        role="user",
        fact_key="history.key",
        now=BASE,
    )
    backfilled = write(
        "以前的构建命令是 yarn build",
        assertion_type="decision",
        role="user",
        fact_key="history.key",
        valid_from="2025-01-01T00:00:00+00:00",
        now=BASE + timedelta(minutes=1),
    )
    assert backfilled.action == "supersede_new"
    current_row = conn.execute(
        "SELECT valid_to, superseded_by FROM memories WHERE id = ?", (current.id,)
    ).fetchone()
    assert current_row["valid_to"] is None
    assert current_row["superseded_by"] is None
    old_row = conn.execute(
        "SELECT valid_to, superseded_by FROM memories WHERE id = ?", (backfilled.id,)
    ).fetchone()
    assert old_row["valid_to"] is not None
    assert old_row["superseded_by"] == current.id


def test_observation_challenging_decision_opens_arbitration(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    write("接口方法定为 POST", assertion_type="decision", role="user", fact_key="arb.key")
    challenger = write(
        "接口方法其实是 GET",
        assertion_type="observation",
        role="agent",
        fact_key="arb.key",
    )
    assert challenger.action == "arbitrate"
    assert challenger.arbitration_id is not None
    assert conn.execute("SELECT COUNT(*) FROM arbitration WHERE status='open'").fetchone()[0] == 1
    # 两条都仍然有效（双注入的前提）
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM memories WHERE fact_key='arb.key' AND valid_to IS NULL"
        ).fetchone()[0]
        == 2
    )


def test_second_challenger_reuses_the_open_arbitration(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """同一 fact_key 已有待裁决时，再来一条跨角色异议不能把整笔写入搞挂。"""
    write("接口方法定为 POST", assertion_type="decision", role="user", fact_key="arb.multi")
    first = write(
        "接口方法其实是 GET", assertion_type="observation", role="agent", fact_key="arb.multi"
    )
    second = write(
        "接口方法其实是 PATCH", assertion_type="claim", role="reviewer", fact_key="arb.multi"
    )
    assert first.arbitration_id is not None
    assert second.arbitration_id == first.arbitration_id
    assert conn.execute("SELECT COUNT(*) FROM arbitration").fetchone()[0] == 1


def test_merge_upgrades_content_and_rebuilds_index(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """补充细节不该丢：新表述是旧文超集时，槽位正文升级，索引同步重建。"""
    first = write(
        "接口统一用 POST", assertion_type="decision", role="user", fact_key="merge.content"
    )
    second = write(
        "接口统一用 POST，超时 30 秒",
        assertion_type="decision",
        role="user",
        fact_key="merge.content",
    )
    assert second.action == "merged"
    content = conn.execute(
        "SELECT content FROM memories WHERE id = ?", (first.id,)
    ).fetchone()["content"]
    assert content == "接口统一用 POST，超时 30 秒"
    assert recall(conn, "超时 30 秒").hits, "合并后新细节必须能被召回（FTS 要重建）"


def test_merge_does_not_burn_a_seq(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    first = write("A 事实", fact_key="seq.a")
    write("A 事实，补充说明", fact_key="seq.a")
    third = write("B 事实", fact_key="seq.b")
    assert third.seq == first.seq + 1, "merge 没落新行却消耗了 seq，会留下无主空洞"


def test_merge_event_does_not_store_raw_content(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """events 没有 scope/visibility 列，payload 存原文会让 private 记忆从审计表漏出去。"""
    write("接口统一用 POST", assertion_type="decision", role="user", fact_key="merge.event")
    write(
        "接口统一用 POST，超时 30 秒",
        assertion_type="decision",
        role="user",
        fact_key="merge.event",
    )
    payload = conn.execute(
        "SELECT payload FROM events WHERE type = 'memory.merged'"
    ).fetchone()["payload"]
    assert "超时 30 秒" not in str(payload)


def test_dedupe_ignores_an_already_expired_row(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """A → B → 60 秒内又改回 A：A 那行已失效，幂等进它等于把第三次写入静默丢弃。"""
    first = write("接口统一用 POST", fact_key="dedupe.expired", now=BASE)
    write("接口统一改用 GET", fact_key="dedupe.expired", now=BASE + timedelta(seconds=10))
    third = write(
        "接口统一用 POST",
        fact_key="dedupe.expired",
        evidence=("ep-3",),
        now=BASE + timedelta(seconds=20),
    )
    assert third.action == "supersede_old"
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3
    evidence = conn.execute(
        "SELECT evidence FROM memories WHERE id = ?", (first.id,)
    ).fetchone()["evidence"]
    assert "ep-3" not in str(evidence), "失效行的证据被后来者污染了"


def test_dedupe_does_not_cross_visibility(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    private = write(
        "调试端口 9222", visibility="private", session_id="s-A", fact_key="dedupe.vis"
    )
    shared = write("调试端口 9222", fact_key="dedupe.vis")
    assert shared.action == "created"
    assert shared.id != private.id
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_private_memory_requires_a_session(service: WriteService) -> None:
    with pytest.raises(ValueError, match="session_id"):
        service.remember("私有片段但没有 session", visibility="private", evidence=("ep-1",))


def test_arbitration_is_scoped_to_the_slot(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """两个私有 session 的同名键各自开仲裁，不能共用一个 fact_key 全局仲裁。

    二轮审查 M-1 变体 1：复用查询只看 fact_key，s-2 的挑战被挂到 s-1 的仲裁上，
    (mem-3, mem-4) 零记录、recall 里挑战静默消失。
    """
    write(
        "接口方法定为 POST",
        assertion_type="decision",
        role="user",
        visibility="private",
        session_id="s-1",
        fact_key="arb.slot",
    )
    first_pair = write(
        "接口方法其实是 GET",
        assertion_type="observation",
        role="agent",
        visibility="private",
        session_id="s-1",
        fact_key="arb.slot",
    )
    second_decision = write(
        "接口方法定为 PUT",
        assertion_type="decision",
        role="user",
        visibility="private",
        session_id="s-2",
        fact_key="arb.slot",
    )
    second_pair = write(
        "接口方法其实是 PATCH",
        assertion_type="observation",
        role="agent",
        visibility="private",
        session_id="s-2",
        fact_key="arb.slot",
    )

    rows = conn.execute(
        "SELECT id, left_id, right_id, scope, visibility, session_id FROM arbitration"
    ).fetchall()
    assert len(rows) == 2, "两个槽位应该各有一条仲裁记录"
    assert second_pair.arbitration_id != first_pair.arbitration_id
    second = conn.execute(
        "SELECT left_id, right_id, visibility, session_id FROM arbitration WHERE id = ?",
        (second_pair.arbitration_id,),
    ).fetchone()
    assert second["session_id"] == "s-2"
    assert second["visibility"] == "private"
    assert {str(second["left_id"]), str(second["right_id"])} == {
        second_decision.id,
        second_pair.id,
    }

    # 两个 session 各自双注入，谁也别把谁挤掉
    s2_hits = recall(conn, "接口方法", viewer_session="s-2").hits
    assert len(s2_hits) == 2


def test_superseded_pair_is_closed_and_new_challenge_gets_its_own(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """二轮审查 M-1 变体 2：B 路由顶掉对子一侧后，旧仲裁必须落终态，
    新挑战要拿到自己的仲裁记录，否则召回只剩拍板那条。"""
    write("接口方法定为 POST", assertion_type="decision", role="user", fact_key="arb.stale")
    challenger = write(
        "接口方法其实是 GET", assertion_type="observation", role="agent", fact_key="arb.stale"
    )
    assert challenger.arbitration_id is not None
    settled = write(
        "接口方法定为 PUT", assertion_type="decision", role="user", fact_key="arb.stale"
    )

    row = conn.execute(
        "SELECT status, resolution, resolved_by FROM arbitration WHERE id = ?",
        (challenger.arbitration_id,),
    ).fetchone()
    assert row["status"] == "resolved"
    assert row["resolution"] == "left", "存续的是 base（left）一侧"
    assert row["resolved_by"] == "system:b-route"

    fresh = write(
        "接口方法其实是 PATCH", assertion_type="observation", role="agent", fact_key="arb.stale"
    )
    assert fresh.arbitration_id != challenger.arbitration_id
    pair = conn.execute(
        "SELECT left_id, right_id, status FROM arbitration WHERE id = ?", (fresh.arbitration_id,)
    ).fetchone()
    assert pair["status"] == "open"
    assert {str(pair["left_id"]), str(pair["right_id"])} == {settled.id, fresh.id}

    injected = {hit.id for hit in recall(conn, "接口方法", 5).hits}
    assert injected == {settled.id, fresh.id}


def test_merge_upgrade_keeps_content_hash_in_sync(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    first = write(
        "接口统一用 POST", assertion_type="decision", role="user", fact_key="merge.hash"
    )
    write(
        "接口统一用 POST，超时 30 秒",
        assertion_type="decision",
        role="user",
        fact_key="merge.hash",
    )
    row = conn.execute(
        "SELECT content, content_hash FROM memories WHERE id = ?", (first.id,)
    ).fetchone()
    assert row["content_hash"] == content_hash(str(row["content"]))


def test_backfill_supersedes_edge_points_from_survivor(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """补录历史时是"旧条压新条"，边方向应是 current → backfilled。"""
    current = write("现在的接口是 POST", assertion_type="decision", role="user", fact_key="edge.h")
    backfilled = write(
        "以前的接口是 GET",
        assertion_type="decision",
        role="user",
        fact_key="edge.h",
        valid_from="2025-01-01T00:00:00+00:00",
    )
    assert backfilled.action == "supersede_new"
    edge = conn.execute(
        "SELECT 1 FROM memory_links WHERE src = ? AND dst = ? AND type = 'supersedes'",
        (current.id, backfilled.id),
    ).fetchone()
    assert edge is not None


def test_two_processes_write_two_hundred_rows_without_holes(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    # 先建好库，避免两个子进程同时跑迁移
    open_database(str(db)).close()
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER.format(root=str(ROOT), db=str(db), tag=tag)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("a", "b")
    ]
    for worker in workers:
        _, stderr = worker.communicate(timeout=180)
        assert worker.returncode == 0, stderr

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, seq FROM memories ORDER BY seq").fetchall()
    finally:
        conn.close()
    assert len(rows) == 200
    assert len({str(row["id"]) for row in rows}) == 200
    assert [int(row["seq"]) for row in rows] == list(range(1, 201))


def test_failed_write_rolls_back_completely(conn: sqlite3.Connection) -> None:
    """事务内失败不留脏数据，也不消耗 seq（counter 的 +1 随事务回滚）。"""
    service = WriteService(conn)
    with pytest.raises(sqlite3.IntegrityError):
        service.remember("重要性越界", evidence=("ep-1",), importance=2.0)
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert service.remember("失败之后的第一条", evidence=("ep-1",)).seq == 1
