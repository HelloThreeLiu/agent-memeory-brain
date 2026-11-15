"""``SessionStart`` hook：会话开场注入项目核心（§10.7）。

与 ``prompt_submit`` 的差别只有三处，每一处都有代价上的理由：

1. **没有 prompt。** 选取走 :func:`nebula.recall.core.core_memories` 的"非查询"路径：
   pinned + 本项目 top-N。没有查询就算相关性是没有意义的，所以它不算。
2. **``injection_log.query`` 用固定标记** ``session-start:<source>``：统计脚本要能把
   "开场注入"与"用户消息轮次"分开（评审 n-2），否则覆盖率的分母会把开场块也算一轮。
3. **不记账**，只落 ``injection_log``：开场块是无条件背景，不构成"这条被用到"的证据。
   给它记账会让重要度最高的那几条每会话 +1、`last_recalled` 永远新鲜，§9.5 的衰减对
   它们永久失效。

也**不查去重集**：resume / compact 之后上下文里的内容可能已经没了，按 per-session
去重抑制重注入会让开场块彻底消失。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from nebula.adapter.project import resolve_for_host
from nebula.core.timeutil import fmt, now_ts
from nebula.core.viewer import Viewer
from nebula.hook.payload import Payload, parse
from nebula.hook.render import render_session_block
from nebula.hook.summary import emit_plain, failure, line, note, utf8_streams
from nebula.recall.core import CORE_PINNED, CORE_PROJECT, core_memories
from nebula.store.db import open_database
from nebula.store.write_service import WriteService

#: 与 MCP / prompt_submit 的 ``--host`` 同名，否则 ``scope=host`` 的记忆两边看到的不一样
HOST_DEFAULT = "workbuddy"

#: 自身耗时软上限（与 prompt_submit 同口径）
SOFT_DEADLINE_MS = 5000

#: ``query`` 的固定前缀；``<source>`` 取 ``startup`` / ``resume`` / ``clear``
QUERY_PREFIX = "session-start"


@dataclass(frozen=True)
class Outcome:
    context: str | None
    summary: str


def run(
    payload: Payload,
    db: str | None,
    *,
    host: str = HOST_DEFAULT,
    now: datetime | None = None,
) -> Outcome:
    """选内容、渲染、落日志。**异常一律上抛**，由 :func:`main` 兜成 fail-open。"""
    started = time.perf_counter()
    stamp = fmt(now) if now is not None else now_ts()
    conn = open_database(db)
    try:
        ref = resolve_for_host(payload.cwd)
        # 不传 viewer_session：与 prompt_submit 同口径（private 分区不注入，§10.7 已知限制 1）
        viewer = Viewer(
            session_id=None,
            project_id=None if ref is None else ref.project_id,
            host=host,
        )
        items = core_memories(conn, viewer, now=stamp)
        block = render_session_block(
            [item for item in items if item.pinned],
            [item for item in items if not item.pinned],
            project_label=None if ref is None else ref.name,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > SOFT_DEADLINE_MS:
            return Outcome(None, line(f"跳过·超时 {elapsed_ms:.0f}ms"))
        WriteService(conn).log_injection(
            query=f"{QUERY_PREFIX}:{payload.source or 'unknown'}",
            memory_ids=[hit.id for hit in block.hits],
            chars=block.chars,
            top_k=CORE_PINNED + CORE_PROJECT,
            host=host,
            session_id=payload.session_id,
            project_id=viewer.project_id,
            elapsed_ms=round(elapsed_ms, 3),
        )
    finally:
        conn.close()

    detail = f"开场 {len(block.hits)} 条 {elapsed_ms:.0f}ms"
    if not block.text:
        detail = f"无内容 {elapsed_ms:.0f}ms"
    return Outcome(block.text or None, line(detail))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nebula.hook.session_start",
        description="SessionStart hook：会话开场注入项目核心（§10.7）",
    )
    parser.add_argument("--db", default=None, help="库路径（默认 ~/.nebula/memory.db）")
    parser.add_argument(
        "--host", default=HOST_DEFAULT, help="宿主名，必须与 MCP 的 --host 同名"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """**永远返回 0**（§10.7 fail-open）。"""
    utf8_streams()
    args, _ = build_parser().parse_known_args(argv)
    try:
        outcome = run(parse(sys.stdin.read()), args.db, host=args.host)
        if outcome.context:
            emit_plain(outcome.context)
    except Exception as exc:
        note(failure(exc))
        return 0
    note(outcome.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
