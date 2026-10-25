from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from ..infra.config import LLMConfig


class ChatModelError(RuntimeError):
    pass


_MODEL_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def _litellm():
    import os

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    import litellm

    litellm.suppress_debug_info = True
    litellm.telemetry = False
    return litellm


def complete(
    config: LLMConfig,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
):
    """统一的 LiteLLM 调用入口；provider/base_url/retry/fallback 都由配置决定。"""
    litellm = _litellm()
    kwargs: dict = {
        "model": config.litellm_model(),
        "messages": messages,
        "api_key": config.api_key(),
        "timeout": config.timeout,
        "num_retries": config.num_retries,
        "temperature": config.temperature if temperature is None else temperature,
    }
    if config.base_url:
        kwargs["api_base"] = config.base_url
    if config.reasoning_effort:
        kwargs["reasoning_effort"] = config.reasoning_effort
    if tools:
        kwargs["tools"] = tools
    if response_format:
        kwargs["response_format"] = response_format
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if config.fallbacks:
        kwargs["fallbacks"] = config.fallbacks
    return litellm.completion(**kwargs)


def embed_texts(config: LLMConfig, texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """调用 embedding 接口；返回顺序与输入一致。"""
    litellm = _litellm()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        kwargs: dict = {
            "model": config.litellm_model(),
            "input": batch,
            "api_key": config.api_key(),
            "timeout": config.timeout,
            "num_retries": config.num_retries,
        }
        if config.base_url:
            kwargs["api_base"] = config.base_url
        response = litellm.embedding(**kwargs)
        items = _field(response, "data") or []
        vectors.extend(
            [float(value) for value in _field(item, "embedding") or []] for item in items
        )
    return vectors


class LiteLLMEmbedder:
    """API 后端 Embedder；满足 retrieval.Embedder 协议，不反向依赖 retrieval。"""

    def __init__(self, config: LLMConfig, batch_size: int = 32):
        self.config = config
        self.batch_size = batch_size

    @property
    def model_id(self) -> str:
        return self.config.litellm_model()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return embed_texts(self.config, list(texts), batch_size=self.batch_size)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@lru_cache(maxsize=128)
def model_metadata(model: str) -> dict:
    """读取 LiteLLM 本地模型元数据；未知模型返回空字典。"""
    litellm = _litellm()
    try:
        return dict(litellm.get_model_info(model))
    except Exception:
        return {}


@lru_cache(maxsize=128)
def supported_params(model: str) -> tuple[str, ...]:
    """读取 LiteLLM 认为该模型支持的 OpenAI 参数。"""
    litellm = _litellm()
    try:
        return tuple(litellm.get_supported_openai_params(model=model) or ())
    except Exception:
        return ()


def fetch_provider_models(
    config: LLMConfig,
    timeout: float = 5.0,
    ttl: float = 12 * 3600,
    refresh: bool = False,
) -> list[str]:
    """向 OpenAI 兼容端点请求真实模型列表；带进程内 12 小时 TTL 缓存。"""
    key = (config.provider, config.base_url)
    now = time.monotonic()
    cached = _MODEL_CACHE.get(key)
    if cached and not refresh:
        max_age = ttl if cached[1] else 30.0
        if now - cached[0] < max_age:
            return list(cached[1])

    models = _fetch_provider_models_uncached(config, timeout)
    _MODEL_CACHE[key] = (now, models)
    return models


def clear_model_cache() -> None:
    _MODEL_CACHE.clear()


def _fetch_provider_models_uncached(config: LLMConfig, timeout: float) -> list[str]:
    api_key = config.api_key()
    if not api_key or not config.base_url:
        return []
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url=config.base_url, timeout=timeout)
        return [model.id for model in client.models.list().data]
    except Exception:
        return []


def _usage(response, litellm) -> Usage:
    usage = _field(response, "usage")
    if usage is None:
        return Usage()
    cost = 0.0
    try:
        cost = float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        cost = 0.0
    return Usage(
        prompt_tokens=int(_field(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(_field(usage, "completion_tokens", 0) or 0),
        cost_usd=cost,
    )


def _tool_calls(message) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, call in enumerate(_field(message, "tool_calls", None) or []):
        function = _field(call, "function")
        raw_arguments = _field(function, "arguments", "{}")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            arguments = {}
        calls.append(
            ToolCall(
                id=_field(call, "id") or f"call-{index}",
                name=_field(function, "name", ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn: ...


class LiteLLMChatModel:
    def __init__(self, config: LLMConfig):
        self.config = config

    def chat(self, messages: list[dict], tools: list[dict], on_delta=None) -> AssistantTurn:
        if not self.config.api_key():
            raise ChatModelError(
                f"未配置 LLM API Key，请设置环境变量 {self.config.api_key_env}，"
                "或运行 qm setup 保存到系统凭据。"
            )
        litellm = _litellm()
        if on_delta is None:
            response = complete(self.config, messages, tools=tools or None)
            message = response.choices[0].message
            return AssistantTurn(
                content=_field(message, "content") or "",
                tool_calls=_tool_calls(message),
                usage=_usage(response, litellm),
            )
        return self._chat_stream(messages, tools or None, on_delta, litellm)

    def _chat_stream(self, messages, tools, on_delta, litellm) -> AssistantTurn:
        """流式：文本 delta 实时回调，tool_calls 增量累积，结束时返回聚合结果。"""
        kwargs: dict = {
            "model": self.config.litellm_model(),
            "messages": messages,
            "api_key": self.config.api_key(),
            "timeout": self.config.timeout,
            "num_retries": self.config.num_retries,
            "temperature": self.config.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        if self.config.reasoning_effort:
            kwargs["reasoning_effort"] = self.config.reasoning_effort
        if tools:
            kwargs["tools"] = tools
        if self.config.fallbacks:
            kwargs["fallbacks"] = self.config.fallbacks

        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage = Usage()
        stream = litellm.completion(**kwargs)
        for chunk in stream:
            chunk_usage = _field(chunk, "usage")
            if chunk_usage is not None:
                try:
                    cost = float(litellm.completion_cost(completion_response=chunk) or 0.0)
                except Exception:
                    cost = 0.0
                usage = Usage(
                    prompt_tokens=int(_field(chunk_usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(_field(chunk_usage, "completion_tokens", 0) or 0),
                    cost_usd=cost,
                )
            choices = _field(chunk, "choices") or []
            if not choices:
                continue
            delta = _field(choices[0], "delta")
            if delta is None:
                continue
            text = _field(delta, "content") or ""
            if text:
                content_parts.append(text)
                on_delta(text)
            for tc in _field(delta, "tool_calls") or []:
                index = _field(tc, "index", 0) or 0
                acc = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                tc_id = _field(tc, "id")
                if tc_id:
                    acc["id"] = tc_id
                function = _field(tc, "function")
                if function is not None:
                    name = _field(function, "name")
                    if name:
                        acc["name"] = name
                    arguments = _field(function, "arguments")
                    if arguments:
                        acc["arguments"] += arguments

        tool_calls: list[ToolCall] = []
        for acc in tool_acc.values():
            try:
                arguments = json.loads(acc["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                ToolCall(id=acc["id"] or f"call-{len(tool_calls)}", name=acc["name"], arguments=arguments)
            )
        return AssistantTurn(content="".join(content_parts), tool_calls=tool_calls, usage=usage)
