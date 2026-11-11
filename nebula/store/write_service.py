"""唯一写入口（INV1）。

规矩（WORKFLOW.md 硬约束）：

1. 任何模块不得绕过本模块写 ``memories`` / ``memory_links`` / ``events``；
2. 一次 ``remember`` 是一个事务，要么全落、要么全不落；
3. 失败必须可见：违反不变量抛 :class:`InvariantViolation`，**不允许假装写成功**；
4. ``seq`` 由 ``counter`` 表在事务内分配，是唯一排序依据（INV2）；
5. 幂等键 = ``fact_key + content_hash`` + 60 秒窗口（§9.1）。

P0 没有 daemon：INV1 在这里只能是"进程内所有写都过本模块"，跨进程靠 SQLite
事务串行化。真正的唯一写者（daemon）在 P4。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nebula.core import conflict
from nebula.core.assertion import role_from_asserted_by
from nebula.core.conflict import FactView
from nebula.core.timeutil import fmt, now_ts, parse_ts
from nebula.core.tokenize import index_text

#: 幂等窗口：同键同内容在此窗口内合并，不新建（§9.1）
DEDUP_WINDOW_SECONDS = 60

#: merge 时的权重强化幅度（§9.2）
MERGE_WEIGHT_STEP = 0.08

#: 回收站保留期（§9.5 / INV10）：进回收站 30 天内可复原，之后才允许硬删。
#: 放在写入层而不是 lifecycle，是因为 restore / purge 的边界判定就在这里，
#: 策略层只是跟着它算"有多少条可清"。
RECYCLE_WINDOW_DAYS = 30

#: 归档原因白名单（与 schema 的 CHECK 一致）
ARCHIVE_REASONS: tuple[str, ...] = ("decay", "capacity", "refuted")

#: 裁决结论。``merged`` 在 §8.1 的枚举里，但 §9.3 没定义合并后的正文归属，
#: P1 不开放——宁可显式拒绝，也不猜一个"合并"出来（见 resolve_arbitration）。
RESOLUTIONS: tuple[str, ...] = ("left", "right", "both")

#: 批量状态迁移的白名单：目标状态 → 允许的前态（防止批量接口变成任意改 status）
_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "dormant": ("active",),
    "archived": ("active", "dormant"),
    "deleted": ("active", "dormant", "archived"),
}

#: ``BEGIN IMMEDIATE`` 的有界重试（BACKLOG Q-9）。只重试"事务还没开始"这一步：
#: 语句已经跑过再重试，等于把可能已部分生效的写入重放一遍，那是改语义，不是容错。
_BEGIN_ATTEMPTS = 5
_BEGIN_BACKOFF_SECONDS = 0.05
_TRANSIENT_MARKERS = (
    "database is locked",
    "database table is locked",
    "attempt to write a readonly database",
    "disk i/o error",
)


def _is_transient(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class InvariantViolation(RuntimeError):
    """写入违反 INV，调用方必须让错误冒出去。"""


def content_hash(content: str) -> str:
    """规范化后取 sha256：折叠连续空白、去首尾空白。

    不做大小写/全角归一——那是把两条不同写法判成同一件事，属于"静默合并"，
    比多一条重复记忆更贵。
    """
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WriteResult:
    id: str
    action: str
    seq: int
    superseded: tuple[str, ...] = ()
    arbitration_id: str | None = None
    note: str = ""


@dataclass(frozen=True)
class _Draft:
    id: str
    seq: int
    content: str
    digest: str
    scope: str
    project_id: str | None
    host: str | None
    session_id: str | None
    visibility: str
    layer: str
    kind: str
    channel: str | None
    assertion_type: str
    asserted_by: str
    role: str
    stance: str | None
    fact_key: str | None
    importance: float
    weight: float
    confidence: float
    valid_from: str
    created_at: str
    evidence: list[str]
    pinned: bool


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


class WriteService:
    """所有写操作的唯一入口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务边界；已在事务里时**复用**而不是嵌套开新事务。

        睡眠周期要把"衰减 + 休眠 + 归档 + 清回收站"一次提交：只落一半比完全
        没跑更难排查。SQLite 不支持嵌套 ``BEGIN``，所以这里用 ``in_transaction``
        判断是否需要自己开。

        ``BEGIN IMMEDIATE`` 拿锁失败时有界重试：Windows 上两个进程直连同一个库
        会偶发 ``locked`` / ``readonly``（Q-9）。重试范围只有"开事务"这一步，
        事务已经跑起来的错误照常冒出去——那种情况重试会把半截写入重放。
        """
        if self._conn.in_transaction:
            yield
            return
        self._begin()
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            # 有些错误（SQLITE_FULL / IOERR 一类）会触发 SQLite 自动回滚，
            # 此时再 ROLLBACK 会抛 "no transaction is active"，把原始异常顶掉
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _begin(self) -> None:
        for attempt in range(_BEGIN_ATTEMPTS):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not _is_transient(exc) or attempt == _BEGIN_ATTEMPTS - 1:
                    raise
                time.sleep(_BEGIN_BACKOFF_SECONDS * (attempt + 1))

    # ------------------------------------------------------------------ 写入

    def remember(
        self,
        content: str,
        *,
        scope: str = "global",
        project_id: str | None = None,
        host: str | None = None,
        session_id: str | None = None,
        visibility: str = "shared",
        layer: str = "semantic",
        kind: str = "fact",
        channel: str | None = None,
        assertion_type: str = "observation",
        role: str = "agent",
        stance: str | None = None,
        fact_key: str | None = None,
        importance: float = 0.5,
        confidence: float = 0.6,
        valid_from: str | None = None,
        evidence: tuple[str, ...] = (),
        pinned: bool = False,
        now: datetime | None = None,
    ) -> WriteResult:
        text = content.strip()
        if not text:
            raise ValueError("content 不能为空")

        if scope not in ("global", "project", "host"):
            raise ValueError(f"未知 scope：{scope!r}")
        if scope == "global" and project_id is not None:
            raise ValueError("scope=global 时不能带 project_id")
        if scope == "project" and project_id is None:
            raise ValueError("scope=project 时必须带 project_id")
        if visibility == "private" and not session_id:
            raise ValueError(
                "visibility=private 必须带 session_id：否则这条记忆对谁都不在候选集里，"
                "写进去就再也召回不到"
            )

        # INV6：decision 只能由 role=user 写，agent 写的"结论"最多是 observation
        if assertion_type == "decision" and role != "user":
            raise InvariantViolation("INV6: decision 只能由 role=user 写入")

        # INV4：semantic 必须 ≥1 条 evidence，或 user 明确给出的 decision
        if (
            layer == "semantic"
            and not evidence
            and not (assertion_type == "decision" and role == "user")
        ):
            raise InvariantViolation(
                "INV4: semantic 记忆必须带 evidence；只有 role=user 的 decision 可以免证据"
            )

        created_at = fmt(now) if now is not None else now_ts()
        if valid_from is None:
            start = created_at
        else:
            parsed = parse_ts(valid_from)
            if parsed is None:
                raise ValueError(f"valid_from 不是可解析的时间：{valid_from!r}")
            start = fmt(parsed)

        with self.transaction():
            return self._write_locked(
                text=text,
                scope=scope,
                project_id=project_id,
                host=host,
                session_id=session_id,
                visibility=visibility,
                layer=layer,
                kind=kind,
                channel=channel,
                assertion_type=assertion_type,
                role=role,
                stance=stance,
                fact_key=fact_key,
                importance=importance,
                confidence=confidence,
                valid_from=start,
                created_at=created_at,
                evidence=evidence,
                pinned=pinned,
            )

    # -------------------------------------------------------------- 生命周期

    def touch_recall(
        self,
        memory_ids: Sequence[str],
        *,
        wake_ids: Sequence[str] | None = None,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """召回记账 + 唤醒，两件事分开。

        ``memory_ids``：被注入给模型的全部命中 → 写 ``last_recalled`` / ``recall_count``。
        ``wake_ids``：其中**相关性达唤醒阈值**的部分 → ``dormant`` 回到 ``active``。
        默认两者相同（不传 wake_ids 时全部唤醒）。

        分开的理由（P1 审查 m-4）：Q-8 的口径是"命中即记账"，而唤醒是 §9.5 的
        另一条判据（"被真的想起来"）。弱命中已经注入给模型、参与了本轮回答，
        不计入 §9.2 的晋升触发，会让"用过的记忆永远升不上去"。

        读路径要写库也必须走本模块，不允许 recall 自己 UPDATE（INV1）。
        """
        ids = tuple(dict.fromkeys(memory_ids))
        if not ids:
            return 0
        wake = tuple(dict.fromkeys(wake_ids)) if wake_ids is not None else ids
        wake_set = set(wake) & set(ids)
        stamp = fmt(now) if now is not None else now_ts()
        encoded = json.dumps(list(ids), ensure_ascii=False)
        encoded_wake = json.dumps(sorted(wake_set), ensure_ascii=False)
        touched = 0
        with self.transaction():
            rows = self._conn.execute(
                """
                SELECT id, seq, scope, visibility FROM memories
                WHERE id IN (SELECT value FROM json_each(?))
                  AND status IN ('active', 'dormant')
                """,
                (encoded,),
            ).fetchall()
            if rows:
                self._conn.execute(
                    """
                    UPDATE memories
                    SET last_recalled = ?, recall_count = recall_count + 1,
                        status = CASE
                            WHEN status = 'dormant'
                             AND id IN (SELECT value FROM json_each(?))
                            THEN 'active' ELSE status END
                    WHERE id IN (SELECT value FROM json_each(?))
                      AND status IN ('active', 'dormant')
                    """,
                    (stamp, encoded_wake, encoded),
                )
                for row in rows:
                    self._log_event(
                        int(row["seq"]),
                        stamp,
                        host,
                        session_id,
                        "memory.recalled",
                        str(row["id"]),
                        None,
                        scope=str(row["scope"]),
                        visibility=str(row["visibility"]),
                    )
            touched = len(rows)
        return touched

    def pin(
        self,
        memory_id: str,
        *,
        pinned: bool = True,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """置顶/取消置顶。返回是否真的改了。

        置顶同时豁免 auto-archive 与触顶归档（§9.5）——用户说"这条别动"就该别动。
        """
        stamp = fmt(now) if now is not None else now_ts()
        return self._mutate(
            memory_id,
            stamp,
            host,
            session_id,
            "memory.pinned" if pinned else "memory.unpinned",
            "UPDATE memories SET pinned = ? WHERE id = ? AND pinned <> ?",
            (1 if pinned else 0, memory_id, 1 if pinned else 0),
        )

    def archive(
        self,
        memory_id: str,
        *,
        reason: str = "decay",
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """归档（可逆，INV3 只禁硬删）。归档条目同时退出 FTS 索引（Q-5）。"""
        if reason not in ARCHIVE_REASONS:
            raise ValueError(f"未知的归档原因：{reason!r}，可选 {ARCHIVE_REASONS}")
        stamp = fmt(now) if now is not None else now_ts()
        changed = self._mutate(
            memory_id,
            stamp,
            host,
            session_id,
            "memory.archived",
            """
            UPDATE memories
            SET status = 'archived', archived_at = ?, archived_reason = ?
            WHERE id = ? AND status IN ('active', 'dormant')
            """,
            (stamp, reason, memory_id),
            payload={"reason": reason},
            fts="drop",
        )
        return changed

    def unarchive(
        self,
        memory_id: str,
        *,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """从归档复原：状态回 active，并把正文重新装回 FTS。"""
        stamp = fmt(now) if now is not None else now_ts()
        return self._mutate(
            memory_id,
            stamp,
            host,
            session_id,
            "memory.unarchived",
            """
            UPDATE memories
            SET status = 'active', archived_at = NULL, archived_reason = NULL
            WHERE id = ? AND status = 'archived'
            """,
            (memory_id,),
            fts="rebuild",
        )

    def forget(
        self,
        memory_id: str,
        *,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """遗忘 = 进回收站，不物理删除（INV10：30 天内可复原）。"""
        stamp = fmt(now) if now is not None else now_ts()
        changed = self._mutate(
            memory_id,
            stamp,
            host,
            session_id,
            "memory.forgotten",
            """
            UPDATE memories
            SET status = 'deleted', deleted_at = ?
            WHERE id = ? AND status IN ('active', 'dormant', 'archived')
            """,
            (stamp, memory_id),
            fts="drop",
        )
        return changed

    def restore(
        self,
        memory_id: str,
        *,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """从回收站复原。超过 30 天窗口抛错——静默失败会让人以为"还在"。

        只认 ``status='deleted'`` 且 ``deleted_at`` 在窗口内的行。``deleted_at``
        缺失（P0 老库手工改成 deleted 的行）按"无窗口信息"处理：允许复原，
        因为拒绝复原比多留一条更难解释。
        """
        stamp_dt = now if now is not None else None
        stamp = fmt(stamp_dt) if stamp_dt is not None else now_ts()
        row = self._conn.execute(
            "SELECT deleted_at FROM memories WHERE id = ? AND status = 'deleted'",
            (memory_id,),
        ).fetchone()
        if row is None:
            return False
        deleted_at = parse_ts(None if row["deleted_at"] is None else str(row["deleted_at"]))
        if deleted_at is not None:
            elapsed = (parse_ts(stamp) or deleted_at) - deleted_at
            if elapsed.days >= RECYCLE_WINDOW_DAYS:
                raise ValueError(
                    f"{memory_id} 已超过回收站 {RECYCLE_WINDOW_DAYS} 天窗口，不能复原"
                )
        changed = self._mutate(
            memory_id,
            stamp,
            host,
            session_id,
            "memory.restored",
            "UPDATE memories SET status = 'active', deleted_at = NULL "
            "WHERE id = ? AND status = 'deleted'",
            (memory_id,),
            fts="rebuild",
        )
        return changed

    def purge_expired(
        self,
        *,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """硬删超期回收站条目。这是全系统唯一允许物理删除记忆的地方。

        先清链接与仲裁再删主体：``memory_links`` 与 ``arbitration`` 对 ``memories``
        有外键，顺序错了会撞 ``FOREIGN KEY constraint failed``。仲裁行不是审计底账
        （``events`` 才是），跟着一起清掉不会丢"谁跟谁争过"这件事——它在 event 里。
        """
        stamp = fmt(now) if now is not None else now_ts()
        cutoff = _days_before(stamp, RECYCLE_WINDOW_DAYS)
        purged = 0
        # 预读必须在拿写锁之后：事务外读到的候选，可能在拿锁前被另一个进程
        # restore 掉，随后按旧 id 硬删就把刚复原的记忆删了（P1 审查 m-3）。
        with self.transaction():
            rows = self._conn.execute(
                "SELECT id FROM memories "
                "WHERE status = 'deleted' AND deleted_at IS NOT NULL AND deleted_at <= ?",
                (cutoff,),
            ).fetchall()
            if rows:
                ids = [str(row["id"]) for row in rows]
                encoded = json.dumps(ids, ensure_ascii=False)
                self._conn.execute(
                    "DELETE FROM memory_links WHERE src IN (SELECT value FROM json_each(?)) "
                    "OR dst IN (SELECT value FROM json_each(?))",
                    (encoded, encoded),
                )
                self._conn.execute(
                    "DELETE FROM arbitration WHERE left_id IN (SELECT value FROM json_each(?)) "
                    "OR right_id IN (SELECT value FROM json_each(?))",
                    (encoded, encoded),
                )
                self._conn.execute(
                    "DELETE FROM memories_fts "
                    "WHERE memory_id IN (SELECT value FROM json_each(?))",
                    (encoded,),
                )
                self._conn.execute(
                    "DELETE FROM memories WHERE id IN (SELECT value FROM json_each(?)) "
                    "AND status = 'deleted' AND deleted_at IS NOT NULL AND deleted_at <= ?",
                    (encoded, cutoff),
                )
                self._log_event(
                    self._current_seq(),
                    stamp,
                    host,
                    session_id,
                    "memory.purged",
                    None,
                    {"count": len(ids)},
                )
                purged = len(ids)
        return purged

    def resolve_arbitration(
        self,
        arbitration_id: str,
        resolution: str,
        *,
        by: str = "user",
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """人工裁决一条 open 仲裁，返回胜出条 id（``both`` 时返回空串）。

        §9.3 的终态三条：
        - 输家写 ``valid_to`` = 赢家 ``valid_from`` + ``expired_at`` + ``superseded_by``，
          **行保留、status 不变**（双时间轴，历史可查）；
        - 输家是 claim → ``status=archived``、``archived_reason=refuted``；
        - ``resolution=both``：两段时间各自成立，两条都不动。

        输家若已经失效（``expired_at`` 非空），不再重写——INV3 的触发器会直接
        ABORT，这里提前跳过，把"为什么没动它"留在 event 里。
        """
        if resolution not in RESOLUTIONS:
            raise ValueError(
                f"不支持的裁决结论：{resolution!r}。P1 只开放 {RESOLUTIONS}；"
                "merged 在 §9.3 没有定义合并后的正文归属，不猜"
            )
        stamp = fmt(now) if now is not None else now_ts()
        winner_id = ""
        with self.transaction():
            arb = self._conn.execute(
                "SELECT id, left_id, right_id, status, scope, visibility "
                "FROM arbitration WHERE id = ?",
                (arbitration_id,),
            ).fetchone()
            if arb is None:
                raise ValueError(f"没有这条仲裁：{arbitration_id}")
            if str(arb["status"]) != "open":
                raise ValueError(f"仲裁 {arbitration_id} 已经裁决过，不再重复落终态")

            if resolution in ("left", "right"):
                winner_id = str(arb["left_id"] if resolution == "left" else arb["right_id"])
                loser_id = str(arb["right_id"] if resolution == "left" else arb["left_id"])
                winner = self._fact_row(winner_id)
                if winner is None:
                    raise ValueError(f"胜出条 {winner_id} 不存在，无法裁决")
                if not _covers_now(winner, stamp):
                    raise ValueError(f"胜出条 {winner_id} 已不在有效区间，裁决没有意义")
                self._retire_loser(loser_id, winner, stamp, host, session_id)

            self._conn.execute(
                """
                UPDATE arbitration
                SET status = 'resolved', resolution = ?, resolved_by = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolution, by, stamp, arbitration_id),
            )
            self._log_event(
                self._current_seq(),
                stamp,
                host,
                session_id,
                "arbitration.resolved",
                arbitration_id,
                {"resolution": resolution, "by": by, "winner": winner_id},
                scope=str(arb["scope"]),
                visibility=str(arb["visibility"]),
            )
        return winner_id

    def apply_heat(
        self,
        updates: Sequence[tuple[str, float]],
        *,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """批量结算 heat（衰减的执行器）。整批一个事务，只记一条汇总事件。

        逐条记事件会让 2000 条的库每个睡眠周期产出 2000 行审计噪音，而
        "这一次结算改了哪些行"用 count + 结算时刻就够追溯了。
        """
        if not updates:
            return 0
        stamp = fmt(now) if now is not None else now_ts()
        batch = [(max(0.0, heat), stamp, memory_id) for memory_id, heat in updates]
        changed = 0
        with self.transaction():
            cursor = self._conn.executemany(
                "UPDATE memories SET heat = ?, decayed_at = ? WHERE id = ?", batch
            )
            changed = int(cursor.rowcount)
            self._log_event(
                self._current_seq(),
                stamp,
                host,
                session_id,
                "memory.heat_settled",
                None,
                {"count": changed},
            )
        return changed

    def sweep_status(
        self,
        memory_ids: Sequence[str],
        target: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """批量状态迁移（休眠 / 归档 / 进回收站），一个事务、一条汇总事件。

        目标状态与允许的前态是白名单（``_STATUS_TRANSITIONS``）。批量接口一旦
        能任意写 status，INV3（不硬删）与 INV10（30 天可复原）就只剩注释了。
        """
        ids = tuple(dict.fromkeys(memory_ids))
        if not ids:
            return 0
        allowed = _STATUS_TRANSITIONS.get(target)
        if allowed is None:
            raise ValueError(
                f"批量迁移不支持目标状态：{target!r}，可选 {tuple(_STATUS_TRANSITIONS)}"
            )
        if target == "archived" and reason not in ARCHIVE_REASONS:
            raise ValueError(f"未知的归档原因：{reason!r}，可选 {ARCHIVE_REASONS}")

        stamp = fmt(now) if now is not None else now_ts()
        encoded = json.dumps(list(ids), ensure_ascii=False)
        if target == "dormant":
            clause = "status = 'dormant'"
            extra: tuple[object, ...] = ()
        elif target == "archived":
            clause = "status = 'archived', archived_at = ?, archived_reason = ?"
            extra = (stamp, reason)
        else:
            clause = "status = 'deleted', deleted_at = ?"
            extra = (stamp,)
        previous = ",".join("?" for _ in allowed)
        # 先按下标取出真正会被改到的行：ids 里可能混着已归档或已删除的，
        # 事件里的 count 必须与 UPDATE 实际影响的行数对得上
        rows = self._conn.execute(
            "SELECT id, seq, scope, visibility FROM memories "
            "WHERE id IN (SELECT value FROM json_each(?)) "
            f"AND status IN ({previous})",
            (encoded, *allowed),
        ).fetchall()
        if not rows:
            return 0
        live = json.dumps([str(row["id"]) for row in rows], ensure_ascii=False)
        changed = 0
        with self.transaction():
            cursor = self._conn.execute(
                f"UPDATE memories SET {clause} "
                "WHERE id IN (SELECT value FROM json_each(?)) "
                f"AND status IN ({previous})",
                (*extra, live, *allowed),
            )
            changed = int(cursor.rowcount)
            if target in ("archived", "deleted"):
                self._conn.execute(
                    "DELETE FROM memories_fts "
                    "WHERE memory_id IN (SELECT value FROM json_each(?))",
                    (live,),
                )
            self._log_event(
                self._current_seq(),
                stamp,
                host,
                session_id,
                "memory.status_swept",
                None,
                {"target": target, "count": changed, "reason": reason},
            )
        return changed

    # ------------------------------------------------------------------ 内部

    def _write_locked(
        self,
        *,
        text: str,
        scope: str,
        project_id: str | None,
        host: str | None,
        session_id: str | None,
        visibility: str,
        layer: str,
        kind: str,
        channel: str | None,
        assertion_type: str,
        role: str,
        stance: str | None,
        fact_key: str | None,
        importance: float,
        confidence: float,
        valid_from: str,
        created_at: str,
        evidence: tuple[str, ...],
        pinned: bool,
    ) -> WriteResult:
        digest = content_hash(text)

        deduped = self._dedupe(
            digest,
            scope,
            project_id,
            fact_key,
            created_at,
            evidence,
            host,
            session_id,
            visibility,
        )
        if deduped is not None:
            return deduped

        seq = self._next_seq()
        draft = _Draft(
            id=f"mem-{seq}",
            seq=seq,
            content=text,
            digest=digest,
            scope=scope,
            project_id=project_id,
            host=host,
            session_id=session_id,
            visibility=visibility,
            layer=layer,
            kind=kind,
            channel=channel,
            assertion_type=assertion_type,
            asserted_by=json.dumps(
                {"host": host, "role": role, "session": session_id}, ensure_ascii=False
            ),
            role=role,
            stance=stance,
            fact_key=fact_key,
            importance=importance,
            weight=1.0,
            confidence=confidence,
            valid_from=valid_from,
            created_at=created_at,
            evidence=list(evidence),
            pinned=pinned,
        )

        old = self._current_in_slot(draft) if fact_key else None
        if old is None:
            self._insert(draft, valid_to=None, superseded_by=None)
            self._log_event(
                draft.seq,
                created_at,
                host,
                session_id,
                "memory.created",
                draft.id,
                scope=draft.scope,
                visibility=draft.visibility,
            )
            return WriteResult(id=draft.id, action="created", seq=draft.seq, note=draft.id)

        decision = conflict.route(_view_of(draft), old)
        return self._apply(draft, old, decision)

    def _dedupe(
        self,
        digest: str,
        scope: str,
        project_id: str | None,
        fact_key: str | None,
        created_at: str,
        evidence: tuple[str, ...],
        host: str | None,
        session_id: str | None,
        visibility: str,
    ) -> WriteResult | None:
        """60 秒窗口内的同键同内容 → 合并证据，不新建（§9.1）。

        只认**当前有效**的行：写 A → 改 B → 60 秒内又改回 A 时，A 那行已经失效，
        再"幂等"到它等于把第三次写入静默丢弃（还会把证据写进历史行）。
        也不跨 visibility / session 合并——那会绕过 INV7 的分区。
        """
        if fact_key is None:
            return None
        row = self._conn.execute(
            """
            SELECT id, seq, created_at, evidence FROM memories
            WHERE fact_key = ? AND content_hash = ? AND scope = ?
              AND COALESCE(project_id, '') = COALESCE(?, '')
              AND visibility = ? AND COALESCE(session_id, '') = COALESCE(?, '')
              AND status IN ('active', 'dormant')
              AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY seq DESC LIMIT 1
            """,
            (fact_key, digest, scope, project_id, visibility, session_id, created_at),
        ).fetchone()
        if row is None:
            return None
        if not _within_window(str(row["created_at"]), created_at):
            return None

        merged = _json_list(str(row["evidence"]))
        for item in evidence:
            if item not in merged:
                merged.append(item)
        self._conn.execute(
            "UPDATE memories SET evidence = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), str(row["id"])),
        )
        memory_id = str(row["id"])
        seq = int(row["seq"])
        self._log_event(
            seq,
            created_at,
            host,
            session_id,
            "memory.deduplicated",
            memory_id,
            scope=scope,
            visibility=visibility,
        )
        return WriteResult(
            id=memory_id,
            action="deduplicated",
            seq=seq,
            note=f"{DEDUP_WINDOW_SECONDS}s 内同键同内容，已合并证据",
        )

    def _next_seq(self) -> int:
        self._conn.execute("INSERT OR IGNORE INTO counter (name, value) VALUES ('seq', 0)")
        row = self._conn.execute(
            "UPDATE counter SET value = value + 1 WHERE name = 'seq' RETURNING value"
        ).fetchone()
        if row is None:  # pragma: no cover - RETURNING 一定返回一行
            raise RuntimeError("seq 分配失败：counter 表异常")
        return int(row[0])

    def _current_in_slot(self, draft: _Draft) -> FactView | None:
        row = self._conn.execute(
            """
            SELECT id, assertion_type, content, scope, project_id, asserted_by, seq, valid_from
            FROM memories
            WHERE fact_key = ? AND scope = ?
              AND COALESCE(project_id, '') = COALESCE(?, '')
              AND visibility = ? AND COALESCE(session_id, '') = COALESCE(?, '')
              AND status IN ('active', 'dormant')
              AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY seq DESC LIMIT 1
            """,
            (
                draft.fact_key,
                draft.scope,
                draft.project_id,
                draft.visibility,
                draft.session_id,
                draft.created_at,
            ),
        ).fetchone()
        if row is None:
            return None
        return FactView(
            id=str(row["id"]),
            assertion_type=str(row["assertion_type"]),
            content=str(row["content"]),
            scope=str(row["scope"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            role=role_from_asserted_by(str(row["asserted_by"])),
            seq=int(row["seq"]),
            valid_from=None if row["valid_from"] is None else str(row["valid_from"]),
        )

    def _apply(self, draft: _Draft, old: FactView, decision: conflict.Route) -> WriteResult:
        action = decision.action

        if action == conflict.ACTION_MERGE:
            merged = _json_list(self._fetch_evidence(old.id))
            for item in draft.evidence:
                if item not in merged:
                    merged.append(item)
            # 新表述更完整（是旧文的超集）时，槽位正文升级成它：信息只增不减
            upgraded = len(draft.content) > len(old.content) and conflict.is_superset(
                draft.content, old.content
            )
            if upgraded:
                # 正文换了，content_hash 必须同步，否则完整性校验会误报
                self._conn.execute(
                    """
                    UPDATE memories
                    SET evidence = ?, weight = weight + ?, content = ?, content_hash = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(merged, ensure_ascii=False),
                        MERGE_WEIGHT_STEP,
                        draft.content,
                        draft.digest,
                        old.id,
                    ),
                )
                self._rewrite_index(old.id, draft.content)
            else:
                self._conn.execute(
                    "UPDATE memories SET evidence = ?, weight = weight + ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), MERGE_WEIGHT_STEP, old.id),
                )
            # 没落新行就不该烧掉一个 seq，否则 seq 会留下无主空洞
            self._conn.execute("UPDATE counter SET value = value - 1 WHERE name = 'seq'")
            self._log_event(
                old.seq,
                draft.created_at,
                draft.host,
                draft.session_id,
                "memory.merged",
                old.id,
                {
                    "reason": decision.reason,
                    # 只存摘要不存正文：events 里的 payload 也可能被导出，
                    # 存原文会让 private 记忆从审计表里漏出去
                    "incoming_hash": draft.digest,
                    "upgraded": upgraded,
                },
                scope=draft.scope,
                visibility=draft.visibility,
            )
            return WriteResult(
                id=old.id,
                action="merged",
                seq=old.seq,
                note=f"P2 内容包含，已合并进 {old.id}：{decision.reason}",
            )

        if action == conflict.ACTION_SUPERSEDE_NEW:
            self._insert(draft, valid_to=old.valid_from, superseded_by=old.id)
            # 方向：更晚的 old 压过补录的 draft（旧条才是"当前"）
            self._link(old.id, draft.id, "supersedes")
            self._log_event(
                draft.seq,
                draft.created_at,
                draft.host,
                draft.session_id,
                "memory.superseded_new",
                draft.id,
                {"reason": decision.reason, "kept": old.id},
                scope=draft.scope,
                visibility=draft.visibility,
            )
            return WriteResult(
                id=draft.id,
                action=action,
                seq=draft.seq,
                superseded=(draft.id,),
                note=f"补录历史，{old.id} 仍是当前：{decision.reason}",
            )

        # 其余三种动作都先落新行，再按需处理旧条
        self._insert(draft, valid_to=None, superseded_by=None)
        arbitration_id: str | None = None
        superseded: tuple[str, ...] = ()

        if action == conflict.ACTION_SUPERSEDE_OLD:
            self._conn.execute(
                """
                UPDATE memories
                SET valid_to = ?, expired_at = ?, superseded_by = ?
                WHERE id = ?
                """,
                (draft.valid_from, draft.created_at, draft.id, old.id),
            )
            self._link(draft.id, old.id, "supersedes")
            superseded = (old.id,)
            # 旧条退场可能让本槽位那条 open 仲裁失去一侧 → 顺手落终态，
            # 否则新挑战会挂到一条已经没人争的死对子上（二轮审查 M-1 变体 2）
            self._settle_open_arbitration(draft)

        if action == conflict.ACTION_CONTRADICT:
            self._link(draft.id, old.id, "contradicts")

        if decision.open_arbitration:
            # 同一**槽位**只留一条待裁决：能复用就复用，复用不了（对子已失效）
            # 先给旧的落终态，再开新的——不能像 P0 首版那样挂到死对子上
            existing_id = self._settle_open_arbitration(draft)
            if existing_id is not None:
                arbitration_id = existing_id
            else:
                arbitration_id = f"arb-{draft.seq}"
                self._conn.execute(
                    """
                    INSERT INTO arbitration
                        (id, created_at, fact_key, left_id, right_id, status, resolution,
                         resolved_by, resolved_at, scope, project_id, visibility, session_id)
                    VALUES (?, ?, ?, ?, ?, 'open', NULL, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        arbitration_id,
                        draft.created_at,
                        draft.fact_key,
                        old.id,
                        draft.id,
                        draft.scope,
                        draft.project_id or "",
                        draft.visibility,
                        draft.session_id or "",
                    ),
                )

        self._log_event(
            draft.seq,
            draft.created_at,
            draft.host,
            draft.session_id,
            f"memory.{action}",
            draft.id,
            {"reason": decision.reason, "old": old.id, "state": decision.state},
            scope=draft.scope,
            visibility=draft.visibility,
        )
        return WriteResult(
            id=draft.id,
            action=action,
            seq=draft.seq,
            superseded=superseded,
            arbitration_id=arbitration_id,
            note=decision.reason,
        )

    def _is_current(self, memory_id: str, now: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM memories
            WHERE id = ? AND status IN ('active', 'dormant')
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to IS NULL OR valid_to > ?)
            """,
            (memory_id, now, now),
        ).fetchone()
        return row is not None

    def _settle_open_arbitration(self, draft: _Draft) -> str | None:
        """返回本槽位仍可复用的 open 仲裁 id；对子已失效则先落终态并返回 ``None``。

        必须按**槽位 + 分区**查：只按 fact_key 查会让两个项目、两个私有 session 的
        同名键共用一条仲裁，新对子既没有记账、召回时又被静默丢掉（二轮审查 M-1）。
        """
        row = self._conn.execute(
            """
            SELECT id, left_id, right_id FROM arbitration
            WHERE status = 'open' AND scope = ? AND project_id = ?
              AND fact_key = ? AND visibility = ? AND session_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                draft.scope,
                draft.project_id or "",
                draft.fact_key,
                draft.visibility,
                draft.session_id or "",
            ),
        ).fetchone()
        if row is None:
            return None
        left_id = str(row["left_id"])
        right_id = str(row["right_id"])
        left_live = self._is_current(left_id, draft.created_at)
        right_live = self._is_current(right_id, draft.created_at)
        if left_live and right_live:
            return str(row["id"])
        # 对子只剩一侧还在当前区间：争议已经不存在，按存续侧落终态。
        # 两侧都不在（只能由人工改库造成）时记 both，避免写一条假的方向。
        resolution = "left" if left_live else ("right" if right_live else "both")
        self._resolve_arbitration(str(row["id"]), resolution, draft)
        return None

    def _resolve_arbitration(self, arbitration_id: str, resolution: str, draft: _Draft) -> None:
        self._conn.execute(
            """
            UPDATE arbitration
            SET status = 'resolved', resolution = ?, resolved_by = 'system:b-route',
                resolved_at = ?
            WHERE id = ?
            """,
            (resolution, draft.created_at, arbitration_id),
        )
        self._log_event(
            draft.seq,
            draft.created_at,
            draft.host,
            draft.session_id,
            "arbitration.auto_resolved",
            arbitration_id,
            {"resolution": resolution, "by": "system:b-route"},
            scope=draft.scope,
            visibility=draft.visibility,
        )

    def _insert(self, draft: _Draft, *, valid_to: str | None, superseded_by: str | None) -> None:
        self._conn.execute(
            """
            INSERT INTO memories (
                id, scope, project_id, host, session_id, visibility, layer, kind, channel,
                assertion_type, asserted_by, stance, fact_key, content, importance, weight,
                confidence, status, archived_reason, valid_from, valid_to, created_at,
                expired_at, superseded_by, seq, pinned, half_life_days, heat, evidence,
                content_hash, last_recalled, recall_count
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, 'active', NULL, ?, ?, ?,
                ?, ?, ?, ?, NULL, 1.0, ?,
                ?, NULL, 0
            )
            """,
            (
                draft.id,
                draft.scope,
                draft.project_id,
                draft.host,
                draft.session_id,
                draft.visibility,
                draft.layer,
                draft.kind,
                draft.channel,
                draft.assertion_type,
                draft.asserted_by,
                draft.stance,
                draft.fact_key,
                draft.content,
                draft.importance,
                draft.weight,
                draft.confidence,
                draft.valid_from,
                valid_to,
                draft.created_at,
                draft.created_at if superseded_by is not None else None,
                superseded_by,
                draft.seq,
                1 if draft.pinned else 0,
                json.dumps(draft.evidence, ensure_ascii=False),
                draft.digest,
            ),
        )
        self._conn.execute(
            "INSERT INTO memories_fts (memory_id, content_2gram) VALUES (?, ?)",
            (draft.id, index_text(draft.content)),
        )

    def _link(self, src: str, dst: str, link_type: str) -> None:
        self._conn.execute(
            """
            INSERT INTO memory_links (src, dst, type, weight, co_activation, last_fired)
            VALUES (?, ?, ?, 1.0, 0.0, NULL)
            ON CONFLICT (src, dst, type) DO UPDATE SET weight = weight + 0.08
            """,
            (src, dst, link_type),
        )

    def _fetch_evidence(self, memory_id: str) -> str:
        row = self._conn.execute(
            "SELECT evidence FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - 调用方刚查到这一行
            return "[]"
        return str(row["evidence"])

    def _rewrite_index(self, memory_id: str, content: str) -> None:
        self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
        self._conn.execute(
            "INSERT INTO memories_fts (memory_id, content_2gram) VALUES (?, ?)",
            (memory_id, index_text(content)),
        )

    def _mutate(
        self,
        memory_id: str,
        stamp: str,
        host: str | None,
        session_id: str | None,
        event_type: str,
        sql: str,
        params: tuple[object, ...],
        *,
        payload: dict[str, object] | None = None,
        fts: str | None = None,
    ) -> bool:
        """单条状态变更的统一外壳：存在性检查 + 事务 + event + FTS 维护。

        ``fts`` 必须和状态变更在**同一个事务**里做：分成两次提交的话，
        状态已改而索引没改（或反过来），库与索引就不一致了，而且没有报错。
        """
        row = self._conn.execute(
            "SELECT seq, scope, visibility FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"没有这条记忆：{memory_id}")
        changed = False
        with self.transaction():
            cursor = self._conn.execute(sql, params)
            changed = int(cursor.rowcount) > 0
            if changed:
                if fts == "drop":
                    self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
                elif fts == "rebuild":
                    content = self._conn.execute(
                        "SELECT content FROM memories WHERE id = ?", (memory_id,)
                    ).fetchone()
                    if content is not None:
                        self._rewrite_index(memory_id, str(content["content"]))
                self._log_event(
                    int(row["seq"]),
                    stamp,
                    host,
                    session_id,
                    event_type,
                    memory_id,
                    payload,
                    scope=str(row["scope"]),
                    visibility=str(row["visibility"]),
                )
        return changed

    def _fact_row(self, memory_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT id, seq, assertion_type, status, valid_from, valid_to, expired_at, "
            "scope, visibility FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        return row

    def _retire_loser(
        self,
        loser_id: str,
        winner: sqlite3.Row,
        stamp: str,
        host: str | None,
        session_id: str | None,
    ) -> None:
        """裁决终态：让输家退出"当前"，但行保留（§9.3）。

        ``expired_at`` 一旦写过就不可改（INV3 触发器），所以已经失效的输家只记
        一条事件、不重写——重复裁决不该撞出 ABORT。
        """
        loser = self._fact_row(loser_id)
        if loser is None:  # pragma: no cover - 仲裁对子有外键，正常不会缺
            return
        scope = str(loser["scope"])
        visibility = str(loser["visibility"])
        if loser["expired_at"] is not None:
            self._log_event(
                int(loser["seq"]),
                stamp,
                host,
                session_id,
                "arbitration.loser_already_expired",
                loser_id,
                {"keeper": str(winner["id"])},
                scope=scope,
                visibility=visibility,
            )
            return

        refuted = str(loser["assertion_type"]) == "claim"
        boundary = _retire_boundary(winner["valid_from"], loser["valid_from"], stamp)
        self._conn.execute(
            """
            UPDATE memories
            SET valid_to = ?, expired_at = ?, superseded_by = ?
            WHERE id = ?
            """,
            (boundary, stamp, str(winner["id"]), loser_id),
        )
        self._link(str(winner["id"]), loser_id, "supersedes")
        if refuted and str(loser["status"]) != "deleted":
            self._conn.execute(
                """
                UPDATE memories
                SET status = 'archived', archived_reason = 'refuted', archived_at = ?
                WHERE id = ? AND status IN ('active', 'dormant')
                """,
                (stamp, loser_id),
            )
            self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (loser_id,))
        self._log_event(
            int(loser["seq"]),
            stamp,
            host,
            session_id,
            "arbitration.loser_retired",
            loser_id,
            {"superseded_by": str(winner["id"]), "refuted": refuted},
            scope=scope,
            visibility=visibility,
        )

    def _drop_index(self, memory_id: str) -> None:
        self._conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))

    def _current_seq(self) -> int:
        row = self._conn.execute("SELECT value FROM counter WHERE name = 'seq'").fetchone()
        return int(row["value"]) if row is not None else 0

    def _log_event(
        self,
        seq: int,
        ts: str,
        host: str | None,
        session: str | None,
        event_type: str,
        target: str | None,
        payload: dict[str, object] | None = None,
        *,
        scope: str | None = None,
        visibility: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO events (seq, ts, host, session, type, target, payload, scope, visibility)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                ts,
                host,
                session,
                event_type,
                target,
                None if payload is None else json.dumps(payload, ensure_ascii=False),
                scope,
                visibility,
            ),
        )


def _retire_boundary(winner_from: object, loser_from: object, stamp: str) -> str:
    """裁决终态的左边界（§9.3「终态的区间几何」）。

    唯一硬约束：``valid_to >= 输家 valid_from``。写成倒挂区间等于宣布这条历史
    从未成立，而 ``expired_at`` 的不可变触发器让它再也改不回来（P1 审查 M-1）。

    - 赢家更晚生效（新压旧）→ 在赢家生效处让位
    - 赢家更早生效（旧条胜出）→ 撑到被裁决为止：输家从自己生效到裁决之间
      **确实被注入过**，"它错了"由 superseded_by / refuted 承载，不由负区间承载
    - 时间缺失或异常 → 裁决时刻，再兜一层不早于输家自己的生效时刻
    """
    started = parse_ts(None if loser_from is None else str(loser_from))
    upstream = parse_ts(None if winner_from is None else str(winner_from))
    decided = parse_ts(stamp)
    boundary: datetime | None

    if upstream is not None and started is not None and upstream >= started:
        boundary = upstream
    else:
        boundary = decided if decided is not None else started
    if started is not None and (boundary is None or boundary < started):
        boundary = started
    return stamp if boundary is None else fmt(boundary)


def _days_before(stamp: str, days: int) -> str:
    parsed = parse_ts(stamp)
    if parsed is None:  # pragma: no cover - stamp 由 fmt 生成，一定可解析
        return stamp
    from datetime import timedelta

    return fmt(parsed - timedelta(days=days))


def _covers_now(row: sqlite3.Row, stamp: str) -> bool:
    """这一行是否落在当前有效区间内（与 §9.4 候选池谓词同口径）。"""
    if str(row["status"]) not in ("active", "dormant"):
        return False
    now = parse_ts(stamp)
    if now is None:  # pragma: no cover
        return False
    start = parse_ts(None if row["valid_from"] is None else str(row["valid_from"]))
    end = parse_ts(None if row["valid_to"] is None else str(row["valid_to"]))
    if start is not None and start > now:
        return False
    return end is None or end > now


def _within_window(previous: str, current: str) -> bool:
    before = parse_ts(previous)
    after = parse_ts(current)
    if before is None or after is None:
        return False
    return abs((after - before).total_seconds()) <= DEDUP_WINDOW_SECONDS


def _view_of(draft: _Draft) -> FactView:
    return FactView(
        id=draft.id,
        assertion_type=draft.assertion_type,
        content=draft.content,
        scope=draft.scope,
        project_id=draft.project_id,
        role=draft.role,
        seq=draft.seq,
        valid_from=draft.valid_from,
    )
