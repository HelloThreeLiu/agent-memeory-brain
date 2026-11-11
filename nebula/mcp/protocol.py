"""MCP 的 stdio 传输层（逐行 JSON-RPC 2.0），零依赖。

只实现"够用"的那一层：换行分帧、请求解码、成功/错误响应编码。
协议版本协商、能力声明、方法分发在 :mod:`nebula.mcp.server`。

为什么自己写而不用官方 ``mcp`` SDK：本项目的硬约束是**零运行时依赖**（§14），
而这里需要的只有三个方法加两个通知。引入 SDK 会把"能跑"变成一个需要联网安装
的前提，与"断网可用"直接冲突。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RpcError(Exception):
    """协议层错误：走 JSON-RPC 的 ``error`` 字段，不是工具的 ``isError``。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Request:
    id: str | int | None
    method: str
    params: dict[str, Any]

    @property
    def is_notification(self) -> bool:
        return self.id is None


def decode(line: str) -> Request:
    try:
        parsed: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RpcError(PARSE_ERROR, f"不是合法 JSON：{exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise RpcError(INVALID_REQUEST, "JSON-RPC 消息必须是对象")
    if parsed.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(INVALID_REQUEST, "缺 jsonrpc=2.0")

    method = parsed.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(INVALID_REQUEST, "缺 method")

    raw_id = parsed.get("id")
    if raw_id is not None and not isinstance(raw_id, (str, int)):
        raise RpcError(INVALID_REQUEST, "id 只能是字符串或数字")

    params = parsed.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params 必须是对象")

    return Request(id=raw_id, method=method, params=params)


def encode(payload: dict[str, Any]) -> str:
    """一条消息一行：不缩进、不带内嵌换行（换行在 stdio 上是分帧符）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def ok(request_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def error(request_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }
