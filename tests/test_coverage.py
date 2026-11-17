"""覆盖率统计脚本（P1.5c 审查 M-1 / N-7）。

这个脚本产出的数字直接决定 C-7 能不能收口，所以它的口径必须有用例钉住——否则
"分母含宿主内部通知"这种错会让 80% 门槛被误判（实测历史 transcript 318 轮里
97 轮是 `<task-notification>`）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from nebula.core.timeutil import parse_ts
from nebula.store.db import open_database
from nebula.store.write_service import WriteService

ROOT = Path(__file__).resolve().parents[1]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "nebula_coverage_script", ROOT / "scripts" / "coverage.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user(text: str, *, stamp: int | None = 1_790_000_000_000) -> str:
    record: dict[str, object] = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }
    if stamp is not None:
        record["timestamp"] = stamp
    return json.dumps(record, ensure_ascii=False)


def _assistant(text: str) -> str:
    return json.dumps(
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": text}]},
        ensure_ascii=False,
    )


def test_user_turns_skip_host_internal_messages(tmp_path: Path) -> None:
    transcript = tmp_path / "s-1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _user("构建命令是什么"),
                _user(  # 宿主把包装一起给过来时，分母仍要认这一轮
                    '<system-reminder data-role="user-context">'
                    "<user_query>第二句</user_query></system-reminder>"
                ),
                _user("<task-notification><task-id>x</task-id><status>completed</status></task-notification>"),
                _assistant("好的"),
                "not json",
            ]
        ),
        encoding="utf-8",
    )

    assert _script()._user_turns(transcript) == 2


def test_turns_can_be_filtered_by_time(tmp_path: Path) -> None:
    transcript = tmp_path / "s-1.jsonl"
    transcript.write_text(
        "\n".join([_user("旧的", stamp=1_000_000_000_000), _user("新的", stamp=1_790_000_000_000)]),
        encoding="utf-8",
    )
    since = 1_700_000_000

    assert _script()._user_turns(transcript, since=since) == 1


def _db_with(tmp_path: Path, rows: list[tuple[str, str | None, str, str]]) -> Path:
    """``(query, session, memory_id, ts)`` → 建一个只含 injection_log 的临时库。

    走 ``WriteService`` 而不是直接 INSERT：测试也不该给出绕过唯一写入口的示范（INV1）。
    """
    path = tmp_path / "coverage.db"
    conn = open_database(str(path))
    try:
        service = WriteService(conn)
        for query, session, memory_id, stamp in rows:
            service.log_injection(
                query=query,
                memory_ids=[memory_id],
                chars=10,
                top_k=3,
                host="workbuddy",
                session_id=session,
                now=parse_ts(stamp),
            )
    finally:
        conn.close()
    return path


def test_session_start_rows_are_not_counted_as_rounds(tmp_path: Path) -> None:
    db = _db_with(
        tmp_path,
        [
            ("session-start:startup", "s-1", "mem-1", "2026-11-17T10:00:00+00:00"),
            ("构建命令是什么", "s-1", "mem-2", "2026-11-17T10:01:00+00:00"),
            ("无会话的 MCP 调用", None, "mem-3", "2026-11-17T10:02:00+00:00"),
        ],
    )

    counted = _script()._injections(db)

    assert counted == {"s-1": 1}


def test_summary_is_printed_per_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    projects = tmp_path / "projects"
    (projects / "demo").mkdir(parents=True)
    (projects / "demo" / "s-1.jsonl").write_text(
        "\n".join([_user("第一轮"), _user("第二轮")]), encoding="utf-8"
    )
    db = _db_with(
        tmp_path,
        [
            ("第一轮", "s-1", "mem-1", "2026-11-17T10:00:00+00:00"),
            ("session-start:startup", "s-1", "mem-1", "2026-11-17T09:59:00+00:00"),
        ],
    )

    code = _script().main(["--db", str(db), "--projects", str(projects)])

    out = capsys.readouterr().out
    assert code == 0
    assert "50.0%" in out


def test_sessions_without_transcript_are_reported_but_not_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    db = _db_with(tmp_path, [("第一轮", "s-orphan", "mem-1", "2026-11-17T10:00:00+00:00")])

    _script().main(["--db", str(db), "--projects", str(projects)])

    out = capsys.readouterr().out
    assert "没有可统计的会话" in out or "未计入" in out


def test_missing_db_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    projects = tmp_path / "projects"
    (projects / "demo").mkdir(parents=True)
    (projects / "demo" / "s-1.jsonl").write_text(_user("第一轮"), encoding="utf-8")

    code = _script().main(["--db", str(tmp_path / "nope.db"), "--projects", str(projects)])

    assert code == 0
    assert "0.0%" in capsys.readouterr().out
