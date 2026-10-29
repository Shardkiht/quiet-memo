from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from .models import MemoryContext


def date_bounds(value: str) -> tuple[date, date]:
    """按原始精度解释日期范围，不把未知月日伪造成事实。"""
    MemoryContext(event_time=value)
    parts = [int(part) for part in value.split("-")]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    start = date(year, month, parts[2] if len(parts) > 2 else 1)
    end_month = parts[1] if len(parts) > 1 else 12
    end = date(year, end_month, parts[2] if len(parts) > 2 else calendar.monthrange(year, end_month)[1])
    return start, end


def validate_range(start: str | None, end: str | None) -> None:
    low = date_bounds(start)[0] if start else date.min
    high = date_bounds(end)[1] if end else date.max
    if low > high:
        raise ValueError("查询结束日期不能早于开始日期。")


def resolve_relative(value: str, anchor: str | None) -> str | None:
    """仅解析可确定的相对日/月/年；锚点必须携带原始时区。"""
    if not anchor:
        return None
    try:
        stamp = datetime.fromisoformat(anchor)
    except ValueError:
        return None
    if stamp.utcoffset() is None:
        return None
    text = value.strip()
    if text in {"今天", "昨天", "前天", "明天", "后天", "现在"}:
        offset = {"前天": -2, "昨天": -1, "今天": 0, "现在": 0, "明天": 1, "后天": 2}[text]
        try:
            return (stamp.date() + timedelta(days=offset)).isoformat()
        except OverflowError:
            return None
    if text in {"去年", "今年", "明年"}:
        year = stamp.year + {"去年": -1, "今年": 0, "明年": 1}[text]
        return f"{year:04d}" if 1 <= year <= 9999 else None
    if text in {"上个月", "这个月", "本月", "下个月"}:
        offset = {"上个月": -1, "这个月": 0, "本月": 0, "下个月": 1}[text]
        year, month = divmod(stamp.year * 12 + stamp.month - 1 + offset, 12)
        return f"{year:04d}-{month + 1:02d}" if 1 <= year <= 9999 else None
    return None


def matches_context(context: MemoryContext | None, start: str | None,
                    end: str | None, location: str | None, emotion: str | None) -> bool:
    if context is None:
        return False
    if location and (not context.location or location.strip().casefold() not in context.location.casefold()):
        return False
    if emotion and (not context.emotion or emotion.strip().casefold() not in context.emotion.casefold()):
        return False
    if start or end:
        # 这里查询事件日期；不拿记录时间或不完整生效区间代替。
        if not context.event_time:
            return False
        low, high = date_bounds(context.event_time)
        if start and high < date_bounds(start)[0]:
            return False
        if end and low > date_bounds(end)[1]:
            return False
    return True
