"""MCP 服务端：方法分发与 stdio 循环。

只实现 B 档需要的五个方法：``initialize`` / ``notifications/initialized`` /
``ping`` / ``tools/list`` / ``tools/call``。Resources 与 Prompts 一律不实现——
DSH 的 ``dsh-mcp-client`` 只桥接 Tools（§14 的【A】结论），实现了也没人消费。

**版本协商取"原样回显"**：我们只暴露 tools 一组能力，协议版本差异基本落在用不到的
部分。回显客户端版本比强制降到自己的版本更不容易把客户端弄断；真遇到不兼容的
客户端，报错会出现在它那边，比在服务端静默降级更好查。
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any, TextIO

from nebula import __version__
from nebula.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    Request,
    RpcError,
    decode,
    encode,
    error,
    ok,
)
from nebula.mcp.tools import TOOLS, ToolContext, call_tool
from nebula.store.write_service import InvariantViolation

SERVER_NAME = "nebula"

#: 客户端没给版本时我们声明的版本。写一个具体日期版而不是 "latest"，
#: 便于排障时知道服务端是按哪一版实现的。
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


class Server:
    def __init__(
        self, ctx: ToolContext, *, protocol_version: str = DEFAULT_PROTOCOL_VERSION
    ) -> None:
        self.ctx = ctx
        self._default_version = protocol_version

    # ------------------------------------------------------------------ 分发

    def handle(self, request: Request) -> dict[str, Any] | None:
        # 通知一律不响应（initialized / cancelled / 未来的进度通知）。
        # 未知通知也不报错：协议要求不响应通知，回了反而让客户端困惑。
        if request.is_notification:
            return None

        if request.method == "initialize":
            return ok(request.id, self._initialize(request.params))
        if request.method == "ping":
            return ok(request.id, {})
        if request.method == "tools/list":
            return ok(request.id, {"tools": [spec.as_dict() for spec in TOOLS]})
        if request.method == "tools/call":
            return self._call_tool(request)
        return error(request.id, METHOD_NOT_FOUND, f"不支持的方法：{request.method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested else self._default_version
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }

    def _call_tool(self, request: Request) -> dict[str, Any]:
        name = request.params.get("name")
        if not isinstance(name, str) or not name:
            return error(request.id, INVALID_PARAMS, "tools/call 缺 name")

        arguments = request.params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return error(request.id, INVALID_PARAMS, "arguments 必须是对象")

        try:
            text = call_tool(name, arguments, self.ctx)
        except KeyError as exc:
            return error(request.id, INVALID_PARAMS, str(exc).strip("'\""))
        except (ValueError, InvariantViolation, sqlite3.Error) as exc:
            # 工具执行失败走 isError（MCP 约定）：这是"这件事做不成"，
            # 与"这条消息本身不合法"（JSON-RPC error）是两件事。
            return ok(
                request.id,
                {"content": [{"type": "text", "text": f"错误：{exc}"}], "isError": True},
            )
        return ok(request.id, {"content": [{"type": "text", "text": text}]})

    # ------------------------------------------------------------------ 循环

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            request: Request | None = None
            try:
                request = decode(line)
                response = self.handle(request)
            except RpcError as exc:
                response = error(None if request is None else request.id, exc.code, exc.message)
            except Exception as exc:  # 一条坏消息不该让整个服务进程退出
                response = error(
                    None if request is None else request.id,
                    INTERNAL_ERROR,
                    f"内部错误：{exc}",
                )
            if response is None:
                continue
            stdout.write(encode(response) + "\n")
            stdout.flush()


def run(ctx: ToolContext, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """在给定流上跑服务（默认 stdin/stdout，强制 UTF-8）。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    for stream in (stdin, stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")
    Server(ctx).serve(stdin, stdout)
