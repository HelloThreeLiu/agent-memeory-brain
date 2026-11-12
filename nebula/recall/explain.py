"""可解释性（§9.6 / M6）：一条记忆"为什么召得到 / 为什么召不到"。

``expand`` 摊开来历，``explain`` 给判定。判定按 §9.4 的候选池谓词逐条过——
凡是能让一条记忆进不了 top-k 的闸，这里都必须有一个对应的理由，否则用户看到
的就是"我明明记得有这条，它却不见了"。

顺序即优先级：先讲状态（删/归档），再讲时间窗，最后讲分区与席位。

**席位闸必须与 ``recall._select_seats`` 同口径**。P1 审查 M-2 抓到过反例：
open 仲裁的对子召回时双注入、explain 却判"被占席"，两个功能对同一事实
给出相反答案。所以这里显式对齐三件事：双注入例外、时间基、同档不硬判。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from nebula.core.assertion import rank as assertion_rank
from nebula.core.timeutil import now_ts, parse_ts
from nebula.core.viewer import Viewer, deny_reason
from nebula.recall.detail import ExpandResult, MemoryDetail, expand


@dataclass(frozen=True)
class Explanation:
    memory_id: str
    recallable: bool
    reasons: tuple[str, ...]
    #: 不可见时为 ``None``：判定本身不需要正文。把内容装进对象再"记得不打印"，
    #: 是以后泄漏的起点（§10.6）。
    detail: ExpandResult | None = None

    def lines(self) -> tuple[str, ...]:
        head = "可召回" if self.recallable else "不可召回"
        out = [f"{head}：{self.memory_id}"]
        out.extend(f"  - {reason}" for reason in self.reasons)
        if self.detail is not None:
            out.append("")
            out.extend(self.detail.lines())
        return tuple(out)


def explain(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    viewer_session: str | None = None,
    viewer: Viewer | None = None,
    show_all: bool = False,
    now: str | None = None,
) -> Explanation:
    """按候选池谓词逐条检查，返回第一处拦截原因与完整来历。

    ``viewer_session`` 与 ``recall`` 同义：不传就当作"没有私有分区身份"，
    private 条目会被判为不可见（这才是真实调用方的默认处境）。
    ``viewer`` 是 P1.5 的完整身份（session + project + host）；两个都给时以 ``viewer`` 为准。

    **不可见时短路**：只给"不可见"这一条理由，不再判状态、时间窗与席位。
    理由集合本身也是信息——"它在回收站里"不该被别的分区反推出来。
    """
    who = viewer if viewer is not None else Viewer(session_id=viewer_session)
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise ValueError(f"没有这条记忆：{memory_id}")

    if not show_all:
        denied = deny_reason(row, who)
        if denied is not None:
            return Explanation(
                memory_id=memory_id, recallable=False, reasons=(denied,), detail=None
            )

    detail = expand(conn, memory_id, viewer=who, show_all=show_all)
    mem = detail.memory
    stamp = now or now_ts()
    parsed_now = parse_ts(stamp)

    reasons: list[str] = []
    blocked = False

    if mem.status == "deleted":
        blocked = True
        reasons.append(
            f"在回收站（{mem.deleted_at or '时间未知'} 起）：30 天内可 restore，之后才物理删"
        )
    elif mem.status == "archived":
        blocked = True
        suffix = f"，原因 {mem.archived_reason}" if mem.archived_reason else ""
        reasons.append(f"已归档（{mem.archived_at or '时间未知'}{suffix}）：不进候选池")

    start = parse_ts(mem.valid_from)
    end = parse_ts(mem.valid_to)
    if parsed_now is not None:
        if start is not None and start > parsed_now:
            blocked = True
            reasons.append(f"尚未生效：valid_from = {mem.valid_from}")
        if end is not None and end <= parsed_now:
            blocked = True
            successor = f"，被 {mem.superseded_by} 取代" if mem.superseded_by else ""
            reasons.append(f"已失效：valid_to = {mem.valid_to}{successor}")

    # 私有分区那一闸已由 deny_reason 在上方短路，这里不再重复判——两处同一条规则
    # 迟早会分叉（P1 审查 M-2 的教训：explain 与 recall 各写一份，结论就相反）。

    if not blocked:
        seat = _seat_check(conn, mem, parsed_now)
        if seat.blocker is not None:
            blocked = True
            reasons.append(seat.blocker)
        else:
            reasons.extend(seat.notes)

    if not blocked:
        reasons.append("四道闸（状态 / 时间窗 / 分区 / 席位）都没拦，应当能被召回")
        reasons.append(
            f"命中与否取决于查询相关性：当前 importance {mem.importance:.2f}、"
            f"weight {mem.weight:.2f}、heat {mem.heat:.3f}"
        )

    return Explanation(
        memory_id=memory_id,
        recallable=not blocked,
        reasons=tuple(reasons),
        detail=detail,
    )


@dataclass(frozen=True)
class _SeatCheck:
    blocker: str | None
    notes: tuple[str, ...] = ()


def _seat_check(
    conn: sqlite3.Connection, mem: MemoryDetail, now: datetime | None
) -> _SeatCheck:
    """一席制判定。

    三处与 ``recall._select_seats`` 对齐（P1 审查 M-2）：

    1. **open 仲裁对子双注入**：召回时它们享受例外，这里不能判占席；
    2. **时间基用传入的 now**，不偷用 ``now_ts()``——否则 ``explain(now=某天)``
       会在时间窗一处用传入值、席位一处用真实时间，自相矛盾；
    3. **同档不判**：席位同档时比综合分（含 relevance），而 explain 没有查询
       语境，算不出 relevance。硬用 ``seq`` 代替会给出与召回相反的结论，
       所以只提示"取决于查询"，不拦。
    """
    if mem.fact_key is None:
        return _SeatCheck(None)

    partner = _open_pair_partner(conn, mem.id, now)
    if partner is not None:
        return _SeatCheck(
            None, (f"与 {partner} 在 open 仲裁中：一席制双注入例外，两条都会注入",)
        )

    rows = conn.execute(
        """
        SELECT id, assertion_type, seq, valid_from, valid_to, status FROM memories
        WHERE fact_key = ? AND scope = ?
          AND COALESCE(project_id, '') = COALESCE(?, '')
        ORDER BY seq
        """,
        (mem.fact_key, mem.scope, mem.project_id),
    ).fetchall()
    mine = assertion_rank(mem.assertion_type)
    same_rank = False
    for row in rows:
        if str(row["id"]) == mem.id:
            continue
        if not _covers(row, now):
            continue
        other = assertion_rank(str(row["assertion_type"]))
        if other > mine:
            return _SeatCheck(
                f"同槽位被 {row['id']}（{row['assertion_type']}，seq={row['seq']}）"
                "占席：档位更高"
            )
        if other == mine:
            same_rank = True

    notes: tuple[str, ...] = ()
    if same_rank:
        notes = ("同槽位有同档条目：席位按综合分决定，explain 无查询语境不判定",)
    return _SeatCheck(None, notes)


def _open_pair_partner(
    conn: sqlite3.Connection, memory_id: str, now: datetime | None
) -> str | None:
    """本条若是某条 open 仲裁的对子（且对侧仍在当前区间），返回对侧 id。"""
    rows = conn.execute(
        "SELECT left_id, right_id FROM arbitration "
        "WHERE status = 'open' AND (left_id = ? OR right_id = ?)",
        (memory_id, memory_id),
    ).fetchall()
    for row in rows:
        left = str(row["left_id"])
        other_id = str(row["right_id"]) if left == memory_id else left
        other = conn.execute(
            "SELECT status, valid_from, valid_to FROM memories WHERE id = ?",
            (other_id,),
        ).fetchone()
        if other is not None and _covers(other, now):
            return other_id
    return None


def _covers(row: sqlite3.Row, now: datetime | None) -> bool:
    """这一行是否落在当前有效区间（与 §9.4 候选池谓词同口径）。"""
    if str(row["status"]) not in ("active", "dormant"):
        return False
    if now is None:
        return True
    start = parse_ts(None if row["valid_from"] is None else str(row["valid_from"]))
    end = parse_ts(None if row["valid_to"] is None else str(row["valid_to"]))
    if start is not None and start > now:
        return False
    return end is None or end > now
