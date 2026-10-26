from __future__ import annotations

import httpx
import pytest

from quiet_memo.agent.session import SessionStore
from quiet_memo.agent.tools import SEARCH_NOTICE, build_tools
from quiet_memo.app.server import build_server
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig, SearchConfig
from quiet_memo.infra.search import (
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
    SearchRouter,
    build_search_router,
)
from quiet_memo.infra.search.cache import SearchCache
from quiet_memo.infra.search.open_websearch import OpenWebSearchProvider
from quiet_memo.infra.search.router import normalize_url
from quiet_memo.memory.service import MemoryService


def envelope(results: list[dict]) -> dict:
    return {
        "status": "ok",
        "data": {
            "query": "q",
            "engines": ["bing"],
            "totalResults": len(results),
            "results": results,
            "partialFailures": [],
        },
        "error": None,
        "hint": None,
    }


def raw_result(title: str, url: str, description: str = "") -> dict:
    return {
        "title": title,
        "url": url,
        "description": description,
        "source": "bing",
        "engine": "bing",
    }


def mock_provider(handler, timeout: float = 1.0) -> OpenWebSearchProvider:
    return OpenWebSearchProvider(
        "http://127.0.0.1:3000",
        timeout=timeout,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


class FakeProvider:
    name = "fake"
    capabilities = {"search"}

    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = 0

    def search(self, query):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.results)


class FailingProvider:
    name = "bad"
    capabilities = {"search"}

    def search(self, query):
        raise ProviderError("bad", ProviderErrorKind.TIMEOUT, "slow")


def test_http_provider_maps_real_schema():
    provider = mock_provider(
        lambda request: httpx.Response(
            200,
            json=envelope([raw_result("标题", "https://example.com/a", "摘要")]),
        )
    )
    results = provider.search(SearchQuery("查询"))

    assert len(results) == 1
    assert results[0].title == "标题"
    assert results[0].url == "https://example.com/a"
    assert results[0].snippet == "摘要"
    assert results[0].published is None
    assert results[0].provider == "open_websearch"


def test_missing_fields_are_tolerated():
    provider = mock_provider(
        lambda request: httpx.Response(
            200,
            json=envelope(
                [
                    {"title": "没有 URL"},
                    {"url": "https://example.com/only-url"},
                    {"title": "正常", "url": "https://example.com/ok", "description": None},
                ]
            ),
        )
    )
    results = provider.search(SearchQuery("q"))

    assert [result.url for result in results] == [
        "https://example.com/only-url",
        "https://example.com/ok",
    ]
    assert results[0].title == "https://example.com/only-url"
    assert results[1].snippet == ""


def test_empty_results():
    provider = mock_provider(lambda request: httpx.Response(200, json=envelope([])))
    assert provider.search(SearchQuery("q")) == []


def test_timeout_maps_to_provider_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as caught:
        mock_provider(handler).search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.TIMEOUT


def test_429_retries_once_with_retry_after():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=envelope([raw_result("ok", "https://example.com")]))

    results = mock_provider(handler).search(SearchQuery("q"))
    assert len(calls) == 2
    assert len(results) == 1


def test_429_after_retry_is_rate_limit():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"})

    with pytest.raises(ProviderError) as caught:
        mock_provider(handler).search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.RATE_LIMIT
    assert len(calls) == 2


def test_5xx_is_unavailable():
    provider = mock_provider(lambda request: httpx.Response(503, text="down"))
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.UNAVAILABLE


def test_connection_error_mentions_daemon_command():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as caught:
        mock_provider(handler).search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.NETWORK
    assert "open-websearch" in caught.value.detail
    assert "serve" in caught.value.detail


def test_bad_envelope_is_bad_response():
    provider = mock_provider(
        lambda request: httpx.Response(
            200, json={"status": "error", "data": None, "error": {"code": "engine_error", "message": "boom"}}
        )
    )
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind is ProviderErrorKind.UNAVAILABLE


def test_url_normalization_and_dedupe_limit():
    provider = FakeProvider(
        [
            SearchResult("a", "HTTPS://Example.COM/path/?utm_source=x", "a"),
            SearchResult("b", "https://example.com/path/", "b"),
            SearchResult("c", "https://example.com/other?utm_medium=y", "c"),
            SearchResult("d", "https://example.com/third", "d"),
        ]
    )
    router = SearchRouter({"fake": provider}, order=["fake"], max_results=2)
    outcome = router.search(SearchQuery("q", limit=5))

    assert len(outcome.results) == 2
    assert outcome.results[0].url == "https://example.com/path"
    assert outcome.results[1].url == "https://example.com/other"
    assert normalize_url("https://EXAMPLE.com/a/?utm_source=x&b=2") == "https://example.com/a?b=2"


def test_router_falls_back_to_next_provider():
    router = SearchRouter(
        {"bad": FailingProvider(), "fake": FakeProvider([SearchResult("ok", "https://example.com", "")])},
        order=["bad", "fake"],
    )
    outcome = router.search(SearchQuery("q"))

    assert outcome.provider == "fake"
    assert len(outcome.results) == 1
    assert outcome.failed[0].provider == "bad"
    assert outcome.failed[0].kind == "timeout"


def test_router_all_fail_returns_structured_errors():
    router = SearchRouter({"bad": FailingProvider()}, order=["bad"])
    outcome = router.search(SearchQuery("q"))

    assert outcome.results == []
    assert outcome.provider == ""
    assert outcome.failed[0].to_dict()["kind"] == "timeout"
    assert "均失败" in outcome.hint


def test_explicit_unknown_engine_does_not_fall_back():
    router = SearchRouter({"fake": FakeProvider([SearchResult("ok", "https://example.com", "")])})
    outcome = router.search(SearchQuery("q", engine="brave"))

    assert outcome.results == []
    assert outcome.failed[0].provider == "brave"


def test_cache_avoids_second_provider_call(tmp_path):
    provider = FakeProvider([SearchResult("ok", "https://example.com", "")])
    cache = SearchCache(tmp_path / "cache.json", ttl=60)
    router = SearchRouter({"fake": provider}, order=["fake"], cache=cache)

    first = router.search(SearchQuery("q"))
    second = router.search(SearchQuery("q"))

    assert len(first.results) == 1
    assert second.cached is True
    assert provider.calls == 1


def test_cache_expires(tmp_path):
    now = [0.0]
    provider = FakeProvider([SearchResult("ok", "https://example.com", "")])
    cache = SearchCache(tmp_path / "cache.json", ttl=10, clock=lambda: now[0])
    router = SearchRouter({"fake": provider}, order=["fake"], cache=cache)

    router.search(SearchQuery("q"))
    now[0] = 11
    router.search(SearchQuery("q"))

    assert provider.calls == 2


def test_disabled_search_does_not_register_tool(tmp_path):
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        search=SearchConfig(enabled=False),
    )
    service = MemoryService(config)
    service.init_vault()
    session = SessionStore(config.vault).create()
    tools = build_tools(service, session)

    assert "search" not in tools._tools


def test_agent_search_tool_returns_notice_and_results(tmp_path):
    provider = FakeProvider([SearchResult("标题", "https://example.com", "摘要")])
    router = SearchRouter({"fake": provider}, order=["fake"])
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(),
        search=SearchConfig(enabled=True),
    )
    service = MemoryService(config)
    service.init_vault()
    session = SessionStore(config.vault).create()
    tools = build_tools(service, session, search_router=router)

    outcome = tools.execute("search", {"query": "查询", "limit": 3})

    assert outcome.ok is True
    assert outcome.content.startswith(SEARCH_NOTICE)
    assert outcome.data["results"][0]["url"] == "https://example.com"
    assert "1 条" in outcome.display


def test_search_router_is_built_from_config(tmp_path):
    router = build_search_router(
        SearchConfig(base_url="http://127.0.0.1:3210", transport="http"),
        tmp_path,
    )
    assert "open_websearch" in router.registry
    assert router.max_results == 5


async def test_mcp_exposes_search_when_enabled(tmp_path):
    provider = FakeProvider([SearchResult("标题", "https://example.com", "摘要")])
    router = SearchRouter({"fake": provider}, order=["fake"])
    config = Config(vault=tmp_path / "vault", llm=LLMConfig(), search=SearchConfig(enabled=True))
    service = MemoryService(config)
    service.init_vault()
    server = build_server(service=service, search_router=router)

    from fastmcp import Client

    async with Client(server) as client:
        tools = await client.list_tools()
        assert "search" in {tool.name for tool in tools}
        result = await client.call_tool("search", {"query": "查询"})
        assert result.data["results"][0]["url"] == "https://example.com"


async def test_mcp_omits_search_when_disabled(tmp_path):
    config = Config(vault=tmp_path / "vault", llm=LLMConfig(), search=SearchConfig(enabled=False))
    service = MemoryService(config)
    service.init_vault()
    server = build_server(service=service)

    from fastmcp import Client

    async with Client(server) as client:
        tools = await client.list_tools()
        assert "search" not in {tool.name for tool in tools}
