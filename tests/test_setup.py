from __future__ import annotations

import sys
import types

from quiet_memo.infra.config import ChatConfig, Config, LLMConfig, load_config
from quiet_memo.infra.providers import PRESETS, write_config


def test_litellm_model_names():
    assert LLMConfig(provider="deepseek", model="deepseek-flash").litellm_model() == "deepseek/deepseek-flash"
    assert LLMConfig(provider="deepseek", model="deepseek-chat").litellm_model() == "deepseek/deepseek-chat"
    assert (
        LLMConfig(provider="siliconflow", model="deepseek-ai/DeepSeek-V3").litellm_model()
        == "siliconflow/deepseek-ai/DeepSeek-V3"
    )
    assert LLMConfig(provider="openai", model="gpt-4o-mini").litellm_model() == "openai/gpt-4o-mini"
    assert (
        LLMConfig(provider="custom", model="qwen-max", base_url="http://localhost:8000/v1").litellm_model()
        == "openai/qwen-max"
    )
    assert LLMConfig(provider="openai", model="openai/gpt-4o-mini").litellm_model() == "openai/gpt-4o-mini"


def test_write_config_roundtrip(tmp_path):
    path = tmp_path / "quiet-memo.toml"
    config = Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(
            provider="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            model="deepseek-ai/DeepSeek-V3",
            api_key_env="SILICONFLOW_API_KEY",
            reasoning_effort="medium",
            fallbacks=["openai/gpt-4o-mini"],
            num_retries=1,
        ),
        chat=ChatConfig(
            context_window=16000,
            keep_recent_tokens=3000,
            recall_rewrite=False,
            recall_context_turns=4,
        ),
    )

    write_config(path, config)
    loaded = load_config(path)

    assert loaded.llm.provider == "siliconflow"
    assert loaded.llm.reasoning_effort == "medium"
    assert loaded.llm.fallbacks == ["openai/gpt-4o-mini"]
    assert loaded.llm.num_retries == 1
    assert loaded.chat.context_window == 16000
    assert loaded.chat.recall_rewrite is False
    assert loaded.chat.recall_context_turns == 4
    assert loaded.vault == config.vault


def test_env_key_takes_precedence_over_keyring(monkeypatch):
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: "from-keyring"
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")

    assert LLMConfig(api_key_env="DEEPSEEK_API_KEY").api_key() == "from-env"

    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert LLMConfig(api_key_env="DEEPSEEK_API_KEY").api_key() == "from-keyring"


def test_provider_presets_cover_domestic_providers():
    keys = {preset.key for preset in PRESETS}
    assert {"deepseek", "siliconflow", "dashscope", "custom"} <= keys


def test_litellm_metadata_exposes_context_and_reasoning():
    from quiet_memo.llm import model_metadata, supported_params

    info = model_metadata("openai/gpt-4o-mini")
    assert info.get("max_input_tokens", 0) > 0
    assert "reasoning_effort" in supported_params("openai/gpt-4o-mini")


def test_provider_model_list_cache(monkeypatch):
    from quiet_memo.llm import client

    config = LLMConfig()
    calls = []
    monkeypatch.setattr(LLMConfig, "api_key", lambda self: "test-key")
    monkeypatch.setattr(
        client,
        "_fetch_provider_models_uncached",
        lambda cfg, timeout: calls.append(1) or ["model-a"],
    )
    client.clear_model_cache()
    try:
        assert client.fetch_provider_models(config) == ["model-a"]
        assert client.fetch_provider_models(config) == ["model-a"]
        assert len(calls) == 1

        assert client.fetch_provider_models(config, refresh=True) == ["model-a"]
        assert len(calls) == 2
    finally:
        client.clear_model_cache()
