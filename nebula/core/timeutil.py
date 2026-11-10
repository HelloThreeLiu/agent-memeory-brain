"""双时间轴的时间解析与格式化。

库里的时间一律存 UTC ISO 文本；比较走 ``datetime`` 而不是字符串字典序——
``2026-3-1`` 与 ``2026-11-10`` 的字典序是反的，靠字符串比会静默错。

约定：不带时区的时间按 UTC 解释（本地单人使用，宿主给的时间不可比，见 §8.1）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def fmt(value: datetime) -> str:
    """统一成 ``YYYY-MM-DDTHH:MM:SS+00:00``。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def now_ts() -> str:
    return fmt(now_utc())


def parse_ts(value: str | None) -> datetime | None:
    """解析失败返回 ``None``（调用方按"无日期"处理），不抛异常。"""
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return None
        parsed = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_before(later_candidate: str | None, earlier_candidate: str | None) -> bool | None:
    """前者是否晚于后者；任一不可解析返回 ``None``（≡ 不可比）。"""
    left = parse_ts(later_candidate)
    right = parse_ts(earlier_candidate)
    if left is None or right is None:
        return None
    return left > right
