"""命令行入口：``python -m nebula <command>``。

P1 起覆盖"能存、能召回、能解释、能忘、能复原"：

- ``init`` / ``remember`` / ``recall`` / ``status``（P0）
- ``explain`` / ``expand``：为什么召到 / 召不到，一条记忆的完整来历
- ``forget`` / ``restore`` / ``pin``：生命周期动作
- ``sleep``：一次维护（``--dry-run`` 只看不改）

断网可用、无 API key 可用——本模块不 import 任何网络或模型库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence

from nebula import __version__
from nebula.core.timeutil import now_ts
from nebula.lifecycle.decay import plan as lifecycle_plan
from nebula.lifecycle.sleep import run_sleep
from nebula.recall.detail import expand
from nebula.recall.explain import explain
from nebula.recall.search import recall
from nebula.store.db import open_database, resolve_db_path, schema_version
from nebula.store.write_service import InvariantViolation, WriteService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nebula",
        description="Nebula：本地优先的四层记忆引擎（P0：能存、能召回）",
    )
    parser.add_argument("--version", action="version", version=f"nebula {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="建库并应用 schema")
    init_parser.add_argument("--db", default=None, help="库路径（默认 ~/.nebula/memory.db）")
    init_parser.set_defaults(handler=_cmd_init)

    remember_parser = subparsers.add_parser("remember", help="写入一条记忆")
    remember_parser.add_argument("content", help="记忆内容")
    remember_parser.add_argument("--db", default=None)
    remember_parser.add_argument("--key", dest="fact_key", default=None, help="唯一真相槽位键")
    remember_parser.add_argument(
        "--type",
        dest="assertion_type",
        default="decision",
        choices=["decision", "observation", "claim", "hypothesis"],
        help="断言类型（命令行默认 decision：你本人说的话就是结论）",
    )
    remember_parser.add_argument(
        "--kind",
        default="fact",
        choices=["fact", "preference", "decision", "relation", "procedure", "event", "handoff"],
    )
    remember_parser.add_argument("--role", default="user", help="写入方角色（默认 user）")
    remember_parser.add_argument("--scope", default="global", choices=["global", "project", "host"])
    remember_parser.add_argument("--project", dest="project_id", default=None)
    remember_parser.add_argument("--importance", type=float, default=0.5)
    remember_parser.add_argument("--valid-from", dest="valid_from", default=None)
    remember_parser.add_argument(
        "--evidence", action="append", default=[], help="证据 episode id（可多次给）"
    )
    remember_parser.add_argument("--pin", action="store_true", help="置顶，召回时加权")
    remember_parser.set_defaults(handler=_cmd_remember)

    recall_parser = subparsers.add_parser("recall", help="按查询召回")
    recall_parser.add_argument("query")
    recall_parser.add_argument("--db", default=None)
    recall_parser.add_argument("-k", "--limit", type=int, default=5)
    recall_parser.add_argument("--scope", default=None, choices=["global", "project", "host"])
    recall_parser.add_argument("--project", dest="project_id", default=None)
    recall_parser.add_argument("--session", dest="viewer_session", default=None)
    recall_parser.add_argument("--json", action="store_true", help="输出 JSON")
    recall_parser.add_argument(
        "--no-record",
        dest="record",
        action="store_false",
        help="不更新 last_recalled / recall_count（只读查询用）",
    )
    recall_parser.set_defaults(handler=_cmd_recall, record=True)

    explain_parser = subparsers.add_parser(
        "explain", help="解释一条记忆为什么召得到 / 召不到"
    )
    explain_parser.add_argument("memory_id", help="记忆 id（形如 mem-12）")
    explain_parser.add_argument("--db", default=None)
    explain_parser.add_argument("--session", dest="viewer_session", default=None)
    explain_parser.set_defaults(handler=_cmd_explain)

    expand_parser = subparsers.add_parser("expand", help="展开一条记忆的完整来历")
    expand_parser.add_argument("memory_id")
    expand_parser.add_argument("--db", default=None)
    expand_parser.set_defaults(handler=_cmd_expand)

    forget_parser = subparsers.add_parser("forget", help="遗忘（进回收站，30 天内可复原）")
    forget_parser.add_argument("memory_id")
    forget_parser.add_argument("--db", default=None)
    forget_parser.set_defaults(handler=_cmd_forget)

    restore_parser = subparsers.add_parser("restore", help="从回收站复原")
    restore_parser.add_argument("memory_id")
    restore_parser.add_argument("--db", default=None)
    restore_parser.set_defaults(handler=_cmd_restore)

    pin_parser = subparsers.add_parser("pin", help="置顶（免于自动归档）")
    pin_parser.add_argument("memory_id")
    pin_parser.add_argument("--db", default=None)
    pin_parser.add_argument("--off", action="store_true", help="取消置顶")
    pin_parser.set_defaults(handler=_cmd_pin)

    sleep_parser = subparsers.add_parser("sleep", help="跑一轮维护：衰减/休眠/归档/清回收站")
    sleep_parser.add_argument("--db", default=None)
    sleep_parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    sleep_parser.add_argument("--json", action="store_true")
    sleep_parser.set_defaults(handler=_cmd_sleep)

    status_parser = subparsers.add_parser("status", help="看库的规模与状态")
    status_parser.add_argument("--db", default=None)
    status_parser.set_defaults(handler=_cmd_status)

    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    path = resolve_db_path(args.db)
    conn = open_database(args.db)
    try:
        print(f"库：{path}")
        print(f"schema 版本：{schema_version(conn)}")
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        print(f"已有记忆：{count} 条")
    finally:
        conn.close()
    return 0


def _cmd_remember(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        service = WriteService(conn)
        result = service.remember(
            args.content,
            scope=args.scope,
            project_id=args.project_id,
            kind=args.kind,
            assertion_type=args.assertion_type,
            role=args.role,
            fact_key=args.fact_key,
            importance=args.importance,
            valid_from=args.valid_from,
            evidence=tuple(args.evidence),
            pinned=args.pin,
        )
    finally:
        conn.close()

    labels = {
        "created": "已写入",
        "deduplicated": "已合并（幂等窗口内重复）",
        "merged": "已合并进既有条目",
        "append": "已追加（旧条不动）",
        "contradict": "已追加并记录异议",
        "supersede_old": "已写入，旧条静默失效",
        "supersede_new": "已写入（补录历史，未顶掉当前结论）",
        "arbitrate": "已写入，已开仲裁",
    }
    print(f"{labels.get(result.action, result.action)}：{result.id}（seq={result.seq}）")
    if result.superseded and result.action == "supersede_old":
        print(f"  失效：{', '.join(result.superseded)}")
    if result.arbitration_id:
        print(f"  仲裁：{result.arbitration_id}")
    if result.action == "merged":
        print(f"  说明：{result.note}")
    return 0


def _cmd_recall(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        result = recall(
            conn,
            args.query,
            args.limit,
            scope=args.scope,
            project_id=args.project_id,
            viewer_session=args.viewer_session,
            record=args.record,
        )
    finally:
        conn.close()

    if args.json:
        payload = {
            "query": result.query,
            "elapsed_ms": result.elapsed_ms,
            "candidate_count": result.candidate_count,
            "hits": [
                {
                    "id": hit.id,
                    "content": hit.content,
                    "assertion_type": hit.assertion_type,
                    "role": hit.role,
                    "fact_key": hit.fact_key,
                    "score": hit.score,
                    "relevance": hit.relevance,
                    "pointer_count": hit.pointer_count,
                }
                for hit in result.hits
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not result.hits:
        print("没有命中。换一种说法，或先 remember 一条。")
        return 0
    for line in result.lines():
        print(line)
    print(f"（候选 {result.candidate_count} 条，{result.elapsed_ms:.1f}ms）")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        result = explain(conn, args.memory_id, viewer_session=args.viewer_session)
    finally:
        conn.close()
    for line in result.lines():
        print(line)
    return 0


def _cmd_expand(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        result = expand(conn, args.memory_id)
    finally:
        conn.close()
    for line in result.lines():
        print(line)
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        changed = WriteService(conn).forget(args.memory_id)
    finally:
        conn.close()
    if not changed:
        print(f"{args.memory_id} 已经不在可遗忘状态（可能已在回收站或已归档删除）")
        return 0
    print(f"已进回收站：{args.memory_id}（30 天内可 restore）")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        changed = WriteService(conn).restore(args.memory_id)
    finally:
        conn.close()
    if not changed:
        print(f"{args.memory_id} 不在回收站，无需复原")
        return 0
    print(f"已复原：{args.memory_id}")
    return 0


def _cmd_pin(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        changed = WriteService(conn).pin(args.memory_id, pinned=not args.off)
    finally:
        conn.close()
    action = "取消置顶" if args.off else "置顶"
    print(f"{action}：{args.memory_id}" if changed else f"{args.memory_id} 状态未变")
    return 0


def _cmd_sleep(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        report = run_sleep(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    if args.json:
        print(
            json.dumps(
                {
                    "now": report.now,
                    "dry_run": report.dry_run,
                    "scanned": report.scanned,
                    "heat_settled": report.heat_settled,
                    "dormant": report.dormant,
                    "archived": report.archived,
                    "archived_capacity": report.archived_capacity,
                    "purged": report.purged,
                    "active_after": report.active_after,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    for line in report.lines():
        print(line)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    conn = open_database(args.db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        print(f"库：{resolve_db_path(args.db)}")
        print(f"schema 版本：{schema_version(conn)}")
        print(f"记忆总数：{total}")
        by_layer = conn.execute(
            "SELECT layer, status, COUNT(*) AS n FROM memories GROUP BY layer, status "
            "ORDER BY layer, status"
        ).fetchall()
        for row in by_layer:
            print(f"  {row['layer']:<9} {row['status']:<9} {row['n']}")
        stamp = now_ts()
        current = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status IN ('active','dormant') "
            "AND (valid_from IS NULL OR valid_from <= ?) "
            "AND (valid_to IS NULL OR valid_to > ?)",
            (stamp, stamp),
        ).fetchone()[0]
        print(f"当前有效：{current}")
        open_arb = conn.execute(
            "SELECT COUNT(*) FROM arbitration WHERE status = 'open'"
        ).fetchone()[0]
        print(f"待仲裁：{open_arb}")
        # 下一轮 sleep 会做什么：让"库会不会膨胀"在跑之前就能看出来
        pending = lifecycle_plan(conn, now=stamp)
        print(
            f"待维护：休眠 {len(pending.to_dormant)} · 归档 {len(pending.to_archive)}"
            f" · 容量归档 {len(pending.to_archive_capacity)}"
            f" · 可清回收站 {pending.purgeable}"
        )
        recycled = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'deleted'"
        ).fetchone()[0]
        pinned = conn.execute("SELECT COUNT(*) FROM memories WHERE pinned = 1").fetchone()[0]
        print(f"回收站：{recycled} · 置顶：{pinned}")
    finally:
        conn.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    try:
        return int(handler(args))
    except (InvariantViolation, ValueError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
