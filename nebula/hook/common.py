"""两个入口共用的常量与结果类型（P1.5c 审查 N-9）。

``Outcome`` 和宿主名在 ``prompt_submit`` / ``session_start`` 各写一遍，改一处漏一处
就会出现"两个 hook 记到不同 host"这类安静的不一致——而 host 是去重集的匹配键之一
（§10.7 配置前提 3）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 必须与 MCP 配置里的 ``--host`` 同名：去重集的"最近两分钟"窗口靠它匹配 MCP 落下的行
HOST_DEFAULT = "workbuddy"


@dataclass(frozen=True)
class Outcome:
    """一次运行的结果：要注入什么 + 给 stderr 的那一句话。"""

    context: str | None
    summary: str


def event_notice(payload_event: str, expected: str) -> str | None:
    """事件名与入口不匹配时给一句提示。

    **不因此跳过**：事件名不参与任何判定，挂错入口的后果是语义错（拿空 prompt 去召回），
    跳过会让整条通道静默失效——那比"多跑一次"糟得多。所以只让它可见（进 stderr 摘要），
    功能照跑，失败方向选"多说话"而不是"不说话"。
    """
    if not payload_event or payload_event == expected:
        return None
    return f"事件名不匹配（期望 {expected}，收到 {payload_event}）"
