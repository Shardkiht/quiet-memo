"""Exa Search API 适配器（type=auto，默认不请求正文等附加计费内容）。

响应无顶层 score；摘要只能来自 summary/highlights/text 中实际返回的
字段，缺什么就不硬造。
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from .base import ProviderError, ProviderErrorKind, SearchQuery, SearchResult
from .http_client import JsonApiClient

NAME = "exa"
DEFAULT_BASE_URL = "https://api.exa.ai"
_SNIPPET_MAX_CHARS = 500


class ExaSearchProvider:
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
        # 产品决策固定 type=auto：不实现 Lite/Deep 模式切换，也不隐式请求正文。
        body = {
            "query": query.query,
            "type": "auto",
            "numResults": max(1, min(query.limit, 100)),
        }
        headers = {"x-api-key": self._api_key}
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
    published = item.get("publishedDate")
    return SearchResult(
        title=str(item.get("title") or "").strip() or url,
        url=url,
        snippet=_snippet(item),
        published=str(published).strip() if published else None,
        provider=NAME,
    )


def _snippet(item: dict[str, Any]) -> str:
    """summary → highlights[0] → text 截断；全缺就空，不编造。"""
    summary = str(item.get("summary") or "").strip()
    if summary:
        return summary[:_SNIPPET_MAX_CHARS]
    highlights = item.get("highlights")
    if isinstance(highlights, list) and highlights:
        first = str(highlights[0] or "").strip()
        if first:
            return first[:_SNIPPET_MAX_CHARS]
    text = str(item.get("text") or "").strip()
    return text[:_SNIPPET_MAX_CHARS]
