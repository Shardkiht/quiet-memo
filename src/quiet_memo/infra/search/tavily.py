"""Tavily Search API 适配器（Basic 深度，默认关闭一切附加计费项）。

auto_parameters=false：不让上游自动升级到 Advanced 消耗更多 Credit；
include_answer/raw_content/images 均关闭，只取网页结果。
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchQuery,
    SearchResult,
    truncate_fetch_content,
)
from .http_client import JsonApiClient

NAME = "tavily"
DEFAULT_BASE_URL = "https://api.tavily.com"


class TavilySearchProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        base_url: str = DEFAULT_BASE_URL,
        depth: str = "basic",
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key
        self._depth = depth if depth in ("basic", "advanced") else "basic"

    def search(self, query: SearchQuery) -> list[SearchResult]:
        body = {
            "query": query.query,
            "search_depth": self._depth,
            "max_results": max(1, min(query.limit, 20)),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
            "include_usage": True,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = self._client.post_json(
            "/search",
            body,
            headers,
            timeout=self._client.effective_timeout(query.timeout),
        )
        payload = self._client.parse_json(response)
        results = payload.get("results")
        if results is None:
            return []
        if not isinstance(results, list):
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, "results 字段类型异常"
            )
        mapped = [_map_result(item) for item in results]
        return [item for item in mapped if item is not None]


class TavilyFetchProvider:
    """Tavily Extract（Basic 档）：单 URL 抓取，不启用 Advanced 等计费能力。"""

    name = NAME
    capabilities = {"fetch"}

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        base_url: str = DEFAULT_BASE_URL,
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key

    def fetch(self, query: FetchQuery) -> FetchResult:
        # 只传 urls：不带 query/extract_depth/chunks_per_source，保持 Basic 语义。
        body = {"urls": [query.url]}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = self._client.post_json("/extract", body, headers)
        payload = self._client.parse_json(response)
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            # HTTP 200 也可能全部失败：错误原因在 failed_results[].error。
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, _failed_reason(payload)
            )
        item = results[0] if isinstance(results[0], dict) else {}
        content = str(item.get("raw_content") or "").strip()
        if not content:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "未返回正文")
        url = str(item.get("url") or "").strip() or query.url
        text, truncated = truncate_fetch_content(content, query.max_chars)
        # Extract 不返回标题；按惯例以最终 URL 充当。
        return FetchResult(
            url=query.url,
            final_url=url,
            title=url,
            content=text,
            truncated=truncated,
        )


def _failed_reason(payload: dict[str, Any]) -> str:
    failed = payload.get("failed_results")
    if isinstance(failed, list) and failed and isinstance(failed[0], dict):
        reason = str(failed[0].get("error") or "").strip()
        if reason:
            return f"抓取失败：{reason}"
    return "抓取结果为空"


def _map_result(item: Any) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    published = item.get("published_date")
    return SearchResult(
        title=str(item.get("title") or "").strip() or url,
        url=url,
        snippet=str(item.get("content") or "").strip(),
        published=str(published).strip() if published else None,
        provider=NAME,
    )
