"""网页正文抓取的独立路由：与搜索的 provider 顺序相互独立。

抓取入口统一做公网 URL 校验（输入与重定向后的 final_url），provider
只负责自身 API 的请求与映射；失败按序回退，全部失败时汇总诊断。
"""
from __future__ import annotations

import os
from typing import Protocol

from ..config import FetchConfig, SearchConfig
from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    SearchFailure,
    assert_public_fetch_url,
)
from .open_websearch import OpenWebSearchProvider

# 需要厂商 API Key 的外部抓取服务；列进 fetch.providers 且 Key 可读才注册。
_EXTERNAL_FETCH_PROVIDERS: dict[str, tuple[str, str]] = {
    "tavily": ("tavily.py:TavilyFetchProvider", "TAVILY_API_KEY"),
    "exa": ("exa.py:ExaFetchProvider", "EXA_API_KEY"),
}


class FetchProvider(Protocol):
    name: str
    capabilities: set[str]

    def fetch(self, query: FetchQuery) -> FetchResult: ...


class FetchRouter:
    """按配置顺序尝试支持 fetch 的 provider；失败降级，错误统一标记为 fetch。"""

    def __init__(
        self,
        registry: dict[str, FetchProvider] | None = None,
        *,
        order: list[str] | None = None,
    ):
        self.registry = dict(registry or {})
        self.order = list(dict.fromkeys(order or list(self.registry)))

    @property
    def supports_fetch(self) -> bool:
        return any(
            "fetch" in provider.capabilities for provider in self.registry.values()
        )

    def fetch(self, url: str, max_chars: int = 4000) -> FetchResult:
        text = url.strip()
        if not text:
            raise ValueError("抓取地址为空")
        assert_public_fetch_url(text)
        failures: list[SearchFailure] = []
        for name in self.order:
            provider = self.registry.get(name)
            if provider is None or "fetch" not in provider.capabilities:
                continue
            try:
                result = provider.fetch(FetchQuery(text, max_chars))
            except ProviderError as exc:
                failures.append(SearchFailure.from_error(exc))
                continue
            except Exception as exc:
                failures.append(SearchFailure(name, "unavailable", str(exc)))
                continue
            if result.final_url and result.final_url != text:
                # 上游已逐跳校验；这里对最终地址再拦一次私网回落。
                assert_public_fetch_url(result.final_url, "重定向后的抓取地址")
            return result
        if failures:
            detail = "；".join(f"{item.provider}: {item.detail}" for item in failures)
            # 这里失败的是抓取而不是搜索；provider 标明 fetch，避免上层误判。
            raise ProviderError("fetch", "unavailable", detail)
        raise ProviderError("fetch", "unavailable", "没有支持网页抓取的 provider")


def build_fetch_registry(
    fetch_config: FetchConfig, search_config: SearchConfig
) -> dict[str, FetchProvider]:
    registry: dict[str, FetchProvider] = {}
    for name in fetch_config.providers:
        if name == "open_websearch":
            # 与搜索共用同一 daemon 配置；抓取能力只在 http transport 下可用。
            registry[name] = OpenWebSearchProvider(
                base_url=search_config.base_url,
                timeout=search_config.timeout,
                transport=search_config.transport,
                engines=search_config.engines,
                fetch_timeout=search_config.fetch_timeout,
            )
        elif name in _EXTERNAL_FETCH_PROVIDERS:
            env_name = _EXTERNAL_FETCH_PROVIDERS[name][1]
            api_key = os.environ.get(env_name, "").strip()
            if not api_key:
                # 未配置 Key 的厂商直接跳过：不参与尝试，也不报错。
                continue
            registry[name] = _build_external_fetch_provider(name, api_key, search_config.timeout)
    return registry


def _build_external_fetch_provider(
    name: str, api_key: str, timeout: float
) -> FetchProvider:
    # 延迟导入：未配置 Key 时不必加载这些模块。
    if name == "tavily":
        from .tavily import TavilyFetchProvider

        return TavilyFetchProvider(api_key, timeout=timeout)
    if name == "exa":
        from .exa import ExaFetchProvider

        return ExaFetchProvider(api_key, timeout=timeout)
    if name == "you":
        from .you import YouFetchProvider

        return YouFetchProvider(api_key, timeout=timeout)
    if name == "volcengine":
        from .volcengine import VolcengineFetchProvider

        return VolcengineFetchProvider(api_key, timeout=timeout)
    raise ValueError(f"未知的外部抓取 provider: {name}")


def build_fetch_router(
    fetch_config: FetchConfig, search_config: SearchConfig
) -> FetchRouter:
    return FetchRouter(
        build_fetch_registry(fetch_config, search_config),
        order=fetch_config.providers,
    )
