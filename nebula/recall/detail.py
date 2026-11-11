"""单条记忆的完整来历（§9.4 的 ``expand``）。

``expand`` 回答的是"这条从哪来、跟谁争过、后来怎么被取代的"。它是纯读：
不更新 heat、不记账、不改状态，所以可以随便查。

与 ``recall`` 的分工：``recall`` 给的是"现在该信什么"，``expand`` 给的是
"为什么现在信这个"。前者要短，后者要全。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from nebula.core.assertion import role_from_asserted_by


def _json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


@dataclass(frozen=True)
class MemoryDetail:
    id: str
    content: str
    scope: str
    project_id: str | None
    host: str | None
    session_id: str | None
    visibility: str
    layer: str
    kind: str
    assertion_type: str
    role: str | None
    stance: str | None
    fact_key: str | None
    status: str
    archived_reason: str | None
    importance: float
    weight: float
    confidence: float
    heat: float
    seq: int
    pinned: bool
    valid_from: str | None
    valid_to: str | None
    created_at: str
    expired_at: str | None
    archived_at: str | None
    deleted_at: str | None
    superseded_by: str | None
    recall_count: int
    last_recalled: str | None
    evidence: tuple[str, ...]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MemoryDetail:
        return cls(
            id=str(row["id"]),
            content=str(row["content"]),
            scope=str(row["scope"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            host=None if row["host"] is None else str(row["host"]),
            session_id=None if row["session_id"] is None else str(row["session_id"]),
            visibility=str(row["visibility"]),
            layer=str(row["layer"]),
            kind=str(row["kind"]),
            assertion_type=str(row["assertion_type"]),
            role=role_from_asserted_by(str(row["asserted_by"])),
            stance=None if row["stance"] is None else str(row["stance"]),
            fact_key=None if row["fact_key"] is None else str(row["fact_key"]),
            status=str(row["status"]),
            archived_reason=(
                None if row["archived_reason"] is None else str(row["archived_reason"])
            ),
            importance=float(row["importance"]),
            weight=float(row["weight"]),
            confidence=float(row["confidence"]),
            heat=float(row["heat"]),
            seq=int(row["seq"]),
            pinned=bool(row["pinned"]),
            valid_from=None if row["valid_from"] is None else str(row["valid_from"]),
            valid_to=None if row["valid_to"] is None else str(row["valid_to"]),
            created_at=str(row["created_at"]),
            expired_at=None if row["expired_at"] is None else str(row["expired_at"]),
            archived_at=None if row["archived_at"] is None else str(row["archived_at"]),
            deleted_at=None if row["deleted_at"] is None else str(row["deleted_at"]),
            superseded_by=(
                None if row["superseded_by"] is None else str(row["superseded_by"])
            ),
            recall_count=int(row["recall_count"]),
            last_recalled=(
                None if row["last_recalled"] is None else str(row["last_recalled"])
            ),
            evidence=_json_list(None if row["evidence"] is None else str(row["evidence"])),
        )

    def header_line(self) -> str:
        role = self.role or "unknown"
        marker = "|未验证" if self.assertion_type in ("claim", "hypothesis") else ""
        return f"[{self.assertion_type}|{role}{marker}] {self.content}"


@dataclass(frozen=True)
class LinkRow:
    src: str
    dst: str
    type: str
    weight: float


@dataclass(frozen=True)
class EventRow:
    ts: str
    type: str
    payload: str | None


@dataclass(frozen=True)
class ArbitrationRow:
    id: str
    status: str
    resolution: str | None
    resolved_by: str | None
    left_id: str
    right_id: str


@dataclass(frozen=True)
class ExpandResult:
    memory: MemoryDetail
    history: tuple[MemoryDetail, ...]
    links: tuple[LinkRow, ...]
    events: tuple[EventRow, ...]
    arbitrations: tuple[ArbitrationRow, ...]

    def lines(self) -> tuple[str, ...]:
        """人读的展开文本。第一行是注入格式，后面是"为什么"。"""
        mem = self.memory
        out: list[str] = [mem.header_line()]
        out.append(
            f"  {mem.id} · {mem.status} · {mem.layer}/{mem.kind} · "
            f"scope={mem.scope}"
            + (f"/{mem.project_id}" if mem.project_id else "")
            + f" · {mem.visibility}"
        )
        out.append(
            f"  生效 {mem.valid_from or '?'} → {mem.valid_to or '现在'}"
            f" · 写入 seq={mem.seq}"
        )
        out.append(
            f"  重要度 {mem.importance:.2f} · 权重 {mem.weight:.2f}"
            f" · 置信 {mem.confidence:.2f} · heat {mem.heat:.3f}"
        )
        out.append(f"  召回 {mem.recall_count} 次" + (
            f"，最近 {mem.last_recalled}" if mem.last_recalled else ""
        ))
        if mem.archived_reason:
            out.append(f"  归档原因：{mem.archived_reason}（{mem.archived_at}）")
        if mem.deleted_at:
            out.append(f"  进回收站：{mem.deleted_at}")
        if mem.superseded_by:
            out.append(f"  被取代：{mem.superseded_by}")

        if mem.evidence:
            out.append("  证据：" + "、".join(mem.evidence))
        else:
            out.append("  证据：无（semantic 条目不应当出现这种情况）")

        if len(self.history) > 1:
            out.append("  历代版本：")
            for entry in self.history:
                mark = "← 当前" if entry.id == mem.id else ""
                out.append(
                    f"    {entry.id} [{entry.status}] {entry.valid_from or '?'}"
                    f" → {entry.valid_to or '现在'} {entry.content[:40]}{mark}"
                )

        if self.links:
            out.append("  关联：")
            for link in self.links:
                out.append(f"    {link.type}: {link.src} → {link.dst}（{link.weight:.2f}）")

        if self.arbitrations:
            out.append("  仲裁：")
            for arb in self.arbitrations:
                outcome = arb.resolution or "待裁决"
                out.append(
                    f"    {arb.id} [{arb.status}/{outcome}] "
                    f"{arb.left_id} vs {arb.right_id}"
                )

        if self.events:
            out.append("  审计事件：")
            for event in self.events:
                suffix = f" {event.payload}" if event.payload else ""
                out.append(f"    {event.ts} {event.type}{suffix}")

        return tuple(out)


def expand(conn: sqlite3.Connection, memory_id: str) -> ExpandResult:
    """按 id 展开。找不到抛 ``ValueError``——静默返回空会让人以为"查过了没问题"。"""
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有这条记忆：{memory_id}")
    detail = MemoryDetail.from_row(row)

    history: tuple[MemoryDetail, ...] = (detail,)
    if detail.fact_key is not None:
        rows = conn.execute(
            """
            SELECT * FROM memories
            WHERE fact_key = ? AND scope = ?
              AND COALESCE(project_id, '') = COALESCE(?, '')
            ORDER BY seq
            """,
            (detail.fact_key, detail.scope, detail.project_id),
        ).fetchall()
        history = tuple(MemoryDetail.from_row(item) for item in rows)

    links = tuple(
        LinkRow(
            src=str(item["src"]),
            dst=str(item["dst"]),
            type=str(item["type"]),
            weight=float(item["weight"]),
        )
        for item in conn.execute(
            "SELECT src, dst, type, weight FROM memory_links "
            "WHERE src = ? OR dst = ? ORDER BY type, src, dst",
            (memory_id, memory_id),
        ).fetchall()
    )

    events = tuple(
        EventRow(
            ts=str(item["ts"]),
            type=str(item["type"]),
            payload=None if item["payload"] is None else str(item["payload"]),
        )
        for item in conn.execute(
            "SELECT ts, type, payload FROM events WHERE target = ? ORDER BY id",
            (memory_id,),
        ).fetchall()
    )

    arbitrations = tuple(
        ArbitrationRow(
            id=str(item["id"]),
            status=str(item["status"]),
            resolution=None if item["resolution"] is None else str(item["resolution"]),
            resolved_by=None if item["resolved_by"] is None else str(item["resolved_by"]),
            left_id=str(item["left_id"]),
            right_id=str(item["right_id"]),
        )
        for item in conn.execute(
            "SELECT id, status, resolution, resolved_by, left_id, right_id "
            "FROM arbitration WHERE left_id = ? OR right_id = ? ORDER BY created_at",
            (memory_id, memory_id),
        ).fetchall()
    )

    return ExpandResult(
        memory=detail,
        history=history,
        links=links,
        events=events,
        arbitrations=arbitrations,
    )
