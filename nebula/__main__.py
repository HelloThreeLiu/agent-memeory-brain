"""命令行入口：``python -m nebula <command>``。

P0 只做四件事：``init`` 建库、``remember`` 写、``recall`` 查、``status`` 看家底。
断网可用、无 API key 可用——本模块不 import 任何网络或模型库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence

from nebula import __version__
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
    recall_parser.set_defaults(handler=_cmd_recall)

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
        current = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status IN ('active','dormant') "
            "AND valid_to IS NULL"
        ).fetchone()[0]
        print(f"当前有效：{current}")
        open_arb = conn.execute(
            "SELECT COUNT(*) FROM arbitration WHERE status = 'open'"
        ).fetchone()[0]
        print(f"待仲裁：{open_arb}")
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
