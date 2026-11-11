"""睡眠维护周期（§9.5）。

P1 的 sleep 只做**维护**：衰减、休眠、归档、清回收站。不做 L3 抽取、不做碎片
合并、不做冲突扫描——这三条都要读 episode，而 P0 没有 L3 写入管道（BACKLOG
P1 的"不做"项）。没有数据来源的"处理未消费 episode"跑起来只会是空转，写出来
比不写更误导。

能保证的是：跑得再勤也不会让库膨胀，且 ``dry_run`` 能先把"打算动哪些"给人看。
全部步骤在**一个事务**里提交：只落一半的系统比完全没跑更难排查。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from nebula.core.timeutil import fmt, now_ts
from nebula.lifecycle import decay
from nebula.store.write_service import WriteService


@dataclass(frozen=True)
class SleepReport:
    now: str
    dry_run: bool
    scanned: int
    heat_settled: int
    dormant: int
    archived: int
    archived_capacity: int
    purged: int
    active_after: int

    def lines(self) -> tuple[str, ...]:
        prefix = "[dry-run] " if self.dry_run else ""
        return (
            f"{prefix}扫描 {self.scanned} 条",
            f"{prefix}衰减结算 {self.heat_settled} 条",
            f"{prefix}转入休眠 {self.dormant} 条",
            f"{prefix}按时间归档 {self.archived} 条",
            f"{prefix}按容量归档 {self.archived_capacity} 条",
            f"{prefix}清理回收站 {self.purged} 条",
            f"{prefix}剩余活跃 {self.active_after} 条",
        )


def run_sleep(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    host: str | None = None,
    session_id: str | None = None,
) -> SleepReport:
    """跑一轮维护。``dry_run`` 只读：不写 heat、不改状态、不清回收站。"""
    stamp = fmt(now) if now is not None else now_ts()
    plan = decay.plan(conn, now=stamp)

    if dry_run:
        return SleepReport(
            now=stamp,
            dry_run=True,
            scanned=plan.scanned,
            heat_settled=len(plan.heat),
            dormant=len(plan.to_dormant),
            archived=len(plan.to_archive),
            archived_capacity=len(plan.to_archive_capacity),
            purged=plan.purgeable,
            active_after=plan.active_after,
        )

    service = WriteService(conn)
    with service.transaction():
        settled = service.apply_heat(
            [(step.memory_id, step.after) for step in plan.heat],
            now=now,
            host=host,
            session_id=session_id,
        )
        dormant = service.sweep_status(
            plan.to_dormant, "dormant", now=now, host=host, session_id=session_id
        )
        archived = service.sweep_status(
            plan.to_archive,
            "archived",
            reason="decay",
            now=now,
            host=host,
            session_id=session_id,
        )
        archived_capacity = service.sweep_status(
            plan.to_archive_capacity,
            "archived",
            reason="capacity",
            now=now,
            host=host,
            session_id=session_id,
        )
        purged = service.purge_expired(now=now, host=host, session_id=session_id)

    active_row = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status = 'active'"
    ).fetchone()
    return SleepReport(
        now=stamp,
        dry_run=False,
        scanned=plan.scanned,
        heat_settled=settled,
        dormant=dormant,
        archived=archived,
        archived_capacity=archived_capacity,
        purged=purged,
        active_after=0 if active_row is None else int(active_row[0]),
    )
