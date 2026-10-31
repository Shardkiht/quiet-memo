"""You.com Web Search API 适配器。

鉴权是 X-API-Key 头（不是 Bearer）；端点固定 ydc-index.io，与
api.you.com 的 Answer/Research 入口不是同一服务，不得混用。
响应为 results.web[]（新闻等类型不在最小实现内）。
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from .base import ProviderError, ProviderErrorKind, SearchQuery, SearchResult
from .http_client import JsonApiClient

NAME = "you"
DEFAULT_BASE_URL = "https://ydc-index.io"


class YouSearchProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        base_url: str = DEFAULT_BASE_URL,
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key

    def search(self, query: SearchQuery) -> list[SearchResult]:
        body = {"query": query.query, "count": max(1, min(query.limit, 100))}
        headers = {"X-API-Key": self._api_key}
        response = self._client.post_json(
            "/v1/search",
            body,
            headers,
            timeout=self._client.effective_timeout(query.timeout),
        )
        payload = self._client.parse_json(response)
        results = payload.get("results")
        web = results.get("web") if isinstance(results, dict) else None
        if web is None:
            return []
        if not isinstance(web, list):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "results.web 类型异常")
        mapped = [_map_result(item) for item in web]
        return [item for item in mapped if item is not None]


def _map_result(item: Any) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    # 官方文档只确认 web 结果的 title/url/description/snippets；日期字段
    # 未经真实响应核实（page_age 是新闻类的可选线索），不猜字段、保持未知。
    return SearchResult(
        title=str(item.get("title") or "").strip() or url,
        url=url,
        snippet=_snippet(item),
        published=None,
        provider=NAME,
    )


def _snippet(item: dict[str, Any]) -> str:
    description = str(item.get("description") or "").strip()
    if description:
        return description
    snippets = item.get("snippets")
    if isinstance(snippets, list) and snippets:
        return str(snippets[0] or "").strip()
    return ""
