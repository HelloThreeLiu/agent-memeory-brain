"""``python -m nebula.mcp``：以 stdio 方式对外提供四个工具。

宿主配置里这一行就够了：

```json
{"mcpServers": {"nebula": {"command": "python", "args": ["-m", "nebula.mcp"]}}}
```

作用域来源（§10.4）优先用命令行显式值，其次读环境变量，最后从当前目录推断。
MCP 客户端 spawn 子进程时工作目录通常是项目根，所以"从当前目录推断"在多数宿主
上已经够用；推断不出来时退化成 global，不会写错项目。
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from nebula.adapter.project import resolve as resolve_project
from nebula.mcp.server import run
from nebula.mcp.tools import ToolContext
from nebula.store.db import open_database

HOST_ENV = "NEBULA_HOST"
SESSION_ENV = "NEBULA_SESSION"
PROJECT_ENV = "NEBULA_PROJECT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nebula.mcp",
        description="Nebula 的 MCP stdio 服务（B 档接入：remember/recall/expand/feedback）",
    )
    parser.add_argument("--db", default=None, help="库路径（默认 ~/.nebula/memory.db）")
    parser.add_argument("--host", default=os.environ.get(HOST_ENV), help="宿主名，用于 host 作用域")
    parser.add_argument(
        "--session", default=os.environ.get(SESSION_ENV), help="会话 id，用于私有分区"
    )
    parser.add_argument(
        "--project",
        default=os.environ.get(PROJECT_ENV),
        help="项目 id；不给则从当前目录解析（git remote 优先）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_id = args.project or resolve_project().project_id
    conn = open_database(args.db)
    try:
        ctx = ToolContext(
            conn=conn,
            host=args.host,
            session_id=args.session,
            project_id=project_id,
        )
        run(ctx)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
