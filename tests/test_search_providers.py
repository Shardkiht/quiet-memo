"""多厂商搜索 Provider 适配测试（任务卡 02-08）。

全部走 httpx.MockTransport，不发真实网络请求；Key 一律用占位字符串。
"""
from __future__ import annotations

import json

import httpx
import pytest

pytestmark = pytest.mark.search

from quiet_memo.infra.config import SearchConfig
from quiet_memo.infra.search.base import (
    FetchQuery,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
)
from quiet_memo.infra.search.baidu import BaiduSearchProvider
from quiet_memo.infra.search.exa import ExaSearchProvider
from quiet_memo.infra.search.http_client import JsonApiClient
from quiet_memo.infra.search.router import build_registry
from quiet_memo.infra.search.tavily import TavilyFetchProvider, TavilySearchProvider
from quiet_memo.infra.search.volcengine import VolcengineSearchProvider
from quiet_memo.infra.search.you import YouSearchProvider

KEY = "test-key-placeholder"


def mock_factory(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def make(provider_cls, handler, **kwargs):
    return provider_cls(KEY, client_factory=lambda: mock_factory(handler), **kwargs)


def capture(handler):
    """包装 handler，记录收到的请求。"""
    requests: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return wrapped, requests


# ---------- 卡 02：registry 最小扩展 ----------


def test_registry_without_new_providers_keeps_old_behavior(monkeypatch):
    config = SearchConfig()
    registry = build_registry(config)
    assert set(registry) == {"open_websearch"}


def test_registry_skips_external_provider_without_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = SearchConfig()
    config.providers = ["open_websearch", "tavily"]
    registry = build_registry(config)
    assert set(registry) == {"open_websearch"}


def test_registry_registers_external_provider_with_api_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    config = SearchConfig()
    config.providers = ["tavily"]
    registry = build_registry(config)
    assert set(registry) == {"tavily"}
    assert registry["tavily"].name == "tavily"


def test_registry_ignores_unknown_provider_names(monkeypatch):
    config = SearchConfig()
    config.providers = ["open_websearch", "not_a_provider"]
    assert set(build_registry(config)) == {"open_websearch"}


# ---------- 卡 03：共用 HTTP 客户端 ----------


def _client_with_status(status: int) -> JsonApiClient:
    return JsonApiClient(
        "probe",
        "https://api.example.com",
        client_factory=lambda: mock_factory(
            lambda request: httpx.Response(status, json={"secret": "x"})
        ),
    )


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ProviderErrorKind.AUTH),
        (403, ProviderErrorKind.AUTH),
        (402, ProviderErrorKind.QUOTA),
        (429, ProviderErrorKind.RATE_LIMIT),
        (500, ProviderErrorKind.UNAVAILABLE),
        # 非 2xx 一律失败：400/404/408/422 与 3xx 重定向不得当成功响应返回。
        (308, ProviderErrorKind.BAD_RESPONSE),
        (400, ProviderErrorKind.BAD_RESPONSE),
        (404, ProviderErrorKind.BAD_RESPONSE),
        (408, ProviderErrorKind.BAD_RESPONSE),
        (422, ProviderErrorKind.BAD_RESPONSE),
        (418, ProviderErrorKind.BAD_RESPONSE),
    ],
)
def test_status_codes_map_to_failure_kinds(status, kind):
    with pytest.raises(ProviderError) as caught:
        _client_with_status(status).post_json("/x", {}, {})
    assert caught.value.kind == kind
    # 不把上游响应体带进异常（可能包含不该进会话的内容）。
    assert "secret" not in caught.value.detail


@pytest.mark.parametrize("status", [200, 201, 204])
def test_2xx_status_returned_as_success(status):
    response = _client_with_status(status).post_json("/x", {}, {})
    assert response.status_code == status


def test_api_key_never_appears_in_error_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client = JsonApiClient(
        "probe", "https://api.example.com", client_factory=lambda: mock_factory(handler)
    )
    with pytest.raises(ProviderError) as caught:
        client.post_json("/x", {}, {"Authorization": f"Bearer {KEY}"})
    assert KEY not in str(caught.value)
    assert KEY not in caught.value.detail


def test_network_and_timeout_errors_are_distinguished():
    def failing_factory():
        return mock_factory(lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom")))

    client = JsonApiClient("probe", "https://api.example.com", client_factory=failing_factory)
    with pytest.raises(ProviderError) as caught:
        client.post_json("/x", {}, {})
    assert caught.value.kind == ProviderErrorKind.NETWORK

    def timeout_factory():
        return mock_factory(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))

    client = JsonApiClient("probe", "https://api.example.com", client_factory=timeout_factory)
    with pytest.raises(ProviderError) as caught:
        client.post_json("/x", {}, {})
    assert caught.value.kind == ProviderErrorKind.TIMEOUT


def test_malformed_json_is_bad_response():
    client = JsonApiClient(
        "probe",
        "https://api.example.com",
        client_factory=lambda: mock_factory(lambda request: httpx.Response(200, text="not-json")),
    )
    with pytest.raises(ProviderError) as caught:
        client.parse_json(client.post_json("/x", {}, {}))
    assert caught.value.kind == ProviderErrorKind.BAD_RESPONSE


def test_effective_timeout_takes_minimum():
    client = JsonApiClient("probe", "https://api.example.com", timeout=10.0)
    assert client.effective_timeout(None) is None
    assert client.effective_timeout(25.0) == 10.0
    assert client.effective_timeout(2.0) == 2.0


# ---------- 卡 04：百度 Search ----------


def baidu_payload(references):
    return {"request_id": "rid", "references": references}


def test_baidu_maps_web_references_only():
    sent, requests = capture(
        lambda request: httpx.Response(
            200,
            json=baidu_payload(
                [
                    {"type": "web", "title": "Tibo 动态", "url": "https://example.com/a",
                     "content": "相关内容", "date": "2026-10-30"},
                    {"type": "video", "title": "视频", "url": "https://example.com/v", "content": "x"},
                    {"title": "无类型", "url": "https://example.com/n", "content": "y"},
                ]
            ),
        )
    )
    provider = make(BaiduSearchProvider, sent)
    results = provider.search(SearchQuery("Tibo 最新消息", limit=10))
    assert [item.url for item in results] == ["https://example.com/a", "https://example.com/n"]
    assert results[0].snippet == "相关内容"
    assert results[0].published == "2026-10-30"
    assert results[0].provider == "baidu_search"
    request = requests[0]
    body = json.loads(request.content)
    assert body["messages"] == [{"role": "user", "content": "Tibo 最新消息"}]
    assert body["edition"] == "standard"
    assert body["search_source"] == "baidu_search_v2"
    # 默认不带时间过滤，不悄悄缩小召回。
    assert "search_recency_filter" not in body
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert "x-appbuilder-authorization" not in request.headers


def test_baidu_alternative_auth_header_is_exclusive():
    sent, requests = capture(lambda request: httpx.Response(200, json=baidu_payload([])))
    provider = make(BaiduSearchProvider, sent, auth_header="x-appbuilder-authorization")
    provider.search(SearchQuery("q"))
    request = requests[0]
    assert request.headers["x-appbuilder-authorization"] == f"Bearer {KEY}"
    assert "authorization" not in request.headers


def test_baidu_business_error_in_http_200_is_auth_failure():
    sent, _ = capture(
        lambda request: httpx.Response(
            200,
            json={
                "requestId": "rid",
                "code": 216003,
                "message": "Authentication error: InvalidHTTPAuthHeader",
            },
        )
    )
    provider = make(BaiduSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.AUTH
    assert KEY not in caught.value.detail


def test_baidu_missing_references_is_empty_not_error():
    sent, _ = capture(lambda request: httpx.Response(200, json={"request_id": "rid"}))
    provider = make(BaiduSearchProvider, sent)
    assert provider.search(SearchQuery("q")) == []


def test_baidu_non_json_response_is_bad_response():
    sent, _ = capture(lambda request: httpx.Response(200, text="gateway html"))
    provider = make(BaiduSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.BAD_RESPONSE


def test_baidu_401_maps_to_auth():
    sent, _ = capture(lambda request: httpx.Response(401, json={}))
    provider = make(BaiduSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.AUTH


# ---------- 卡 05：Tavily Search ----------


def test_tavily_request_shape_and_result_mapping():
    sent, requests = capture(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Tibo latest updates", "url": "https://example.com/a",
                     "content": "announcement body", "score": 0.9,
                     "published_date": "2026-10-29"},
                    {"url": "https://example.com/b"},
                ]
            },
        )
    )
    provider = make(TavilySearchProvider, sent)
    results = provider.search(SearchQuery("Tibo latest updates", limit=10))
    assert [item.url for item in results] == ["https://example.com/a", "https://example.com/b"]
    assert results[0].snippet == "announcement body"
    assert results[0].published == "2026-10-29"
    assert results[0].provider == "tavily"
    body = json.loads(requests[0].content)
    # 免费优先：Basic 深度 + 不自动升级 + 关闭一切附加计费项。
    assert body == {
        "query": "Tibo latest updates",
        "search_depth": "basic",
        "max_results": 10,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "auto_parameters": False,
        "include_usage": True,
    }
    assert requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_tavily_402_and_429_map_to_quota_and_rate_limit():
    sent, _ = capture(lambda request: httpx.Response(402, json={}))
    provider = make(TavilySearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.QUOTA

    sent, _ = capture(lambda request: httpx.Response(429, json={}))
    provider = make(TavilySearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.RATE_LIMIT


def test_tavily_missing_results_is_empty():
    sent, _ = capture(lambda request: httpx.Response(200, json={"response_time": 0.1}))
    provider = make(TavilySearchProvider, sent)
    assert provider.search(SearchQuery("q")) == []


# ---------- 卡 06：Exa Search（固定 auto） ----------


def test_exa_fixed_auto_request_and_snippet_fallback():
    sent, requests = capture(
        lambda request: httpx.Response(
            200,
            json={
                "requestId": "rid",
                "results": [
                    {"title": "Exa result", "url": "https://example.com/a",
                     "publishedDate": "2026-10-01", "summary": "总结内容"},
                    {"title": "No summary", "url": "https://example.com/b",
                     "highlights": ["高亮片段", "another"]},
                    {"title": "Text only", "url": "https://example.com/c",
                     "text": "x" * 600},
                    {"title": "No snippet", "url": "https://example.com/d"},
                ],
            },
        )
    )
    provider = make(ExaSearchProvider, sent)
    results = provider.search(SearchQuery("Tibo latest updates", limit=10))
    assert [item.snippet for item in results] == [
        "总结内容",
        "高亮片段",
        "x" * 500,  # text 截断到 500，不把整页灌进摘要
        "",
    ]
    assert results[0].published == "2026-10-01"
    body = json.loads(requests[0].content)
    assert body == {"query": "Tibo latest updates", "type": "auto", "numResults": 10}
    assert requests[0].headers["x-api-key"] == KEY


def test_exa_does_not_send_contents_or_crawl_parameters():
    sent, requests = capture(lambda request: httpx.Response(200, json={"results": []}))
    provider = make(ExaSearchProvider, sent)
    provider.search(SearchQuery("q"))
    body = json.loads(requests[0].content)
    # 隐式正文抓取是额外计费项：请求体里不允许出现 contents/livecrawl 类参数。
    assert set(body) == {"query", "type", "numResults"}


def test_exa_403_maps_to_auth():
    sent, _ = capture(lambda request: httpx.Response(403, json={}))
    provider = make(ExaSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.AUTH


# ---------- 卡 07：You.com Search ----------


def test_you_request_header_and_web_mapping():
    sent, requests = capture(
        lambda request: httpx.Response(
            200,
            json={
                "results": {
                    "web": [
                        {"title": "Tibo news", "url": "https://example.com/a",
                         "description": "full description"},
                        {"title": "Snippet only", "url": "https://example.com/b",
                         "snippets": ["first snippet", "second"]},
                        {"title": "No text", "url": "https://example.com/c"},
                    ]
                }
            },
        )
    )
    provider = make(YouSearchProvider, sent)
    results = provider.search(SearchQuery("Tibo latest news", limit=10))
    assert [item.snippet for item in results] == ["full description", "first snippet", ""]
    # 日期字段未经真实响应核实：published 保持未知，不猜。
    assert all(item.published is None for item in results)
    assert all(item.provider == "you" for item in results)
    request = requests[0]
    assert request.headers["x-api-key"] == KEY
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    # 不发送 livecrawl/extraction_mode 等会产生抓取计费的参数。
    assert body == {"query": "Tibo latest news", "count": 10}


def test_you_missing_web_section_is_empty():
    sent, _ = capture(lambda request: httpx.Response(200, json={"results": {}}))
    provider = make(YouSearchProvider, sent)
    assert provider.search(SearchQuery("q")) == []


def test_you_401_maps_to_auth():
    sent, _ = capture(lambda request: httpx.Response(401, json={"message": "Missing Authentication Token"}))
    provider = make(YouSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.AUTH


# ---------- 卡 08：火山引擎 Global / Custom ----------


def volc_payload(documents):
    return {
        "ResponseMetadata": {"RequestId": "rid", "Action": "", "Version": "", "Service": "", "Region": ""},
        "Result": {"Documents": documents, "TotalDocCount": len(documents), "ErrorCode": "", "ErrorMsg": ""},
    }


def test_volcengine_global_request_and_document_mapping():
    sent, requests = capture(
        lambda request: httpx.Response(
            200,
            json=volc_payload(
                [
                    {"Rank": 0, "Url": "https://example.com/a", "Title": "Tibo latest",
                     "Snippet": [{"Type": "image", "Text": None}, {"Type": "text", "Text": "announcement"}],
                     "DocumentInfo": {"PublishTime": "2026-10-30", "Filetype": "webpage"},
                     "HostInfo": {"Hostname": "example.com"}},
                    {"Url": "https://example.com/b", "Title": "No snippet"},
                ]
            ),
        )
    )
    provider = make(VolcengineSearchProvider, sent, mode="global")
    results = provider.search(SearchQuery("Tibo latest updates", limit=10))
    assert [item.url for item in results] == ["https://example.com/a", "https://example.com/b"]
    assert results[0].snippet == "announcement"
    assert results[0].published == "2026-10-30"
    assert results[0].provider == "volcengine"
    request = requests[0]
    assert request.url.path == "/search_api/global_search"
    body = json.loads(request.content)
    assert body["Query"] == "Tibo latest updates"
    assert body["SearchType"] == "web"
    assert body["DocCount"] == 10
    assert request.headers["authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("mode", ["global", "custom"])
def test_volcengine_over_limit_query_rejected_not_truncated(mode):
    # 官方 Query 上限 100 字符；超限显式报错，不静默截断改写查询语义。
    sent, requests = capture(lambda request: httpx.Response(200, json=volc_payload([])))
    provider = make(VolcengineSearchProvider, sent, mode=mode)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("x" * 101))
    assert caught.value.kind == ProviderErrorKind.BAD_RESPONSE
    assert "100" in caught.value.detail
    assert requests == []


def test_volcengine_query_exactly_100_chars_is_sent():
    sent, requests = capture(lambda request: httpx.Response(200, json=volc_payload([])))
    provider = make(VolcengineSearchProvider, sent, mode="global")
    provider.search(SearchQuery("x" * 100))
    assert len(json.loads(requests[0].content)["Query"]) == 100


def test_volcengine_custom_request_shape():
    sent, requests = capture(lambda request: httpx.Response(200, json=volc_payload([])))
    provider = make(VolcengineSearchProvider, sent, mode="custom")
    provider.search(SearchQuery("q", limit=10))
    request = requests[0]
    assert request.url.path == "/search_api/web_search"
    body = json.loads(request.content)
    assert body["Count"] == 10
    assert body["Filter"] == {"NeedContent": True, "NeedUrl": True}
    assert body["QueryControl"] == {"QueryRewrite": False}


def test_volcengine_metadata_error_maps_to_auth():
    sent, _ = capture(
        lambda request: httpx.Response(
            200,
            json={
                "ResponseMetadata": {"Error": {"Code": "InvalidAuthorization", "Message": "auth failed"}},
                "Result": None,
            },
        )
    )
    provider = make(VolcengineSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.AUTH


def test_volcengine_null_result_without_error_is_empty():
    sent, _ = capture(
        lambda request: httpx.Response(
            200, json={"ResponseMetadata": {"RequestId": "rid"}, "Result": None}
        )
    )
    provider = make(VolcengineSearchProvider, sent)
    assert provider.search(SearchQuery("q")) == []


def test_volcengine_5xx_maps_to_unavailable():
    sent, _ = capture(lambda request: httpx.Response(502, json={}))
    provider = make(VolcengineSearchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.search(SearchQuery("q"))
    assert caught.value.kind == ProviderErrorKind.UNAVAILABLE


# ---------- 卡 10A：Tavily Extract ----------


def test_tavily_fetch_request_and_mapping():
    sent, requests = capture(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.com/a", "raw_content": "正文内容" * 10}
                ],
                "failed_results": [],
            },
        )
    )
    provider = make(TavilyFetchProvider, sent)
    result = provider.fetch(FetchQuery("https://example.com/a", 4000))

    request = requests[0]
    assert request.url.path == "/extract"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {"urls": ["https://example.com/a"]}
    assert result.url == "https://example.com/a"
    assert result.final_url == "https://example.com/a"
    assert result.content == "正文内容" * 10
    assert result.truncated is False


def test_tavily_fetch_truncates_to_max_chars():
    sent, _ = capture(
        lambda request: httpx.Response(
            200,
            json={"results": [{"url": "https://example.com/a", "raw_content": "x" * 500}]},
        )
    )
    provider = make(TavilyFetchProvider, sent)
    result = provider.fetch(FetchQuery("https://example.com/a", 100))
    assert result.content == "x" * 100
    assert result.truncated is True


def test_tavily_fetch_empty_results_reports_failed_reason():
    sent, _ = capture(
        lambda request: httpx.Response(
            200,
            json={
                "results": [],
                "failed_results": [{"url": "https://example.com/a", "error": "blocked"}],
            },
        )
    )
    provider = make(TavilyFetchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.fetch(FetchQuery("https://example.com/a", 4000))
    assert caught.value.kind == ProviderErrorKind.BAD_RESPONSE
    assert "blocked" in caught.value.detail
    assert KEY not in caught.value.detail


def test_tavily_fetch_missing_raw_content_is_error():
    sent, _ = capture(
        lambda request: httpx.Response(
            200, json={"results": [{"url": "https://example.com/a", "raw_content": ""}]}
        )
    )
    provider = make(TavilyFetchProvider, sent)
    with pytest.raises(ProviderError, match="未返回正文"):
        provider.fetch(FetchQuery("https://example.com/a", 4000))


def test_tavily_fetch_429_maps_to_rate_limit():
    sent, _ = capture(lambda request: httpx.Response(429, json={}))
    provider = make(TavilyFetchProvider, sent)
    with pytest.raises(ProviderError) as caught:
        provider.fetch(FetchQuery("https://example.com/a", 4000))
    assert caught.value.kind == ProviderErrorKind.RATE_LIMIT


def test_fetch_registry_registers_tavily_with_api_key(monkeypatch):
    from quiet_memo.infra.config import FetchConfig
    from quiet_memo.infra.search.fetch_router import build_fetch_registry

    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    registry = build_fetch_registry(FetchConfig(providers=["tavily"]), SearchConfig())
    assert "tavily" in registry
    assert "fetch" in registry["tavily"].capabilities

    monkeypatch.delenv("TAVILY_API_KEY")
    registry = build_fetch_registry(FetchConfig(providers=["tavily"]), SearchConfig())
    assert "tavily" not in registry
