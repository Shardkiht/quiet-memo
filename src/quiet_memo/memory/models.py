from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    text: str
    topic: str = "uncategorized"
    title: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    sensitive: bool = False
    kind: Literal["add", "update", "conflict"] = "add"
    reason: str = ""


class ExtractionResult(BaseModel):
    memories: list[Candidate] = Field(default_factory=list)


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
        )


class Entry(BaseModel):
    id: str
    topic: str
    text: str
    timestamp: str
    status: str = "active"
    supersedes: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class TopicInfo(BaseModel):
    topic: str
    title: str
    entries: int
    updated: str
