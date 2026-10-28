from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

MAX_SEARCH_ENGINES = 3


def _fake_ip_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """与 open-websearch 同名的 FAKE_IP_CIDRS：代理的合成 DNS 结果不算私网目标。"""
    networks = []
    for item in os.environ.get("FAKE_IP_CIDRS", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _is_blocked_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowed: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_global:
        return False
    return not any(address in network for network in allowed)


def assert_public_fetch_url(url: str, label: str = "抓取地址") -> None:
    """抓取入口只接受公网 http(s) 地址，作为上游 daemon 校验之外的纵深防御。"""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"{label}必须是 http 或 https 链接")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError(f"{label}缺少主机名")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"{label}不能指向本机地址")
    allowed = _fake_ip_networks()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(literal, allowed):
            raise ValueError(f"{label}不能指向私网或本机地址")
        return
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except ValueError:
        raise ValueError(f"{label}端口不合法") from None
    except OSError as exc:
        raise ValueError(f"{label}无法解析主机名：{host}") from exc
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if _is_blocked_address(address, allowed):
            raise ValueError(f"{label}解析到非公网地址，已拒绝抓取")


def normalize_search_engines(value: object, allowed: Sequence[str]) -> tuple[str, ...]:
    """把单个或一组引擎名规范化；auto 和空值表示使用配置默认。"""
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("search_engine entries must be strings")
        values = list(value)
    else:
        raise ValueError("search_engine must be a string or list of strings")

    selected: list[str] = []
    allowed_set = set(allowed)
    for item in values:
        name = item.strip().lower()
        if not name or name == "auto":
            continue
        if name not in allowed_set:
            raise ValueError(f"unsupported search engine {name}")
        if name not in selected:
            selected.append(name)
    if len(selected) > MAX_SEARCH_ENGINES:
        raise ValueError(f"at most {MAX_SEARCH_ENGINES} search engines")
    return tuple(selected)


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
    # 指定上游搜索引擎（bing/baidu/sogou...）；空表示用 provider 的默认配置。
    engines: tuple[str, ...] = ()


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
