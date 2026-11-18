"""P1.5c：``SessionStart`` 开场块——非查询选取、可见性、预算、不记账。

开场块是**唯一不看 prompt 的注入路径**，所以它有两条独有风险：

- 看不到 prompt 意味着**没人会去核内容对不对**，可见性漏一个轴就会长期静默泄漏；
- 它是无条件背景，一旦记账就会让重要度最高的那几条永远不老（§9.5 失效）。
两条都在下面钉住。
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from nebula.core.viewer import Viewer
from nebula.hook.payload import parse
from nebula.hook.render import SESSION_MAX_CHARS
from nebula.hook.session_start import main, run
from nebula.recall.core import core_memories
from nebula.store.write_service import WriteResult

T0 = datetime(2026, 5, 1, 10, 0, 0)

Invoke = Callable[[str], "tuple[int, str, str]"]
Write = Callable[..., WriteResult]


def _project_dir(tmp_path: Path, *, project_id: str = "p-demo", name: str = "demo") -> Path:
    """造一个带 ``.nebula/project.json`` 的项目（不依赖 git 命令）。"""
    root = tmp_path / name
    (root / ".nebula").mkdir(parents=True)
    (root / ".nebula" / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    return root


def _raw(cwd: Path, *, session: str = "s-1", source: str = "startup") -> str:
    return json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": session,
            "cwd": str(cwd),
            "source": source,
            "transcript_path": "/tmp/t.jsonl",
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def invoke(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> Invoke:
    def _invoke(raw: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        code = main(["--db", str(db_path)])
        return code, out.getvalue(), err.getvalue()

    return _invoke


def _logs(conn: sqlite3.Connection) -> list[tuple[str, str | None, str]]:
    rows = conn.execute(
        "SELECT query, session, memory_ids FROM injection_log ORDER BY ts"
    ).fetchall()
    return [(str(row["query"]), row["session"], str(row["memory_ids"])) for row in rows]


def _seed(write: Write) -> None:
    """核心 / 本项目 / 他项目 / 私有 / 他宿主 / 全局普通，各一条。"""
    write(
        "接口统一用 POST",
        assertion_type="decision",
        role="user",
        scope="global",
        pinned=True,
        importance=1.0,
        now=T0,
    )
    write("构建命令为 npm run build", scope="project", project_id="p-demo", importance=0.9, now=T0)
    write("构建命令为 npm run make", scope="project", project_id="p-other", importance=0.99, now=T0)
    write(
        "私密吐槽一句",
        scope="project",
        project_id="p-demo",
        visibility="private",
        session_id="s-2",
        now=T0,
    )
    write("别的宿主的约定", scope="host", host="other-host", importance=0.98, now=T0)
    write("全局普通背景", scope="global", importance=0.97, now=T0)


# --------------------------------------------------------------------- 契约


def test_block_shape_and_project_label(invoke: Invoke, write: Write, tmp_path: Path) -> None:
    _seed(write)
    root = _project_dir(tmp_path)

    code, out, err = invoke(_raw(root))

    assert code == 0
    assert out.startswith("[记忆|会话开始] 项目 demo")
    assert "〔核心〕" in out
    assert "〔本项目〕" in out
    assert "接口统一用 POST" in out
    assert "构建命令为 npm run build" in out
    assert len(out) <= SESSION_MAX_CHARS
    assert "[nebula-hook] 开场 2 条" in err


def test_only_pinned_and_current_project_are_selected(
    invoke: Invoke, write: Write, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    _seed(write)
    root = _project_dir(tmp_path)

    _, out, _ = invoke(_raw(root))

    assert "npm run make" not in out  # 他项目
    assert "私密吐槽" not in out  # 他人 private
    assert "别的宿主的约定" not in out  # 他宿主
    assert "全局普通背景" not in out  # 全局但既非 pinned 也不属于本项目


def test_core_section_comes_first(invoke: Invoke, write: Write, tmp_path: Path) -> None:
    _seed(write)
    root = _project_dir(tmp_path)

    _, out, _ = invoke(_raw(root))

    assert out.index("〔核心〕") < out.index("〔本项目〕")
    assert out.index("接口统一用 POST") < out.index("npm run build")


# --------------------------------------------------------- 日志与不记账（§10.7）


def test_injection_log_marks_the_session_start_query(
    invoke: Invoke, write: Write, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    _seed(write)
    root = _project_dir(tmp_path)

    invoke(_raw(root, session="s-9", source="resume"))

    logged = _logs(conn)
    assert len(logged) == 1
    query, session, ids = logged[0]
    assert query == "session-start:resume"  # 统计脚本靠前缀把开场与对话轮次分开
    assert session == "s-9"
    assert len(json.loads(ids)) == 2


def test_session_start_does_not_record_recall_count(
    invoke: Invoke, write: Write, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """开场块是无条件背景，不构成"这条被用到"的证据；记了账它们就永远不老。"""
    _seed(write)
    root = _project_dir(tmp_path)

    invoke(_raw(root))
    invoke(_raw(root, session="s-2", source="resume"))

    rows = conn.execute("SELECT recall_count FROM memories").fetchall()
    assert [row[0] for row in rows] == [0] * len(rows)


# ----------------------------------------------------------------- 空与失败


def test_empty_library_injects_nothing_but_still_logs(
    invoke: Invoke, tmp_path: Path, conn: sqlite3.Connection
) -> None:
    code, out, err = invoke(_raw(_project_dir(tmp_path)))

    assert code == 0
    assert out == ""
    assert "无内容" in err
    assert _logs(conn) == [("session-start:startup", "s-1", "[]")]


def test_broken_database_is_fail_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(_raw(_project_dir(tmp_path))))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    code = main(["--db", str(tmp_path)])

    assert code == 0
    assert out.getvalue() == ""
    assert "[nebula-hook] 失败·" in err.getvalue()


# ------------------------------------------------------------------- 预算


def test_budget_caps_the_block_and_says_how_many(
    write: Write, tmp_path: Path, db_path: Path
) -> None:
    """两段都塞满（各 5 条长正文）时，10 条 × 120 字装不进 1200 字符。"""
    for index in range(5):
        write(
            f"置顶约定第{index}条：" + "很长的说明" * 30,
            assertion_type="decision",
            role="user",
            scope="global",
            pinned=True,
            now=T0,
        )
        write(
            f"项目约定第{index}条：" + "很长的说明" * 30,
            scope="project",
            project_id="p-demo",
            importance=0.5,
            now=T0,
        )

    outcome = run(parse(_raw(_project_dir(tmp_path))), str(db_path), now=T0)

    assert outcome.context is not None
    assert len(outcome.context) <= SESSION_MAX_CHARS
    assert "条未列出" in outcome.context
    assert outcome.context.count("（mem-") < 10  # 确实有整条没进来，不是缩了字数硬塞


# --------------------------------------------------------- 非查询选取单元用例


def test_core_memories_without_project_only_takes_pinned(
    write: Write, conn: sqlite3.Connection
) -> None:
    _seed(write)

    hits = core_memories(conn, Viewer(host="workbuddy"), now="2026-05-01T10:00:00+00:00")

    assert [hit.content for hit in hits] == ["接口统一用 POST"]


def test_core_memories_orders_by_importance_within_the_project(
    write: Write, conn: sqlite3.Connection
) -> None:
    write("低重要度", scope="project", project_id="p-demo", importance=0.2, now=T0)
    write("高重要度", scope="project", project_id="p-demo", importance=0.9, now=T0)

    hits = core_memories(
        conn,
        Viewer(project_id="p-demo", host="workbuddy"),
        now="2026-05-01T10:00:00+00:00",
    )

    assert [hit.content for hit in hits] == ["高重要度", "低重要度"]
    assert all(hit.relevance == 0.0 for hit in hits)  # 没有查询，就没有相关性
