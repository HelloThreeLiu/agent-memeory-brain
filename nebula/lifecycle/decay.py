"""衰减与遗忘阶梯的计算部分（§9.5）：只算，不写。

把"该动谁"与"怎么动"分开，是为了让阈值调参可以单测：``plan()`` 是纯函数式的
读操作，任何一条判定都能用一个构造好的库断言，不需要真的跑一次睡眠周期。

遗忘阶梯：

    active --heat<0.25 且 30 天未召回--> dormant
    dormant/active --heat 进最低 10% 且 180 天未召回--> archived(decay)
    archived/任意 --用户 forget--> deleted（回收站）--30 天--> 物理删

``pinned`` 在每一级都豁免：用户说"这条别动"就该别动。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from nebula.core.timeutil import fmt, now_ts, parse_ts
from nebula.lifecycle import policy
from nebula.store.write_service import RECYCLE_WINDOW_DAYS

#: 浮点比较的容差：heat 每次结算都会变，但变化小于它就不值得写一次库
HEAT_EPSILON = 1e-9


@dataclass(frozen=True)
class HeatStep:
    memory_id: str
    before: float
    after: float


@dataclass(frozen=True)
class LifecyclePlan:
    """一次睡眠周期**打算**做什么。dry_run 直接把它打印出来。"""

    now: str
    scanned: int
    heat: tuple[HeatStep, ...]
    to_dormant: tuple[str, ...]
    to_archive: tuple[str, ...]
    to_archive_capacity: tuple[str, ...]
    purgeable: int

    @property
    def active_after(self) -> int:
        gone = len(self.to_dormant) + len(self.to_archive) + len(self.to_archive_capacity)
        return max(0, self.scanned - gone)


def half_life_for(kind: str, stored: float | None) -> float:
    """库里有 ``half_life_days`` 就用它，否则按 kind 取先验值。"""
    if stored is not None and stored > 0:
        return float(stored)
    return policy.HALF_LIFE_DAYS.get(kind, policy.DEFAULT_HALF_LIFE_DAYS)


def decayed_heat(heat: float, half_life_days: float, elapsed_days: float) -> float:
    """指数衰减：``heat * 0.5 ** (elapsed / half_life)``。

    ``elapsed`` 为负（时钟回拨 / 未来时间戳）按 0 处理——不能靠"负衰减"
    把 heat 抬高，那会让改系统时间变成刷权重。
    """
    if half_life_days <= 0:
        return heat
    if elapsed_days <= 0:
        return heat
    return heat * math.pow(0.5, elapsed_days / half_life_days)


def _elapsed_days(start: str | None, now: datetime) -> float:
    parsed = parse_ts(start)
    if parsed is None:
        return 0.0
    return max(0.0, (now - parsed).total_seconds() / 86400.0)


def idle_days(last_touched: str | None, created_at: str, now: datetime) -> float:
    """距上次被想起的天数。从未召回过的条目按创建时间起算。"""
    return _elapsed_days(last_touched or created_at, now)


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位。空集返回 0，小样本也照算（不引统计库）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def plan(
    conn: sqlite3.Connection, *, now: datetime | str | None = None
) -> LifecyclePlan:
    """算出这一轮该结算的 heat、该休眠的、该归档的、能清回收站的条数。"""
    stamp = now_ts() if now is None else (fmt(now) if isinstance(now, datetime) else now)
    parsed_now = parse_ts(stamp)
    if parsed_now is None:  # pragma: no cover - stamp 由 fmt 生成
        raise ValueError(f"无法解析的时间：{stamp!r}")

    rows = conn.execute(
        """
        SELECT id, status, kind, heat, half_life_days, pinned,
               last_recalled, created_at, decayed_at
        FROM memories
        WHERE status IN ('active', 'dormant')
        ORDER BY seq
        """
    ).fetchall()

    steps: list[HeatStep] = []
    heats: dict[str, float] = {}
    for row in rows:
        memory_id = str(row["id"])
        before = float(row["heat"])
        half_life = half_life_for(str(row["kind"]), _opt_float(row["half_life_days"]))
        # 用上次结算时刻而不是创建时刻：否则每跑一次睡眠周期都会把
        # 整个历史区间重算一遍，同一段时间被衰减多次
        base = row["decayed_at"] or row["created_at"]
        after = decayed_heat(before, half_life, _elapsed_days(_opt_str(base), parsed_now))
        heats[memory_id] = after
        if abs(after - before) > HEAT_EPSILON:
            steps.append(HeatStep(memory_id=memory_id, before=before, after=after))

    to_dormant: list[str] = []
    archive_pool: list[tuple[str, float]] = []
    for row in rows:
        memory_id = str(row["id"])
        if int(row["pinned"]) == 1:
            continue
        heat = heats[memory_id]
        idle = idle_days(
            _opt_str(row["last_recalled"]), str(row["created_at"]), parsed_now
        )
        if (
            str(row["status"]) == "active"
            and heat < policy.DORMANT_HEAT
            and idle >= policy.DORMANT_IDLE_DAYS
        ):
            to_dormant.append(memory_id)
        if idle >= policy.ARCHIVE_IDLE_DAYS:
            archive_pool.append((memory_id, heat))

    threshold = max(
        percentile([heat for _, heat in archive_pool], policy.ARCHIVE_HEAT_PERCENTILE),
        policy.ARCHIVE_HEAT_FLOOR,
    )
    to_archive = [memory_id for memory_id, heat in archive_pool if heat <= threshold]
    to_archive, to_archive_capacity = _apply_capacity(rows, heats, to_archive, to_dormant)

    # 用 Python 算 cutoff 再用字符串比较：SQLite 的 datetime() 输出是
    # "YYYY-MM-DD HH:MM:SS"，与库里存的 "+00:00" 格式字典序不可比
    cutoff = fmt(parsed_now - timedelta(days=RECYCLE_WINDOW_DAYS))
    purgeable = int(
        conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'deleted' "
            "AND deleted_at IS NOT NULL AND deleted_at <= ?",
            (cutoff,),
        ).fetchone()[0]
    )

    return LifecyclePlan(
        now=stamp,
        scanned=len(rows),
        heat=tuple(steps),
        to_dormant=tuple(to_dormant),
        to_archive=tuple(to_archive),
        to_archive_capacity=tuple(to_archive_capacity),
        purgeable=purgeable,
    )


def _apply_capacity(
    rows: Sequence[sqlite3.Row],
    heats: dict[str, float],
    to_archive: list[str],
    to_dormant: Sequence[str],
) -> tuple[list[str], list[str]]:
    """触顶时按 heat 从低到高补归档，直到回到目标水位。

    补进来的条目**不要求**"180 天未召回"——容量压力是另一条独立的遗忘理由
    （``archived_reason='capacity'``）。但它仍然只从 active/dormant 里选，
    且绝不碰 pinned。

    返回 ``(按时间归档的, 按容量归档的)`` 两组：理由不同，审计时要分得开。
    """
    active = [row for row in rows if str(row["status"]) == "active"]
    removed = set(to_dormant) | set(to_archive)
    projected = sum(1 for row in active if str(row["id"]) not in removed)
    if projected <= policy.MAX_ACTIVE_MEMORIES:
        return to_archive, []

    already = set(to_archive)
    candidates = sorted(
        (
            (str(row["id"]), heats[str(row["id"])])
            for row in rows
            if int(row["pinned"]) == 0 and str(row["id"]) not in already
        ),
        # 必须显式按 heat 排：元组默认先比 id，"mem-1" 永远排最前，
        # 归档的就成了最早写入的而不是最冷的
        key=lambda item: item[1],
    )
    need = projected - policy.CAPACITY_TARGET
    capacity: list[str] = []
    for memory_id, _heat in candidates:
        if need <= 0:
            break
        capacity.append(memory_id)
        need -= 1
    return to_archive, capacity


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_float(value: object) -> float | None:
    return None if value is None else float(str(value))
