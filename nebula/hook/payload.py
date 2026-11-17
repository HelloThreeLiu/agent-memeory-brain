"""hook 的 stdin payload（§10.7「通道与触发」）。

WorkBuddy 每次触发 hook 都把一段 JSON 塞进 stdin。这里只取四件事，其余字段
（``permission_mode`` / ``model`` / ``generation_id`` / ``agent_type``）刻意不解析：
hook 的判定不该依赖宿主实现细节，取多了等于把自己绑死在某个版本上。

三个必须处理的意外：

1. **宿主自身的内部消息也走 ``UserPromptSubmit``**（2026-11-13 探针实测）：后台任务
   完成的通知，``prompt`` 是 ``<task-notification><task-id>…``。不拦掉的话每次任务通知
   都要召回一次——白烧 token，还会把覆盖率的分母搅乱。
2. **包装标签不是内部消息**（P1.5c 审查 N-2）：宿主在 transcript 里给每条 user 消息套
   ``<system-reminder data-role="user-context">…<user_query>真正的话</user_query>``，
   而 hook 目前收到的是干净原文。两者随时可能变，所以包装标签要**剥掉后继续召回**，
   不能当成内部消息跳过——否则某天宿主换了传法，召回会整轮失效，只剩一行 stderr。
3. 两张表必须分开（:data:`WRAPPER_TAGS` / :data:`INTERNAL_TAGS`），否则"剥掉"与"跳过"
   这两种完全不同的处置会被混成一个。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: 内部消息标签：命中即**跳过**（不召回、不记账、不注入）
INTERNAL_TAGS: frozenset[str] = frozenset({"task-notification"})

#: 包装标签：**剥掉**外壳后按用户原文继续处理
WRAPPER_TAGS: frozenset[str] = frozenset({"system-reminder"})

#: 只认"整条消息以标签开头"。在正文里搜 ``<`` 会把用户贴的 HTML/XML 误杀。
_TAG_HEAD = re.compile(r"^<\s*([A-Za-z][A-Za-z0-9_:.-]*)")

#: 成对出现的包装块。**必须整条包住**（``$`` 锚）才剥：只去掉外壳、保留里面的内容，
#: 否则会把用户原话一起删掉。``</tag>`` 与开标签同名，大小写不敏感。
_WRAPPER_BLOCK = re.compile(
    r"^<\s*(?P<tag>[A-Za-z][A-Za-z0-9_:.-]*)\b[^>]*>(?P<body>.*)</\s*(?P=tag)\s*>\s*$",
    re.S | re.I,
)

#: 宿主把用户原话放在这个标签里
_USER_QUERY_BLOCK = re.compile(r"<user_query>(.*?)</user_query>", re.S | re.I)

#: 剥包装的层数上限：防畸形输入把这里变成死循环
_MAX_WRAPPERS = 4


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


def strip_wrappers(prompt: str) -> str:
    """剥掉宿主的包装标签，返回用户原文（拿它去召回，不要拿原始 prompt）。

    处理两种形态：整条被 ``<system-reminder …>…</system-reminder>`` 包住（可以套多层），
    以及用户原话在 ``<user_query>…</user_query>`` 里。剥完是空串说明这条消息只有宿主
    样板，调用方按空消息跳过。
    """
    text = prompt.strip()
    for _ in range(_MAX_WRAPPERS):
        match = _WRAPPER_BLOCK.match(text)
        if match is None or match.group("tag").lower() not in WRAPPER_TAGS:
            break
        text = match.group("body").strip()
    found = _USER_QUERY_BLOCK.search(text)
    if found is not None:
        text = found.group(1).strip()
    return text


def internal_message(prompt: str) -> tuple[bool, str | None]:
    """判断是否为宿主内部消息，返回 ``(是否内部, 需要记 stderr 的未知标签名)``。

    **先剥包装再看开头**（N-2）：只有剥完之后仍以标签开头的才是宿主拼的消息。
    三种返回值对应三种行为（§10.7）：

    - ``(False, None)``：用户消息，正常召回；
    - ``(True, None)``：已知内部消息，**静默**跳过（不召回、不记账、不注入）；
    - ``(True, "xxx")``：以标签开头但不在名单里，跳过并记 stderr——宿主升级后补
      名单全靠这一行；静默跳过只会让人以为 hook 坏了。未闭合的包装标签也落在这里。
    """
    text = strip_wrappers(prompt)
    if not text.startswith("<"):
        return False, None
    match = _TAG_HEAD.match(text)
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
