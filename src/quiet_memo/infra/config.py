from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
CONFIG_FILENAME = "quiet-memo.toml"


KEYRING_SERVICE = "quiet-memo"


@dataclass(slots=True)
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key_env: str = DEFAULT_API_KEY_ENV
    reasoning_effort: str | None = None
    fallbacks: list[str] = field(default_factory=list)
    num_retries: int = 2
    temperature: float = 0.1
    timeout: float = 60.0

    def litellm_model(self) -> str:
        provider = (self.provider or "").strip().lower()
        model = (self.model or "").strip()
        if provider in ("", "custom", "openai-compatible"):
            return model if model.startswith("openai/") else f"openai/{model}"
        if model.startswith(f"{provider}/"):
            return model
        return f"{provider}/{model}"

    def requires_api_key(self) -> bool:
        """本地 provider 可以不带 Key；其余走 keyring/环境变量。"""
        return (self.provider or "").strip().lower() not in {"ollama", "local", "lmstudio"}

    def api_key(self) -> str | None:
        env_value = os.environ.get(self.api_key_env)
        if env_value:
            return env_value
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, self.api_key_env) or None
        except Exception:
            return None


@dataclass(slots=True)
class ChatConfig:
    context_window: int = 32000
    reserve_output_tokens: int = 1500
    keep_recent_tokens: int = 6000
    compact_threshold: float = 0.8
    max_tool_steps: int = 8
    tool_timeout: float = 60.0
    auto_recall: int = 3
    recall_rewrite: bool = True
    recall_context_turns: int = 2
    extract_on_exit: bool = True


@dataclass(slots=True)
class EmbeddingConfig:
    backend: str = "local"  # local：fastembed 离线推理；api：embedding 供应商
    model: str = DEFAULT_EMBEDDING_MODEL
    provider: str = "siliconflow"
    base_url: str = DEFAULT_EMBEDDING_BASE_URL
    api_key_env: str = "SILICONFLOW_API_KEY"


@dataclass(slots=True)
class RetrievalConfig:
    method: str = "hybrid"  # keyword / vector / hybrid
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


@dataclass(slots=True)
class SearchConfig:
    enabled: bool = True
    providers: list[str] = field(default_factory=lambda: ["open_websearch"])
    base_url: str = "http://127.0.0.1:3000"
    timeout: float = 10.0
    max_results: int = 5
    cache_ttl: int = 86400
    transport: str = "http"  # http / mcp
    auto_start: bool = False  # 打开 TUI 时后台预热 HTTP daemon


@dataclass(slots=True)
class Config:
    vault: Path
    llm: LLMConfig = field(default_factory=LLMConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    confidence_threshold: float = 0.8
    auto_push: bool = False


def default_config_path() -> Path:
    env_path = os.environ.get("QUIET_MEMO_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    local = Path.cwd() / CONFIG_FILENAME
    if local.exists():
        return local
    return Path.home() / ".config" / "quiet-memo" / "config.toml"


def default_vault_path() -> Path:
    env_vault = os.environ.get("QUIET_MEMO_VAULT")
    if env_vault:
        return Path(env_vault).expanduser()
    return Path.home() / ".quiet-memo" / "vault"


def infer_provider(base_url: str) -> str:
    lowered = (base_url or "").lower()
    hints = (
        ("api.deepseek.com", "deepseek"),
        ("siliconflow.cn", "siliconflow"),
        ("dashscope.aliyuncs.com", "dashscope"),
        ("openrouter.ai", "openrouter"),
        ("api.openai.com", "openai"),
    )
    for marker, provider in hints:
        if marker in lowered:
            return provider
    return "custom"


def load_config(path: Path | str | None = None) -> Config:
    config_path = Path(path).expanduser() if path else default_config_path()
    data: dict = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    vault_raw = data.get("vault") or str(default_vault_path())
    llm_raw = data.get("llm", {})
    base_url = llm_raw.get("base_url", DEFAULT_BASE_URL)
    fallbacks = llm_raw.get("fallbacks", [])
    if not isinstance(fallbacks, list):
        fallbacks = []
    llm = LLMConfig(
        provider=str(llm_raw.get("provider") or infer_provider(base_url)),
        base_url=base_url,
        model=llm_raw.get("model", DEFAULT_MODEL),
        api_key_env=llm_raw.get("api_key_env", DEFAULT_API_KEY_ENV),
        reasoning_effort=(str(llm_raw.get("reasoning_effort") or "").strip() or None),
        fallbacks=[str(item) for item in fallbacks if str(item).strip()],
        num_retries=int(llm_raw.get("num_retries", 2)),
        temperature=float(llm_raw.get("temperature", 0.1)),
        timeout=float(llm_raw.get("timeout", 60.0)),
    )
    chat_raw = data.get("chat", {})
    chat = ChatConfig(
        context_window=int(chat_raw.get("context_window", 32000)),
        reserve_output_tokens=int(chat_raw.get("reserve_output_tokens", 1500)),
        keep_recent_tokens=int(chat_raw.get("keep_recent_tokens", 6000)),
        compact_threshold=float(chat_raw.get("compact_threshold", 0.8)),
        max_tool_steps=int(chat_raw.get("max_tool_steps", 8)),
        tool_timeout=float(chat_raw.get("tool_timeout", 60.0)),
        auto_recall=int(chat_raw.get("auto_recall", 3)),
        recall_rewrite=bool(chat_raw.get("recall_rewrite", True)),
        recall_context_turns=int(chat_raw.get("recall_context_turns", 2)),
        extract_on_exit=bool(chat_raw.get("extract_on_exit", True)),
    )
    retrieval_raw = data.get("retrieval", {})
    embedding_raw = retrieval_raw.get("embedding", {})
    retrieval = RetrievalConfig(
        method=str(retrieval_raw.get("method", "hybrid")),
        embedding=EmbeddingConfig(
            backend=str(embedding_raw.get("backend", "local")),
            model=str(embedding_raw.get("model", DEFAULT_EMBEDDING_MODEL)),
            provider=str(embedding_raw.get("provider", "siliconflow")),
            base_url=str(embedding_raw.get("base_url", DEFAULT_EMBEDDING_BASE_URL)),
            api_key_env=str(embedding_raw.get("api_key_env", "SILICONFLOW_API_KEY")),
        ),
    )
    search_raw = data.get("search", {})
    providers = search_raw.get("providers", ["open_websearch"])
    if not isinstance(providers, list):
        providers = ["open_websearch"]
    search = SearchConfig(
        enabled=bool(search_raw.get("enabled", True)),
        providers=[str(item) for item in providers if str(item).strip()] or ["open_websearch"],
        base_url=str(search_raw.get("base_url", "http://127.0.0.1:3000")),
        timeout=float(search_raw.get("timeout", 10.0)),
        max_results=int(search_raw.get("max_results", 5)),
        cache_ttl=int(search_raw.get("cache_ttl", 86400)),
        transport=str(search_raw.get("transport", "http")),
        auto_start=bool(search_raw.get("auto_start", False)),
    )
    return Config(
        vault=Path(vault_raw).expanduser(),
        llm=llm,
        chat=chat,
        retrieval=retrieval,
        search=search,
        confidence_threshold=float(data.get("confidence_threshold", 0.8)),
        auto_push=bool(data.get("auto_push", False)),
    )


def write_example_config(path: Path, vault: Path) -> None:
    content = f'''# quiet-memo 配置
vault = "{vault.as_posix()}"

confidence_threshold = 0.8
auto_push = false

[llm]
provider = "deepseek"
base_url = "{DEFAULT_BASE_URL}"
model = "{DEFAULT_MODEL}"
api_key_env = "{DEFAULT_API_KEY_ENV}"
reasoning_effort = ""
fallbacks = []
num_retries = 2
temperature = 0.1
timeout = 60.0

[chat]
context_window = 32000
reserve_output_tokens = 1500
keep_recent_tokens = 6000
compact_threshold = 0.8
max_tool_steps = 8
tool_timeout = 60.0
auto_recall = 3
recall_rewrite = true
recall_context_turns = 2
extract_on_exit = true

[retrieval]
# keyword / vector / hybrid；hybrid 在向量后端不可用时自动回退 keyword
method = "hybrid"

[retrieval.embedding]
# local：fastembed 本地离线推理（uv sync --extra local-embed 安装）
# api：把记忆文本发给 embedding 供应商换取向量（注意隐私）
backend = "local"
model = "{DEFAULT_EMBEDDING_MODEL}"
# backend = "api" 时的示例：
# provider = "siliconflow"
# base_url = "{DEFAULT_EMBEDDING_BASE_URL}"
# api_key_env = "SILICONFLOW_API_KEY"

[search]
enabled = true
providers = ["open_websearch"]
base_url = "http://127.0.0.1:3000"
timeout = 10.0
max_results = 5
cache_ttl = 86400
# http：调用本地 daemon；mcp：每次搜索启动 npx MCP 进程
transport = "http"
# 打开 TUI 时后台探测并拉起 HTTP daemon，不阻塞界面
auto_start = true
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
