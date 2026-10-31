"""火山引擎豆包搜索适配器：Global 与 Custom 两种模式。

两个模式的端点、请求字段与响应结构不同，不能共用映射。响应按官方
文档：顶层 ResponseMetadata/Result，结果在 Result.Documents[]，摘要在
Snippet[].Text，日期在 DocumentInfo.PublishTime。Custom 的响应形状
文档描述较少，映射写得偏防御，真实冒烟后固化。
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

NAME = "volcengine"
DEFAULT_BASE_URL = "https://open.feedcoopapi.com"
GLOBAL_PATH = "/search_api/global_search"
CUSTOM_PATH = "/search_api/web_search"
FETCH_PATH = "/search_api/fetch"
# 官方两种模式的 Query 上限都是 1-100 字符；超限不静默截断，显式报错。
QUERY_MAX_CHARS = 100


def _error_from_metadata(payload: dict[str, Any]) -> ProviderError | None:
    metadata = payload.get("ResponseMetadata")
    if not isinstance(metadata, dict):
        return None
    error = metadata.get("Error")
    if not isinstance(error, dict):
        return None
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or "")
    text = f"{code} {message}".lower()
    if "auth" in text or "signature" in text or "token" in text:
        kind = ProviderErrorKind.AUTH
    elif "quota" in text or "limit" in text or "balance" in text:
        kind = ProviderErrorKind.QUOTA
    else:
        kind = ProviderErrorKind.BAD_RESPONSE
    return ProviderError(NAME, kind, f"{code}: {message}")


def _checked_query(text: str) -> str:
    if len(text) > QUERY_MAX_CHARS:
        # 截断会静默改写查询语义；超限直接报给上层，由 router 换下一家。
        raise ProviderError(
            NAME,
            ProviderErrorKind.BAD_RESPONSE,
            f"查询长度 {len(text)} 超过火山引擎 {QUERY_MAX_CHARS} 字符上限",
        )
    return text


def _documents(payload: dict[str, Any]) -> list[Any]:
    result = payload.get("Result")
    if result is None:
        # 失败时 Result 为 null；无 Error 又无结果按业务失败处理。
        error = _error_from_metadata(payload)
        if error is not None:
            raise error
        return []
    documents = result.get("Documents") if isinstance(result, dict) else None
    if documents is None:
        return []
    if not isinstance(documents, list):
        raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "Result.Documents 类型异常")
    return documents


def _map_document(item: Any) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("Url") or "").strip()
    if not url:
        return None
    snippet = ""
    snippets = item.get("Snippet")
    if isinstance(snippets, list):
        for part in snippets:
            if isinstance(part, dict) and part.get("Type") == "text" and part.get("Text"):
                snippet = str(part["Text"]).strip()
                break
    info = item.get("DocumentInfo") if isinstance(item.get("DocumentInfo"), dict) else {}
    published = str(info.get("PublishTime") or "").strip() or None
    return SearchResult(
        title=str(item.get("Title") or "").strip() or url,
        url=url,
        snippet=snippet,
        published=published,
        provider=NAME,
    )


class VolcengineSearchProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        base_url: str = DEFAULT_BASE_URL,
        mode: str = "global",
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key
        self._mode = mode if mode in ("global", "custom") else "global"

    def search(self, query: SearchQuery) -> list[SearchResult]:
        count = max(1, min(query.limit, 20))
        if self._mode == "global":
            path, body = self._global_request(query.query, count)
        else:
            path, body = self._custom_request(query.query, count)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = self._client.post_json(
            path, body, headers, timeout=self._client.effective_timeout(query.timeout)
        )
        payload = self._client.parse_json(response)
        error = _error_from_metadata(payload)
        if error is not None:
            raise error
        mapped = [_map_document(item) for item in _documents(payload)]
        return [item for item in mapped if item is not None]

    def _global_request(self, text: str, count: int) -> tuple[str, dict[str, Any]]:
        return GLOBAL_PATH, {
            "Query": _checked_query(text),
            "SearchType": "web",
            "DocCount": count,
            "MaxSnippetLength": 1000,
        }

    def _custom_request(self, text: str, count: int) -> tuple[str, dict[str, Any]]:
        # NeedContent=true：搜索结果需要 snippet 供相关性门控使用；
        # 方案示例里的 false 会拿不到正文摘要，实现上按搜索需求取内容。
        return CUSTOM_PATH, {
            "Query": _checked_query(text),
            "SearchType": "web",
            "Count": count,
            "Filter": {"NeedContent": True, "NeedUrl": True},
            "QueryControl": {"QueryRewrite": False},
        }


class VolcengineFetchProvider:
    """火山引擎网页抓取（/search_api/fetch）：单 URL 抓正文。

    官方公开文档未索引到该端点的字段说明；请求/响应按豆包搜索 API 的
    同族约定（PascalCase 字段、ResponseMetadata.Error、Result 承载
    数据）实现，Title/Content 字段名待真实冒烟确认后固化。
    """

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
        body = {"Url": query.url}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        response = self._client.post_json(FETCH_PATH, body, headers)
        payload = self._client.parse_json(response)
        error = _error_from_metadata(payload)
        if error is not None:
            raise error
        result = payload.get("Result")
        if not isinstance(result, dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "Result 字段缺失")
        content = str(result.get("Content") or "").strip()
        if not content:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "未返回正文")
        url = str(result.get("Url") or "").strip() or query.url
        text, truncated = truncate_fetch_content(content, query.max_chars)
        return FetchResult(
            url=query.url,
            final_url=url,
            title=str(result.get("Title") or "").strip() or url,
            content=text,
            truncated=truncated,
        )
