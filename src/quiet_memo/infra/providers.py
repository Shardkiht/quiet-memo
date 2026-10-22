from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import questionary
from rich.console import Console

from .config import (
    CONFIG_FILENAME,
    KEYRING_SERVICE,
    ChatConfig,
    Config,
    LLMConfig,
    default_config_path,
    load_config,
    write_example_config,
)

console = Console()


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    model: str
    api_key_env: str


PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "deepseek",
        "DeepSeek（国内，便宜）",
        "https://api.deepseek.com/v1",
        "deepseek-chat",
        "DEEPSEEK_API_KEY",
    ),
    ProviderPreset(
        "siliconflow",
        "硅基流动 SiliconFlow",
        "https://api.siliconflow.cn/v1",
        "deepseek-ai/DeepSeek-V3",
        "SILICONFLOW_API_KEY",
    ),
    ProviderPreset(
        "dashscope",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "DASHSCOPE_API_KEY",
    ),
    ProviderPreset(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "OPENAI_API_KEY",
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o-mini",
        "OPENROUTER_API_KEY",
    ),
    ProviderPreset("custom", "自定义 OpenAI 兼容接口", "", "", "OPENAI_API_KEY"),
)


def save_api_key(env_name: str, api_key: str) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, env_name, api_key)
        return True
    except Exception:
        return False


def test_connection(llm: LLMConfig) -> tuple[bool, str]:
    api_key = llm.api_key()
    if not api_key:
        return False, "没有可用的 API Key"
    try:
        from ..llm import complete

        complete(
            llm,
            [{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=1,
        )
        return True, "连接成功"
    except Exception as exc:
        return False, str(exc)[:300]


def write_config(path: Path, config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f'''# quiet-memo 配置（由 qm setup 生成）
vault = "{config.vault.as_posix()}"

confidence_threshold = {config.confidence_threshold}
auto_push = {str(config.auto_push).lower()}

[llm]
provider = "{config.llm.provider}"
base_url = "{config.llm.base_url}"
model = "{config.llm.model}"
api_key_env = "{config.llm.api_key_env}"
fallbacks = [{", ".join(f'"{item}"' for item in config.llm.fallbacks)}]
num_retries = {config.llm.num_retries}
temperature = {config.llm.temperature}
timeout = {config.llm.timeout}

[chat]
context_window = {config.chat.context_window}
reserve_output_tokens = {config.chat.reserve_output_tokens}
keep_recent_tokens = {config.chat.keep_recent_tokens}
compact_threshold = {config.chat.compact_threshold}
max_tool_steps = {config.chat.max_tool_steps}
tool_timeout = {config.chat.tool_timeout}
auto_recall = {config.chat.auto_recall}
extract_on_exit = {str(config.chat.extract_on_exit).lower()}
'''
    path.write_text(content, encoding="utf-8")


def resolve_config_path(config_path: Path | None) -> Path:
    if config_path:
        return config_path.expanduser()
    local = Path.cwd() / CONFIG_FILENAME
    if local.exists():
        return local
    fallback = default_config_path()
    return fallback if fallback.exists() else local


def run_setup(config_path: Path | None = None, vault: Path | None = None) -> dict:
    path = resolve_config_path(config_path)
    config = load_config(path) if path.exists() else Config(vault=Path.home() / ".quiet-memo" / "vault")
    if vault is not None:
        config.vault = vault.expanduser().resolve()

    choice = questionary.select(
        "选择模型供应商",
        choices=[questionary.Choice(title=preset.label, value=preset) for preset in PRESETS],
    ).ask()
    if choice is None:
        raise KeyboardInterrupt
    preset: ProviderPreset = choice

    if preset.key == "custom":
        base_url = questionary.text("Base URL", default=config.llm.base_url or "https://").ask()
        model = questionary.text("模型名称", default=config.llm.model).ask()
        env_name = questionary.text("API Key 环境变量名", default=config.llm.api_key_env).ask()
    else:
        base_url = questionary.text("Base URL", default=preset.base_url).ask()
        model = questionary.text("模型名称", default=preset.model).ask()
        env_name = questionary.text("API Key 环境变量名", default=preset.api_key_env).ask()
    if base_url is None or model is None or env_name is None:
        raise KeyboardInterrupt

    api_key = questionary.password(
        "API Key（不会写入配置文件；留空则跳过）"
    ).ask()
    key_saved = False
    keyring_error = ""
    if api_key:
        key_saved = save_api_key(env_name.strip(), api_key.strip())
        if not key_saved:
            keyring_error = "系统凭据存储不可用，请改用环境变量。"

    config.llm = LLMConfig(
        provider=preset.key if preset.key != "custom" else "custom",
        base_url=base_url.strip(),
        model=model.strip(),
        api_key_env=env_name.strip(),
        fallbacks=config.llm.fallbacks,
        num_retries=config.llm.num_retries,
        temperature=config.llm.temperature,
        timeout=config.llm.timeout,
    )
    write_config(path, config)

    check = False
    detail = "已跳过连接测试"
    if api_key and key_saved:
        if questionary.confirm("现在测试连接？", default=True).ask():
            check, detail = test_connection(config.llm)

    initialized = False
    if not (config.vault / ".git").exists():
        if questionary.confirm(f"初始化记忆库 {config.vault}？", default=True).ask():
            from ..memory.service import MemoryService

            MemoryService(config).init_vault()
            initialized = True

    return {
        "config_path": str(path),
        "provider": config.llm.provider,
        "base_url": config.llm.base_url,
        "model": config.llm.model,
        "api_key_env": config.llm.api_key_env,
        "key_saved": key_saved,
        "keyring_error": keyring_error,
        "checked": check,
        "check_detail": detail,
        "initialized": initialized,
        "vault": str(config.vault),
    }


def write_default_config(path: Path, vault: Path) -> None:
    write_example_config(path, vault)
