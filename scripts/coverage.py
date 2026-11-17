#!/usr/bin/env python
"""A 档覆盖率：``injection_log`` 行数 ÷ transcript 里的 user turn 数（§10.7 验收 1）。

分母为什么从 transcript 数：hook 没跑时分子分母会一起为 0，那种"假绿"在只看
``injection_log`` 的统计里永远看不出来。分母必须是宿主的真实轮次。

**分母必须与 hook 用同一份"什么算一轮"的判定**（P1.5c 审查 M-1）。宿主的后台任务通知
也以 ``role=user`` 落进 transcript，而 hook 对它们是**跳过且不落行**的——两边口径不一致
时，分母凭空多出 30%（实测历史 transcript 318 轮里 97 轮是 `<task-notification>`），
80% 门槛会被误判成不达标。所以这里直接 import hook 的判定，不另写一份正则。

分子口径（评审 n-2）：只数 ``query`` 不以 ``session-start:`` 开头、且 ``session`` 非空的
行——前者排除开场注入，后者排除 B 档 MCP 的主动 recall（它不带 session）。

用法：

    python scripts/coverage.py                      # 默认库与默认 transcript 目录
    python scripts/coverage.py --days 3             # 只看最近三天
    python scripts/coverage.py --db D:/x/memory.db --projects C:/Users/me/.workbuddy/projects
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# 脚本在 scripts/ 下，直接跑时 sys.path[0] 是 scripts/，导入不了 nebula。
# 而"分母口径"必须与 hook 共用一份实现（审查 M-1），所以宁可加这一行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nebula.hook.payload import is_user_turn

DEFAULT_DB = Path.home() / ".nebula" / "memory.db"
DEFAULT_PROJECTS = Path.home() / ".workbuddy" / "projects"

SESSION_START_PREFIX = "session-start:"


def _message_text(record: dict[str, object]) -> str:
    content = record.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(str(item["text"]))
    return " ".join(parts)


def _user_turns(transcript: Path, *, since: float | None = None) -> int:
    """数 transcript 里**真实的** user turn。

    判定整条委托给 ``payload.is_user_turn``：内部通知（``<task-notification>``）、
    宿主控制消息（``data-role="error-recovery"``）、剥壳后什么都没剩的样板都不算一轮。
    分数口径只能有一份实现——两边各写一份，读数就会漂（复审 R2-2）。
    坏行直接跳过：统计工具不该因为一行坏 JSON 罢工。
    """
    turns = 0
    with transcript.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "message" or record.get("role") != "user":
                continue
            if since is not None and not _not_before(record, since):
                continue
            if not is_user_turn(_message_text(record)):
                continue
            turns += 1
    return turns


def _not_before(record: dict[str, object], since: float) -> bool:
    """``--days`` 按**轮次时间**筛，不按文件 mtime：旧会话今天被 touch 过，
    把陈年轮次带进分母会让覆盖率忽然掉一大截（审查 N-7）。拿不到时间戳时放行。"""
    stamp = record.get("timestamp")
    if not isinstance(stamp, (int, float)):
        return True
    return float(stamp) / 1000 >= since


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
    parser.add_argument("--days", type=int, default=0, help="只看最近 N 天（0=全部）")
    args = parser.parse_args(argv)

    cutoff = time.time() - args.days * 86400 if args.days else None
    transcripts = sorted(Path(args.projects).glob("*/*.jsonl"))
    counted = _injections(Path(args.db))

    rows: list[tuple[str, int, int]] = []
    for transcript in transcripts:
        try:
            modified = transcript.stat().st_mtime
        except OSError:  # 文件在 glob 与 stat 之间被删（审查 N-7）
            continue
        if cutoff is not None and modified < cutoff:
            continue
        session = transcript.stem
        turns = _user_turns(transcript, since=cutoff)
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
    print("口径：分母=transcript 的真实 user turn；分子=session 非空且非开场注入的行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
