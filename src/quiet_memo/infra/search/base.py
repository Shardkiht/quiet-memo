from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

MAX_SEARCH_ENGINES = 3
# 常见 TUN 代理的合成 DNS 段，仅用于域名解析结果，不放行直接 IP 目标。
DEFAULT_FAKE_IP_CIDRS = "198.18.0.0/15,2001:2::/48"


def _fake_ip_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """与 open-websearch 同名的 FAKE_IP_CIDRS：代理的合成 DNS 结果不算私网目标。"""
    networks = []
    for item in os.environ.get("FAKE_IP_CIDRS", DEFAULT_FAKE_IP_CIDRS).split(","):
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
        if _is_blocked_address(literal, ()):
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
        # Some providers serialize an array-valued tool argument as a JSON string.
        if value.strip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("search_engine must be a valid JSON array or engine name") from None
            if not isinstance(value, list):
                raise ValueError("search_engine must be a list of strings")
            return normalize_search_engines(value, allowed)
        values = [value]
    elif isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("search_engine entries must be strings")
        values = []
        for item in value:
            # 双重序列化：有的 provider 会把数组元素本身也编码成 JSON 字符串
            # （如 ["[\"baidu\"]"]），解析还原而不是当成引擎名报错。
            stripped = item.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    raise ValueError(
                        "search_engine must be a valid JSON array or engine name"
                    ) from None
                if not isinstance(parsed, list) or not all(
                    isinstance(inner, str) for inner in parsed
                ):
                    raise ValueError("search_engine must be a list of strings")
                values.extend(parsed)
            else:
                values.append(item)
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
    # 鉴权失效/权限不足（401/403）与配额/余额限制（402、额度用尽）；
    # 不做快速重试，也不得把这类失败缓存成空结果。
    AUTH = "auth"
    QUOTA = "quota"
    # 本地单次搜索的请求次数预算耗尽：与厂商账户配额（quota）是两回事。
    BUDGET_EXHAUSTED = "budget_exhausted"


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
    # 会话上下文里的补充关键词（人物全名、项目、产品），用于短名称消歧；
    # 只参与派生查询变体和相关性判断，不改变原始 query 的语义。
    context_terms: tuple[str, ...] = ()
    # 单次请求超时上限（秒）；由 router 按剩余预算下发，None 表示用 provider 默认。
    timeout: float | None = None


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published: str | None = None
    provider: str = ""
    # 结果实际来自哪个上游搜索引擎（bing/baidu/...）；provider 字段始终是 QM 的
    # SearchProvider 名，二者不能混用。
    engine: str = ""


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


def truncate_fetch_content(content: str, max_chars: int) -> tuple[str, bool]:
    """正文按 max_chars 截断；返回（截断后文本, 是否发生截断）。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


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
                    "engine": result.engine,
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
