"""hook 打开库的方式（§10.7「fail-open」，P1.5c 审查 C-1）。

与 CLI / MCP 的差别只有一个：**等不起**。hook 每轮都跑，宿主给它的超时是 10s，
而 ``store.db.connect()`` 的 ``busy_timeout=10000`` 在这里会把一次
``BEGIN IMMEDIATE`` 拖到 10s：``_BEGIN_ATTEMPTS=5`` + 每次退避 ⇒ 最坏约 51s，
再加一次写事务就是 100s。审查实测（另一连接持写锁）：**阻塞 55.3s**——那时
"永远退出 0 / 5s 软上限 / 绝不拖住宿主"三条都不成立。

所以 hook 自己组合连接，三条都必须有：

1. ``busy_timeout`` 压到 300ms，而且**在 ``connect()`` 里就给**——``journal_mode = WAL``
   的切换同样要拿写锁，库还不是 WAL 时那一步也会等满 10s（复审残余风险 1）；
2. ``set_progress_handler``：到点让 SQLite **中断当前语句**，而不是等它自己返回
   （busy_timeout 只管等锁，管不了慢查询）；
3. 迁移前就把 1、2 装好——``open_database()`` 是先 connect 再 migrate，锁争用时
   卡在 migrate 里就已经晚了。
"""

from __future__ import annotations

import sqlite3
import time

from nebula.store.db import connect, migrate, resolve_db_path

#: 等锁上限。5 次 BEGIN 尝试 × 300ms + 退避（0.1+0.2+0.3+0.4s）≈ 2.5s，
#: 落在 5s 软上限内；也远小于宿主的 10s 硬超时。
HOOK_BUSY_TIMEOUT_MS = 300

#: progress handler 的触发间隔（VM 指令数）。太小会让长查询变慢，太大会晚到点。
_PROGRESS_STEPS = 1000


def open_hook_database(path: str | None, *, deadline_ms: int) -> sqlite3.Connection:
    """打开库并装好"快速失败"的两道闸。

    ``deadline_ms`` 从**调用这一刻**算起，与调用方的软上限同源，避免两处各算一套。
    """
    conn = connect(resolve_db_path(path), busy_timeout_ms=HOOK_BUSY_TIMEOUT_MS)
    deadline = time.monotonic() + deadline_ms / 1000

    def _abort_when_late() -> int:
        # 返回非 0 让当前语句以 OperationalError("interrupted") 结束
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_abort_when_late, _PROGRESS_STEPS)
    migrate(conn)
    return conn
