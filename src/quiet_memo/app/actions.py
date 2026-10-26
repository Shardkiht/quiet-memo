"""UI 无关的设置动作：选项构造与应用落盘分离，界面层只负责收集选择并展示结果。"""
from __future__ import annotations

from dataclasses import dataclass

from ..agent.runtime import AgentRuntime
from ..infra.tokens import format_tokens
from ..llm import fetch_provider_models, model_metadata, supported_params
from ..memory.service import MemoryService


@dataclass
class ModelOption:
    provider: str
    model: str
    label: str
    current: bool = False


def model_options(service: MemoryService, refresh: bool = False) -> list[ModelOption]:
    """枚举可选模型：当前供应商（可刷新线上列表）+ 已配置 Key 的其他供应商。"""
    from dataclasses import replace
    from ..infra.providers import PRESETS, display_model, has_api_key, provider_label

    current = service.config.llm
    fetched = fetch_provider_models(current, refresh=refresh)
    entries: list[tuple[str, str]] = []
    for preset in PRESETS:
        if preset.key == "custom":
            continue
        if preset.key != current.provider and not has_api_key(preset):
            continue
        models = fetched if preset.key == current.provider and fetched else list(preset.models)
        entries.extend((preset.key, model) for model in models)
    if current.provider not in {preset.key for preset in PRESETS}:
        entries = [(current.provider, model) for model in (fetched or [current.model])] + entries
    if (current.provider, current.model) not in entries:
        entries.insert(0, (current.provider, current.model))

    seen: set[tuple[str, str]] = set()
    options: list[ModelOption] = []
    for provider, model in entries:
        if (provider, model) in seen:
            continue
        seen.add((provider, model))
        key = replace(
            current,
            provider=provider,
            model=model,
        ).litellm_model()
        info = model_metadata(key)
        caps = []
        max_input = info.get("max_input_tokens")
        if max_input:
            try:
                caps.append(format_tokens(int(max_input)))
            except (TypeError, ValueError):
                pass
        if "reasoning_effort" in supported_params(key):
            caps.append("reasoning")
        label = f"{provider_label(provider)} · {display_model(model)}"
        if caps:
            label += f"  ({', '.join(caps)})"
        options.append(
            ModelOption(
                provider,
                model,
                label,
                current=(provider, model) == (current.provider, current.model),
            )
        )
    # 当前模型排最前，其余保持供应商顺序。
    options.sort(key=lambda option: not option.current)
    return options


def apply_model(
    service: MemoryService,
    runtime: AgentRuntime,
    provider: str,
    model: str,
) -> str:
    """切换模型并落盘热更新；未配置 Key 等情况不生效，返回提示消息。"""
    from dataclasses import replace
    from ..infra.providers import PRESETS, has_api_key, write_config

    current = service.config.llm
    preset = next((item for item in PRESETS if item.key == provider), None)
    new_llm = replace(
        current,
        provider=provider,
        model=model,
        base_url=preset.base_url if preset else current.base_url,
        api_key_env=preset.api_key_env if preset else current.api_key_env,
    )
    if new_llm.provider != current.provider and not new_llm.api_key():
        return f"{provider} has no API key yet; run qm setup in a terminal to configure it."

    info = model_metadata(new_llm.litellm_model())
    if new_llm.reasoning_effort and "reasoning_effort" not in supported_params(
        new_llm.litellm_model()
    ):
        new_llm.reasoning_effort = None
    context_window = service.config.chat.context_window
    max_input = info.get("max_input_tokens")
    if max_input:
        try:
            max_input = int(max_input)
        except (TypeError, ValueError):
            max_input = None
    if max_input and context_window > max_input:
        context_window = max_input

    service.config.llm = new_llm
    service.config.chat.context_window = context_window
    # 热更新写回 service 记住的加载路径；未记录时（内存构造的 service）跳过落盘。
    if service.config_path:
        write_config(service.config_path, service.config)
    # 传当前配置对象热更新，不重读文件：写盘与重读之间没有竞态。
    runtime.reload_llm(service.config)
    return (
        f"Model switched: {runtime.model_label}"
        f" · context {format_tokens(context_window)}"
        f" · reasoning {new_llm.reasoning_effort or 'default'}"
    )


def supports_reasoning(service: MemoryService) -> bool:
    return "reasoning_effort" in supported_params(service.config.llm.litellm_model())


def reasoning_options(service: MemoryService) -> list[tuple[str, str]]:
    """(值, 标签) 列表，空串表示默认；模型不支持时返回空。"""
    if not supports_reasoning(service):
        return []
    return [("", "default"), ("low", "low"), ("medium", "medium"), ("high", "high")]


def apply_reasoning(service: MemoryService, value: str) -> str:
    from ..infra.providers import write_config

    service.config.llm.reasoning_effort = value or None
    # 热更新写回 service 记住的加载路径；未记录时（内存构造的 service）跳过落盘。
    if service.config_path:
        write_config(service.config_path, service.config)
    return f"Reasoning effort: {value or 'default'}"


def context_options(service: MemoryService) -> list[int]:
    """按模型上下文上限过滤出的档位。"""
    max_input = model_metadata(service.config.llm.litellm_model()).get("max_input_tokens")
    try:
        max_input = int(max_input) if max_input else None
    except (TypeError, ValueError):
        max_input = None
    presets = [8_000, 16_000, 32_000, 64_000, 128_000, 200_000]
    if max_input:
        presets = [size for size in presets if size <= max_input]
        if max_input not in presets:
            presets.append(max_input)
    current = service.config.chat.context_window
    if current not in presets:
        presets.insert(0, current)
    return sorted(presets)


def apply_context(service: MemoryService, value: int) -> str:
    """设置上下文长度（下一轮生效）；非法值不生效，返回提示消息。"""
    from ..infra.providers import write_config

    max_input = model_metadata(service.config.llm.litellm_model()).get("max_input_tokens")
    try:
        max_input = int(max_input) if max_input else None
    except (TypeError, ValueError):
        max_input = None
    if value < 2000:
        return "Context window must be at least 2000 tokens."
    if max_input and value > max_input:
        value = max_input
        note = f"Exceeds the model limit; capped at {format_tokens(max_input)}."
    else:
        note = ""
    if value == service.config.chat.context_window:
        return "Context window unchanged."
    service.config.chat.context_window = value
    # 热更新写回 service 记住的加载路径；未记录时（内存构造的 service）跳过落盘。
    if service.config_path:
        write_config(service.config_path, service.config)
    return f"Context window set to {format_tokens(value)} · applies next turn. {note}".strip()
