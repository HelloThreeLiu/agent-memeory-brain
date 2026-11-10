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

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._write_locked(
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
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return result

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
            self._log_event(draft.seq, created_at, host, session_id, "memory.created", draft.id)
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
        self._log_event(seq, created_at, host, session_id, "memory.deduplicated", memory_id)
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
                    # 只存摘要不存正文：events 没有 scope/visibility 列，
                    # 存原文会让 private 记忆从审计表里漏出去
                    "incoming_hash": draft.digest,
                    "upgraded": upgraded,
                },
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

    def _log_event(
        self,
        seq: int,
        ts: str,
        host: str | None,
        session: str | None,
        event_type: str,
        target: str | None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO events (seq, ts, host, session, type, target, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                ts,
                host,
                session,
                event_type,
                target,
                None if payload is None else json.dumps(payload, ensure_ascii=False),
            ),
        )


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
