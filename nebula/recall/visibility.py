"""读路径的可见性口径（§10.6）。

一句话：**读路径与召回同一条谓词**。召回早就按 ``visibility`` / ``scope`` 过滤候选池，
而 ``expand`` / ``explain`` 在 P1 之前是本地 CLI 工具、调用方就是库的主人，所以没过滤。
P1.5 把它们交给 agent 调之后，这个前提消失——凭一个 id 就能读到别的 session 的私有片段。

三条落地约定：

1. **默认拒绝**：不传 ``Viewer`` 等于"没有身份"，此时 ``private`` 条目一律不可见。
   忘了传的失败模式必须是"看不到"，不能是"全泄漏"。
2. **不可见要说出来**：抛 :class:`MemoryNotVisible`，由调用方转成一句人话。
   静默返回空结构会让人以为这条不存在，那是记忆系统最不该犯的错。
3. **连带数据同口径**：历代版本、关联边、审计事件都过同一个函数，不另立规则。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol


class _RowLike(Protocol):
    """``sqlite3.Row`` 与其子集都满足：只需要按下标取值。"""

    def __getitem__(self, key: str) -> object: ...


@dataclass(frozen=True)
class Viewer:
    """调用方身份。三个字段都可缺省，缺省即"该维度什么都看不到"。"""

    session_id: str | None = None
    project_id: str | None = None
    host: str | None = None


class MemoryNotVisible(ValueError):
    """条目存在但当前调用方无权读。

    继承 ``ValueError``：CLI 的 ``main`` 已经统一捕获 ValueError 打印 ``错误：…``
    并返回 2，不需要为它单开一条错误通道。
    """

    def __init__(self, memory_id: str, reason: str) -> None:
        super().__init__(f"该条目对当前调用方不可见：{memory_id}（{reason}）")
        self.memory_id = memory_id
        self.reason = reason


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def deny_reason(row: _RowLike, viewer: Viewer) -> str | None:
    """返回不可见的原因；``None`` 表示可见。

    顺序是**先可见性、后作用域**：私有分区的约束比作用域更强，先判它能保证
    private 条目即便落在 global 也不会因为作用域放行而漏出去。
    """
    visibility = _text(row["visibility"])
    if visibility == "private":
        owner = _text(row["session_id"])
        if viewer.session_id is None or owner != viewer.session_id:
            caller = (
                f"当前调用方是 {viewer.session_id}"
                if viewer.session_id
                else "当前调用方没有 session 身份"
            )
            return f"私有分区：只对 session={owner} 可见，{caller}"

    scope = _text(row["scope"])
    if scope == "global":
        return None
    if scope == "project":
        if viewer.project_id is not None and _text(row["project_id"]) == viewer.project_id:
            return None
        return f"属于另一个项目（project={_text(row['project_id'])}），当前调用方没有该项目的上下文"
    if scope == "host":
        if viewer.host is not None and _text(row["host"]) == viewer.host:
            return None
        return f"属于另一个宿主（host={_text(row['host'])}）"
    return f"未知的 scope：{scope!r}"


def event_deny_reason(row: _RowLike, viewer: Viewer) -> str | None:
    """审计事件用同一口径；缺分区列的历史行按"归属未知"处理，不返回。

    events 的列名与 memories 不同（``session`` 而非 ``session_id``，v4 才补上
    ``project_id``），所以单独一个函数，但规则逐条对应 :func:`deny_reason`。
    """
    scope = _text(row["scope"])
    visibility = _text(row["visibility"])
    if scope is None or visibility is None:
        return "历史事件没有分区信息（归属未知），不对外返回"

    if visibility == "private":
        owner = _text(row["session"])
        if viewer.session_id is None or owner != viewer.session_id:
            return "事件属于他人的私有分区"

    if scope == "global":
        return None
    if scope == "project":
        if viewer.project_id is not None and _text(row["project_id"]) == viewer.project_id:
            return None
        return "事件属于另一个项目"
    if scope == "host":
        if viewer.host is not None and _text(row["host"]) == viewer.host:
            return None
        return "事件属于另一个宿主"
    return f"事件的分区未知：{scope!r}"


def visible_ids(
    conn: sqlite3.Connection, ids: tuple[str, ...], viewer: Viewer
) -> set[str]:
    """批量筛出可见的 id（关联边与历史版本用）。

    一次 ``IN`` 查询，不逐条命中数据库：expand 的输出里关联边可能几十条，
    每条查一次会把"看一眼来历"变成几十次 IO。
    """
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT id, scope, project_id, host, session_id, visibility FROM memories "
        f"WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return {str(row["id"]) for row in rows if deny_reason(row, viewer) is None}
