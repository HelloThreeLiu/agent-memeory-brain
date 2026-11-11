"""生命周期策略常量（§9.5）。

数字集中在这一处，是为了让"调参"只有一个入口。它们全是**先验**：§9.5 只给了
半衰期 180–3650 天的范围，没给分 kind 的具体值；P1.5 拿到真实召回日志后应该
整表复核一次，而不是继续沿用这里的猜测。
"""

from __future__ import annotations

#: 按 kind 的半衰期（天）。决策最稳（用户定下的结论不该随时间长毛），
#: 事件最短（一次性上下文的时效性最强）。
HALF_LIFE_DAYS: dict[str, float] = {
    "decision": 3650.0,
    "relation": 730.0,
    "procedure": 730.0,
    "preference": 365.0,
    "fact": 365.0,
    "handoff": 90.0,
    "event": 30.0,
}

DEFAULT_HALF_LIFE_DAYS = 365.0

#: 休眠：heat 低于阈值且连续 N 天没被召回。休眠仍进候选池，命中即唤醒。
DORMANT_HEAT = 0.25
DORMANT_IDLE_DAYS = 30.0

#: 归档：既老又冷。两个条件必须同时满足，否则冷门但重要的事实会被误埋。
ARCHIVE_IDLE_DAYS = 180.0
ARCHIVE_HEAT_PERCENTILE = 0.10
ARCHIVE_HEAT_FLOOR = 0.05

#: 容量软上限：触顶后归档到目标水位，留出 10% 余量给接下来的写入
MAX_ACTIVE_MEMORIES = 2000
CAPACITY_TARGET = 1800

#: 唤醒阈值：命中相关性达到它才算"真的想起来了"，dormant 才回 active
WAKE_RELEVANCE = 0.25
