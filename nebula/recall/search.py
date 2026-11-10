"""召回：候选池谓词 + FTS5 检索 + 一席制（设计文档 §9.4）。

三条硬规则都在这里落地，缺一条 G1 就当场翻车：

1. **候选池只取有效区间覆盖当前时刻的条目**（v0.3 只排除了 archived，被取代的条
   照样进 top-k，二轮评审的致命 2）；
2. **同一 fact_key 最多占 1 席，席位按档位固定优先**（decision > observation >
   claim > hypothesis），同档才比综合分——否则新写的 claim 会靠时近性把 decision
   挤掉，agent 回头再问用户一遍；
3. **落选的异议不许静默吞掉**：与占席条 contradicts 的落选 claim，给一行指针。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from nebula.core import scoring
from nebula.core.assertion import rank as assertion_rank
from nebula.core.assertion import role_from_asserted_by
from nebula.core.timeutil import now_ts
from nebula.core.tokenize import query_tokens

#: FTS 先粗捞再精排：候选上限与 limit 的关系
CANDIDATE_FACTOR = 20
MIN_CANDIDATES = 100


@dataclass(frozen=True)
class Hit:
    id: str
    content: str
    assertion_type: str
    role: str | None
    fact_key: str | None
    layer: str
    importance: float
    relevance: float
    score: float
    seq: int
    pinned: bool
    pointer_count: int = 0

    def format_line(self) -> str:
        """注入格式（§9.4）：``[decision|user] 内容（id）``。

        claim / hypothesis 一律带"未验证"标记——这是防"意见变事实"的最后一道闸，
        P0 必须先立住格式，P1 接宿主时才有东西可稳定。
        """
        role = self.role or "unknown"
        marker = "|未验证" if self.assertion_type in ("claim", "hypothesis") else ""
        return f"[{self.assertion_type}|{role}{marker}] {self.content}（{self.id}）"


@dataclass(frozen=True)
class RecallResult:
    query: str
    hits: tuple[Hit, ...]
    candidate_count: int
    elapsed_ms: float
    degraded: bool = False

    def lines(self) -> tuple[str, ...]:
        rendered: list[str] = []
        for hit in self.hits:
            rendered.append(hit.format_line())
            if hit.pointer_count:
                rendered.append(f"  另有 {hit.pointer_count} 条未验证异议（expand 查看）")
        return tuple(rendered)


@dataclass
class _Candidate:
    hit: Hit
    rank: float
    slot: str
    doc_tokens: set[str] = field(default_factory=set)


def recall(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 5,
    *,
    scope: str | None = None,
    project_id: str | None = None,
    viewer_session: str | None = None,
    now: str | None = None,
) -> RecallResult:
    """按查询取 top-k。

    ``viewer_session`` 决定 ``visibility=private`` 的条目能不能进候选集（INV7）：
    私有片段只对同一 session 可见，不是取回来再过滤。
    """
    started = time.perf_counter()
    tokens = query_tokens(query)
    stamp = now or now_ts()
    if not tokens:
        return RecallResult(query=query, hits=(), candidate_count=0, elapsed_ms=_ms(started))

    candidate_limit = max(limit * CANDIDATE_FACTOR, MIN_CANDIDATES)
    match_expression = " OR ".join(f'"{token}"' for token in tokens)
    fts_rows = conn.execute(
        """
        SELECT memory_id, bm25(memories_fts) AS rank, content_2gram
        FROM memories_fts
        WHERE memories_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match_expression, candidate_limit),
    ).fetchall()
    if not fts_rows:
        return RecallResult(query=query, hits=(), candidate_count=0, elapsed_ms=_ms(started))

    rank_by_id: dict[str, float] = {}
    tokens_by_id: dict[str, set[str]] = {}
    for row in fts_rows:
        memory_id = str(row["memory_id"])
        rank_by_id[memory_id] = float(row["rank"])
        tokens_by_id[memory_id] = set(str(row["content_2gram"]).split())

    rows = _fetch_pool(
        conn,
        tuple(rank_by_id),
        stamp=stamp,
        scope=scope,
        project_id=project_id,
        viewer_session=viewer_session,
    )

    candidates: list[_Candidate] = []
    for row in rows:
        memory_id = str(row["id"])
        doc_tokens = tokens_by_id.get(memory_id, set())
        relevance = scoring.coverage(tokens, doc_tokens)
        pinned = bool(row["pinned"])
        if relevance < scoring.MIN_RELEVANCE and not pinned:
            continue
        importance = float(row["importance"])
        layer = str(row["layer"])
        hit = Hit(
            id=memory_id,
            content=str(row["content"]),
            assertion_type=str(row["assertion_type"]),
            role=role_from_asserted_by(str(row["asserted_by"])),
            fact_key=None if row["fact_key"] is None else str(row["fact_key"]),
            layer=layer,
            importance=importance,
            relevance=relevance,
            score=scoring.score(
                relevance=relevance, importance=importance, layer=layer, pinned=pinned
            ),
            seq=int(row["seq"]),
            pinned=pinned,
        )
        candidates.append(
            _Candidate(
                hit=hit,
                rank=rank_by_id[memory_id],
                slot=_slot_of(row),
                doc_tokens=doc_tokens,
            )
        )

    seated, pointers = _select_seats(conn, candidates)
    seated.sort(key=lambda item: (-item.hit.score, item.rank, -item.hit.seq))
    hits = tuple(
        Hit(
            id=item.hit.id,
            content=item.hit.content,
            assertion_type=item.hit.assertion_type,
            role=item.hit.role,
            fact_key=item.hit.fact_key,
            layer=item.hit.layer,
            importance=item.hit.importance,
            relevance=item.hit.relevance,
            score=item.hit.score,
            seq=item.hit.seq,
            pinned=item.hit.pinned,
            pointer_count=pointers.get(item.hit.id, 0),
        )
        for item in seated[:limit]
    )
    return RecallResult(
        query=query,
        hits=hits,
        candidate_count=len(candidates),
        elapsed_ms=_ms(started),
    )


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _slot_of(row: sqlite3.Row) -> str:
    """席位 = 作用域内的 fact_key。

    fact_key 只在 scope + project 内唯一（§9.3 P1：两个项目各有一条 build.cmd，
    不该互相更替）。读路径若只按 fact_key 分组，就把写路径好不容易分开的槽位
    又合并成一席——p1 的决定会从 top-k 里凭空消失。
    """
    fact_key = row["fact_key"]
    if fact_key is None:
        return f"__id__:{row['id']}"
    project = row["project_id"] or ""
    return f"{row['scope']}\x00{project}\x00{fact_key}"


def _fetch_pool(
    conn: sqlite3.Connection,
    ids: tuple[str, ...],
    *,
    stamp: str,
    scope: str | None,
    project_id: str | None,
    viewer_session: str | None,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in ids)
    conditions = [
        "status IN ('active', 'dormant')",
        "(valid_from IS NULL OR valid_from <= ?)",
        "(valid_to IS NULL OR valid_to > ?)",
        "(visibility = 'shared' OR session_id = ?)",
        f"id IN ({placeholders})",
    ]
    params: list[object] = [stamp, stamp, viewer_session, *ids]
    if scope is not None:
        conditions.append("scope = ?")
        params.append(scope)
    if project_id is not None:
        # 项目视图 = 该项目的条目 + 全局条目；scope=host 的条目不属于任何项目
        conditions.append("(scope = 'global' OR project_id = ?)")
        params.append(project_id)
    sql = (
        "SELECT id, content, assertion_type, asserted_by, fact_key, layer, importance, "
        "pinned, seq, scope, project_id FROM memories WHERE " + " AND ".join(conditions)
    )
    rows: list[sqlite3.Row] = conn.execute(sql, params).fetchall()
    return rows


def _open_pairs(conn: sqlite3.Connection, ids: tuple[str, ...]) -> set[frozenset[str]]:
    """仍在 open 的仲裁对：它们享受双注入例外，不参与一席制淘汰。"""
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT left_id, right_id FROM arbitration "
        f"WHERE status = 'open' AND left_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {frozenset((str(row["left_id"]), str(row["right_id"]))) for row in rows}


def _select_seats(
    conn: sqlite3.Connection, candidates: list[_Candidate]
) -> tuple[list[_Candidate], dict[str, int]]:
    groups: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.slot, []).append(candidate)

    open_pairs = _open_pairs(conn, tuple(candidate.hit.id for candidate in candidates))
    seated: list[_Candidate] = []
    dropped: list[_Candidate] = []

    for members in groups.values():
        if len(members) == 1:
            seated.append(members[0])
            continue
        # 唯一双注入例外：仍在 open 仲裁的**对子**两条都留。
        # 不能"组内有对子就整组放行"——那会给一个槽位开出 3 席，正是 G1 翻车的形态。
        paired = [
            member
            for member in members
            if any(
                other is not member
                and frozenset((member.hit.id, other.hit.id)) in open_pairs
                for other in members
            )
        ]
        if paired:
            paired_ids = {member.hit.id for member in paired}
            seated.extend(paired)
            dropped.extend(member for member in members if member.hit.id not in paired_ids)
            continue
        members.sort(
            key=lambda item: (
                -assertion_rank(item.hit.assertion_type),
                -item.hit.score,
                -item.hit.seq,
            )
        )
        seated.append(members[0])
        dropped.extend(members[1:])

    return seated, _pointers(conn, seated, dropped)


def _pointers(
    conn: sqlite3.Connection, seated: list[_Candidate], dropped: list[_Candidate]
) -> dict[str, int]:
    """统计每条占席条有多少条落选异议指向它（§9.4 的"不许静默吞掉"）。"""
    if not dropped or not seated:
        return {}
    dropped_ids = tuple(item.hit.id for item in dropped)
    placeholders = ",".join("?" for _ in dropped_ids)
    rows = conn.execute(
        "SELECT src, dst FROM memory_links "
        f"WHERE type = 'contradicts' AND src IN ({placeholders})",
        dropped_ids,
    ).fetchall()
    seat_ids = {item.hit.id for item in seated}
    counts: dict[str, int] = {}
    for row in rows:
        dst = str(row["dst"])
        if dst in seat_ids:
            counts[dst] = counts.get(dst, 0) + 1
    return counts
