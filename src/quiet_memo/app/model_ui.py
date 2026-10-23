from __future__ import annotations

from dataclasses import replace

import questionary

from ..agent.runtime import AgentRuntime
from ..infra.prompts import ask
from ..llm import fetch_provider_models, model_metadata, supported_params
from ..memory.service import MemoryService
from .ui import MENU_STYLE, console


def switch_model(runtime: AgentRuntime, service: MemoryService, refresh: bool = False) -> None:
    from ..infra.providers import (
        PRESETS,
        display_model,
        has_api_key,
        provider_label,
        resolve_config_path,
        write_config,
    )

    current = service.config.llm
    fetched_models = fetch_provider_models(current, refresh=refresh)
    entries: list[tuple[str, str, object | None]] = []
    seen: set[tuple[str, str]] = set()
    for preset in PRESETS:
        if preset.key == "custom":
            continue
        # 只展示当前供应商和已配置 API Key 的其他供应商
        if preset.key != current.provider and not has_api_key(preset):
            continue
        models = fetched_models if preset.key == current.provider and fetched_models else list(preset.models)
        for model in models:
            entries.append((preset.key, model, preset))
            seen.add((preset.key, model))
    if current.provider not in {preset.key for preset in PRESETS} or current.provider == "custom":
        for model in (fetched_models or [current.model]):
            entries.insert(0, (current.provider, model, None))
            seen.add((current.provider, model))
    if (current.provider, current.model) not in seen:
        entries.insert(0, (current.provider, current.model, None))
    entries.sort(
        key=lambda item: 0
        if (item[0], item[1]) == (current.provider, current.model)
        else 1
    )

    choices = []
    for provider, model, preset in entries:
        candidate = replace(
            current,
            provider=provider,
            model=model,
            base_url=preset.base_url if preset else current.base_url,
            api_key_env=preset.api_key_env if preset else current.api_key_env,
        )
        key = candidate.litellm_model()
        info = model_metadata(key)
        caps = []
        max_input = info.get("max_input_tokens")
        if max_input:
            try:
                caps.append(f"{int(max_input) // 1000}k")
            except (TypeError, ValueError):
                pass
        if "reasoning_effort" in supported_params(key):
            caps.append("reasoning")
        label = f"{provider_label(provider)} · {display_model(model)}"
        if caps:
            label += f"  ({', '.join(caps)})"
        if (provider, model) == (current.provider, current.model):
            label += "  ← 当前"
        choices.append(questionary.Choice(label, value=(provider, model)))
    choices.append(questionary.Choice("↻ 刷新模型列表", value=("__refresh__", "")))
    choices.append(questionary.Choice("自定义模型…", value=("__custom__", "")))

    selected = ask(
        questionary.select(
            f"当前模型：{runtime.model_label}",
            choices=choices,
            style=MENU_STYLE,
            qmark="›",
            pointer="❯",
            instruction="↑↓ 选择 · Enter 确认",
        )
    )
    if selected is None:
        return
    if selected[0] == "__refresh__":
        console.print("[dim]正在刷新模型列表…[/dim]")
        switch_model(runtime, service, refresh=True)
        return

    if selected[0] == "__custom__":
        value = ask(questionary.text("模型名称", default=current.model))
        if value is None or not value.strip():
            return
        new_llm = replace(current, model=value.strip())
    else:
        provider, model = selected
        preset = next((item for item in PRESETS if item.key == provider), None)
        new_llm = replace(
            current,
            provider=provider,
            model=model,
            base_url=preset.base_url if preset else current.base_url,
            api_key_env=preset.api_key_env if preset else current.api_key_env,
        )
        if new_llm.provider != current.provider and not new_llm.api_key():
            console.print(
                f"[#b8b8b8]{provider} 还没有 API Key，请先用 /setup 配置该供应商。[/]"
            )
            return

    key = new_llm.litellm_model()
    info = model_metadata(key)
    params = supported_params(key)
    if new_llm.reasoning_effort and "reasoning_effort" not in params:
        new_llm.reasoning_effort = None
    max_input = info.get("max_input_tokens")
    if max_input is not None:
        try:
            max_input = int(max_input)
        except (TypeError, ValueError):
            max_input = None
    context_window = service.config.chat.context_window
    if max_input and context_window > max_input:
        context_window = max_input

    service.config.llm = new_llm
    service.config.chat.context_window = context_window
    write_config(resolve_config_path(None), service.config)
    runtime.reload_llm()
    console.print(
        f"[bold]模型已切换：{runtime.model_label}[/] "
        f"[dim]上下文 {context_window} · 思考强度 {new_llm.reasoning_effort or '默认'}[/dim]"
    )
