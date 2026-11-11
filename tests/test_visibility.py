"""P1.5a：读路径可见性口径（§10.6，R-8 收口）。

这一组用例的全部意义是"会红"：把默认身份换成"能看到全部"，每一条都应当挂。
所以断言不只看返回结构，还要看**输出里没有原文**——泄漏通常不是结构错，是某一行
顺手把 content 拼进去了。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from nebula.core.timeutil import fmt
from nebula.recall.detail import expand
from nebula.recall.explain import explain
from nebula.recall.visibility import MemoryNotVisible, Viewer
from nebula.store.write_service import WriteResult, WriteService

BASE = datetime(2026, 5, 1, tzinfo=UTC)
SECRET = "私有片段：端口 9222"


def _private(write: Callable[..., WriteResult], content: str = SECRET, **kwargs: object) -> str:
    kwargs.setdefault("visibility", "private")
    kwargs.setdefault("session_id", "s-1")
    return write(content, **kwargs).id


def test_private_memory_invisible_without_identity(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = _private(write)
    with pytest.raises(MemoryNotVisible) as excinfo:
        expand(conn, memory_id)
    # 异常里只许说"看不到"，不许带正文
    assert SECRET not in str(excinfo.value)
    assert "不可见" in str(excinfo.value)
    assert "s-1" in excinfo.value.reason


def test_private_memory_visible_to_owner(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = _private(write)
    result = expand(conn, memory_id, viewer=Viewer(session_id="s-1"))
    assert result.memory.content == SECRET

    with pytest.raises(MemoryNotVisible):
        expand(conn, memory_id, viewer=Viewer(session_id="s-2"))


def test_project_memory_needs_matching_project(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = write("构建命令是 npm run build", scope="project", project_id="p-1").id

    with pytest.raises(MemoryNotVisible, match="另一个项目"):
        expand(conn, memory_id, viewer=Viewer(project_id="p-2"))
    # 没有项目上下文 ≠ 看得见所有项目
    with pytest.raises(MemoryNotVisible):
        expand(conn, memory_id)

    assert expand(conn, memory_id, viewer=Viewer(project_id="p-1")).memory.id == memory_id


def test_host_memory_needs_matching_host(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = write("这个宿主的快捷键是 Ctrl+K", scope="host", host="claude-code").id
    with pytest.raises(MemoryNotVisible, match="另一个宿主"):
        expand(conn, memory_id, viewer=Viewer(host="opencode"))
    assert expand(conn, memory_id, viewer=Viewer(host="claude-code")).memory.id == memory_id


def test_global_shared_memory_needs_no_identity(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = write("记忆库用 SQLite 存储").id
    assert expand(conn, memory_id).memory.id == memory_id


def test_expand_hides_history_from_another_session(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """同槽位的历代版本也要过口径：留一条就泄漏了"另一个 session 改过这个键"。"""
    mine = _private(write, "端口是 9222", fact_key="debug.port", now=BASE)
    theirs = _private(
        write,
        "端口是 9333",
        fact_key="debug.port",
        session_id="s-2",
        now=BASE + timedelta(days=1),
    )

    result = expand(conn, mine, viewer=Viewer(session_id="s-1"))
    assert [entry.id for entry in result.history] == [mine]
    assert result.hidden_history == 1
    text = "\n".join(result.lines())
    assert "9333" not in text
    assert "另有 1 个历史版本对该调用方不可见" in text

    # 换一个有全部权限的本地 CLI 视角，两条都在——证明上面是过滤而非数据缺失
    everything = expand(conn, mine, show_all=True)
    assert {entry.id for entry in everything.history} == {mine, theirs}
    assert everything.hidden_history == 0


def test_expand_hides_links_to_invisible_neighbours(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    visible = write("接口用 POST").id
    hidden = _private(write, "接口其实是 PUT")
    # 测试准备数据直接插边：这里要测的是读路径过滤，不是写路径怎么建边
    conn.execute(
        "INSERT INTO memory_links (src, dst, type) VALUES (?, ?, 'supports')",
        (visible, hidden),
    )

    result = expand(conn, visible)
    assert all(hidden not in (link.src, link.dst) for link in result.links)
    assert expand(conn, visible, show_all=True).links != ()


def test_expand_hides_events_without_partition(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = write("随便一条").id
    conn.execute(
        "INSERT INTO events (seq, ts, type, target) VALUES (1, ?, 'legacy.event', ?)",
        (fmt(BASE), memory_id),
    )
    result = expand(conn, memory_id)
    assert all(event.type != "legacy.event" for event in result.events)
    everything = expand(conn, memory_id, show_all=True)
    assert any(event.type == "legacy.event" for event in everything.events)


def test_project_events_need_project_context(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    """事件跟着条目的分区走：换一个项目上下文，事件也不能被读出来。"""
    memory_id = write("项目内的决定", scope="project", project_id="p-1").id
    with pytest.raises(MemoryNotVisible):
        expand(conn, memory_id)

    result = expand(conn, memory_id, viewer=Viewer(project_id="p-1"))
    assert any(event.type == "memory.created" for event in result.events)


def test_explain_short_circuits_on_invisible(
    conn: sqlite3.Connection,
    write: Callable[..., WriteResult],
    service: WriteService,
) -> None:
    """不可见时只给一条理由：连"已归档"都不说，理由集合也是信息。"""
    memory_id = _private(write)
    service.archive(memory_id, reason="decay", now=BASE)

    result = explain(conn, memory_id)
    assert result.recallable is False
    assert len(result.reasons) == 1
    assert "私有分区" in result.reasons[0]
    assert "归档" not in "".join(result.reasons)
    assert result.detail is None
    assert SECRET not in "\n".join(result.lines())

    # 有身份时照常判后续闸门
    owner = explain(conn, memory_id, viewer=Viewer(session_id="s-1"))
    assert owner.recallable is False
    assert any("归档" in reason for reason in owner.reasons)
    assert owner.detail is not None


def test_show_all_is_the_explicit_local_escape(
    conn: sqlite3.Connection, write: Callable[..., WriteResult]
) -> None:
    memory_id = _private(write)
    with pytest.raises(MemoryNotVisible):
        expand(conn, memory_id)
    assert expand(conn, memory_id, show_all=True).memory.content == SECRET
