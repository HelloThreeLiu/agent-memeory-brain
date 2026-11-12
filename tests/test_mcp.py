"""P1.5a：MCP 接口层。

两层验证：

1. :class:`Server` 直接喂字典——管协议语义（版本回显、通知不响应、方法不存在）；
2. 真起一个 ``python -m nebula.mcp`` 子进程——管入口、UTF-8、逐行分帧、进程不因
   坏消息退出。第二层不能省：前一层跑通而 ``python -m`` 起不来的情况真实存在
   （编码、包发现、参数解析都只在这一层暴露）。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nebula.mcp.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR, Request
from nebula.mcp.server import Server
from nebula.mcp.tools import ToolContext
from nebula.store.db import connect, migrate
from nebula.store.write_service import WriteService

ROOT = Path(__file__).resolve().parents[1]
SECRET = "私有端口是 9222"


# ------------------------------------------------------------------ 单元层


@pytest.fixture()
def server(conn: sqlite3.Connection) -> Server:
    return Server(
        ToolContext(conn=conn, host="test-host", session_id="s-1", project_id="p-1")
    )


def _call(server: Server, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = server.handle(Request(id=request_id, method=method, params=params))
    assert response is not None
    return response


def test_initialize_echoes_client_version(server: Server) -> None:
    response = _call(server, 1, "initialize", {"protocolVersion": "2024-11-05"})
    result = response["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "nebula"


def test_notifications_get_no_response(server: Server) -> None:
    assert server.handle(Request(id=None, method="notifications/initialized", params={})) is None
    # 未知通知也不报错：回了反而让客户端困惑
    assert server.handle(Request(id=None, method="notifications/whatever", params={})) is None


def test_unknown_method_is_an_error(server: Server) -> None:
    response = _call(server, 2, "resources/list", {})
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_unknown_tool_is_invalid_params(server: Server) -> None:
    response = _call(server, 3, "tools/call", {"name": "delete_everything", "arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS


def test_tools_list_exposes_exactly_four(server: Server) -> None:
    tools = _call(server, 4, "tools/list", {})["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["remember", "recall", "expand", "feedback"]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"].strip()


def test_missing_required_argument_is_tool_error(server: Server) -> None:
    response = _call(server, 5, "tools/call", {"name": "remember", "arguments": {}})
    assert response["result"]["isError"] is True
    assert "错误：" in response["result"]["content"][0]["text"]


def test_remember_then_recall_then_expand(server: Server) -> None:
    written = _call(
        server,
        6,
        "tools/call",
        {
            "name": "remember",
            "arguments": {"content": "构建用 npm run build", "fact_key": "build.cmd"},
        },
    )
    assert "已写入" in written["result"]["content"][0]["text"]

    recalled = _call(
        server, 7, "tools/call", {"name": "recall", "arguments": {"query": "构建命令"}}
    )
    text = recalled["result"]["content"][0]["text"]
    assert "npm run build" in text

    expanded = _call(server, 8, "tools/call", {"name": "expand", "arguments": {"id": "mem-1"}})
    assert "证据" in expanded["result"]["content"][0]["text"]


def test_recall_writes_an_injection_row(server: Server, conn: sqlite3.Connection) -> None:
    """每轮注入都必须有日志：三项指标的分母，漏记等于指标作废。"""
    _call(server, 9, "tools/call", {"name": "remember", "arguments": {"content": "召回这条"}})
    empty = _call(
        server, 10, "tools/call", {"name": "recall", "arguments": {"query": "不存在的话题"}}
    )
    assert "没有命中" in empty["result"]["content"][0]["text"]

    _call(server, 11, "tools/call", {"name": "recall", "arguments": {"query": "召回"}})
    rows = conn.execute("SELECT * FROM injection_log ORDER BY ts").fetchall()
    # 命不命中都记：没命中的查询正是"哪种问法召不回来"的样本
    assert len(rows) == 2
    assert json.loads(rows[0]["memory_ids"]) == []
    assert json.loads(rows[1]["memory_ids"]) == ["mem-1"]
    assert rows[1]["chars"] > 0
    assert rows[1]["host"] == "test-host"
    assert rows[1]["project_id"] == "p-1"


def test_feedback_links_the_latest_injection(server: Server, conn: sqlite3.Connection) -> None:
    _call(server, 12, "tools/call", {"name": "remember", "arguments": {"content": "接口用 POST"}})
    _call(server, 13, "tools/call", {"name": "recall", "arguments": {"query": "接口"}})

    response = _call(
        server,
        14,
        "tools/call",
        {"name": "feedback", "arguments": {"id": "mem-1", "kind": "wrong", "note": "其实用 PUT"}},
    )
    assert "已记录反馈" in response["result"]["content"][0]["text"]

    row = conn.execute("SELECT * FROM feedback WHERE memory_id = 'mem-1'").fetchone()
    assert row["kind"] == "wrong"
    assert row["source"] == "mcp"
    assert row["note"] == "其实用 PUT"
    assert row["injection_id"] is not None


def test_feedback_does_not_mislink_prefix_ids(
    server: Server, conn: sqlite3.Connection
) -> None:
    """``mem-1`` 是 ``mem-10`` 的前缀：用 LIKE 会把反馈挂到错的那次注入。"""
    for index in range(1, 11):
        _call(
            server,
            20 + index,
            "tools/call",
            {"name": "remember", "arguments": {"content": f"第 {index} 条记录"}},
        )
    service = WriteService(conn)
    service.log_injection(query="只有第十条", memory_ids=("mem-10",), chars=1, top_k=1)
    right = service.log_injection(query="只有第一条", memory_ids=("mem-1",), chars=1, top_k=1)

    _call(
        server,
        41,
        "tools/call",
        {"name": "feedback", "arguments": {"id": "mem-1", "kind": "useless"}},
    )
    row = conn.execute("SELECT injection_id FROM feedback WHERE memory_id = 'mem-1'").fetchone()
    assert row["injection_id"] == right


def test_loose_project_defaults_to_global_scope(conn: sqlite3.Connection) -> None:
    """非 git 目录的 id 是路径 hash，换路径就失效——不该拿它当作用域边界。"""
    loose = Server(
        ToolContext(conn=conn, host="h", project_id="loose-hash", project_is_loose=True)
    )
    _call(
        loose,
        60,
        "tools/call",
        {"name": "remember", "arguments": {"content": "非 git 目录里的偏好"}},
    )
    row = conn.execute("SELECT scope, project_id FROM memories").fetchone()
    assert row["scope"] == "global"
    assert row["project_id"] is None

    # 想按目录分项目必须显式给 id，不能让系统拿 loose hash 猜
    rejected = _call(
        loose,
        61,
        "tools/call",
        {"name": "remember", "arguments": {"content": "硬要分项目", "scope": "project"}},
    )
    assert rejected["result"]["isError"] is True


def test_private_memory_is_not_expandable_by_another_session(
    conn: sqlite3.Connection,
) -> None:
    owner = Server(ToolContext(conn=conn, host="h", session_id="s-1", project_id="p-1"))
    _call(
        owner,
        50,
        "tools/call",
        {
            "name": "remember",
            "arguments": {"content": SECRET, "visibility": "private"},
        },
    )

    stranger = Server(ToolContext(conn=conn, host="h", session_id="s-2", project_id="p-1"))
    response = _call(stranger, 51, "tools/call", {"name": "expand", "arguments": {"id": "mem-1"}})
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "不可见" in text
    assert SECRET not in text
    assert "9222" not in text


# ------------------------------------------------------------------ 子进程层


class _Client:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.next_id = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            payload["params"] = params
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        assert line, "服务进程没有返回任何东西"
        decoded: dict[str, Any] = json.loads(line)
        return decoded

    def notify(self, method: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[_Client]:
    db = tmp_path / "memory.db"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nebula.mcp",
            "--db",
            str(db),
            "--host",
            "claude-code",
            "--session",
            "s-1",
            "--project",
            "p-1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
    )
    try:
        yield _Client(process)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


def test_stdio_round_trip(client: _Client) -> None:
    init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    assert init["result"]["serverInfo"]["name"] == "nebula"

    client.notify("notifications/initialized")

    tools = client.request("tools/list")["result"]["tools"]
    assert len(tools) == 4

    written = client.call("remember", {"content": "库用 SQLite"})
    assert "已写入" in written["result"]["content"][0]["text"]

    recalled = client.call("recall", {"query": "库用什么"})
    assert "SQLite" in recalled["result"]["content"][0]["text"]


def test_bad_json_does_not_kill_the_process(client: _Client) -> None:
    assert client.process.stdin is not None
    client.process.stdin.write("{这不是 json}\n")
    client.process.stdin.flush()
    assert client.process.stdout is not None
    response = json.loads(client.process.stdout.readline())
    assert response["error"]["code"] == PARSE_ERROR

    # 进程还活着：下一条正常请求照样有响应
    assert client.request("ping")["result"] == {}


def test_database_is_created_and_migrated(tmp_path: Path, client: _Client) -> None:
    client.call("remember", {"content": "随便一条"})
    # 服务进程用的库在 tmp_path 下，直接打开同一文件核对
    db_files = list(tmp_path.glob("memory.db"))
    assert db_files
    conn = connect(db_files[0])
    try:
        migrate(conn)
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM injection_log").fetchone()[0] == 0
    finally:
        conn.close()


def test_feedback_on_invisible_memory_looks_exactly_like_missing(
    conn: sqlite3.Connection,
) -> None:
    """审查 m-3：不可见也不能写反馈，且不能报出"这条存在"。

    两条路径的措辞除 id 外必须逐字一致——否则模型可以靠错误文本枚举出
    "库里有没有 mem-7" 这件事。
    """
    owner = Server(ToolContext(conn=conn, host="h", session_id="s-1", project_id="p-1"))
    _call(
        owner,
        70,
        "tools/call",
        {"name": "remember", "arguments": {"content": SECRET, "visibility": "private"}},
    )

    stranger = Server(ToolContext(conn=conn, host="h", session_id="s-2", project_id="p-1"))
    invisible = _call(
        stranger,
        71,
        "tools/call",
        {
            "name": "feedback",
            "arguments": {"id": "mem-1", "kind": "wrong", "note": "私密吐槽：端口抄错了"},
        },
    )
    missing = _call(
        stranger,
        72,
        "tools/call",
        {"name": "feedback", "arguments": {"id": "mem-404", "kind": "wrong"}},
    )

    assert invisible["result"]["isError"] is True
    assert missing["result"]["isError"] is True
    assert invisible["result"]["content"][0]["text"].replace(
        "mem-1", "ID"
    ) == missing["result"]["content"][0]["text"].replace("mem-404", "ID")

    # 一行都不能落：note 里可能有私密内容
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE type = 'feedback.recorded'").fetchone()[0]
        == 0
    )


def test_owner_can_still_give_feedback_on_own_private_memory(
    conn: sqlite3.Connection,
) -> None:
    owner = Server(ToolContext(conn=conn, host="h", session_id="s-1", project_id="p-1"))
    _call(
        owner,
        73,
        "tools/call",
        {"name": "remember", "arguments": {"content": SECRET, "visibility": "private"}},
    )
    response = _call(
        owner, 74, "tools/call", {"name": "feedback", "arguments": {"id": "mem-1", "kind": "wrong"}}
    )
    assert "已记录反馈" in response["result"]["content"][0]["text"]
