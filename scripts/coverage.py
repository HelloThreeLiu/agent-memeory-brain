#!/usr/bin/env python
"""A 档覆盖率：``injection_log`` 行数 ÷ transcript 里的 user turn 数（§10.7 验收 1）。

分母为什么从 transcript 数：hook 没跑时分子分母会一起为 0，那种"假绿"在只看
``injection_log`` 的统计里永远看不出来。分母必须是宿主的真实轮次。

分子口径（评审 n-2）：只数 ``query`` 不以 ``session-start:`` 开头、且 ``session`` 非空的
行——前者排除开场注入，后者排除 B 档 MCP 的主动 recall（它不带 session）。

用法：

    python scripts/coverage.py                      # 默认库与默认 transcript 目录
    python scripts/coverage.py --days 3             # 只看最近三天有更新的会话
    python scripts/coverage.py --db D:/x/memory.db --projects C:/Users/me/.workbuddy/projects
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".nebula" / "memory.db"
DEFAULT_PROJECTS = Path.home() / ".workbuddy" / "projects"

SESSION_START_PREFIX = "session-start:"


def _user_turns(transcript: Path) -> int:
    """数 transcript 里的 user turn。坏行直接跳过——统计工具不该因为一行坏 JSON 罢工。"""
    turns = 0
    with transcript.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "message" and record.get("role") == "user":
                turns += 1
    return turns


def _injections(db: Path) -> dict[str, int]:
    if not db.is_file():
        return {}
    # 只读打开：统计工具不该有机会写库。URI 里必须用正斜杠，Windows 的反斜杠会被当转义
    uri = f"file:{db.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT session, COUNT(*) AS n FROM injection_log "
            "WHERE session IS NOT NULL AND query NOT LIKE ? GROUP BY session",
            (f"{SESSION_START_PREFIX}%",),
        ).fetchall()
    finally:
        conn.close()
    return {str(session): int(count) for session, count in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A 档注入覆盖率（§10.7 验收 1）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="记忆库路径")
    parser.add_argument("--projects", default=str(DEFAULT_PROJECTS), help="transcript 根目录")
    parser.add_argument("--days", type=int, default=0, help="只看最近 N 天有更新的会话（0=全部）")
    args = parser.parse_args(argv)

    cutoff = time.time() - args.days * 86400 if args.days else None
    transcripts = sorted(Path(args.projects).glob("*/*.jsonl"))
    counted = _injections(Path(args.db))

    rows: list[tuple[str, int, int]] = []
    for transcript in transcripts:
        if cutoff is not None and transcript.stat().st_mtime < cutoff:
            continue
        session = transcript.stem
        turns = _user_turns(transcript)
        hooked = counted.get(session, 0)
        if turns == 0 and hooked == 0:
            continue
        rows.append((session, turns, hooked))

    if not rows:
        print("没有可统计的会话：检查 --projects 与 --days")
        return 0

    print(f"{'session':<10}{'user turn':>10}{'injection':>11}{'coverage':>10}")
    total_turns = total_hooked = 0
    for session, turns, hooked in rows:
        total_turns += turns
        total_hooked += hooked
        ratio = f"{hooked / turns:.0%}" if turns else "—"
        flag = "" if turns and hooked <= turns else "  ← 行数超过轮次，口径要复核"
        print(f"{session[:8]:<10}{turns:>10}{hooked:>11}{ratio:>10}{flag}")

    ratio = f"{total_hooked / total_turns:.1%}" if total_turns else "—"
    print(f"\n合计：{total_hooked} 行 / {total_turns} 轮 = **{ratio}**（门槛 80%）")
    orphans = set(counted) - {session for session, _, _ in rows}
    if orphans:
        rows_sum = sum(counted[session] for session in orphans)
        # 只有注入、没有 transcript：通常来自本机冒烟或另一个宿主写进同一个库。
        # 不计入分子——否则分母不变而分子变大，覆盖率会显得比实际好
        print(f"另有 {rows_sum} 行注入没有对应 transcript（{len(orphans)} 个 session），未计入")
    print("口径：分母=transcript 的 user turn；分子=session 非空且非开场注入的行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
