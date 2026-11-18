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

#: 包装上的 ``data-role``：**只有** ``user-context`` 是真用户输入。
#: 其余（``error-recovery`` 这类宿主自动续跑）既不该召回，也不该进覆盖率分母（复审 R2-2）。
USER_CONTEXT_ROLE = "user-context"

#: 只认"整条消息以标签开头"。在正文里搜 ``<`` 会把用户贴的 HTML/XML 误杀。
_TAG_HEAD = re.compile(r"^<\s*([A-Za-z][A-Za-z0-9_:.-]*)")

#: 包装上的属性区里取 ``data-role``
_ROLE_ATTR = re.compile(r"""data-role\s*=\s*["']([^"']+)["']""", re.I)

#: 成对出现的包装块，**整条包住**（``$`` 锚）：只去掉外壳、保留里面的内容，
#: 否则会把用户原话一起删掉。``</tag>`` 与开标签同名，大小写不敏感。
_WRAPPER_BLOCK = re.compile(
    r"^<\s*(?P<tag>[A-Za-z][A-Za-z0-9_:.-]*)\b(?P<attrs>[^>]*)>(?P<body>.*)"
    r"</\s*(?P=tag)\s*>\s*$",
    re.S | re.I,
)

#: 前缀外壳：壳外还有正文（``<system-reminder …>ctx</system-reminder>真正的提问``，复审残余 3）
_WRAPPER_PREFIX = re.compile(
    r"^<\s*(?P<tag>[A-Za-z][A-Za-z0-9_:.-]*)\b(?P<attrs>[^>]*)>.*?</\s*(?P=tag)\s*>",
    re.S | re.I,
)

#: 宿主把用户原话放在这个标签里
_USER_QUERY_BLOCK = re.compile(r"<user_query>(.*?)</user_query>", re.S | re.I)

#: 任意标签块——用来判断"去掉标签后还剩不剩正文"
_ANY_TAG_BLOCK = re.compile(r"<[^>]*>.*?</[^>]*>", re.S)

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

    处理三种形态：整条被 ``<system-reminder …>…</system-reminder>`` 包住（可多层）、
    只有前缀外壳而正文在壳外、以及用户原话在 ``<user_query>…</user_query>`` 里。
    剥完是空串说明这条消息只有宿主样板，调用方按空消息跳过。
    """
    return _unwrap(prompt)[0]


def internal_message(prompt: str) -> tuple[bool, str | None]:
    """判断是否为宿主内部消息，返回 ``(是否内部, 需要记 stderr 的未知标签名)``。

    判定顺序（N-2 / R2-2）：

    1. **包装上的 ``data-role`` 不是 ``user-context``** → 宿主控制消息（如
       ``error-recovery``），静默跳过。这类消息 hook 根本不该召回，覆盖率也不该算一轮；
    2. 剥完包装仍以标签开头 → 按标签名单决定：命中内部名单静默跳过，未知标签跳过并记 stderr
       （宿主升级后补名单全靠这一行）；
    3. 其余按用户消息处理。

    三种返回值：``(False, None)`` 正常召回；``(True, None)`` 静默跳过；
    ``(True, "xxx")`` 跳过并记一行 stderr。未闭合的包装标签落在第三种。
    """
    text, role = _unwrap(prompt)
    if role is not None and role != USER_CONTEXT_ROLE:
        return True, None
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


def is_user_turn(prompt: str) -> bool:
    """这条消息算不算一轮**真实用户**输入。

    hook 与覆盖率统计共用这一份判定（M-1 / R2-2）：两边各写一份，口径就会漂——
    ``<task-notification>`` 是一类，``data-role="error-recovery"`` 是另一类，实测后者
    能让 4 轮样本里的覆盖率从 100% 掉到 75%。
    """
    internal, _ = internal_message(prompt)
    return not internal and bool(strip_wrappers(prompt))


def _unwrap(prompt: str) -> tuple[str, str | None]:
    """剥壳，返回 ``(正文, 最外层包装的 data-role)``。"""
    text = prompt.strip()
    role: str | None = None
    for _ in range(_MAX_WRAPPERS):
        match = _WRAPPER_BLOCK.match(text)
        if match is not None and match.group("tag").lower() in WRAPPER_TAGS:
            if role is None:
                role = _role_of(match.group("attrs"))
            text = match.group("body").strip()
            continue
        prefix = _WRAPPER_PREFIX.match(text)
        if prefix is not None and prefix.group("tag").lower() in WRAPPER_TAGS:
            if role is None:
                role = _role_of(prefix.group("attrs"))
            text = text[prefix.end() :].strip()
            continue
        break

    # ``<user_query>`` 只在本条消息"除标签外没有别的正文"时才采纳（R2-1）：
    # 用户正文里提到这个标签（讨论 hook payload、贴 transcript）时不能把话截掉半句，
    # 那段文本既要去召回、又要落进 injection_log.query，截错了两边都错且不报错。
    found = _USER_QUERY_BLOCK.search(text)
    if found is not None and _only_tag_blocks(text):
        text = found.group(1).strip()
    return text, role


def _role_of(attrs: str) -> str | None:
    match = _ROLE_ATTR.search(attrs)
    return None if match is None else match.group(1).strip().lower()


def _only_tag_blocks(text: str) -> bool:
    """去掉所有标签块后是否什么都不剩。"""
    return not _ANY_TAG_BLOCK.sub("", text).strip()


def _text(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
