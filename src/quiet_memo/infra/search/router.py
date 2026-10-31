from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
import dataclasses
from dataclasses import dataclass
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
from .quality import (
    QueryAnalysis,
    ScoreBreakdown,
    analyze_query,
    has_core_hit,
    quality_gate,
    score_entry,
)

DEFAULT_PROVIDER = "open_websearch"
KNOWN_PROVIDERS = {DEFAULT_PROVIDER}

# 需要厂商 API Key 的外部搜索服务；列进 providers 且 Key 可读才会注册。
_EXTERNAL_PROVIDERS = {
    "baidu_search": ("baidu.py:BaiduSearchProvider", "BAIDU_SEARCH_API_KEY"),
    "tavily": ("tavily.py:TavilySearchProvider", "TAVILY_API_KEY"),
    "exa": ("exa.py:ExaSearchProvider", "EXA_API_KEY"),
    "you": ("you.py:YouSearchProvider", "YDC_API_KEY"),
    "volcengine": ("volcengine.py:VolcengineSearchProvider", "VOLCENGINE_SEARCH_API_KEY"),
}


def _build_external_provider(name: str, api_key: str, timeout: float) -> SearchProvider:
    # 延迟导入：未配置 Key 时不必加载这些模块。
    if name == "baidu_search":
        from .baidu import BaiduSearchProvider

        return BaiduSearchProvider(api_key, timeout=timeout)
    if name == "tavily":
        from .tavily import TavilySearchProvider

        return TavilySearchProvider(api_key, timeout=timeout)
    if name == "exa":
        from .exa import ExaSearchProvider

        return ExaSearchProvider(api_key, timeout=timeout)
    if name == "you":
        from .you import YouSearchProvider

        return YouSearchProvider(api_key, timeout=timeout)
    if name == "volcengine":
        from .volcengine import VolcengineSearchProvider

        return VolcengineSearchProvider(api_key, timeout=timeout)
    raise ValueError(f"未知的外部搜索 provider: {name}")

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


@dataclass(slots=True)
class PoolEntry:
    result: SearchResult
    # (provider, engine, rank_in_engine) 证据列表：同一页面被多个来源返回时
    # 全部保留；provider 与 engine 是两层身份，交叉统计按 (provider, engine) 计。
    ranks: list[tuple[str, str, int]]
    # 最近一次 ranked 计算的逐特征贡献，供解释与调试。
    score: ScoreBreakdown = dataclasses.field(default_factory=ScoreBreakdown)


class CandidatePool:
    """跨引擎/变体的统一候选池：URL 去重合并，保留各路排名证据。"""

    def __init__(self) -> None:
        self._entries: dict[str, PoolEntry] = {}
        self._order: list[str] = []

    @property
    def size(self) -> int:
        return len(self._order)

    def add(self, results: list[SearchResult]) -> None:
        for rank, result in enumerate(results, 1):
            url = normalize_url(result.url)
            key = url or f"{result.title}\x00{result.snippet}"
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = PoolEntry(
                    result, [(result.provider, result.engine, rank)]
                )
                self._order.append(key)
                continue
            entry.ranks.append((result.provider, result.engine, rank))
            # 同一页面多来源合并时保留更完整的标题/摘要。
            if len(result.title) > len(entry.result.title):
                entry.result.title = result.title
            if len(result.snippet) > len(entry.result.snippet):
                entry.result.snippet = result.snippet

    def ranked_entries(self, analysis: QueryAnalysis) -> list[PoolEntry]:
        """确定性多特征排序；平分保持首次入池顺序（稳定可复现）。"""
        entries = [self._entries[key] for key in self._order]
        for entry in entries:
            entry.score = score_entry(entry.result, analysis, entry.ranks)
        if analysis.core_terms:
            entries.sort(key=lambda entry: -entry.score.total)
        return entries

    def ranked(self, analysis: QueryAnalysis) -> list[SearchResult]:
        return [entry.result for entry in self.ranked_entries(analysis)]


def weak_share_hint(merged: list[SearchResult], analysis: QueryAnalysis) -> str:
    """输出中多数条目与核心词无关时如实提示，避免整批被当作相关结果。

    上游单字切词（如 bing 把「智谱」拆成「智」）会让候选池混入大量噪音；
    少数弱候选是常态，不提示，过半才提示。
    """
    if len(merged) < 2:
        return ""
    weak = sum(1 for result in merged if not has_core_hit(result, analysis))
    if weak * 2 > len(merged):
        return "部分结果与查询核心词无关，仅供参考。"
    return ""


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
        elif name in _EXTERNAL_PROVIDERS:
            env_name = _EXTERNAL_PROVIDERS[name][1]
            api_key = os.environ.get(env_name, "").strip()
            if not api_key:
                # 未配置 Key 的厂商直接跳过：不参与尝试，也不报错。
                continue
            registry[name] = _build_external_provider(name, api_key, config.timeout)
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
        max_requests: int = 9,
    ):
        self.registry = registry
        self.order = list(dict.fromkeys(order or list(registry)))
        self.max_results = max(1, max_results)
        self.cache = cache
        self.engines = tuple(engines or ())
        self.fallback_engines = tuple(fallback_engines or ())
        self.max_engines = max(1, max_engines)
        self.timeout_budget = timeout_budget
        # 单次 search() 的跨 Provider 请求次数上限：默认 9 恰好容纳
        # 单 Provider 的最坏情况（3 个引擎槽位 × 原始查询 + 2 个变体）。
        self.max_requests = max(1, max_requests)

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
        # v7：context 影响变体与相关性判断、auto/explicit 决定回退策略、
        # 回退配置改变尝试序列——都要进键，与旧策略缓存隔离。
        engines = tuple(query.engines) or self.engines
        analysis = analyze_query(text, query.context_terms)
        # 向上游多拉候选（展示条数的 3 倍，10~30），重排后仍只输出 limit；
        # 不能为了默认展示 5 条就只向上游要 5 条。
        fetch_limit = min(30, max(10, limit * 3))
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
        pool = CandidatePool()
        # 查询变体只用于第一个引擎槽位：上下文消歧针对的是原始查询，不值得
        # 为每个候补引擎都放大请求数。显式指定引擎时保持单次请求语义。
        slot_queries = [text]
        if not query.engines:
            slot_queries += query_variants(text, query.context_terms)
        # 变体只在第一家实际尝试的 provider 上执行：后续 provider 只跑原始
        # 查询，避免每家重复放大同一组变体请求。
        variants_used = False
        budget = {"requests": 0}

        started = time.perf_counter()  # Windows monotonic 分辨率粗，预算计时用高精度时钟
        for name in names:
            provider = self.registry.get(name)
            if provider is None:
                failures.append(SearchFailure(name, "unavailable", "未注册的搜索 provider"))
                continue
            if query.engines or variants_used:
                provider_queries = [text]
            else:
                provider_queries = slot_queries
                variants_used = True
            outcome = self._search_one_provider(
                provider, name, query, text, limit, fetch_limit, engine, engines,
                provider_queries, analysis, pool, cache_key, failures, started, budget,
            )
            if outcome is not None:
                return outcome
        return self._final_outcome(
            failures, pool, analysis, limit, cache_key,
            name_hint=names[-1] if names else "",
        )

    def _search_one_provider(
        self,
        provider: SearchProvider,
        name: str,
        query: SearchQuery,
        text: str,
        limit: int,
        fetch_limit: int,
        engine: str,
        engines: tuple[str, ...],
        slot_queries: list[str],
        analysis: QueryAnalysis,
        pool: CandidatePool,
        cache_key: str,
        failures: list[SearchFailure],
        started: float,
        budget: dict[str, int],
    ) -> SearchOutcome | None:
        """渐进式尝试：原始查询优先，逐槽位补候补；候选池质量够好才早停。

        引擎回退只对声明 engines 能力的 provider 生效；其余 provider 单次尝试，
        失败交给上层换下一个 provider。请求次数预算跨 Provider 共享。
        """
        if query.engines:
            slots: tuple[tuple[str, ...], ...] = (query.engines,)
        elif "engines" in provider.capabilities:
            slots = self._engine_slots(engines)
        else:
            slots = ((),)

        for slot_index, slot_engines in enumerate(slots):
            queries = slot_queries if slot_index == 0 else [text]
            for attempt_query in queries:
                # 单次请求的超时上限取剩余预算，避免默认 10s 越过总预算。
                remaining = self.timeout_budget - (time.perf_counter() - started)
                if remaining <= 0:
                    failures.append(
                        SearchFailure(name, "timeout", "搜索超时预算已用尽，停止引擎回退")
                    )
                    return self._final_outcome(
                        failures, pool, analysis, limit, cache_key, name_hint=name
                    )
                if budget["requests"] >= self.max_requests:
                    failures.append(
                        SearchFailure(name, "quota", "单次搜索请求次数预算已用尽")
                    )
                    return self._final_outcome(
                        failures, pool, analysis, limit, cache_key,
                        name_hint=name, budget_exhausted=True,
                    )
                budget["requests"] += 1
                try:
                    raw = provider.search(
                        SearchQuery(
                            query=attempt_query,
                            limit=fetch_limit,
                            engine=engine,
                            lang=query.lang,
                            freshness=query.freshness,
                            engines=slot_engines,
                            timeout=round(max(remaining, 0.05), 3),
                        )
                    )
                except ProviderError as exc:
                    failures.append(SearchFailure.from_error(exc))
                    # 请求级失败（超时/网络/限流）：换下一个引擎，不再变体。
                    break
                except Exception as exc:
                    failures.append(SearchFailure(name, "unavailable", str(exc)))
                    break

                # daemon 报告的部分引擎失败转成结构化诊断，不静默吞掉。
                failures.extend(getattr(provider, "last_partial_failures", ()) or ())
                results = normalize_results(raw)
                for result in results:
                    # daemon 已逐条标注真实引擎时保留；缺失才补槽位引擎。
                    if not result.engine and slot_engines:
                        result.engine = slot_engines[0]
                if not results:
                    continue
                pool.add(results)
                decision = quality_gate(pool.ranked(analysis), analysis)
                if decision.should_stop:
                    # 缓存与返回必须是同一份最终结果：先排序截断再入缓存，
                    # 否则缓存命中后顺序与首查不一致。
                    merged = pool.ranked(analysis)[:limit]
                    self._cache_put(cache_key, merged, name, analysis)
                    return SearchOutcome(
                        results=merged,
                        provider=name,
                        failed=failures,
                        hint=weak_share_hint(merged, analysis),
                    )
                # 质量不足：保留候选继续候补引擎/变体/下一个 provider，
                # 由 search() 在全部遍历或预算耗尽后统一收尾。
        return None

    def _final_outcome(
        self,
        failures: list[SearchFailure],
        pool: CandidatePool,
        analysis: QueryAnalysis,
        limit: int,
        cache_key: str,
        name_hint: str = "",
        budget_exhausted: bool = False,
    ) -> SearchOutcome:
        merged = pool.ranked(analysis)[:limit]
        # 预算耗尽时如实告知是截断收尾，不能伪装成正常完成。
        budget_hint = (
            "已达到单次搜索的请求次数上限，以下为当前最佳候选。" if budget_exhausted else ""
        )
        if merged and any(has_core_hit(result, analysis) for result in merged):
            # 池里至少有核心词命中：按成功语义缓存（时效查询仍短 TTL）。
            self._cache_put(cache_key, merged, name_hint, analysis)
            weak = weak_share_hint(merged, analysis)
            hints = [item for item in (budget_hint, weak) if item]
            return SearchOutcome(
                results=merged,
                provider=name_hint,
                failed=failures,
                hint=" ".join(hints),
            )
        if merged:
            # 候补都没命中核心词：返回候选但如实提示低相关，不伪装成精准命中；
            # 低相关候选只短 TTL 缓存，给之后的回退留机会。
            self._cache_put(cache_key, merged, name_hint, analysis, short=True)
            base = "候补引擎未找到与查询核心词直接相关的结果，以下为各引擎返回的候选。"
            return SearchOutcome(
                results=merged,
                provider=name_hint,
                failed=failures,
                hint=f"{base}{budget_hint}" if budget_hint else base,
            )
        if self.cache is not None and not failures:
            # 真正的零命中（没有任何上游失败）才短 TTL 缓存；失败导致的空
            # 永不入缓存，否则验证码/限流期间会一直拿到空。
            self.cache.set(cache_key, [], name_hint, ttl=EMPTY_RESULT_TTL)
        if failures and all(
            failure.kind
            in ("timeout", "network", "rate_limit", "unavailable", "bad_response", "auth", "quota")
            for failure in failures
        ):
            hint = "所有搜索引擎均失败。"
        else:
            hint = "未找到相关结果。"
        if any(failure.kind == "auth" for failure in failures):
            hint += " 存在鉴权失败：请检查对应厂商的 API Key 环境变量。"
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
                "search-v6",
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
                str(self.max_requests),
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
    # 谨慎规范化：只合并 www. 前缀差异；m. 等站点变体内容可能不同，不动。
    host = parts.netloc.lower()
    if host.startswith("www.") and "@" not in host:
        host = host[4:]
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
            host,
            parts.path.rstrip("/"),
            query,
            parts.fragment,
        )
    )


def normalize_results(results: list[SearchResult]) -> list[SearchResult]:
    """URL 规范化 + 请求内去重；不在这里截断，候选量由上游 fetch_limit 决定。"""
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
                engine=result.engine,
            )
        )
    return normalized
