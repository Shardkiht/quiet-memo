"""百度 AppBuilder AI 搜索（baidu_search）适配器。

与 open-websearch 的 baidu 上游引擎是两层概念：本 provider 走官方
qianfan 接口，name 固定 `baidu_search`，不得与引擎名混用。
响应形状按 2026-10 官方文档：references[] 含 type/title/url/content/date。
"""
from __future__ import annotations

from typing import Any, Callable

import httpx

from .base import ProviderError, ProviderErrorKind, SearchQuery, SearchResult
from .http_client import JsonApiClient

NAME = "baidu_search"
DEFAULT_BASE_URL = "https://qianfan.baidubce.com"


def _business_kind(code: Any, message: str) -> ProviderErrorKind:
    text = f"{code} {message}".lower()
    if "auth" in text or "鉴权" in text:
        return ProviderErrorKind.AUTH
    if "quota" in text or "余额" in text or "限流" in text or "限频" in text:
        return ProviderErrorKind.QUOTA
    return ProviderErrorKind.BAD_RESPONSE


class BaiduSearchProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        api_key: str,
        timeout: float = 10.0,
        base_url: str = DEFAULT_BASE_URL,
        edition: str = "standard",
        auth_header: str = "authorization",
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self._client = JsonApiClient(NAME, base_url, timeout, client_factory)
        self._api_key = api_key
        self._edition = edition if edition in ("standard", "lite") else "standard"
        # 官方文档写 Authorization，同一页面的 cURL 示例却写
        # X-Appbuilder-Authorization；两种官方写法都可配置，真实冒烟后定夺。
        self._auth_header = (
            "x-appbuilder-authorization" if auth_header == "x-appbuilder-authorization" else "authorization"
        )

    def search(self, query: SearchQuery) -> list[SearchResult]:
        body = {
            # messages 长度须为奇数且以 user 开头结尾；单轮即一条 user。
            "messages": [{"role": "user", "content": query.query}],
            "edition": self._edition,
            "search_source": "baidu_search_v2",
        }
        headers = {self._auth_header: f"Bearer {self._api_key}"}
        response = self._client.post_json(
            "/v2/ai_search/web_search",
            body,
            headers,
            timeout=self._client.effective_timeout(query.timeout),
        )
        payload = self._client.parse_json(response)
        # HTTP 200 里也可能带业务错误（如 216003 鉴权失败），不能当真空结果。
        code = payload.get("code")
        if code is not None:
            message = str(payload.get("message") or "")
            raise ProviderError(NAME, _business_kind(code, message), f"code={code}: {message}")
        references = payload.get("references")
        if references is None:
            return []
        if not isinstance(references, list):
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, "references 字段类型异常"
            )
        return [item for item in map(_map_reference, references) if item is not None]


def _map_reference(item: Any) -> SearchResult | None:
    if not isinstance(item, dict):
        return None
    # 只要网页类型；video/image/aladdin 不是通用网页结果。
    if item.get("type") not in (None, "web"):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    content = str(item.get("content") or "").strip()
    return SearchResult(
        title=str(item.get("title") or "").strip() or url,
        url=url,
        snippet=content,
        published=str(item.get("date") or "").strip() or None,
        provider=NAME,
    )
