from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from ..infra.config import LLMConfig


class ChatModelError(RuntimeError):
    pass


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
    import litellm

    litellm.suppress_debug_info = True
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
    if tools:
        kwargs["tools"] = tools
    if response_format:
        kwargs["response_format"] = response_format
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if config.fallbacks:
        kwargs["fallbacks"] = config.fallbacks
    return litellm.completion(**kwargs)


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


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

    def chat(self, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        if not self.config.api_key():
            raise ChatModelError(
                f"未配置 LLM API Key，请设置环境变量 {self.config.api_key_env}，"
                "或运行 qm setup 保存到系统凭据。"
            )
        litellm = _litellm()
        response = complete(self.config, messages, tools=tools or None)
        message = response.choices[0].message
        return AssistantTurn(
            content=_field(message, "content") or "",
            tool_calls=_tool_calls(message),
            usage=_usage(response, litellm),
        )
