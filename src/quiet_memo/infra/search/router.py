from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import SearchConfig
from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    SearchFailure,
    SearchOutcome,
    SearchProvider,
    SearchQuery,
    SearchResult,
)
from .cache import SearchCache
from .open_websearch import OpenWebSearchProvider

DEFAULT_PROVIDER = "open_websearch"
KNOWN_PROVIDERS = {DEFAULT_PROVIDER}


def build_registry(config: SearchConfig) -> dict[str, SearchProvider]:
    registry: dict[str, SearchProvider] = {}
    for name in config.providers:
        if name == DEFAULT_PROVIDER:
            registry[name] = OpenWebSearchProvider(
                base_url=config.base_url,
                timeout=config.timeout,
                transport=config.transport,
            )
    return registry


def build_search_router(
    config: SearchConfig,
    vault: Path | None = None,
) -> "SearchRouter":
    cache = None
    if config.cache_ttl > 0 and vault is not None:
        cache = SearchCache(vault / ".quiet-memo" / "search-cache.json", config.cache_ttl)
    return SearchRouter(
        build_registry(config),
        order=config.providers,
        max_results=config.max_results,
        cache=cache,
    )


class SearchRouter:
    """按配置顺序路由；失败时降级，结果统一在这里归一化和截断。"""

    def __init__(
        self,
        registry: dict[str, SearchProvider],
        *,
        order: list[str] | None = None,
        max_results: int = 5,
        cache: SearchCache | None = None,
    ):
        self.registry = registry
        self.order = list(dict.fromkeys(order or list(registry)))
        self.max_results = max(1, max_results)
        self.cache = cache

    @property
    def supports_fetch(self) -> bool:
        return any(
            "fetch" in provider.capabilities for provider in self.registry.values()
        )

    def search(self, query: SearchQuery) -> SearchOutcome:
        text = query.query.strip()
        if not text:
            raise ValueError("搜索关键词为空")
        limit = max(1, min(query.limit or self.max_results, self.max_results))
        engine = (query.engine or "auto").strip()
        names = [engine] if engine != "auto" else self.order
        cache_key = self._cache_key(text, limit, engine, query.lang, query.freshness, names)
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                results, provider = cached
                return SearchOutcome(
                    results=results,
                    provider=provider,
                    cached=True,
                    hint="" if results else "未找到相关结果。",
                )

        failures: list[SearchFailure] = []
        for name in names:
            provider = self.registry.get(name)
            if provider is None:
                failures.append(SearchFailure(name, "unavailable", "未注册的搜索 provider"))
                continue
            try:
                raw = provider.search(
                    SearchQuery(
                        query=text,
                        limit=limit,
                        engine=engine,
                        lang=query.lang,
                        freshness=query.freshness,
                    )
                )
            except ProviderError as exc:
                failures.append(SearchFailure.from_error(exc))
                continue
            except Exception as exc:
                failures.append(SearchFailure(name, "unavailable", str(exc)))
                continue

            results = normalize_results(raw, limit)
            if self.cache is not None:
                self.cache.set(cache_key, results, name)
            return SearchOutcome(
                results=results,
                provider=name,
                failed=failures,
                hint="" if results else "未找到相关结果。",
            )

        hint = "所有搜索 provider 均失败。"
        if any("open-websearch" in failure.detail or "daemon" in failure.detail for failure in failures):
            hint += " 请确认 open-websearch 已启动：npx open-websearch@latest serve"
        return SearchOutcome(failed=failures, hint=hint)

    def fetch(self, url: str, max_chars: int = 4000) -> FetchResult:
        """抓取搜索结果正文；按 provider 顺序尝试支持 fetch 的实现。"""
        text = url.strip()
        if not text:
            raise ValueError("抓取地址为空")
        failures: list[SearchFailure] = []
        for name in self.order:
            provider = self.registry.get(name)
            if provider is None or "fetch" not in provider.capabilities:
                continue
            try:
                return provider.fetch(FetchQuery(text, max_chars))
            except ProviderError as exc:
                failures.append(SearchFailure.from_error(exc))
            except Exception as exc:
                failures.append(SearchFailure(name, "unavailable", str(exc)))
        if failures:
            detail = "；".join(f"{item.provider}: {item.detail}" for item in failures)
            raise ProviderError("search", "unavailable", detail)
        raise ProviderError("search", "unavailable", "没有支持网页抓取的 provider")

    def _cache_key(
        self,
        text: str,
        limit: int,
        engine: str,
        lang: str | None,
        freshness: str | None,
        names: list[str],
    ) -> str:
        raw = "\x1f".join(
            [text, str(limit), engine, lang or "", freshness or "", *names]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rstrip("/")
    if not parts.scheme or not parts.netloc:
        return url.rstrip("/")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ],
        doseq=True,
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            query,
            parts.fragment,
        )
    )


def normalize_results(results: list[SearchResult], limit: int) -> list[SearchResult]:
    seen: set[str] = set()
    normalized: list[SearchResult] = []
    for result in results:
        url = normalize_url(result.url)
        key = url or f"{result.title}\x00{result.snippet}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            SearchResult(
                title=result.title,
                url=url,
                snippet=result.snippet,
                published=result.published,
                provider=result.provider,
            )
        )
        if len(normalized) >= limit:
            break
    return normalized
