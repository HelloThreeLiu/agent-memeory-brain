"""断言档位与权威（设计文档 §9.3 的"档"、INV6）。

注意：这里的"档"取 ``assertion_type`` 的固定档位，**不是** ``memories.confidence``
字段的值。v0.3 混用过这两者造成歧义，不要再来一次。
"""

from __future__ import annotations

import json
from typing import Any

ASSERTION_TYPES: tuple[str, ...] = ("decision", "observation", "claim", "hypothesis")

_ASSERTION_RANK: dict[str, int] = {
    "hypothesis": 1,
    "claim": 2,
    "observation": 3,
    "decision": 4,
}

#: agent 接受（accepted）一条 claim 时，置信度上限（INV6）
AGENT_ACCEPT_CONFIDENCE_CAP = 0.85


def rank(assertion_type: str) -> int:
    try:
        return _ASSERTION_RANK[assertion_type]
    except KeyError:
        raise ValueError(f"未知的 assertion_type：{assertion_type!r}") from None


def promotion_target(current: str, by_role: str) -> str:
    """按 INV6 给出"被接受后"的断言类型。

    - 只有 ``role=user`` 能把东西升成 ``decision``；
    - agent 的接受最多把 ``claim`` / ``hypothesis`` 升为 ``observation``；
    - 其余情况保持原档（不越权）。
    """
    if current not in _ASSERTION_RANK:
        raise ValueError(f"未知的 assertion_type：{current!r}")
    if by_role == "user":
        return "decision"
    if current in ("claim", "hypothesis"):
        return "observation"
    return current


def capped_confidence(confidence: float, by_role: str) -> float:
    """agent 接受时置信度不得越过 0.85（INV6）。"""
    if by_role == "user":
        return confidence
    return min(confidence, AGENT_ACCEPT_CONFIDENCE_CAP)


def role_from_asserted_by(raw: str) -> str | None:
    """从 ``memories.asserted_by``（JSON）里取写入方角色。"""
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        role = parsed.get("role")
        return str(role) if role is not None else None
    return None
