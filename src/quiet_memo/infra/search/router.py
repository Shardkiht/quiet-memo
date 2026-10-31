from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
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
    assert_public_fetch_url,
)
from .cache import SearchCache
from .open_websearch import OpenWebSearchProvider
from .quality import QueryAnalysis, analyze_query, is_relevant, result_relevance

DEFAULT_PROVIDER = "open_websearch"
KNOWN_PROVIDERS = {DEFAULT_PROVIDER}

# 空结果与时效性查询的缓存短命 TTL；成功结果沿用配置的 cache_ttl。
EMPTY_RESULT_TTL = 300.0
FRESHNESS_TTL = 600.0


def query_variants(text: str, context_terms: Sequence[str] = ()) -> list[str]:
    """用会话上下文派生查询变体（确定性规则，不调用 LLM）。

    短人物名、英文账号、产品代号单独搜索常无有效结果；把上下文里的
    专有名词逐个追加，最多两个变体。原始拼写原样保留。
    """
    variants: list[str] = []
    lowered = text.lower()
    for term in context_terms:
        term = str(term).strip()
        if not term or term.lower() in lowered:
            continue
        variant = f"{text} {term}"
        if variant not in variants:
            variants.append(variant)
        if len(variants) == 2:
            break
    return variants


def merge_results(
    candidates: list[SearchResult], analysis: QueryAnalysis, limit: int
) -> list[SearchResult]:
    """多引擎候选合并：URL 去重，按相关性分排序后截断；同分保持原顺序。"""
    seen: set[str] = set()
    merged: list[SearchResult] = []
    for result in candidates:
        url = normalize_url(result.url)
        key = url or f"{result.title}\x00{result.snippet}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            SearchResult(
                title=result.title,
                url=url,
                snippet=result.snippet,
                published=result.published,
                provider=result.provider,
                engine=result.engine,
            )
        )
    if analysis.core_terms:
        merged.sort(key=lambda item: -result_relevance(item, analysis))
    return merged[:limit]


def build_registry(config: SearchConfig) -> dict[str, SearchProvider]:
    registry: dict[str, SearchProvider] = {}
    for name in config.providers:
        if name == DEFAULT_PROVIDER:
            registry[name] = OpenWebSearchProvider(
                base_url=config.base_url,
                timeout=config.timeout,
                transport=config.transport,
                engines=config.engines,
                fetch_timeout=config.fetch_timeout,
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
        engines=config.engines,
        fallback_engines=config.fallback_engines,
        max_engines=config.max_engines,
        timeout_budget=config.timeout_budget,
    )


class SearchRouter:
    """按配置顺序路由；失败时降级，结果统一在这里归一化和截断。

    上游引擎回退（bing 空结果/失败 → baidu → duckduckgo...）只对支持多引擎的
    provider 生效；QM 的 SearchProvider 与上游搜索引擎是两层概念，不混用。
    """

    def __init__(
        self,
        registry: dict[str, SearchProvider],
        *,
        order: list[str] | None = None,
        max_results: int = 5,
        cache: SearchCache | None = None,
        engines: list[str] | tuple[str, ...] | None = None,
        fallback_engines: Sequence[str] | None = ("baidu", "duckduckgo", "sogou"),
        max_engines: int = 3,
        timeout_budget: float = 25.0,
    ):
        self.registry = registry
        self.order = list(dict.fromkeys(order or list(registry)))
        self.max_results = max(1, max_results)
        self.cache = cache
        self.engines = tuple(engines or ())
        self.fallback_engines = tuple(fallback_engines or ())
        self.max_engines = max(1, max_engines)
        self.timeout_budget = timeout_budget

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
        # 缓存必须区分实际使用的上游引擎，否则改配置后会命中旧引擎的结果。
        # v5：context 影响变体与相关性判断、auto/explicit 决定回退策略、
        # 回退配置改变尝试序列——都要进键，与旧策略缓存隔离。
        engines = tuple(query.engines) or self.engines
        analysis = analyze_query(text, query.context_terms)
        cache_key = self._cache_key(
            text, limit, engine, query.lang, query.freshness, names, engines, query
        )
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
        # 查询变体只用于第一个引擎槽位：上下文消歧针对的是原始查询，不值得
        # 为每个候补引擎都放大请求数。显式指定引擎时保持单次请求语义。
        slot_queries = [text]
        if not query.engines:
            slot_queries += query_variants(text, query.context_terms)

        started = time.perf_counter()  # Windows monotonic 分辨率粗，预算计时用高精度时钟
        for name in names:
            provider = self.registry.get(name)
            if provider is None:
                failures.append(SearchFailure(name, "unavailable", "未注册的搜索 provider"))
                continue
            outcome = self._search_one_provider(
                provider, name, query, text, limit, engine, engines,
                slot_queries, analysis, cache_key, failures, started,
            )
            if outcome is not None:
                return outcome
        return self._final_outcome(failures, [], analysis, limit, cache_key, text, name_hint=names[-1] if names else "")

    def _search_one_provider(
        self,
        provider: SearchProvider,
        name: str,
        query: SearchQuery,
        text: str,
        limit: int,
        engine: str,
        engines: tuple[str, ...],
        slot_queries: list[str],
        analysis: QueryAnalysis,
        cache_key: str,
        failures: list[SearchFailure],
        started: float,
    ) -> SearchOutcome | None:
        """单 provider 的引擎/变体尝试计划；产出结果时返回 outcome，否则 None。

        引擎回退只对声明 engines 能力的 provider 生效；其余 provider 单次尝试，
        失败交给上层换下一个 provider。
        """
        if query.engines:
            slots: tuple[tuple[str, ...], ...] = (query.engines,)
        elif "engines" in provider.capabilities:
            slots = self._engine_slots(engines)
        else:
            slots = ((),)

        candidates: list[SearchResult] = []
        for slot_index, slot_engines in enumerate(slots):
            queries = slot_queries if slot_index == 0 else [text]
            for attempt_query in queries:
                if time.perf_counter() - started > self.timeout_budget:
                    failures.append(
                        SearchFailure(name, "timeout", "搜索超时预算已用尽，停止引擎回退")
                    )
                    if candidates:
                        return self._final_outcome(
                            failures, candidates, analysis, limit, cache_key, text, name_hint=name
                        )
                    return None
                try:
                    raw = provider.search(
                        SearchQuery(
                            query=attempt_query,
                            limit=limit,
                            engine=engine,
                            lang=query.lang,
                            freshness=query.freshness,
                            engines=slot_engines,
                        )
                    )
                except ProviderError as exc:
                    failures.append(SearchFailure.from_error(exc))
                    # 请求级失败（超时/网络/限流）：换下一个引擎，不再变体。
                    break
                except Exception as exc:
                    failures.append(SearchFailure(name, "unavailable", str(exc)))
                    break

                results = normalize_results(raw, limit)
                for result in results:
                    result.engine = slot_engines[0] if slot_engines else ""
                if not results:
                    continue
                if not analysis.core_terms or is_relevant(results, analysis):
                    # 缓存与返回必须是同一份最终结果：先合并排序截断再入缓存，
                    # 否则缓存命中后顺序与首查不一致。
                    merged = merge_results(results, analysis, limit)
                    self._cache_put(cache_key, merged, name, analysis)
                    return SearchOutcome(
                        results=merged,
                        provider=name,
                        failed=failures,
                        hint="",
                    )
                # 有结果但与核心词零命中：保留为候选（保守，不丢弃），继续候补。
                candidates.extend(results)

        if candidates:
            return self._final_outcome(
                failures, candidates, analysis, limit, cache_key, text, name_hint=name
            )
        return None

    def _final_outcome(
        self,
        failures: list[SearchFailure],
        candidates: list[SearchResult],
        analysis: QueryAnalysis,
        limit: int,
        cache_key: str,
        text: str,
        name_hint: str = "",
    ) -> SearchOutcome:
        if candidates:
            # 候补都没命中核心词时返回已有候选并如实提示，不伪装成精准命中；
            # 候选结果用短 TTL 缓存，给之后的回退留机会。
            merged = merge_results(candidates, analysis, limit)
            if self.cache is not None:
                self.cache.set(cache_key, merged, name_hint, ttl=FRESHNESS_TTL)
            return SearchOutcome(
                results=merged,
                provider=name_hint,
                failed=failures,
                hint="候补引擎未找到与查询词直接相关的结果，以下为各引擎返回的候选。",
            )
        if self.cache is not None and not failures:
            # 真正的零命中（没有任何上游失败）才短 TTL 缓存；失败导致的空
            # 永不入缓存，否则验证码/限流期间会一直拿到空。
            self.cache.set(cache_key, [], name_hint, ttl=EMPTY_RESULT_TTL)
        if failures and all(
            failure.kind in ("timeout", "network", "rate_limit", "unavailable", "bad_response")
            for failure in failures
        ):
            hint = "所有搜索引擎均失败。"
        else:
            hint = "未找到相关结果。"
        if any("open-websearch" in failure.detail or "daemon" in failure.detail for failure in failures):
            hint += " 请确认 open-websearch 已启动：npx open-websearch@latest serve"
        return SearchOutcome(failed=failures, hint=hint)

    def _cache_put(
        self,
        cache_key: str,
        results: list[SearchResult],
        name: str,
        analysis: QueryAnalysis,
        short: bool = False,
    ) -> None:
        if self.cache is None or not results:
            return
        # 时效性查询（今天/最新/latest...）成功结果也只短缓存。
        if short or analysis.freshness_requested:
            self.cache.set(cache_key, results, name, ttl=FRESHNESS_TTL)
        else:
            self.cache.set(cache_key, results, name)

    def _exhausted(self, failures: list[SearchFailure]) -> SearchOutcome:
        if failures and all(
            failure.kind in ("timeout", "network", "rate_limit", "unavailable", "bad_response")
            for failure in failures
        ) and not any("未注册" in failure.detail or "预算" in failure.detail for failure in failures):
            hint = "所有搜索引擎均失败。"
        else:
            hint = "未找到相关结果。"
        if any("open-websearch" in failure.detail or "daemon" in failure.detail for failure in failures):
            hint += " 请确认 open-websearch 已启动：npx open-websearch@latest serve"
        return SearchOutcome(failed=failures, hint=hint)

    def _engine_slots(
        self, default_engines: tuple[str, ...]
    ) -> tuple[tuple[str, ...], ...]:
        """自动搜索的引擎尝试序列；显式指定引擎时不进入本方法（保持用户语义）。"""
        if default_engines:
            ordered = list(default_engines)
            ordered += [item for item in self.fallback_engines if item not in ordered]
            slots = [(item,) for item in ordered[: self.max_engines]]
        else:
            # 未配置首选引擎：首个请求交给 daemon 默认引擎，再按候补序列降级。
            slots = [()] + [
                (item,) for item in self.fallback_engines[: max(0, self.max_engines - 1)]
            ]
        if not slots:
            slots = [()]
        return tuple(slots)

    def fetch(self, url: str, max_chars: int = 4000) -> FetchResult:
        """抓取搜索结果正文；按 provider 顺序尝试支持 fetch 的实现。"""
        text = url.strip()
        if not text:
            raise ValueError("抓取地址为空")
        assert_public_fetch_url(text)
        failures: list[SearchFailure] = []
        for name in self.order:
            provider = self.registry.get(name)
            if provider is None or "fetch" not in provider.capabilities:
                continue
            try:
                result = provider.fetch(FetchQuery(text, max_chars))
            except ProviderError as exc:
                failures.append(SearchFailure.from_error(exc))
                continue
            except Exception as exc:
                failures.append(SearchFailure(name, "unavailable", str(exc)))
                continue
            if result.final_url and result.final_url != text:
                # 上游 daemon 已逐跳校验；这里对最终地址再拦一次私网回落。
                assert_public_fetch_url(result.final_url, "重定向后的抓取地址")
            return result
        if failures:
            detail = "；".join(f"{item.provider}: {item.detail}" for item in failures)
            # 这里失败的是抓取而不是搜索；provider 标明 fetch，避免上层误判成搜索故障。
            raise ProviderError("fetch", "unavailable", detail)
        raise ProviderError("fetch", "unavailable", "没有支持网页抓取的 provider")

    def _cache_key(
        self,
        text: str,
        limit: int,
        engine: str,
        lang: str | None,
        freshness: str | None,
        names: list[str],
        engines: tuple[str, ...],
        query: SearchQuery,
    ) -> str:
        # v5：context（保留顺序，影响变体生成）、auto/explicit 策略、回退
        # 引擎序列与预算都参与键；策略或配置一变即视为新查询。
        strategy = "explicit" if query.engines else "auto"
        context_sig = "\x1e".join(
            term.strip() for term in query.context_terms if term.strip()
        )
        raw = "\x1f".join(
            [
                "search-v5",
                text,
                str(limit),
                engine,
                lang or "",
                freshness or "",
                strategy,
                context_sig,
                str(self.max_engines),
                ",".join(self.fallback_engines),
                f"{self.timeout_budget:g}",
                *engines,
                *names,
            ]
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
