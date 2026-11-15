"""P1.5c：``UserPromptSubmit`` hook 的契约、记账、去重与 fail-open。

对应设计文档 §10.7 与方案 §10 的测试计划。三类断言要特别当回事，因为它们的失败
模式都是"看起来正常"：

- **内部消息不落任何行**：落了行，覆盖率分母就被宿主自己的通知灌满；
- **同一 prompt 两轮只记一次账**：多记会让 ``last_recalled`` 永不老化，§9.5 的
  30 天遗忘永远不触发，且没有事后修复工具；
- **失败一律退出 0**：hook 抛非零退出码会拦住用户的消息，记忆系统就成了可用性单点。
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nebula.core.timeutil import fmt
from nebula.hook.payload import Payload, internal_message, parse
from nebula.hook.prompt_submit import QUERY_MAX_CHARS, main, run
from nebula.hook.render import LINE_MAX_CHARS, PROMPT_MAX_CHARS, render_prompt_block
from nebula.recall.search import Hit
from nebula.store.write_service import WriteResult, WriteService

#: 可注入的"当前时刻"。**种数据必须用同一个 T0**：`recall` 的候选池有时间窗谓词
#: （§9.4），记忆若按真实时间写入，在 T0 那一刻还没生效，用例会看到"无命中"。
T0 = datetime(2026, 5, 1, 10, 0, 0)

Invoke = Callable[[str], "tuple[int, str, str]"]
Write = Callable[..., WriteResult]


def _payload(prompt: str, *, session: str = "s-1", cwd: str | None = None) -> Payload:
    return Payload(
        event="UserPromptSubmit",
        session_id=session,
        cwd=cwd,
        prompt=prompt,
        transcript_path=None,
    )


def _raw(prompt: str, *, session: str = "s-1", cwd: str | None = None) -> str:
    return json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "cwd": cwd,
            "prompt": prompt,
            "transcript_path": "/tmp/t.jsonl",
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def invoke(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> Invoke:
    """走 ``main`` 的完整路径（stdin → stdout/stderr），返回 (退出码, stdout, stderr)。"""

    def _invoke(raw: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        code = main(["--db", str(db_path)])
        return code, out.getvalue(), err.getvalue()

    return _invoke


def _injections(conn: sqlite3.Connection) -> list[tuple[str, str | None, str]]:
    rows = conn.execute(
        "SELECT query, session, memory_ids FROM injection_log ORDER BY ts"
    ).fetchall()
    return [(str(row["query"]), row["session"], str(row["memory_ids"])) for row in rows]


# --------------------------------------------------------------------- 契约


def test_main_emits_hook_specific_output_with_the_block(
    invoke: Invoke, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global")

    code, out, err = invoke(_raw("构建命令是什么"))

    assert code == 0
    data = json.loads(out)
    context = data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert context.startswith("[记忆|本次检索]")
    assert "mem-1" in context
    assert "feedback" in context  # 尾注：纠错入口
    assert len(context) <= PROMPT_MAX_CHARS
    assert "[nebula-hook] 注入 1 条" in err
    logged = _injections(conn)
    assert len(logged) == 1
    assert logged[0][2] == '["mem-1"]'


def test_query_is_truncated_in_the_log(
    invoke: Invoke, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global")

    invoke(_raw("构建命令" * 100))

    assert len(_injections(conn)[0][0]) == QUERY_MAX_CHARS


# ------------------------------------------------------- 内部消息（探针新发现）


def test_internal_message_is_skipped_without_any_side_effect(
    invoke: Invoke, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global")

    code, out, err = invoke(
        _raw("<task-notification><task-id>x</task-id><status>completed</status></task-notification>")
    )

    assert code == 0
    assert out == ""
    assert "跳过·内部消息" in err
    assert _injections(conn) == []
    assert conn.execute("SELECT recall_count FROM memories WHERE id = 'mem-1'").fetchone()[0] == 0


def test_unknown_tag_is_skipped_but_reported(invoke: Invoke) -> None:
    code, out, err = invoke(_raw("<custom-tag>x</custom-tag>"))

    assert code == 0
    assert out == ""
    assert "未知内部标签 <custom-tag>" in err


# ----------------------------------------------------------------- 记账口径


def test_second_round_with_the_same_prompt_injects_and_records_nothing(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global", now=T0)

    first = run(_payload("构建命令是什么"), str(db_path), now=T0)
    second = run(_payload("构建命令是什么"), str(db_path), now=T0 + timedelta(minutes=10))

    assert first.context is not None
    assert second.context is None
    assert "跳过·已注入" in second.summary
    row = conn.execute("SELECT recall_count, last_recalled FROM memories").fetchone()
    assert row["recall_count"] == 1
    # 第二轮不许刷 last_recalled，否则"30 天未召回"的判据永远不成立
    assert row["last_recalled"] == fmt(T0)


def test_only_the_injected_hits_are_recorded(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    """记的是"进了块的那几条"，不是"召回出来的全部"。"""
    for index in range(5):
        write(f"构建命令是 npm run build 第{index}号补充说明", scope="global", now=T0)

    outcome = run(_payload("构建命令是什么"), str(db_path), now=T0)

    assert outcome.context is not None
    logged = json.loads(_injections(conn)[0][2])
    recorded = [
        str(row["id"])
        for row in conn.execute("SELECT id FROM memories WHERE recall_count > 0").fetchall()
    ]
    assert sorted(logged) == sorted(recorded)
    assert len(recorded) == 3  # PROMPT_TOP_K


# ------------------------------------------------------------------- 去重


def test_session_dedup_has_no_window(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global", now=T0)
    WriteService(conn).log_injection(
        query="构建命令",
        memory_ids=["mem-1"],
        chars=40,
        top_k=3,
        host="workbuddy",
        session_id="s-1",
        now=T0 - timedelta(hours=3),
    )

    outcome = run(_payload("构建命令是什么"), str(db_path), now=T0)

    assert outcome.context is None
    assert "跳过·已注入" in outcome.summary


def test_mcp_injection_inside_the_window_suppresses_the_same_item(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    """MCP 侧 ``session`` 是 NULL，只能靠 host + 两分钟窗口兜住（评审 M-2b）。"""
    write("构建命令是 npm run build", scope="global", now=T0)
    WriteService(conn).log_injection(
        query="构建命令",
        memory_ids=["mem-1"],
        chars=40,
        top_k=3,
        host="workbuddy",
        session_id=None,
        now=T0 - timedelta(seconds=60),
    )

    outcome = run(_payload("构建命令是什么"), str(db_path), now=T0)

    assert outcome.context is None


def test_mcp_injection_outside_the_window_does_not_suppress(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global", now=T0)
    WriteService(conn).log_injection(
        query="构建命令",
        memory_ids=["mem-1"],
        chars=40,
        top_k=3,
        host="workbuddy",
        session_id=None,
        now=T0 - timedelta(seconds=200),
    )

    outcome = run(_payload("构建命令是什么"), str(db_path), now=T0)

    assert outcome.context is not None


# --------------------------------------------------------------- 零命中 / 失败


def test_no_hit_injects_nothing_but_still_logs_a_row(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="global", now=T0)

    outcome = run(_payload("完全不相关的希腊字母 αβγ"), str(db_path), now=T0)

    assert outcome.context is None
    assert "无命中" in outcome.summary
    assert _injections(conn)[0][2] == "[]"


def test_broken_database_is_fail_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """库路径是个目录：打不开也必须退出 0、不往 stdout 写、stderr 有摘要。"""
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(_raw("构建命令是什么")))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    code = main(["--db", str(tmp_path)])

    assert code == 0
    assert out.getvalue() == ""
    assert "[nebula-hook] 失败·" in err.getvalue()


def test_bad_json_is_fail_open(invoke: Invoke) -> None:
    code, out, err = invoke("not json")

    assert code == 0
    assert out == ""
    assert "失败·" in err


# ------------------------------------------------------------------- 可见性


def test_other_project_and_private_rows_never_reach_the_block(
    db_path: Path, write: Write, conn: sqlite3.Connection
) -> None:
    write("构建命令是 npm run build", scope="project", project_id="p-other", now=T0)
    write("构建命令是 npm run build", visibility="private", session_id="s-2", now=T0)

    # loose 目录 → 只有 global 可见；不给 viewer_session → private 一律不可见
    outcome = run(
        _payload("构建命令是什么", cwd=str(db_path.parent / "loose")), str(db_path), now=T0
    )

    assert outcome.context is None
    assert _injections(conn)[0][2] == "[]"


# ------------------------------------------------------------- 渲染单元用例


def _hit(memory_id: str = "mem-1", content: str = "内容", *, pointer_count: int = 0) -> Hit:
    return Hit(
        id=memory_id,
        content=content,
        assertion_type="observation",
        role="agent",
        fact_key=None,
        layer="episodic",
        importance=0.5,
        relevance=0.6,
        score=0.5,
        seq=1,
        pinned=False,
        pointer_count=pointer_count,
    )


def test_clip_keeps_the_id_and_marks_the_cut() -> None:
    block = render_prompt_block([_hit(content="很长的说明" * 60)])

    line = block.text.splitlines()[1]
    assert line.endswith("（mem-1）")
    assert "…" in line
    assert len(line) <= LINE_MAX_CHARS + len("[observation|agent] ") + len("（mem-1）")


def test_budget_drops_items_and_says_how_many() -> None:
    hits = [_hit(f"mem-{index}", "说明" * 40) for index in range(1, 4)]

    block = render_prompt_block(hits, max_chars=200)

    assert len(block.text) <= 200
    assert block.dropped == 2
    assert len(block.hits) == 1
    assert "另有 2 条未列出" in block.text


def test_top_k_caps_the_block_but_counts_the_rest() -> None:
    hits = [_hit(f"mem-{index}") for index in range(1, 6)]

    block = render_prompt_block(hits)

    assert [hit.id for hit in block.hits] == ["mem-1", "mem-2", "mem-3"]
    assert block.dropped == 2
    assert "另有 2 条未列出" in block.text


def test_empty_input_renders_nothing() -> None:
    block = render_prompt_block([])

    assert block.text == ""
    assert block.hits == ()


def test_pointer_line_is_kept() -> None:
    block = render_prompt_block([_hit(pointer_count=2)])

    assert "另有 2 条未验证异议（expand mem-1）" in block.text


# ------------------------------------------------------- payload 解析单元用例


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("<task-notification>x</task-notification>", (True, None)),
        ("构建命令是什么", (False, None)),
        ("关于 <task-notification> 的处理", (False, None)),
        ("<3 哈哈", (False, None)),
        ("<custom-tag>x</custom-tag>", (True, "custom-tag")),
    ],
)
def test_internal_message(prompt: str, expected: tuple[bool, str | None]) -> None:
    assert internal_message(prompt) == expected


def test_parse_tolerates_missing_fields() -> None:
    payload = parse('{"hook_event_name": "UserPromptSubmit"}')

    assert payload.event == "UserPromptSubmit"
    assert payload.session_id is None
    assert payload.prompt == ""


def test_parse_rejects_bad_json() -> None:
    with pytest.raises(ValueError):
        parse("not json")
