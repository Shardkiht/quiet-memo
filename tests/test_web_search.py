from __future__ import annotations

import json
import os
import socket

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
from quiet_memo.infra.search.base import assert_public_fetch_url
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
    # 不硬编码 engine/source：真实来源由 daemon 逐条标注，缺失时由 router
    # 回填槽位引擎；混入其他引擎的用例需显式传入来源字段。
    return {
        "title": title,
        "url": url,
        "description": description,
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


@pytest.fixture
def public_dns(monkeypatch):
    from quiet_memo.infra.search import base as search_base

    monkeypatch.setattr(
        search_base.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "http://localhost:8080/admin",
        "http://127.0.0.1:8080/admin",
        "http://[::1]/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
    ],
)
def test_fetch_url_rejects_non_public_targets(url):
    with pytest.raises(ValueError):
        assert_public_fetch_url(url)


def test_fetch_url_rejects_private_dns_answer(monkeypatch):
    from quiet_memo.infra.search import base as search_base

    monkeypatch.setattr(
        search_base.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
        ],
    )
    with pytest.raises(ValueError, match="非公网"):
        assert_public_fetch_url("https://rebind.example/")


def test_fetch_url_allows_configured_fake_ip(monkeypatch):
    from quiet_memo.infra.search import base as search_base

    monkeypatch.setenv("FAKE_IP_CIDRS", "198.18.0.0/15")
    monkeypatch.setattr(
        search_base.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.7", port))
        ],
    )

    assert_public_fetch_url("https://example.com/")


@pytest.mark.parametrize("final_url", ["https://example.com/", "https://example.com/final"])
def test_router_fetch_allows_default_dual_stack_fake_dns(monkeypatch, final_url):
    from quiet_memo.infra.search import base as search_base

    monkeypatch.delenv("FAKE_IP_CIDRS", raising=False)
    monkeypatch.setattr(
        search_base.socket, "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.15", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:2::12", port, 0, 0)),
        ],
    )

    class FetchProvider:
        capabilities = {"fetch"}

        def fetch(self, query):
            return FetchResult(query.url, final_url, "Title", "正文")

    router = SearchRouter({"fake": FetchProvider()})
    assert router.fetch("https://example.com/").content == "正文"


@pytest.mark.parametrize("configured", ["", "203.0.113.0/24"])
def test_explicit_fake_ip_ranges_replace_defaults(monkeypatch, configured):
    monkeypatch.setenv("FAKE_IP_CIDRS", configured)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("198.18.0.15", port))
        ],
    )
    with pytest.raises(ValueError, match="非公网"):
        assert_public_fetch_url("https://example.com/")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "fc00::1"])
def test_fake_dns_does_not_hide_private_answer(monkeypatch, address):
    monkeypatch.delenv("FAKE_IP_CIDRS", raising=False)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
            for ip in ("198.18.0.15", address)
        ],
    )
    with pytest.raises(ValueError, match="非公网"):
        assert_public_fetch_url("https://example.com/")


@pytest.mark.parametrize("configured", [None, "0.0.0.0/0,::/0"])
@pytest.mark.parametrize("host", ["198.18.0.15", "[2001:2::12]", "127.0.0.1", "[::ffff:127.0.0.1]"])
def test_fake_ip_exceptions_never_allow_literal_non_public_targets(monkeypatch, configured, host):
    if configured is None:
        monkeypatch.delenv("FAKE_IP_CIDRS", raising=False)
    else:
        monkeypatch.setenv("FAKE_IP_CIDRS", configured)
    with pytest.raises(ValueError, match="私网或本机"):
        assert_public_fetch_url(f"http://{host}/")


def test_router_fetch_rejects_private_url_before_provider():
    class CountingProvider:
        name = "fake"
        capabilities = {"fetch"}
        calls = 0

        def fetch(self, query):
            self.calls += 1
            return FetchResult(query.url, query.url, "Title", "正文")

    provider = CountingProvider()
    router = SearchRouter({"fake": provider}, order=["fake"])

    with pytest.raises(ValueError, match="私网或本机"):
        router.fetch("http://127.0.0.1:8080/admin", 1000)
    assert provider.calls == 0


def test_router_fetch_rejects_private_redirect_target(public_dns):
    class RedirectingProvider:
        name = "fake"
        capabilities = {"fetch"}

        def fetch(self, query):
            return FetchResult(query.url, "http://10.0.0.5/secret", "Title", "正文")

    router = SearchRouter({"fake": RedirectingProvider()}, order=["fake"])

    with pytest.raises(ValueError, match="私网或本机"):
        router.fetch("https://example.com/start", 1000)


def test_router_fetch_uses_fetch_capable_provider(public_dns):
    class FetchProvider:
        name = "fake"
        capabilities = {"search", "fetch"}

        def fetch(self, query):
            return FetchResult(query.url, query.url, "Title", "正文内容")

    router = SearchRouter({"fake": FetchProvider()}, order=["fake"])

    assert router.supports_fetch
    assert router.fetch("https://example.com", 1000).content == "正文内容"


def test_router_fetch_failure_is_labeled_as_fetch(public_dns):
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


def test_budget_exhausted_results_are_not_cached(tmp_path):
    """预算耗尽的截断结果不进缓存：同一搜索下次要能重新发起请求。"""
    weak = FakeProvider([SearchResult("Unrelated", "https://example.com/w", "")])
    never = FakeProvider([SearchResult("Tibo roadmap", "https://example.com/s", "details")])
    cache = SearchCache(tmp_path / "cache.json", ttl=60)
    router = SearchRouter(
        {"weak": weak, "never": never},
        order=["weak", "never"],
        cache=cache,
        max_requests=1,
    )

    first = router.search(SearchQuery("tibo"))
    assert weak.calls == 1 and never.calls == 0
    assert "请求次数上限" in first.hint

    second = router.search(SearchQuery("tibo"))
    # 未命中缓存：第一家被重新请求；预算 1 依旧轮不到第二家。
    assert weak.calls == 2 and never.calls == 0
    assert "请求次数上限" in second.hint


def test_budget_exhausted_core_hits_are_not_cached(tmp_path):
    """池内已有核心词命中时，预算耗尽结果同样不得按成功语义缓存。"""
    provider = FakeProvider(
        [SearchResult("Tibo update", "https://example.com/a", "notes about tibo")]
    )
    second = FakeProvider([])
    cache = SearchCache(tmp_path / "cache.json", ttl=60)
    router = SearchRouter(
        {"fake": provider, "second": second},
        order=["fake", "second"],
        cache=cache,
        max_requests=1,
    )

    outcome = router.search(SearchQuery("tibo codex"))
    # 单条强命中不足以早停（两个核心词只覆盖一个）：预算在第二家前耗尽。
    assert provider.calls == 1 and second.calls == 0
    assert outcome.results and "请求次数上限" in outcome.hint

    router.search(SearchQuery("tibo codex"))
    assert provider.calls == 2


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


@pytest.mark.parametrize("installed", [None, "/installed/open-websearch"])
def test_ensure_daemon_prefers_installed_service(monkeypatch, tmp_path, installed):
    checks = iter([False, True])
    commands = []
    monkeypatch.setattr(daemon_module.shutil, "which", lambda name: installed)
    monkeypatch.setattr(daemon_module, "npx_command", lambda command, args: [command, *args])
    monkeypatch.setattr(daemon_module, "_spawn_daemon", lambda command, log: commands.append(command))
    assert ensure_daemon(
        "http://127.0.0.1:3000", health_check=lambda: next(checks),
        sleep=lambda _: None, log_path=tmp_path / "daemon.log",
    ) == "started"
    expected = [installed] if installed else ["npx", "-y", "open-websearch@latest"]
    assert commands == [expected + ["serve", "--port", "3000"]]


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


@pytest.mark.parametrize("configured", [None, "", "203.0.113.0/24"])
def test_spawn_daemon_shares_fake_dns_policy(tmp_path, monkeypatch, configured):
    from quiet_memo.infra.search.base import DEFAULT_FAKE_IP_CIDRS

    captured = {}
    if configured is None:
        monkeypatch.delenv("FAKE_IP_CIDRS", raising=False)
    else:
        monkeypatch.setenv("FAKE_IP_CIDRS", configured)
    monkeypatch.setattr(
        daemon_module.subprocess, "Popen",
        lambda command, **kwargs: captured.update(kwargs),
    )
    daemon_module._spawn_daemon(["unused"], tmp_path / "daemon.log")
    assert captured["env"]["FAKE_IP_CIDRS"] == (
        DEFAULT_FAKE_IP_CIDRS if configured is None else configured
    )


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

    assert tools._tools["fetch_web"].executor == "network"
    outcome = tools.execute("fetch_web", {"url": "https://example.com/news"})

    assert outcome.ok is True
    assert outcome.content.startswith(SEARCH_NOTICE)
    assert "2026-09-21" in outcome.content
    assert outcome.display == "Fetched example.com"


def test_search_router_is_built_from_config(tmp_path):
    router = build_search_router(
        SearchConfig(
            base_url="http://127.0.0.1:3210",
            transport="http",
            max_requests=4,
        ),
        tmp_path,
    )
    assert "open_websearch" in router.registry
    assert router.max_results == 5
    assert router.max_requests == 4


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


@pytest.mark.parametrize("value, expected", [
    ('["bing"]', ("bing",)),
    ('["baidu", "bing", "baidu"]', ("baidu", "bing")),
])
def test_engine_argument_accepts_json_array_string(value, expected):
    from quiet_memo.infra.search.base import normalize_search_engines
    assert normalize_search_engines(value, ["bing", "baidu"]) == expected


@pytest.mark.parametrize("value", ['[1]', '["notreal"]', '["bing"'])
def test_engine_argument_rejects_invalid_json_array_string(value):
    from quiet_memo.infra.search.base import normalize_search_engines
    with pytest.raises(ValueError):
        normalize_search_engines(value, ["bing", "baidu"])


@pytest.mark.parametrize("value, expected", [
    (['["baidu"]'], ("baidu",)),
    (['["baidu"]', '["bing"]'], ("baidu", "bing")),
    (['baidu', '["bing", "baidu"]'], ("baidu", "bing")),
])
def test_engine_argument_accepts_nested_json_array_items(value, expected):
    # 有的 provider 把数组参数双重序列化成元素级 JSON 字符串。
    from quiet_memo.infra.search.base import normalize_search_engines
    assert normalize_search_engines(value, ["bing", "baidu"]) == expected


@pytest.mark.parametrize("value", [['[1]'], ['["notreal"]'], ['["bing"'], ['[1]', 'bing']])
def test_engine_argument_rejects_invalid_nested_json_array_items(value):
    from quiet_memo.infra.search.base import normalize_search_engines
    with pytest.raises(ValueError):
        normalize_search_engines(value, ["bing", "baidu"])


def test_default_engine_failure_falls_back_once():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["engines"] == ["bing"]:
            payload = envelope([])
            payload["data"]["partialFailures"] = [{"engine": "bing", "code": "ECONNRESET", "message": "TLS connection failed"}]
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=envelope([raw_result("Synthetic", "https://example.com")]))
    provider = mock_provider(handler, engines=["bing"])
    router = SearchRouter({"open_websearch": provider}, order=["open_websearch"], engines=["bing"])
    results = router.search(SearchQuery("synthetic"))
    assert len(results.results) == 1
    assert [item["engines"] for item in requests] == [["bing"], ["baidu"]]


# ---------- 引擎候补链、查询变体与相关性（搜索增强） ----------


def test_relevant_results_return_without_fallback_requests():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json=envelope([raw_result("Tibo Codex resets", "https://example.com/tibo")])
        )

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("tibo codex"))
    assert len(outcome.results) == 1
    assert len(calls) == 1  # 相关结果直接返回，不触发候补请求


def test_empty_first_result_falls_back_to_candidate_engine():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["engines"] == ["bing"]:
            return httpx.Response(200, json=envelope([]))
        return httpx.Response(
            200, json=envelope([raw_result("Tibo 重置追踪", "https://example.com/tibo")])
        )

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("tibo"))
    assert len(outcome.results) == 1
    assert outcome.results[0].engine == "baidu"
    assert [item["engines"] for item in requests] == [["bing"], ["baidu"]]


def test_irrelevant_results_trigger_candidate_engine():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["engines"] == ["bing"]:
            # 有结果但与查询词元零命中 → 触发候补，原候选不丢弃。
            return httpx.Response(
                200, json=envelope([raw_result("完全无关的内容", "https://example.com/other")])
            )
        return httpx.Response(
            200, json=envelope([raw_result("Tibo Codex 重置", "https://example.com/tibo")])
        )

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("tibo"))
    assert outcome.results[0].engine == "baidu"
    assert [item["engines"] for item in requests] == [["bing"], ["baidu"]]


def test_context_terms_generate_variant_queries():
    queries = []

    def handler(request):
        body = json.loads(request.content)
        queries.append(body["query"])
        if body["query"] == "tibo":
            return httpx.Response(200, json=envelope([]))
        return httpx.Response(
            200,
            json=envelope(
                [raw_result("Tibo (Thibault Sottiaux)", "https://example.com/tibo")]
            ),
        )

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("tibo", context_terms=("Codex", "OpenAI")))
    assert len(outcome.results) == 1
    # 原始查询空结果后用上下文派生变体（最多两个），专有名词保持原拼写。
    assert queries[0] == "tibo"
    assert queries[1] == "tibo Codex"


def test_explicit_engine_skips_variants_and_fallback():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=envelope([]))

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("tibo", engines=("baidu",), context_terms=("Codex",)))
    assert not outcome.results
    # 显式指定引擎：单次请求、不派生变体、不回退。
    assert len(calls) == 1 and calls[0]["engines"] == ["baidu"]


def test_timeout_budget_stops_fallback_before_requests():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=envelope([]))

    router = SearchRouter(
        {"open_websearch": mock_provider(handler)},
        engines=["bing"],
        timeout_budget=0.0,
    )
    outcome = router.search(SearchQuery("tibo"))
    assert not outcome.results and not outcome.cached
    assert calls == []  # 预算为 0：一次上游请求都不发起
    assert outcome.failed[0].kind == "timeout"
    assert "预算" in outcome.failed[0].detail


def test_generic_provider_without_engines_capability_runs_once():
    provider = FakeProvider([SearchResult("ok", "https://example.com", "")])
    router = SearchRouter({"fake": provider}, order=["fake"])
    outcome = router.search(SearchQuery("synthetic"))
    assert len(outcome.results) == 1
    assert provider.calls == 1  # 无 engines 能力的 provider 不做引擎回退


def test_multi_engine_candidates_dedupe_and_hint():
    def handler(request):
        body = json.loads(request.content)
        engines = body["engines"]
        if engines == ["bing"]:
            return httpx.Response(
                200,
                json=envelope(
                    [
                        raw_result("noise one", "https://example.com/dup"),
                        raw_result("noise two", "https://example.com/b"),
                    ]
                ),
            )
        if engines == ["baidu"]:
            return httpx.Response(
                200,
                json=envelope(
                    [
                        raw_result("noise three", "https://example.com/dup"),
                        raw_result("noise four", "https://example.com/d"),
                    ]
                ),
            )
        return httpx.Response(200, json=envelope([]))

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("xyzabc"))
    # 三个引擎各试一次，跨引擎 URL 去重后保留候选并如实提示。
    assert len(outcome.results) == 3
    assert {item.url for item in outcome.results} == {
        "https://example.com/dup",
        "https://example.com/b",
        "https://example.com/d",
    }
    assert "候补" in outcome.hint
    assert [item.engine for item in outcome.results][:3] == ["bing", "bing", "baidu"]


def test_results_ranked_title_before_snippet():
    def handler(request):
        return httpx.Response(
            200,
            json=envelope(
                [
                    raw_result(
                        "无关标题", "https://example.com/1", "这里提到 tibo codex 的转述"
                    ),
                    raw_result("Tibo Codex 指南", "https://example.com/2"),
                ]
            ),
        )

    router = SearchRouter({"open_websearch": mock_provider(handler)}, engines=["bing"])
    outcome = router.search(SearchQuery("tibo codex"))
    assert outcome.results[0].url == "https://example.com/2"  # 标题命中的排前


def test_freshness_query_success_cache_is_short(tmp_path):
    now = [1000.0]
    provider = FakeProvider([SearchResult("ok", "https://example.com", "")])
    cache = SearchCache(tmp_path / "cache.json", ttl=86400, clock=lambda: now[0])
    router = SearchRouter({"fake": provider}, order=["fake"], cache=cache)
    # 普通查询：成功结果走完整 TTL，700 秒后仍命中缓存。
    router.search(SearchQuery("ok 查询"))
    now[0] += 700
    assert router.search(SearchQuery("ok 查询")).cached
    # 时效性查询：成功结果短 TTL，700 秒后必须重新搜索。
    assert router.search(SearchQuery("最新 ok 新闻")).results
    now[0] += 700
    outcome = router.search(SearchQuery("最新 ok 新闻"))
    assert outcome.results and not outcome.cached
    assert provider.calls == 3


def test_cache_key_distinguishes_context_strategy_and_fallback(tmp_path):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append((body["query"], body.get("engines")))
        if body["query"] == "tibo":
            return httpx.Response(200, json=envelope([]))
        return httpx.Response(
            200,
            json=envelope([raw_result(f"{body['query']} hit", "https://example.com/x")]),
        )

    cache = SearchCache(tmp_path / "cache.json", ttl=3600)
    router = SearchRouter(
        {"open_websearch": mock_provider(handler)}, engines=["bing"], cache=cache
    )
    # context 参与键且保留顺序：换序即视为新查询，重新消歧。
    router.search(SearchQuery("tibo", context_terms=("Codex", "OpenAI")))
    assert len(calls) == 2  # 空结果 + 变体
    router.search(SearchQuery("tibo", context_terms=("OpenAI", "Codex")))
    assert len(calls) == 4
    # 相同 context（同序）命中缓存，不再打上游。
    router.search(SearchQuery("tibo", context_terms=("Codex", "OpenAI")))
    assert len(calls) == 4
    # auto 与显式同引擎不共用键：显式请求必须真的打到该引擎。
    router.search(SearchQuery("litellm docs"))
    router.search(SearchQuery("litellm docs", engines=("bing",)))
    assert len(calls) == 6
    # 回退配置不同（候补链变化）视为新查询，不命中旧策略缓存。
    other = SearchRouter(
        {"open_websearch": mock_provider(handler)},
        engines=["bing"],
        cache=cache,
        fallback_engines=("sogou",),
    )
    other.search(SearchQuery("tibo", context_terms=("Codex", "OpenAI")))
    assert len(calls) == 8


def test_cached_results_match_first_request_order(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json=envelope(
                [
                    raw_result(
                        "无关标题A", "https://example.com/1", "这里提到 tibo codex 转述"
                    ),
                    raw_result("Tibo Codex 指南", "https://example.com/2"),
                ]
            ),
        )

    cache = SearchCache(tmp_path / "cache.json", ttl=3600)
    router = SearchRouter(
        {"open_websearch": mock_provider(handler)}, engines=["bing"], cache=cache
    )
    first = router.search(SearchQuery("tibo codex"))
    second = router.search(SearchQuery("tibo codex"))
    assert second.cached
    # 首次与缓存命中逐项一致（顺序、URL、来源引擎）。
    assert [(r.title, r.url, r.engine) for r in first.results] == [
        (r.title, r.url, r.engine) for r in second.results
    ]


def test_explicit_engine_failure_is_not_cached_as_empty(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        payload = envelope([])
        payload["data"]["partialFailures"] = [{"engine": "bing", "code": "ETIMEDOUT", "message": "request timed out"}]
        return httpx.Response(200, json=payload)
    provider = mock_provider(handler)
    router = SearchRouter({"open_websearch": provider}, order=["open_websearch"], cache=SearchCache(tmp_path / "cache.json", ttl=300))
    for _ in range(2):
        result = router.search(SearchQuery("synthetic", engines=("bing",)))
        assert not result.results
        assert result.failed[0].kind == "timeout"
        assert not result.cached
    assert len(calls) == 2


def test_legacy_empty_search_cache_is_not_reused(tmp_path):
    import hashlib
    provider = FakeProvider([SearchResult("ok", "https://example.com", "")])
    cache = SearchCache(tmp_path / "cache.json", ttl=60)
    legacy_key = hashlib.sha256("\x1f".join(["q", "5", "auto", "", "", "fake"]).encode()).hexdigest()
    cache.set(legacy_key, [], "fake")
    router = SearchRouter({"fake": provider}, order=["fake"], cache=cache)
    result = router.search(SearchQuery("q"))
    assert result.results and not result.cached
    assert provider.calls == 1


def test_empty_results_use_short_ttl(tmp_path):
    # 真空结果短 TTL 缓存：5 分钟内不重复打上游，过期后重新搜索能拿到新结果。
    now = [1000.0]
    provider = FakeProvider([])
    cache = SearchCache(tmp_path / "cache.json", ttl=3600, clock=lambda: now[0])
    router = SearchRouter({"fake": provider}, order=["fake"], cache=cache)
    first = router.search(SearchQuery("q"))
    assert not first.results and not first.cached
    now[0] += 60
    second = router.search(SearchQuery("q"))
    assert not second.results and second.cached
    assert provider.calls == 1
    provider.results = [SearchResult("ok", "https://example.com", "")]
    now[0] += 300
    third = router.search(SearchQuery("q"))
    assert third.results and not third.cached
    assert provider.calls == 2


def test_budget_exhaustion_preserves_collected_candidates(monkeypatch):
    from quiet_memo.infra.search import router as router_module

    clock = [0.0]
    calls = []

    class SlowProvider:
        capabilities = {"search", "engines"}

        def search(self, query):
            calls.append(query)
            clock[0] = 2.0
            return [SearchResult("Unrelated candidate", "https://example.com/candidate", "")]

    monkeypatch.setattr(router_module.time, "perf_counter", lambda: clock[0])
    router = SearchRouter({"fake": SlowProvider()}, engines=["bing"], timeout_budget=1)
    result = router.search(SearchQuery("alpha"))
    assert [item.title for item in result.results] == ["Unrelated candidate"]
    assert result.hint
    assert any(item.kind == "timeout" for item in result.failed)
    assert len(calls) == 1
