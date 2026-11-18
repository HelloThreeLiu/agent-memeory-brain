"""开场块的非查询选取（§10.7「SessionStart」，P1.5c 评审 m-7）。

现有召回全部是 FTS 驱动的——没有查询词就没有候选。开场块要的恰恰是"不用问也在那里"
的东西，所以这是一条**新查询路径**：pinned + 本项目 top-N。

既然是新路径，就必须复用 §10.6 的谓词（:func:`visibility_predicate` /
:func:`scope_predicate`），**不许自己拼 WHERE**：漏一个轴，private 或他项目的条目就会从
开场块里漏出去，而开场块是最不容易被察觉的一条路径（它不看 prompt，人不会去核）。

两条刻意的取舍：

1. **只按 importance / heat 排序，不算相关性**。没有查询，算相关性没有意义；硬拿
   prompt 去算，开场块就变成了"第一轮召回"，与 §10.7「内容小而稳定」冲突。
2. **``Hit.relevance`` 一律 0.0**。它在这里只承担"要不要唤醒 dormant"的判断
   （:func:`nebula.recall.search.record_hits` 用 ``WAKE_RELEVANCE`` 比它），而开场块
   本来就不记账（§10.7），给个假的相关系数只会误导后来的人。
"""

from __future__ import annotations

import sqlite3

from nebula.core.assertion import role_from_asserted_by
from nebula.core.timeutil import now_ts
from nebula.core.viewer import Viewer, deny_reason, scope_predicate, visibility_predicate
from nebula.recall.search import Hit

#: 〔核心〕最多几条：pinned 是用户明确说"这条别动"的，理应少而稳
CORE_PINNED = 5

#: 〔本项目〕最多几条：再多就不是"背景"而是"检索结果"了
CORE_PROJECT = 5

_COLUMNS = (
    "id, content, assertion_type, asserted_by, fact_key, layer, importance, "
    "weight, confidence, pinned, seq, scope, project_id, session_id, host, visibility"
)


def core_memories(
    conn: sqlite3.Connection,
    viewer: Viewer,
    *,
    now: str | None = None,
    pinned_limit: int = CORE_PINNED,
    project_limit: int = CORE_PROJECT,
) -> tuple[Hit, ...]:
    """按"核心在前、本项目在后"的顺序取开场块的内容。

    ``viewer.project_id`` 为空（非 git 目录 / 解析失败）时只取 pinned——**不猜项目**，
    也不拿全局 top-N 凑数：那等于把别的项目的结论当背景注入。
    """
    stamp = now or now_ts()
    picked: list[Hit] = []
    seen: set[str] = set()

    groups: list[list[sqlite3.Row]] = [
        _select(conn, viewer, stamp, pinned_limit, section="pinned")
    ]
    if viewer.project_id is not None:
        groups.append(_select(conn, viewer, stamp, project_limit, section="project"))

    for rows in groups:
        for row in rows:
            memory_id = str(row["id"])
            if memory_id in seen:
                continue
            seen.add(memory_id)
            picked.append(_hit_of(row))
    return tuple(picked)


def _select(
    conn: sqlite3.Connection,
    viewer: Viewer,
    stamp: str,
    limit: int,
    *,
    section: str,
) -> list[sqlite3.Row]:
    """按分区取候选。返回前再用 :func:`deny_reason` 复核一遍（与 §9.4 同一条规矩：

    复核只能"再删"不能"补回"，分叉时表现为少注入而不是泄漏。
    """
    conditions = [
        "status IN ('active', 'dormant')",
        "(valid_from IS NULL OR valid_from <= ?)",
        "(valid_to IS NULL OR valid_to > ?)",
    ]
    params: list[object] = [stamp, stamp]

    visibility_sql, visibility_params = visibility_predicate(viewer)
    conditions.append(visibility_sql)
    params.extend(visibility_params)

    if section == "pinned":
        conditions.append("pinned = 1")
    else:
        # 收窄到本项目分区：这是 scope_predicate 的子集，不是替代
        conditions.append("scope = 'project' AND project_id = ?")
        params.append(viewer.project_id)

    scope_sql, scope_params = scope_predicate(viewer)
    conditions.append(scope_sql)
    params.extend(scope_params)

    params.append(limit)
    # 表名与列名是常量，条件全部来自谓词函数与占位符——没有字符串拼接的取值
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM memories WHERE "
        + " AND ".join(conditions)
        + " ORDER BY importance DESC, heat DESC, seq DESC LIMIT ?",
        params,
    ).fetchall()
    return [row for row in rows if deny_reason(row, viewer) is None]


def _hit_of(row: sqlite3.Row) -> Hit:
    importance = float(row["importance"])
    return Hit(
        id=str(row["id"]),
        content=str(row["content"]),
        assertion_type=str(row["assertion_type"]),
        role=role_from_asserted_by(str(row["asserted_by"])),
        fact_key=None if row["fact_key"] is None else str(row["fact_key"]),
        layer=str(row["layer"]),
        importance=importance,
        relevance=0.0,
        score=importance,
        seq=int(row["seq"]),
        pinned=bool(row["pinned"]),
        weight=float(row["weight"]),
        confidence=float(row["confidence"]),
    )
