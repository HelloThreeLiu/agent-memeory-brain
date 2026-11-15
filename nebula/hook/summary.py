"""hook 与宿主的两种说话方式：stdout 的注入块、stderr 的摘要（§10.7）。

两个入口（``prompt_submit`` / ``session_start``）共用这几个函数，因为协议细节
（``hookEventName`` 必带、Windows 代码页、fail-open 时也要留痕）与业务无关。

**退出 0 ≠ 不许说话。** hook 永远退出 0，所以"它到底有没有干活"只能靠 stderr 那一行。
不写的话，静默失效的症状是"记忆好像没了"——正是 §16.2 说的信任静默衰减。宿主是否
展示 stderr 取决于实现；不可见也不影响功能，但排障全靠它。
"""

from __future__ import annotations

import json
import sys
from contextlib import suppress

PREFIX = "[nebula-hook]"


def line(detail: str) -> str:
    return f"{PREFIX} {detail}"


def failure(exc: BaseException) -> str:
    """一行放得下的失败摘要：异常类名 + 首行。

    不写 traceback：宿主未必展示 stderr，多行只会把关键那句淹掉。
    """
    message = str(exc).strip()
    head = message.splitlines()[0].strip() if message else ""
    detail = f"失败·{type(exc).__name__}"
    return line(f"{detail}: {head}" if head else detail)


def emit(event: str, context: str) -> None:
    """把注入块写到 stdout（``hookSpecificOutput`` 契约，评审 m-4）。

    ``hookEventName`` 必带：少了它宿主不认这个字段，表现是"hook 跑了但什么都没注入"。
    """
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_plain(context: str) -> None:
    """``SessionStart`` 走纯文本：宿主文档写的是"stdout 追加到会话上下文"。

    与 :func:`emit` 的差别是实测出来的取舍——开场块如果被当作 JSON 原样贴进上下文，
    模型看到的就是一段没人解析的 JSON。这条在端到端验证里复核；若宿主其实只认 JSON，
    换回 :func:`emit`（一个调用点的事）。
    """
    sys.stdout.write(context + "\n")
    sys.stdout.flush()


def note(text: str) -> None:
    """stderr 摘要。写不出去也不能影响主流程。"""
    with suppress(OSError):
        sys.stderr.write(text + "\n")
        sys.stderr.flush()


def utf8_streams() -> None:
    """Windows 默认代码页是 cp936，中文注入块会 ``UnicodeEncodeError``。

    wrapper 里也设了 ``PYTHONIOENCODING``，这里再兜一层：hook 是宿主直接调起的
    子进程，环境变量未必继承得到。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")
