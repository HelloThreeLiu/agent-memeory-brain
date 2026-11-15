"""hook 的 stdin payload（§10.7「通道与触发」）。

WorkBuddy 每次触发 hook 都把一段 JSON 塞进 stdin。这里只取四件事，其余字段
（``permission_mode`` / ``model`` / ``generation_id`` / ``agent_type``）刻意不解析：
hook 的判定不该依赖宿主实现细节，取多了等于把自己绑死在某个版本上。

一个必须处理的意外（2026-11-13 探针实测）：**宿主自身的内部消息也走
``UserPromptSubmit``**。后台任务完成的通知，``prompt`` 是
``<task-notification><task-id>…``。不拦掉的话，每次任务通知都要召回一次——
白烧 token，还会把覆盖率的分母搅乱。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: 宿主内部消息的标签名单。可扩展：宿主升级可能新增标签，命中名单外的
#: ``<`` 开头消息时调用方要记一行 stderr，事后才好补进来。
INTERNAL_TAGS: frozenset[str] = frozenset({"task-notification"})

#: 只认"整条消息以标签开头"。在正文里搜 ``<`` 会把用户贴的 HTML/XML 误杀。
_TAG_HEAD = re.compile(r"^<\s*([A-Za-z][A-Za-z0-9_:.-]*)")


@dataclass(frozen=True)
class Payload:
    """一次 hook 触发拿到的输入。字段缺失一律给空值，不在解析阶段抛错。"""

    event: str
    session_id: str | None
    cwd: str | None
    prompt: str
    transcript_path: str | None
    #: 只有 ``SessionStart`` 有：``startup`` / ``resume`` / ``clear``（探针实测前两种）
    source: str | None = None


def parse(raw: str) -> Payload:
    """解析 stdin 的 JSON；坏输入抛 ``ValueError``，由调用方 fail-open。"""
    data = json.loads(raw)  # JSONDecodeError 是 ValueError 的子类
    if not isinstance(data, dict):
        raise ValueError("hook payload 必须是 JSON 对象")
    return Payload(
        event=_text(data, "hook_event_name") or "",
        session_id=_text(data, "session_id"),
        cwd=_text(data, "cwd"),
        prompt=_text(data, "prompt") or "",
        transcript_path=_text(data, "transcript_path"),
        source=_text(data, "source"),
    )


def internal_message(prompt: str) -> tuple[bool, str | None]:
    """判断是否为宿主内部消息，返回 ``(是否内部, 需要记 stderr 的未知标签名)``。

    三种返回值对应三种行为（§10.7）：

    - ``(False, None)``：用户消息，正常召回；
    - ``(True, None)``：已知内部消息，**静默**跳过（不召回、不记账、不注入）；
    - ``(True, "xxx")``：以标签开头但不在名单里，跳过并记 stderr——宿主升级后补
      名单全靠这一行；静默跳过只会让人以为 hook 坏了。
    """
    head = prompt.lstrip()
    if not head.startswith("<"):
        return False, None
    match = _TAG_HEAD.match(head)
    if match is None:
        # ``<3`` 这类不是标签，按用户原文处理
        return False, None
    tag = match.group(1).lower()
    if tag in INTERNAL_TAGS:
        return True, None
    return True, tag


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
