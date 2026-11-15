"""hook 的 stderr 摘要（§10.7「fail-open 与故障可见」）。

hook 永远退出 0，所以"它到底有没有干活"只能靠这一行。不写的话，静默失效的症状
是"记忆好像没了"——正是 §16.2 说的信任静默衰减。宿主是否展示 stderr 取决于实现；
不可见也不影响功能，但排障全靠它。
"""

from __future__ import annotations

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
