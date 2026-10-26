from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class ProviderErrorKind(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    BAD_RESPONSE = "bad_response"
    UNAVAILABLE = "unavailable"


class ProviderError(Exception):
    def __init__(self, provider: str, kind: ProviderErrorKind | str, detail: str = ""):
        self.provider = provider
        self.kind = kind if isinstance(kind, ProviderErrorKind) else ProviderErrorKind(kind)
        self.detail = detail
        super().__init__(f"{provider}: {self.kind.value}: {detail}".rstrip(": "))

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "kind": self.kind.value, "detail": self.detail}


@dataclass(slots=True)
class SearchQuery:
    query: str
    limit: int = 5
    engine: str = "auto"
    lang: str | None = None
    freshness: str | None = None


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published: str | None = None
    provider: str = ""


@dataclass(slots=True)
class FetchQuery:
    url: str
    max_chars: int = 4000


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    title: str
    content: str
    truncated: bool = False
    site_name: str = ""


@dataclass(slots=True)
class SearchFailure:
    provider: str
    kind: str
    detail: str = ""

    @classmethod
    def from_error(cls, error: ProviderError) -> "SearchFailure":
        return cls(provider=error.provider, kind=error.kind.value, detail=error.detail)

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "kind": self.kind, "detail": self.detail}


@dataclass(slots=True)
class SearchOutcome:
    results: list[SearchResult] = field(default_factory=list)
    provider: str = ""
    failed: list[SearchFailure] = field(default_factory=list)
    hint: str = ""
    cached: bool = False

    def to_payload(self) -> dict:
        payload: dict = {
            "results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "published": result.published,
                    "provider": result.provider,
                }
                for result in self.results
            ],
            "provider": self.provider,
            "failed": [failure.to_dict() for failure in self.failed],
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


class SearchProvider(Protocol):
    name: str
    capabilities: set[str]

    def search(self, query: SearchQuery) -> list[SearchResult]: ...
