from __future__ import annotations

import calendar
import re
import secrets
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryContext(BaseModel):
    """可选的事实上下文；日期字符串自身保留年、月、日精度。"""

    model_config = ConfigDict(extra="forbid")

    event_time: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    time_text: str | None = None
    location: str | None = None
    location_kind: Literal["event", "residence"] | None = None
    emotion: str | None = None
    emotion_at: Literal["event", "expression"] | None = None
    emotion_source: Literal["explicit"] | None = None

    @field_validator("event_time", "valid_from", "valid_to")
    @classmethod
    def validate_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?", value):
            raise ValueError("事实日期须为 YYYY、YYYY-MM 或 YYYY-MM-DD")
        parts = [int(part) for part in value.split("-")]
        try:
            date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
        except ValueError as exc:
            raise ValueError("事实日期不存在") from exc
        return value

    @field_validator("time_text", "location", "emotion")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("上下文文本不能为空")
        return value

    @model_validator(mode="after")
    def validate_context(self) -> MemoryContext:
        if (self.location is None) != (self.location_kind is None):
            raise ValueError("地点与地点类型须同时提供")
        if self.emotion is None:
            if self.emotion_at is not None or self.emotion_source is not None:
                raise ValueError("情绪标注缺少情绪原文")
        elif self.emotion_source != "explicit":
            raise ValueError("第一版仅保存用户明确表达的情绪")
        elif self.emotion_at is None:
            # 无法区分时，只记录当前表达，不能倒推为事件当时的情绪。
            self.emotion_at = "expression"
        if self.valid_from and self.valid_to:
            start = date.fromisoformat(self.valid_from + "-01" * (3 - len(self.valid_from.split("-"))))
            end_parts = [int(part) for part in self.valid_to.split("-")]
            year = end_parts[0]
            month = end_parts[1] if len(end_parts) > 1 else 12
            day = end_parts[2] if len(end_parts) > 2 else calendar.monthrange(year, month)[1]
            if start > date(year, month, day):
                raise ValueError("事实生效结束时间不能早于开始时间")
        return self


class Candidate(BaseModel):
    text: str
    topic: str = "uncategorized"
    title: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    sensitive: bool = False
    kind: Literal["add", "update", "conflict"] = "add"
    reason: str = ""
    context: MemoryContext | None = None


class ExtractionResult(BaseModel):
    memories: list[Candidate] = Field(default_factory=list)


InferenceKind = Literal["profile", "emotion"]
InferenceStatus = Literal["hypothesis", "confirmed", "rejected", "superseded"]


class Inference(BaseModel):
    """独立假设记录；不属于事实条目，也不能作为另一推断的事实证据。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    kind: InferenceKind
    source_ids: list[str] = Field(min_length=1)
    created_at: str
    updated_at: str
    status: InferenceStatus = "hypothesis"

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("推断来源 ID 不能为空")
        return sorted({value.strip() for value in values})


class PendingItem(BaseModel):
    id: str
    created_at: str
    source: str
    text: str
    topic: str
    title: str | None = None
    confidence: float
    sensitive: bool = False
    kind: str = "add"
    reason: str = ""
    session_id: str | None = None
    message_id: str | None = None
    conflict_with: str | None = None
    context: MemoryContext | None = None

    @classmethod
    def from_candidate(
        cls,
        candidate: Candidate,
        source: str,
        reason: str,
        session_id: str | None = None,
        message_id: str | None = None,
        conflict_with: str | None = None,
    ) -> "PendingItem":
        stamp = datetime.now()
        return cls(
            id=f"p-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}",
            created_at=stamp.isoformat(timespec="seconds"),
            source=source,
            text=candidate.text,
            topic=candidate.topic,
            title=candidate.title,
            confidence=candidate.confidence,
            sensitive=candidate.sensitive,
            kind=candidate.kind,
            reason=reason,
            session_id=session_id,
            message_id=message_id,
            conflict_with=conflict_with,
            context=candidate.context,
        )


class Entry(BaseModel):
    id: str
    topic: str
    text: str
    timestamp: str
    status: str = "active"
    supersedes: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def context(self) -> MemoryContext | None:
        raw = self.metadata.get("context")
        return MemoryContext.model_validate_json(raw) if raw else None


class TopicInfo(BaseModel):
    topic: str
    title: str
    entries: int
    updated: str
