"""轻量额度限制（UsageLimiter）的回归测试：全部用假时钟与临时文件。"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.search

from quiet_memo.infra.config import FetchConfig, SearchConfig, UsageLimit
from quiet_memo.infra.search.base import (
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
)
from quiet_memo.infra.search.fetch_router import FetchRouter, build_fetch_router
from quiet_memo.infra.search.router import SearchRouter
from quiet_memo.infra.search.usage import UsageLimiter


class _SearchProvider:
    def __init__(self, name: str, results=None, error: Exception | None = None):
        self.name = name
        self.capabilities = {"search"}
        self.results = list(results or [])
        self.error = error
        self.calls: list[SearchQuery] = []

    def search(self, query):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return list(self.results)


class _FetchProvider:
    def __init__(self, name: str, error: Exception | None = None):
        self.name = name
        self.capabilities = {"fetch"}
        self.error = error
        self.calls: list[str] = []

    def fetch(self, query):
        self.calls.append(query.url)
        if self.error is not None:
            raise self.error
        return FetchResult(query.url, query.url, "Title", "正文")


def test_unlimited_provider_is_never_blocked():
    limiter = UsageLimiter({}, clock=lambda: 0.0)
    assert limiter.remaining("tavily") is None
    assert limiter.allows("tavily")
    limiter.record("tavily")
    assert limiter.remaining("tavily") is None


def test_daily_window_counts_and_resets():
    now = [1_700_000_000.0]
    limits = {"tavily": UsageLimit(period="daily", max_calls=2)}
    limiter = UsageLimiter(limits, clock=lambda: now[0])

    assert limiter.remaining("tavily") == 2
    limiter.record("tavily")
    limiter.record("tavily")
    assert limiter.remaining("tavily") == 0
    assert not limiter.allows("tavily")

    now[0] += 86_400  # 越过一天：计数重置
    assert limiter.remaining("tavily") == 2
    assert limiter.allows("tavily")


def test_monthly_window_and_invalid_period_ignored():
    now = [1_700_000_000.0]  # 2023-11
    limits = {"exa": UsageLimit(period="monthly", max_calls=1)}
    limiter = UsageLimiter(limits, clock=lambda: now[0])
    limiter.record("exa")
    assert not limiter.allows("exa")
    now[0] += 40 * 86_400  # 跨月
    assert limiter.allows("exa")

    broken = {"you": UsageLimit(period="weekly", max_calls=5)}
    limiter = UsageLimiter(broken, clock=lambda: now[0])
    assert limiter.remaining("you") is None


def test_counts_persist_across_instances(tmp_path):
    path = tmp_path / "usage.json"
    limits = {"tavily": UsageLimit(period="daily", max_calls=3)}
    first = UsageLimiter(limits, path, clock=lambda: 0.0)
    first.record("tavily")

    second = UsageLimiter(limits, path, clock=lambda: 0.0)
    assert second.remaining("tavily") == 2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tavily"]["count"] == 1


def test_reload_sees_other_instance_writes(tmp_path):
    path = tmp_path / "usage.json"
    limits = {"volcengine": UsageLimit(period="daily", max_calls=2)}
    searcher = UsageLimiter(limits, path, clock=lambda: 0.0)
    fetcher = UsageLimiter(limits, path, clock=lambda: 0.0)

    searcher.record("volcengine")
    # Fetch 实例在 Search 实例落盘后应看到同一计数池。
    assert fetcher.remaining("volcengine") == 1


def test_corrupted_usage_file_restarts_empty(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("not json", encoding="utf-8")
    limiter = UsageLimiter(
        {"tavily": UsageLimit(period="daily", max_calls=1)}, path, clock=lambda: 0.0
    )
    assert limiter.remaining("tavily") == 1


def _limited_router(tmp_path, pre_used: int = 0):
    limited = _SearchProvider("limited", [SearchResult("无关", "https://example.com/w", "")])
    fallback = _SearchProvider("fallback", [SearchResult("Tibo roadmap", "https://example.com/s", "d")])
    limiter = UsageLimiter(
        {"limited": UsageLimit(period="daily", max_calls=1)},
        tmp_path / "usage.json",
        clock=lambda: 0.0,
    )
    for _ in range(pre_used):
        limiter.record("limited")
    router = SearchRouter(
        {"limited": limited, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )
    return router, limited, fallback, limiter


def test_search_falls_back_when_provider_limit_reached(tmp_path):
    router, limited, fallback, _ = _limited_router(tmp_path, pre_used=1)
    outcome = router.search(SearchQuery("tibo"))

    assert limited.calls == []
    assert fallback.calls
    assert outcome.results[0].url == "https://example.com/s"
    assert any(
        failure.kind == "quota" and failure.provider == "limited"
        for failure in outcome.failed
    )


def test_search_counts_failed_requests(tmp_path):
    broken = _SearchProvider("limited", error=ProviderError("limited", ProviderErrorKind.NETWORK, "reset"))
    fallback = _SearchProvider("fallback", [SearchResult("Tibo", "https://example.com/s", "d")])
    limiter = UsageLimiter(
        {"limited": UsageLimit(period="daily", max_calls=1)},
        tmp_path / "usage.json",
        clock=lambda: 0.0,
    )
    router = SearchRouter(
        {"limited": broken, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )

    router.search(SearchQuery("tibo"))  # 失败也计数 → limited 额度用尽
    router.search(SearchQuery("tibo"))

    assert len(broken.calls) == 1  # 第二次不再调用（无无限重试）
    assert limiter.remaining("limited") == 0


def test_fetch_and_search_share_provider_pool(tmp_path):
    search_provider = _SearchProvider("tavily", [SearchResult("无关", "https://example.com/w", "")])
    fetch_provider = _FetchProvider("tavily")
    limiter = UsageLimiter(
        {"tavily": UsageLimit(period="daily", max_calls=1)},
        tmp_path / "usage.json",
        clock=lambda: 0.0,
    )
    search_router = SearchRouter(
        {"tavily": search_provider}, usage_limiter=limiter
    )
    fetch_router = FetchRouter(
        {"tavily": fetch_provider}, order=["tavily"], usage_limiter=limiter
    )

    search_router.search(SearchQuery("tibo"))  # Search 用掉唯一一次额度
    outcome = search_router.search(SearchQuery("tibo"))
    assert len(search_provider.calls) == 1
    assert any(f.kind == "quota" for f in outcome.failed)

    # Fetch 与 Search 同池：额度已用尽时 Fetch 不发请求、直接报错。
    assert limiter.remaining("tavily") == 0
    with pytest.raises(ProviderError, match="已达到本地调用上限"):
        fetch_router.fetch("https://example.com/a", 1000)
    assert fetch_provider.calls == []


def test_fetch_router_falls_back_when_limit_reached(tmp_path):
    limited = _FetchProvider("limited")
    fallback = _FetchProvider("fallback")
    limiter = UsageLimiter(
        {"limited": UsageLimit(period="daily", max_calls=1)},
        tmp_path / "usage.json",
        clock=lambda: 0.0,
    )
    limiter.record("limited")  # 预记账：额度已用尽
    router = FetchRouter(
        {"limited": limited, "fallback": fallback},
        order=["limited", "fallback"],
        usage_limiter=limiter,
    )

    result = router.fetch("https://example.com/a", 1000)
    assert limited.calls == []
    assert fallback.calls == ["https://example.com/a"]
    assert result.content == "正文"


def test_build_routers_wire_limiter_from_config(tmp_path):
    search_config = SearchConfig(
        usage_limits={"tavily": UsageLimit(period="daily", max_calls=5)}
    )
    from quiet_memo.infra.config import load_config

    router = build_fetch_router(
        FetchConfig(), search_config, tmp_path / "vault"
    )
    assert router.usage_limiter is not None
    assert router.usage_limiter.remaining("tavily") == 5

    config_path = tmp_path / "quiet-memo.toml"
    config_path.write_text(
        '[search.usage_limits.exa]\nperiod = "daily"\nmax_calls = 7\n',
        encoding="utf-8",
    )
    loaded = load_config(config_path)
    assert loaded.search.usage_limits["exa"].max_calls == 7
