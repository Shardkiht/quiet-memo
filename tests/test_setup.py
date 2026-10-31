from __future__ import annotations

import pytest

pytestmark = pytest.mark.config

import sys
import tomllib
import types

from quiet_memo.infra.config import (
    ChatConfig,
    Config,
    ConfigError,
    EmbeddingConfig,
    LLMConfig,
    RetrievalConfig,
    SearchConfig,
    load_config,
    toml_literal,
)
from quiet_memo.infra.providers import PRESETS, display_model, provider_label, write_config


@pytest.mark.parametrize("provider", ["custom", "deepseek", "spark"])
def test_model_change_preserves_same_provider_endpoint_and_credentials(tmp_path, monkeypatch, provider):
    from types import SimpleNamespace
    from quiet_memo.app import actions
    from quiet_memo.memory.service import MemoryService

    config_path = tmp_path / "config.toml"
    config = Config(
        vault=tmp_path / "vault", retrieval=RetrievalConfig(method="keyword"),
        llm=LLMConfig(provider=provider, model="synthetic-old",
                      base_url="https://provider.example.invalid/v1", api_key_env="SYNTHETIC_CUSTOM_KEY"),
    )
    service = MemoryService(config, config_path=config_path)
    reloaded = []
    runtime = SimpleNamespace(reload_llm=reloaded.append, model_label=f"{provider}/synthetic-new")
    monkeypatch.setattr(actions, "model_metadata", lambda model: {})
    monkeypatch.setattr(actions, "supported_params", lambda model: ())
    actions.apply_model(service, runtime, provider, "synthetic-new")
    saved = load_config(config_path)
    assert saved.llm.model == "synthetic-new"
    assert saved.llm.base_url == "https://provider.example.invalid/v1"
    assert saved.llm.api_key_env == "SYNTHETIC_CUSTOM_KEY"
    assert reloaded == [config]


def test_litellm_model_names():
    assert LLMConfig(provider="deepseek", model="deepseek-flash").litellm_model() == "deepseek/deepseek-flash"
    assert LLMConfig(provider="deepseek", model="deepseek-chat").litellm_model() == "deepseek/deepseek-chat"
    assert (
        LLMConfig(provider="siliconflow", model="deepseek-ai/DeepSeek-V3").litellm_model()
        == "openai/deepseek-ai/DeepSeek-V3"
    )
    assert LLMConfig(provider="openai", model="gpt-4o-mini").litellm_model() == "openai/gpt-4o-mini"
    assert (
        LLMConfig(provider="custom", model="qwen-max", base_url="http://localhost:8000/v1").litellm_model()
        == "openai/qwen-max"
    )
    assert LLMConfig(provider="openai", model="openai/gpt-4o-mini").litellm_model() == "openai/gpt-4o-mini"


def test_spark_preset_and_configuration_roundtrip(tmp_path):
    from quiet_memo.infra.config import infer_provider

    preset = next(item for item in PRESETS if item.key == "spark")
    assert preset.base_url == "https://maas-api.cn-huabei-1.xf-yun.com/v2"
    assert preset.model == "spark-x2.6"
    assert preset.api_key_env == "SPARK_API_KEY"
    llm = LLMConfig(provider=preset.key, base_url=preset.base_url, model=preset.model, api_key_env=preset.api_key_env)
    assert llm.litellm_model() == "openai/spark-x2.6"
    assert infer_provider(preset.base_url) == "spark"
    assert provider_label("spark") == "讯飞星火"
    assert display_model(preset.model) == "Spark X2.6"
    path = tmp_path / "config.toml"
    write_config(path, Config(vault=tmp_path / "vault", llm=llm))
    assert load_config(path).llm == llm


def test_spark_chat_parameters_match_maas_contract(monkeypatch):
    from types import SimpleNamespace
    from quiet_memo.llm import client

    calls = []
    monkeypatch.setenv("SPARK_API_KEY", "synthetic-key")
    fake = SimpleNamespace(completion=lambda **kwargs: calls.append(kwargs) or "response")
    monkeypatch.setattr(client, "_litellm", lambda: fake)
    preset = next(item for item in PRESETS if item.key == "spark")
    config = LLMConfig(provider="spark", model=preset.model, base_url=preset.base_url,
                       api_key_env=preset.api_key_env, reasoning_effort="high")
    tools = [{"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}]
    assert client.complete(config, [{"role": "user", "content": "合成输入"}], tools=tools,
                           response_format={"type": "json_object"}, max_tokens=128) == "response"
    sent = calls[0]
    assert sent["model"] == "openai/spark-x2.6"
    assert sent["api_base"] == preset.base_url
    assert sent["api_key"] == "synthetic-key"
    assert sent["tools"] == tools and sent["max_completion_tokens"] == 128
    assert not {"temperature", "reasoning_effort", "response_format", "max_tokens"} & sent.keys()


def test_spark_streaming_preserves_deltas_and_usage(monkeypatch):
    from types import SimpleNamespace
    from quiet_memo.llm import client

    calls = []
    monkeypatch.setenv("SPARK_API_KEY", "synthetic-key")
    chunks = [
        {"choices": [{"delta": {"content": "合成"}}]},
        {"choices": [{"delta": {"content": "回复"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    fake = SimpleNamespace(completion=lambda **kwargs: calls.append(kwargs) or iter(chunks),
                           completion_cost=lambda **kwargs: 0)
    monkeypatch.setattr(client, "_litellm", lambda: fake)
    config = LLMConfig(provider="spark", model="spark-x2.6", api_key_env="SPARK_API_KEY", reasoning_effort="high")
    deltas = []
    result = client.LiteLLMChatModel(config).chat([{"role": "user", "content": "合成输入"}], [], on_delta=deltas.append)
    assert result.content == "合成回复" and deltas == ["合成", "回复"]
    assert result.usage.prompt_tokens == 3 and result.usage.completion_tokens == 2
    assert calls[0]["stream"] is True
    assert not {"temperature", "reasoning_effort", "stream_options"} & calls[0].keys()


def test_spark_does_not_offer_openai_reasoning_effort():
    from quiet_memo.llm.client import supported_params

    assert "reasoning_effort" not in supported_params("openai/spark-x2.6")
    assert "tools" in supported_params("openai/spark-x2.6")


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
        retrieval=RetrievalConfig(
            method="vector",
            embedding=EmbeddingConfig(
                backend="api",
                model="BAAI/bge-m3",
                provider="dashscope",
                base_url="https://dashscope.example/v1",
                api_key_env="DASHSCOPE_API_KEY",
            ),
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
    assert loaded.retrieval.method == "vector"
    assert loaded.retrieval.embedding.backend == "api"
    assert loaded.retrieval.embedding.model == "BAAI/bge-m3"
    assert loaded.retrieval.embedding.api_key_env == "DASHSCOPE_API_KEY"
    assert loaded.vault == config.vault
    assert not (tmp_path / "quiet-memo.toml.tmp").exists()


def test_load_config_reports_invalid_toml(tmp_path):
    path = tmp_path / "quiet-memo.toml"
    path.write_text("[chat\ncontext_window = 1000\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="TOML 语法错误"):
        load_config(path)


def test_load_config_rejects_wrong_types_and_ranges(tmp_path):
    cases = [
        ('[chat]\ncontext_window = "big"\n', "必须是整数"),
        ("[chat]\ncontext_window = 0\n", "必须大于 0"),
        ("confidence_threshold = 1.5\n", "必须在 0 到 1 之间"),
        ("auto_push = \"yes\"\n", "必须是布尔值"),
        ("[search]\nproviders = [1, 2]\n", "必须是字符串"),
    ]
    for content, message in cases:
        path = tmp_path / "quiet-memo.toml"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigError, match=message):
            load_config(path)


def test_write_config_escapes_special_characters(tmp_path):
    path = tmp_path / "quiet-memo.toml"
    vault = tmp_path / 'vault "quoted" \\ path\x7f'
    write_config(path, Config(vault=vault, llm=LLMConfig(model="model\x7f")))

    loaded = load_config(path)
    assert loaded.vault == vault
    assert loaded.llm.model == "model\x7f"


def test_toml_literal_roundtrips_unicode_and_controls():
    value = '中文 "quote" \\ backslash \x7f'
    assert tomllib.loads(f"value = {toml_literal(value)}")["value"] == value


def test_load_config_defaults_to_hybrid_local(tmp_path):
    loaded = load_config(tmp_path / "not-exist.toml")

    assert loaded.retrieval.method == "hybrid"
    assert loaded.retrieval.embedding.backend == "local"
    assert loaded.retrieval.embedding.model == "BAAI/bge-small-zh-v1.5"


def test_vault_precedence_config_then_env(tmp_path, monkeypatch):
    path = tmp_path / "quiet-memo.toml"
    configured = tmp_path / "configured"
    path.write_text(f'vault = "{configured.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("QUIET_MEMO_VAULT", str(tmp_path / "from-env"))

    # 与测试隔离口径一致：显式配置的 vault 优先，环境变量只补默认值。
    assert load_config(path).vault == configured

    path.write_text("confidence_threshold = 0.8\n", encoding="utf-8")
    assert load_config(path).vault == tmp_path / "from-env"


def test_search_auto_start_defaults_match_example(tmp_path):
    assert SearchConfig().auto_start is True
    assert SearchConfig().engines == ["bing"]
    assert SearchConfig().fetch_timeout == 45.0
    assert load_config(tmp_path / "missing.toml").search.auto_start is True


def test_local_provider_without_key_reaches_backend(monkeypatch):
    import pytest

    from quiet_memo.llm.client import LiteLLMChatModel

    model = LiteLLMChatModel(LLMConfig(provider="ollama", api_key_env="MISSING_LOCAL_KEY"))
    called = []

    def fake_complete(*args, **kwargs):
        called.append(True)
        raise RuntimeError("backend reached")

    monkeypatch.setattr("quiet_memo.llm.client.complete", fake_complete)
    with pytest.raises(RuntimeError, match="backend reached"):
        model.chat([], [])
    assert called


def test_search_config_roundtrip(tmp_path):
    path = tmp_path / "quiet-memo.toml"
    config = Config(
        vault=tmp_path / "vault",
        search=SearchConfig(
            enabled=False,
            providers=["open_websearch"],
            base_url="http://127.0.0.1:3210",
            timeout=4.0,
            max_results=2,
            cache_ttl=0,
            transport="mcp",
            auto_start=True,
            engines=["baidu", "sogou"],
            fetch_timeout=15.0,
        ),
    )

    write_config(path, config)
    loaded = load_config(path)

    assert loaded.search.enabled is False
    assert loaded.search.providers == ["open_websearch"]
    assert loaded.search.base_url == "http://127.0.0.1:3210"
    assert loaded.search.timeout == 4.0
    assert loaded.search.max_results == 2
    assert loaded.search.cache_ttl == 0
    assert loaded.search.transport == "mcp"
    assert loaded.search.auto_start is True
    assert loaded.search.engines == ["baidu", "sogou"]
    assert loaded.search.fetch_timeout == 15.0


def test_resolve_config_path_env_wins_over_cwd(tmp_path, monkeypatch):
    from quiet_memo.infra.providers import resolve_config_path

    (tmp_path / "quiet-memo.toml").write_text("vault = 'local'\n", encoding="utf-8")
    env_path = tmp_path / "isolated.toml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUIET_MEMO_CONFIG", str(env_path))

    assert resolve_config_path(None) == env_path


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


def test_openai_compatible_model_does_not_claim_openai_params():
    from quiet_memo.llm import supported_params

    # 硅基流动等自定义端点用 openai/ 前缀；未知 id 不能套用通用 OpenAI 参数表。
    assert supported_params("openai/deepseek-ai/DeepSeek-V4.1-Flash") == ()


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


def test_cached_provider_models_reads_cache_only(monkeypatch):
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
        assert client.cached_provider_models(config) is None
        assert client.fetch_provider_models(config) == ["model-a"]
        assert client.cached_provider_models(config) == ["model-a"]
        assert len(calls) == 1
    finally:
        client.clear_model_cache()
