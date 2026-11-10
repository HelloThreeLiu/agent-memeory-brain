"""冲突路由：设计文档 §9.3 的三级路由（类型 → 权威档 → 时间）。

这是一个**纯函数**：输入新旧两条事实，输出"该做什么"，不碰数据库。
好处是 4×4=16 格能逐格断言（`tests/test_conflict_routing.py`）——9.3 正是三轮
评审查出 4 个洞的地方，靠肉眼过清单是不够的。

四个互斥且穷尽的去向（INV5）：

====  ==========================================  =========================
态    触发                                         落库动作
====  ==========================================  =========================
A'   新条是 hypothesis                              只追加，永不仲裁
A    新条是 claim                                  追加 + contradicts；跨角色开仲裁
B    新条不是 claim/hypothesis 且新档 ≥ 旧档        按时序静默失效（可回退）
C    新条不是 claim/hypothesis 且新档 < 旧档        open arbitration + 双注入
====  ==========================================  =========================

另有两个前置判断：P1 同作用域（不同 scope 直接不路由）、P2 内容包含（走 merge）。
"""

from __future__ import annotations

from dataclasses import dataclass

from nebula.core.assertion import rank
from nebula.core.timeutil import is_before, same_day

# A'：只追加，永不仲裁
ACTION_APPEND = "append"
# A：追加 + contradicts 边（INV12：claim 自身永不使他人失效）
ACTION_CONTRADICT = "contradict"
# B：新条更晚 → 旧条静默失效
ACTION_SUPERSEDE_OLD = "supersede_old"
# B：新条更早（补录历史）→ 新条落历史，旧条保持当前
ACTION_SUPERSEDE_NEW = "supersede_new"
# C：开仲裁，两条都不动，召回双注入
ACTION_ARBITRATE = "arbitrate"
# P2：内容包含关系，不是冲突
ACTION_MERGE = "merge"
# P1：不同作用域的同名键，各占各的槽位
ACTION_UNRELATED = "unrelated"

CONFLICT_STATES = ("A_prime", "A", "B", "C")


@dataclass(frozen=True)
class FactView:
    """路由只需要这些字段，不把整行传进来。"""

    id: str
    assertion_type: str
    content: str
    scope: str
    project_id: str | None
    role: str | None
    seq: int
    valid_from: str | None


@dataclass(frozen=True)
class Route:
    action: str
    reason: str
    open_arbitration: bool = False

    @property
    def state(self) -> str:
        """归到 INV5 的四态之一，供测试与审计使用。"""
        if self.action == ACTION_APPEND:
            return "A_prime"
        if self.action == ACTION_CONTRADICT:
            return "A"
        if self.action in (ACTION_SUPERSEDE_OLD, ACTION_SUPERSEDE_NEW):
            return "B"
        if self.action == ACTION_ARBITRATE:
            return "C"
        return "precondition"


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _is_contained_in(part: str, whole: str) -> bool:
    first = _normalized(part)
    second = _normalized(whole)
    return bool(first) and bool(second) and first in second


def _order(new: FactView, old: FactView) -> str:
    """按 ``valid_from`` 定先后：``later`` / ``earlier`` / ``incomparable``。

    同日、无日期、解析失败都算不可比（§9.3 第 2 条），只有不可比才回退 seq。
    """
    if same_day(new.valid_from, old.valid_from):
        return "incomparable"
    latest = is_before(new.valid_from, old.valid_from)
    if latest is None:
        return "incomparable"
    return "later" if latest else "earlier"


def route(new: FactView, old: FactView) -> Route:
    """判定新条与同槽位旧条的关系。"""
    # P1 同作用域：global 键跨项目共享，project 键不跨
    if new.scope != old.scope or new.project_id != old.project_id:
        return Route(ACTION_UNRELATED, "P1 不同作用域：不参与路由，各占各的槽位")

    # P2 真互斥还是补充：包含关系（任一方向）走 merge，不当冲突处理
    if _is_contained_in(new.content, old.content) or _is_contained_in(old.content, new.content):
        return Route(ACTION_MERGE, "P2 内容包含：合并证据与权重，不失效")

    # 0) hypothesis 只追加，连跨角色也不仲裁
    if new.assertion_type == "hypothesis":
        return Route(ACTION_APPEND, "A' hypothesis 只追加，永不仲裁")

    # 1) claim 只追加 + contradicts；跨角色才开仲裁（INV12）
    if new.assertion_type == "claim":
        if new.role != old.role:
            return Route(
                ACTION_CONTRADICT,
                "A claim 只追加并建 contradicts；跨角色开仲裁，旧条不动",
                open_arbitration=True,
            )
        return Route(
            ACTION_CONTRADICT,
            "A claim 只追加并建 contradicts；同角色只记录，不打扰人",
        )

    # 2) 新档 ≥ 旧档：按时序静默失效
    if rank(new.assertion_type) >= rank(old.assertion_type):
        order = _order(new, old)
        if order == "later":
            return Route(
                ACTION_SUPERSEDE_OLD,
                "B 新档≥旧档且新条更晚：旧条静默失效（行保留可查）",
            )
        if order == "earlier":
            return Route(
                ACTION_SUPERSEDE_NEW,
                "B 新条更早（补录历史）：新条落历史区间，旧条仍是当前",
            )
        # 同日 / 无日期 / 解析失败：只剩 seq 可比
        if new.seq > old.seq:
            return Route(
                ACTION_SUPERSEDE_OLD,
                "B 时间不可比，回退 seq：后写者更晚，旧条静默失效",
            )
        return Route(
            ACTION_SUPERSEDE_NEW,
            "B 时间不可比，回退 seq：新条更早，落历史区间",
        )

    # 3) 新档 < 旧档：低挑战高，开仲裁
    return Route(
        ACTION_ARBITRATE,
        "C 低档挑战高档：开仲裁，两条都不动，召回双注入",
        open_arbitration=True,
    )
