"""Tavily Search API 适配器（Basic 深度，默认关闭一切附加计费项）。

auto_parameters=false：不让上游自动升级到 Advanced 消耗更多 Credit；
include_answer/raw_content/images 均关闭，只取网页结果。
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from .base import ProviderError, ProviderErrorKind, SearchQuery, SearchResult
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
