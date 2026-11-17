"""A 档的增量去重（§10.7「增量去重」，P1.5c 评审 M-2）。

B 档模型自己决定查不查，不存在重复；A 档每轮都跑，同一批记忆会被反复注入。

去重集 = 本会话已注入（**无时间窗口**）∪ 最近两分钟任意同 host 注入。

两个刻意选择：

1. **本会话无窗口**：上下文里已经有了，重注入纯属浪费 token；滚动窗口还会让重注入
   逐条错开发生。会话结束 ``session_id`` 变化，去重集自然重置。
2. **补一个 host 时间窗口**：MCP 侧不带 session（§10.7 已知限制 1），``session = ?``
   永远匹配不到 ``NULL`` 行，只能靠"同 host + 两分钟"兜住"模型刚查过、hook 立刻
   再注入一遍"的竞态。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from nebula.core.timeutil import fmt, now_ts, parse_ts

#: MCP 竞态窗口。两分钟足够覆盖"查完立刻说话"的间隔，又不至于把上一轮的消息也算进来。
DEDUP_WINDOW_SECONDS = 120


def seen_ids(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    host: str,
    now: datetime | None = None,
    window_seconds: int = DEDUP_WINDOW_SECONDS,
) -> set[str]:
    """本会话与近窗口内已经注入过的 memory id。

    ``session_id`` 为空时，SQL 里的 ``session = NULL`` 不成立，只剩时间窗口生效：
    去重变弱（同一会话的旧条目可能被再注入一次），但不会误抑制。这个方向是有意的——
    少去重只是浪费一点 token，多去重会让该出现的记忆消失。

    **两条查询而不是一条 `OR`**（审查 M-3）：`OR` 的另一支（`session`）上没有索引，
    优化器只能 `SCAN injection_log` + `USE TEMP B-TREE FOR DISTINCT`，实测 300k 行
    20.8ms 且随库线性增长——而这条查询每轮都跑。拆开后两支各走 `ix_injection_log_session`
    与 `ix_injection_log_ts`，在 Python 侧取并集。

    窗口加了上界 ``ts <= now``（审查 N-1）：没有上界时，任何"未来时间"的行会永久命中
    去重集；测试里注入 `now` 与真实时钟不一致时就是这么被掩盖的。
    """
    stamp = fmt(now) if now is not None else now_ts()
    moment = parse_ts(stamp)
    if moment is None:  # pragma: no cover - stamp 由 fmt 生成，一定可解析
        return set()
    cutoff = fmt(moment - timedelta(seconds=window_seconds))

    ids: set[str] = set()
    if session_id is not None:
        rows = conn.execute(
            """
            SELECT DISTINCT je.value AS memory_id
            FROM injection_log AS il, json_each(il.memory_ids) AS je
            WHERE il.session = ?
            """,
            (session_id,),
        ).fetchall()
        ids.update(str(row["memory_id"]) for row in rows)

    rows = conn.execute(
        """
        SELECT DISTINCT je.value AS memory_id
        FROM injection_log AS il, json_each(il.memory_ids) AS je
        WHERE il.host = ? AND il.ts >= ? AND il.ts <= ?
        """,
        (host, cutoff, stamp),
    ).fetchall()
    ids.update(str(row["memory_id"]) for row in rows)
    return ids
