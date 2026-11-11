"""MCP 的四个工具（§15 P1.5 B 档：``remember`` / ``recall`` / ``expand`` / ``feedback``）。

B 档没有 hook 可以注入，全靠**工具描述驱动模型主动调用**，所以描述里写的是
"什么时候该调"而不是"这个函数干什么"（§10.5）。写清楚触发条件的收益，比多一个
参数大得多。

四条实现约束：

1. 所有写经 :class:`~nebula.store.write_service.WriteService`（INV1）——工具层不做 SQL 写；
2. ``recall`` 每次落一条 ``injection_log``，这是三项指标的**分母**，不许跳过；
3. ``expand`` 用 :class:`~nebula.recall.visibility.Viewer` 过滤，且**不暴露 show_all**
   ——agent 不该有"看全部"的开关（§10.6）；
4. ``feedback`` 自己回连最近一次注入，不靠模型传 id（模型抄 id 一定会抄错）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from nebula.recall.detail import expand
from nebula.recall.search import recall
from nebula.recall.visibility import Viewer
from nebula.store.write_service import FEEDBACK_KINDS, WriteService

#: 工具返回给模型的注入块上限（字符）。recall 的 limit 之外再加一道硬闸：
#: 模型把 5 条长记忆全塞进上下文，比少给两条更糟。
MAX_INJECT_CHARS = 4000

REMEMBER_ACTIONS = {
    "created": "已写入",
    "deduplicated": "已合并（窗口内重复）",
    "merged": "已合并进既有条目",
    "append": "已追加，旧条不动",
    "contradict": "已追加并记录异议",
    "supersede_old": "已写入，旧条失效",
    "supersede_new": "已写入（补录历史，未顶掉当前结论）",
    "arbitrate": "已写入，已开仲裁",
}


@dataclass(frozen=True)
class ToolContext:
    conn: sqlite3.Connection
    host: str | None = None
    session_id: str | None = None
    project_id: str | None = None

    @property
    def viewer(self) -> Viewer:
        return Viewer(
            session_id=self.session_id, project_id=self.project_id, host=self.host
        )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


def _require_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"参数 {key} 必须是非空字符串")
    return value.strip()


def _optional_string(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"参数 {key} 必须是字符串")
    text = value.strip()
    return text or None


def _optional_number(arguments: dict[str, Any], key: str) -> float | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"参数 {key} 必须是数字")
    return float(value)


def _optional_bool(arguments: dict[str, Any], key: str) -> bool:
    value = arguments.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"参数 {key} 必须是布尔值")
    return value


def _optional_string_list(arguments: dict[str, Any], key: str) -> tuple[str, ...]:
    value = arguments.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"参数 {key} 必须是字符串数组")
    return tuple(item.strip() for item in value if item.strip())


def _enum(arguments: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"参数 {key} 只能是 {allowed} 之一")
    return value


# ----------------------------------------------------------------------- 工具


def _remember(arguments: dict[str, Any], ctx: ToolContext) -> str:
    content = _require_string(arguments, "content")
    evidence = _optional_string_list(arguments, "evidence")
    assertion_type = _enum(
        arguments,
        "assertion_type",
        ("observation", "decision", "claim", "hypothesis"),
        "observation",
    )
    role = _enum(arguments, "role", ("user", "agent"), "agent")
    kind = _enum(
        arguments,
        "kind",
        ("fact", "preference", "decision", "relation", "procedure", "event", "handoff"),
        "fact",
    )
    scope = _enum(
        arguments,
        "scope",
        ("global", "project", "host"),
        "project" if ctx.project_id else "global",
    )
    project_id = _optional_string(arguments, "project_id")

    if scope == "global":
        project_id = None
    elif scope == "project":
        project_id = project_id or ctx.project_id
        if project_id is None:
            raise ValueError(
                "scope=project 需要项目上下文：当前目录解析不出项目，"
                "请显式传 project_id 或改用 scope=global"
            )

    # 有证据的进 L4（事实层），没证据的进 L3（情节层）。INV4 要求 semantic 必须带
    # evidence，而 agent 的大部分记录本来就是"看到/听到"，不该硬凑一个证据 id。
    user_decision = assertion_type == "decision" and role == "user"
    layer = "semantic" if evidence or user_decision else "episodic"

    visibility = _enum(arguments, "visibility", ("shared", "private"), "shared")
    if visibility == "private" and not ctx.session_id:
        raise ValueError("private 需要 session 身份：请用 --session 启动，或改用 shared")

    # 显式判断而不是 `x or 0.5`：importance=0 是合法值，`0 or 0.5` 会把它悄悄改成 0.5
    importance = _optional_number(arguments, "importance")
    result = WriteService(ctx.conn).remember(
        content,
        scope=scope,
        project_id=project_id,
        host=ctx.host,
        session_id=ctx.session_id if visibility == "private" else None,
        visibility=visibility,
        layer=layer,
        kind=kind,
        assertion_type=assertion_type,
        role=role,
        fact_key=_optional_string(arguments, "fact_key"),
        importance=0.5 if importance is None else importance,
        evidence=evidence,
        pinned=_optional_bool(arguments, "pinned"),
    )
    label = REMEMBER_ACTIONS.get(result.action, result.action)
    extra = f"，旧条 {', '.join(result.superseded)} 已失效" if result.superseded else ""
    if result.arbitration_id:
        extra += f"，已开仲裁 {result.arbitration_id}（两条都会注入，等用户裁决）"
    return f"{label}：{result.id}（seq={result.seq}{extra}）"


def _recall(arguments: dict[str, Any], ctx: ToolContext) -> str:
    query = _require_string(arguments, "query")
    requested = _optional_number(arguments, "limit")
    limit = 5 if requested is None else int(requested)
    if limit < 1 or limit > 20:
        raise ValueError("limit 必须在 1..20 之间")

    result = recall(
        ctx.conn,
        query,
        limit,
        project_id=ctx.project_id,
        viewer_session=ctx.session_id,
        record=True,
    )
    lines = list(result.lines())
    text = "\n".join(lines)
    if len(text) > MAX_INJECT_CHARS:
        text = text[:MAX_INJECT_CHARS] + "\n…（已截断，需要细节用 expand）"

    WriteService(ctx.conn).log_injection(
        query=query,
        memory_ids=[hit.id for hit in result.hits],
        chars=len(text),
        top_k=limit,
        host=ctx.host,
        session_id=ctx.session_id,
        project_id=ctx.project_id,
        degraded_level=1 if result.degraded else 0,
        elapsed_ms=result.elapsed_ms,
    )

    if not result.hits:
        return "没有命中。不要编造记忆内容；换个说法再试一次，或直接说没查到。"
    return f"{text}\n\n（{len(result.hits)} 条，{result.elapsed_ms:.0f}ms）"


def _expand(arguments: dict[str, Any], ctx: ToolContext) -> str:
    memory_id = _require_string(arguments, "id")
    detail = expand(ctx.conn, memory_id, viewer=ctx.viewer)
    return "\n".join(detail.lines())


def _feedback(arguments: dict[str, Any], ctx: ToolContext) -> str:
    memory_id = _require_string(arguments, "id")
    kind = _enum(arguments, "kind", FEEDBACK_KINDS, "wrong")
    note = _optional_string(arguments, "note")
    injection_id = _latest_injection(ctx.conn, memory_id)

    result = WriteService(ctx.conn).record_feedback(
        memory_id,
        kind,
        source="mcp",
        note=note,
        injection_id=injection_id,
        host=ctx.host,
        session_id=ctx.session_id,
    )
    linked = "，已关联到最近一次注入" if injection_id else ""
    return f"已记录反馈：{memory_id} ← {kind}（{result.id}{linked}）"


def _latest_injection(conn: sqlite3.Connection, memory_id: str) -> str | None:
    """回连最近一次包含该条目的注入。

    用 ``json_each`` 而不是 ``LIKE``：``mem-1`` 是 ``mem-10`` 的前缀，
    字符串包含会把反馈挂到错的那次注入上，负反馈率就跟着漂。
    """
    row = conn.execute(
        """
        SELECT il.id AS id FROM injection_log AS il, json_each(il.memory_ids) AS je
        WHERE je.value = ?
        ORDER BY il.ts DESC, il.id DESC
        LIMIT 1
        """,
        (memory_id,),
    ).fetchone()
    return None if row is None else str(row["id"])


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="remember",
        description=(
            "把一条跨会话有用的信息写进长期记忆。"
            "什么时候调：用户明确下了结论、纠正了你、或你学到了下次还会用到的命令/路径/偏好。"
            "不要调：临时状态（当前时间、一次性命令输出）、能从仓库里直接读到的代码结构。"
            "带 evidence 的进事实层（L4），不带的进情节层（L3）；推测用 assertion_type=claim。"
        ),
        schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "一句话说清这条记忆，别贴大段原文"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "fact",
                        "preference",
                        "decision",
                        "relation",
                        "procedure",
                        "event",
                        "handoff",
                    ],
                    "description": "记忆类别，默认 fact",
                },
                "assertion_type": {
                    "type": "string",
                    "enum": ["observation", "decision", "claim", "hypothesis"],
                    "description": (
                        "observation=看到的事实，decision=用户拍的结论，"
                        "claim=推测（会被标未验证）"
                    ),
                },
                "role": {
                    "type": "string",
                    "enum": ["user", "agent"],
                    "description": (
                        "谁说的：用户本人 = user（只有 user 能写 decision），你转述 = agent"
                    ),
                },
                "fact_key": {
                    "type": "string",
                    "description": (
                        "唯一真相槽位（如 build.cmd）。同键的新值会顶掉旧值，"
                        "别给一次性信息填这个"
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project", "host"],
                    "description": "global=跨项目，project=只对本项目（默认），host=只对本宿主",
                },
                "project_id": {"type": "string", "description": "默认用当前目录解析出的项目"},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "证据 id（文件/命令输出/episode）。给了才进事实层",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["shared", "private"],
                    "description": "private 只对本 session 可见；默认 shared",
                },
                "pinned": {"type": "boolean", "description": "置顶，免于自动归档"},
            },
            "required": ["content"],
        },
    ),
    ToolSpec(
        name="recall",
        description=(
            "按查询召回相关记忆。"
            "什么时候调：会话开始、接到新任务、要回忆项目约定或用户偏好时——先调它再回答。"
            "查询用用户的原话，不要改写成关键词。"
            "没命中就照实说没查到，不要编造记忆内容。"
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询原文，例如“上次那个构建命令是什么”",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="expand",
        description=(
            "展开一条记忆的完整来历：证据、历代版本、冲突与审计事件。"
            "什么时候调：召回结果带 |未验证 标记、两条互相矛盾、或要确认某条依据是否还成立。"
        ),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "记忆 id，形如 mem-12"},
            },
            "required": ["id"],
        },
    ),
    ToolSpec(
        name="feedback",
        description=(
            "纠正一条记忆：useful / useless / wrong。"
            "什么时候调：用户说“这条记错了/没用”，或你按记忆行动后发现它已经过时。"
            "不要自己改写正文——用这个工具留下纠正痕迹，系统据此降低注入。"
        ),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "被纠正的记忆 id"},
                "kind": {
                    "type": "string",
                    "enum": ["useful", "useless", "wrong"],
                    "description": "wrong=内容是错的，useless=内容对但这次没用，useful=有用",
                },
                "note": {"type": "string", "description": "一句话说明错在哪（可选）"},
            },
            "required": ["id"],
        },
    ),
)

_DISPATCH = {
    "remember": _remember,
    "recall": _recall,
    "expand": _expand,
    "feedback": _feedback,
}


def call_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
    handler = _DISPATCH.get(name)
    if handler is None:
        raise KeyError(f"没有这个工具：{name}")
    return handler(arguments, ctx)
