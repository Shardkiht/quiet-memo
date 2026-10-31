"""多厂商搜索 API 共用 JSON 客户端：固定官方主机、错误分级、Key 不外泄。

各厂商适配器只负责构造自己的鉴权 Header 与请求/响应映射；HTTP 层的
超时、状态码分级、网络异常归一在这里统一处理。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import ProviderError, ProviderErrorKind


def _status_kind(status: int) -> ProviderErrorKind | None:
    # 非 2xx 一律失败：400/404/422 等也必须抛 ProviderError，
    # 绝不能把错误响应当成功返回给调用方。
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        # 403 兼作 Scope/套餐不足；与鉴权一样不做快速重试。
        return ProviderErrorKind.AUTH
    if status == 402:
        return ProviderErrorKind.QUOTA
    if status == 429:
        return ProviderErrorKind.RATE_LIMIT
    if status >= 500:
        return ProviderErrorKind.UNAVAILABLE
    # 3xx 重定向与 400/404/408/422 等其余状态：按坏响应处理，只报状态码。
    return ProviderErrorKind.BAD_RESPONSE


class JsonApiClient:
    """同步 JSON POST 客户端；client_factory 可注入 MockTransport 测试。"""

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        timeout: float = 10.0,
        client_factory: Any = None,
    ):
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=timeout)
        )

    def effective_timeout(self, query_timeout: float | None) -> float | None:
        """单次请求超时取配置与 router 下发剩余预算的较小值。"""
        if query_timeout is None:
            return None
        return max(min(self.timeout, query_timeout), 0.05)

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            with self._client_factory() as client:
                response = client.post(
                    f"{self.base_url}{path}",
                    json=body,
                    headers=headers,
                    timeout=timeout or self.timeout,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(self.provider_name, ProviderErrorKind.TIMEOUT, str(exc)) from exc
        except httpx.RequestError as exc:
            raise ProviderError(self.provider_name, ProviderErrorKind.NETWORK, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(self.provider_name, ProviderErrorKind.NETWORK, str(exc)) from exc
        kind = _status_kind(response.status_code)
        if kind is not None:
            # 只回状态码，不回响应体：错误页可能包含不该进会话的内容。
            raise ProviderError(self.provider_name, kind, f"HTTP {response.status_code}")
        return response

    def parse_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderError(
                self.provider_name, ProviderErrorKind.BAD_RESPONSE, "响应不是合法 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                self.provider_name, ProviderErrorKind.BAD_RESPONSE, "响应顶层不是 JSON 对象"
            )
        return payload
