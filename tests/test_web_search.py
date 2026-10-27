from __future__ import annotations

import json
import os

import httpx
import pytest

pytestmark = pytest.mark.search

from quiet_memo.agent.session import SessionStore
from quiet_memo.agent.tools import SEARCH_NOTICE, build_tools
from quiet_memo.app.server import build_server
from quiet_memo.infra.config import ChatConfig, Config, LLMConfig, SearchConfig
from quiet_memo.infra.search import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
    SearchRouter,
    build_search_router,
)
from quiet_memo.infra.search import daemon as daemon_module
from quiet_memo.infra.search.cache import SearchCache
from quiet_memo.infra.search.daemon import ensure_daemon
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


def mock_provider(handler, timeout: float = 1.0, **kwargs) -> OpenWebSearchProvider:
    return OpenWebSearchProvider(
        "http://127.0.0.1:3000",
        timeout=timeout,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_http_provider_maps_fetch_result():
    def handler(request):
        assert request.url.path == "/fetch-web"
        body = json.loads(request.content)
        assert body["url"] == "https://example.com/news"
        assert body["readability"] is True
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "url": "https://example.com/news",
                    "finalUrl": "https://example.com/news/",
                    "title": "News",
                    "content": "发布日期 2026-09-21",
                    "truncated": False,
                    "siteName": "Example",
                },
                "error": None,
                "hint": None,
            },
        )

    result = mock_provider(handler).fetch(FetchQuery("https://example.com/news", 2000))

    assert result.title == "News"
    assert result.final_url == "https://example.com/news/"
    assert result.site_name == "Example"
    assert "2026-09-21" in result.content


def test_router_fetch_uses_fetch_capable_provider():
    class FetchProvider:
        name = "fake"
        capabilities = {"search", "fetch"}

        def fetch(self, query):
            return FetchResult(query.url, query.url, "Title", "正文内容")

    router = SearchRouter({"fake": FetchProvider()}, order=["fake"])

    assert router.supports_fetch
    assert router.fetch("https://example.com", 1000).content == "正文内容"


def test_router_fetch_failure_is_labeled_as_fetch():
    class BrokenFetch:
        name = "fake"
        capabilities = {"fetch"}

        def fetch(self, query):
            raise ProviderError("open_websearch", ProviderErrorKind.UNAVAILABLE, "HTTP 502")

    router = SearchRouter({"fake": BrokenFetch()}, order=["fake"])
    with pytest.raises(ProviderError) as caught:
        router.fetch("https://example.com", 1000)

    assert caught.value.provider == "fetch"
    assert "open_websearch" in caught.value.detail
    assert "502" in caught.value.detail


class FakeProvider:
    name = "fake"
    capabilities = {"search"}

    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = 0
        self.queries: list[SearchQuery] = []

    def search(self, query):
        self.calls += 1
        self.queries.append(query)
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


def test_search_sends_configured_and_per_query_engines():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=envelope([]))

    provider = mock_provider(handler, engines=["bing", "baidu"])
    provider.search(SearchQuery("q"))
    provider.search(SearchQuery("q", engines=("sogou",)))

    assert bodies[0]["engines"] == ["bing", "baidu"]
    assert bodies[1]["engines"] == ["sogou"]


def test_fetch_clamps_max_chars_and_uses_fetch_timeout():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "url": "https://example.com/news",
                    "finalUrl": "https://example.com/news",
                    "title": "News",
                    "content": "正文",
                    "truncated": False,
                    "siteName": "Example",
                },
                "error": None,
                "hint": None,
            },
        )

    provider = mock_provider(handler, fetch_timeout=12.0)
    provider.fetch(FetchQuery("https://example.com/news", 500))

    assert seen["body"]["maxChars"] == 1000
    assert seen["timeout"]["read"] == 12.0


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


def test_cache_key_separates_search_engines(tmp_path):
    provider = FakeProvider([SearchResult("ok", "https://example.com", "")])
    cache = SearchCache(tmp_path / "cache.json", ttl=60)
    router = SearchRouter(
        {"fake": provider}, order=["fake"], cache=cache, engines=["bing"]
    )

    router.search(SearchQuery("q"))
    router.search(SearchQuery("q", engines=("baidu",)))
    router.search(SearchQuery("q"))

    assert provider.calls == 2
    assert provider.queries[1].engines == ("baidu",)


def test_ensure_daemon_reuses_running_process():
    spawns = []
    result = ensure_daemon(
        "http://127.0.0.1:3000",
        health_check=lambda: True,
        spawn=lambda: spawns.append(1),
    )

    assert result == "running"
    assert spawns == []


def test_ensure_daemon_health_uses_spawn_port_when_url_has_no_port(monkeypatch):
    seen = []
    monkeypatch.setattr(
        daemon_module, "daemon_health", lambda base_url: seen.append(base_url) or True
    )

    result = ensure_daemon("http://127.0.0.1")

    assert result == "running"
    assert seen == ["http://127.0.0.1:3000"]


def test_corrupted_cache_entry_is_treated_as_miss(tmp_path):
    import json

    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "bad-expiry": {"expires": "not-a-number", "results": []},
                    "bad-result": {"expires": 9999999999, "results": [{"unexpected": 1}]},
                },
            }
        ),
        encoding="utf-8",
    )
    cache = SearchCache(path, ttl=60)

    assert cache.get("bad-expiry") is None
    assert cache.get("bad-result") is None
    cache.set("good", [], "fake")
    assert cache.get("good") == ([], "fake")


def test_ensure_daemon_spawns_and_waits_for_health():
    checks = iter([False, False, True])
    spawns = []
    result = ensure_daemon(
        "http://127.0.0.1:3000",
        health_check=lambda: next(checks),
        spawn=lambda: spawns.append(1),
        sleep=lambda _: None,
    )

    assert result == "started"
    assert spawns == [1]


def test_ensure_daemon_timeout_is_structured():
    with pytest.raises(ProviderError) as caught:
        ensure_daemon(
            "http://127.0.0.1:3000",
            timeout=0,
            health_check=lambda: False,
            spawn=lambda: None,
            sleep=lambda _: None,
        )
    assert caught.value.kind is ProviderErrorKind.TIMEOUT


@pytest.mark.skipif(os.name != "nt", reason="Windows console flags")
def test_spawn_daemon_hides_console_window(tmp_path, monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured.update(kwargs)

    monkeypatch.setattr(daemon_module.subprocess, "Popen", FakePopen)
    daemon_module._spawn_daemon(["cmd", "/c", "npx"], tmp_path / "daemon.log")

    assert captured["creationflags"] & daemon_module.subprocess.CREATE_NO_WINDOW
    assert captured["startupinfo"].dwFlags & daemon_module.subprocess.STARTF_USESHOWWINDOW
    assert captured["startupinfo"].wShowWindow == daemon_module.subprocess.SW_HIDE


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
    assert "fetch_web" not in tools._tools


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
    assert "1 results" in outcome.display


def test_agent_search_tool_passes_search_engine(tmp_path):
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

    outcome = tools.execute(
        "search", {"query": "智谱 GLM 最新消息", "search_engine": "baidu"}
    )

    assert outcome.ok is True
    assert provider.queries[-1].engines == ("baidu",)


def test_agent_search_tool_accepts_legacy_engine_and_multiple_engines(tmp_path):
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

    legacy = tools.execute("search", {"query": "旧参数", "engine": "baidu"})
    multiple = tools.execute(
        "search", {"query": "多引擎", "search_engine": ["bing", "baidu", "bing"]}
    )

    assert legacy.ok is True
    assert multiple.ok is True
    assert provider.queries[-2].engines == ("baidu",)
    assert provider.queries[-1].engines == ("bing", "baidu")


def test_agent_search_tool_rejects_too_many_engines(tmp_path):
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

    outcome = tools.execute(
        "search",
        {"query": "查询", "search_engine": ["bing", "baidu", "sogou", "brave"]},
    )

    assert outcome.ok is False
    assert "at most 3 search engines" in outcome.display
    assert provider.calls == 0


def test_agent_search_schema_does_not_expose_provider_engine(tmp_path):
    provider = FakeProvider([])
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

    properties = tools._tools["search"].parameters["properties"]

    assert "engine" not in properties
    assert "search_engine" in properties


def test_agent_search_tool_rejects_unknown_search_engine(tmp_path):
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

    outcome = tools.execute("search", {"query": "查询", "search_engine": "notreal"})

    assert outcome.ok is False
    assert "unsupported search engine" in outcome.display
    assert provider.calls == 0


def test_agent_fetch_tool_returns_page_content(tmp_path):
    class FetchRouter:
        supports_fetch = True

        def fetch(self, url, max_chars=4000):
            return FetchResult(url, url, "Page", "发布日期 2026-09-21", False, "Example")

    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        chat=ChatConfig(),
        search=SearchConfig(enabled=True),
    )
    service = MemoryService(config)
    service.init_vault()
    session = SessionStore(config.vault).create()
    tools = build_tools(service, session, search_router=FetchRouter())

    outcome = tools.execute("fetch_web", {"url": "https://example.com/news"})

    assert outcome.ok is True
    assert outcome.content.startswith(SEARCH_NOTICE)
    assert "2026-09-21" in outcome.content
    assert outcome.display == "Fetched example.com"


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
        await client.call_tool(
            "search", {"query": "智谱 GLM", "search_engine": "baidu"}
        )
        assert provider.queries[-1].engines == ("baidu",)
        await client.call_tool(
            "search", {"query": "智谱 GLM", "search_engine": ["baidu", "bing"]}
        )
        assert provider.queries[-1].engines == ("baidu", "bing")


async def test_mcp_omits_search_when_disabled(tmp_path):
    config = Config(vault=tmp_path / "vault", llm=LLMConfig(), search=SearchConfig(enabled=False))
    service = MemoryService(config)
    service.init_vault()
    server = build_server(service=service)

    from fastmcp import Client

    async with Client(server) as client:
        tools = await client.list_tools()
        assert "search" not in {tool.name for tool in tools}
