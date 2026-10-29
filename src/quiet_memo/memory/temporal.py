from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from pathlib import Path
from typing import TYPE_CHECKING

from .models import Entry, MemoryContext
from .store import MemoryStore

if TYPE_CHECKING:
    from .service import MemoryService


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


def disjoint_periods(left: MemoryContext | None, right: MemoryContext | None) -> bool:
    """只有明确封闭且不重叠的有效期才排除冲突，缺省结束日期不补猜。"""
    if left is None or right is None:
        return False
    return bool(
        (left.valid_to and right.valid_from and date_bounds(left.valid_to)[1] < date_bounds(right.valid_from)[0])
        or (right.valid_to and left.valid_from and date_bounds(right.valid_to)[1] < date_bounds(left.valid_from)[0])
    )


def select_facts(entries: list[Entry], *, as_of: str | None = None,
                 history: bool = False) -> list[Entry]:
    """历史只接受明确的演化链；纠错与删除不能使旧内容重新成为事实。"""
    target = date.fromisoformat(as_of) if as_of else date.today()
    if as_of and (len(as_of) != 10 or target.isoformat() != as_of):
        raise ValueError("历史时点须为 YYYY-MM-DD。")
    children: dict[str, list[Entry]] = {}
    for entry in entries:
        if entry.supersedes:
            children.setdefault(entry.supersedes, []).append(entry)
    result = []
    for entry in entries:
        if entry.status == "deleted":
            continue
        context = entry.context
        start = date_bounds(context.valid_from)[0] if context and context.valid_from else None
        end = date_bounds(context.valid_to)[1] if context and context.valid_to else None
        if entry.status != "active":
            if not (history or as_of) or entry.status != "superseded":
                continue
            node = entry
            visited = {node.id}
            valid_chain = True
            while node.status == "superseded":
                successors = children.get(node.id, [])
                if len(successors) != 1:
                    valid_chain = False
                    break
                successor = successors[0]
                if successor.id in visited or successor.metadata.get("change_kind") != "evolution":
                    valid_chain = False
                    break
                successor_context = successor.context
                if not successor_context or not successor_context.valid_from:
                    valid_chain = False
                    break
                transition = date_bounds(successor_context.valid_from)[0]
                if transition == date.min:
                    valid_chain = False
                    break
                boundary = transition - timedelta(days=1)
                end = min(end, boundary) if end else boundary
                visited.add(successor.id)
                node = successor
            if not valid_chain or node.status != "active":
                continue
        if history and not as_of:
            result.append(entry)
            continue
        if as_of and start is None:
            # 无起点的历史状态只可展示来源，不能断言其在某一天成立。
            continue
        if (start and target < start) or (end and target > end):
            continue
        result.append(entry)
    return result


def evolve_fact(service: MemoryService, entry_id: str, text: str, effective_from: str) -> Entry:
    """用户明确确认的状态变化；保留旧事实，区别于纠正错误。"""
    from .detect import contains_secret

    start = date.fromisoformat(effective_from)
    if len(effective_from) != 10 or start.isoformat() != effective_from or start > date.today():
        raise ValueError("变化日期须为已发生的 YYYY-MM-DD；未来计划请另记为计划。")
    if not text.strip() or contains_secret(text):
        raise ValueError("新状态为空或包含凭据。")
    with service.write_lock:
        service.inferences.all()
        old = service.store.find(entry_id)
        if old is None or old.status != "active":
            raise ValueError("只能为当前有效条目记录时间变化。")
        if old.context and old.context.valid_from and start <= date_bounds(old.context.valid_from)[0]:
            raise ValueError("变化日期必须晚于原状态的开始日期。")
        if any(entry.text == text.strip() for entry in service.current_facts()):
            raise ValueError("相同内容已存在。")
        entry = service.store.append(old.topic, text.strip(), source="user-evolution",
            supersedes=old.id, extra={"change_kind": "evolution"},
            context=MemoryContext(valid_from=effective_from))
        service.store.set_status(old.id, "superseded")
        service.audit.write("evolve", old_entry=old.id, new_entry=entry.id, effective_from=effective_from)
        service._invalidate_inferences()
        return entry


class TemporalStore(MemoryStore):
    """显式历史查询的只读快照，复用关键词检索；不覆盖向量缓存。"""

    def __init__(self, vault: Path, selected: list[Entry]) -> None:
        super().__init__(vault)
        self.selected = selected

    def entries(self, topic: str | None = None, include_inactive: bool = False) -> list[Entry]:
        return [entry for entry in self.selected if topic is None or entry.topic == topic]
