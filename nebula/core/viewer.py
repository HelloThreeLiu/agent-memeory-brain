"""可见性口径（§10.6）：读路径与写侧共用的一条规则。

一句话：**能召回的东西，和能展开的东西，是同一个集合。**

放在 ``core`` 而不是 ``recall``，是因为写侧也要用它（``record_feedback`` 不该允许
对看不见的条目写反馈），而 ``store`` 反向依赖 ``recall`` 是坏方向。这里只有判定与
SQL 片段，没有业务 I/O。

三条落地约定：

1. **默认拒绝**：不传 ``Viewer`` 等于"没有身份"，``private`` 与 ``project`` / ``host``
   一律不可见。忘了传的失败模式必须是"看不到"，不能是"全泄漏"。
2. **两处实现必须同结果**：:func:`deny_reason` 是 Python 判定，:func:`scope_predicate`
   与 :func:`visibility_predicate` 是 SQL 片段。候选池走 SQL（性能），读单条走
   Python；两者由 ``tests/test_visibility.py`` 的一致性用例钉住，分叉即变红。
3. **不可见时说"不可见"，但不报别人的身份**：原因里只出现维度名，不出现对方的
   session id / project hash / host 名——那些本身也是信息。
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

    措辞里只有维度名，没有对方的标识（审查 m-2）：把 owner 的 session id 回给
    调用方，等于用一个"看不见"的消息告诉他"存在一个属于 s-3 的东西"。
    """
    if _text(row["visibility"]) == "private":
        owner = _text(row["session_id"])
        if viewer.session_id is None or owner != viewer.session_id:
            return "visibility 不匹配（private 分区：只对写入方会话可见）"

    scope = _text(row["scope"])
    if scope == "global":
        return None
    if scope == "project":
        if viewer.project_id is not None and _text(row["project_id"]) == viewer.project_id:
            return None
        return "scope 不匹配（属于另一个项目）"
    if scope == "host":
        if viewer.host is not None and _text(row["host"]) == viewer.host:
            return None
        return "scope 不匹配（属于另一个宿主）"
    return f"未知的 scope：{scope!r}"


def event_deny_reason(row: _RowLike, viewer: Viewer) -> str | None:
    """审计事件用同一口径；缺分区列的历史行按"归属未知"处理，不返回。

    events 的列名与 memories 不同（``session`` 而非 ``session_id``），所以单独一个
    函数，但规则逐条对应 :func:`deny_reason`。
    """
    scope = _text(row["scope"])
    visibility = _text(row["visibility"])
    if scope is None or visibility is None:
        return "历史事件没有分区信息（归属未知），不对外返回"

    if visibility == "private":
        owner = _text(row["session"])
        if viewer.session_id is None or owner != viewer.session_id:
            return "visibility 不匹配（事件属于他人的私有分区）"

    if scope == "global":
        return None
    if scope == "project":
        if viewer.project_id is not None and _text(row["project_id"]) == viewer.project_id:
            return None
        return "scope 不匹配（事件属于另一个项目）"
    if scope == "host":
        if viewer.host is not None and _text(row["host"]) == viewer.host:
            return None
        return "scope 不匹配（事件属于另一个宿主）"
    return f"事件的分区未知：{scope!r}"


def visibility_predicate(viewer: Viewer) -> tuple[str, tuple[object, ...]]:
    """``visibility`` 维度的 SQL 片段（对应 :func:`deny_reason` 的第一段）。

    ``session_id = NULL`` 在 SQL 里既不真也不假，所以 private 行对没有身份的
    调用方自然不命中——与 Python 侧的 ``owner != viewer.session_id`` 同义。
    """
    return "(visibility = 'shared' OR session_id = ?)", (viewer.session_id,)


def scope_predicate(viewer: Viewer) -> tuple[str, tuple[object, ...]]:
    """``scope`` 维度的 SQL 片段。

    **缺 project 或 host 时对应的分区整段不出现**，这正是 §10.6 的"没给 project
    → 只看得到 global"。P1.5a 初版写成了"project_id 为 None 就不加任何 scope
    条件"，等于把 loose 视图放大成全库视图（审查 C-1）。
    """
    clauses = ["scope = 'global'"]
    params: list[object] = []
    if viewer.project_id is not None:
        clauses.append("(scope = 'project' AND project_id = ?)")
        params.append(viewer.project_id)
    if viewer.host is not None:
        clauses.append("(scope = 'host' AND host = ?)")
        params.append(viewer.host)
    return "(" + " OR ".join(clauses) + ")", tuple(params)


def visible_ids(conn: sqlite3.Connection, ids: tuple[str, ...], viewer: Viewer) -> set[str]:
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
