from __future__ import annotations

import json
import math
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
CONFIG_FILENAME = "quiet-memo.toml"


KEYRING_SERVICE = "quiet-memo"


class ConfigError(ValueError):
    """配置读取、TOML 语法或字段校验失败；消息可直接展示给用户。"""


def toml_literal(value: str | list[str]) -> str:
    """序列化 TOML 字符串及字符串数组，保留 Unicode 标量。"""
    # ASCII 模式会把非 BMP 字符拆成 TOML 不接受的代理对；DEL 也必须转义。
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


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
        # 硅基流动与 Spark MaaS 使用 OpenAI 兼容协议，不走旧版 Spark 鉴权适配。
        if provider in ("", "custom", "openai-compatible", "siliconflow", "spark"):
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
    auto_start: bool = True  # 打开 TUI 时后台预热 HTTP daemon
    # 上游搜索引擎；空列表表示沿用 daemon 的 DEFAULT_SEARCH_ENGINE（默认 bing）。
    engines: list[str] = field(default_factory=lambda: ["bing"])
    fetch_timeout: float = 45.0  # 抓取正文单独超时，秒


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
        ("maas-api.cn-huabei-1.xf-yun.com", "spark"),
        ("openrouter.ai", "openrouter"),
        ("api.openai.com", "openai"),
    )
    for marker, provider in hints:
        if marker in lowered:
            return provider
    return "custom"


def _validate_types(data: dict, defaults: dict, prefix: str = "") -> None:
    # 用默认配置描述结构，避免维护第二份字段类型表；未知字段仍沿用忽略行为。
    for key, default in defaults.items():
        if key not in data:
            continue
        name = f"{prefix}.{key}" if prefix else key
        value = data[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"配置字段 {name} 必须是表（section）。")
            _validate_types(value, default, name)
        elif isinstance(default, list):
            if not isinstance(value, list):
                raise ConfigError(f"配置字段 {name} 必须是字符串数组。")
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    raise ConfigError(f"配置字段 {name}[{index}] 必须是字符串。")
        elif isinstance(default, bool):
            if type(value) is not bool:
                raise ConfigError(f"配置字段 {name} 必须是布尔值（true/false）。")
        elif isinstance(default, int):
            if type(value) is not int:
                raise ConfigError(f"配置字段 {name} 必须是整数。")
        elif isinstance(default, float):
            if type(value) not in (int, float):
                raise ConfigError(f"配置字段 {name} 必须是数值。")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ConfigError(f"配置字段 {name} 必须是有限数值。")
        elif not isinstance(value, str):
            # vault 在默认对象中是 Path；reasoning_effort 的默认值为 None。
            raise ConfigError(f"配置字段 {name} 必须是字符串。")


def _validate_range(
    data: dict, name: str, *, positive: bool = False, unit_interval: bool = False
) -> None:
    value = data
    for key in name.split("."):
        if key not in value:
            return
        value = value[key]
    if unit_interval:
        if not 0 <= value <= 1:
            raise ConfigError(f"配置字段 {name} 必须在 0 到 1 之间（含边界）。")
    elif positive:
        if value <= 0:
            raise ConfigError(f"配置字段 {name} 必须大于 0。")
    elif value < 0:
        raise ConfigError(f"配置字段 {name} 必须大于或等于 0。")


def _validate_config(data: dict) -> None:
    _validate_types(data, asdict(Config(vault=Path("."))))
    for name in (
        "llm.timeout", "chat.context_window", "chat.max_tool_steps", "chat.tool_timeout",
        "search.timeout", "search.max_results", "search.fetch_timeout",
    ):
        _validate_range(data, name, positive=True)
    for name in (
        "llm.num_retries", "llm.temperature", "chat.reserve_output_tokens",
        "chat.keep_recent_tokens", "chat.auto_recall", "chat.recall_context_turns",
        "search.cache_ttl",
    ):
        _validate_range(data, name)
    for name in ("confidence_threshold", "chat.compact_threshold"):
        _validate_range(data, name, unit_interval=True)


def load_config(path: Path | str | None = None) -> Config:
    config_path = Path(path).expanduser() if path else default_config_path()
    data: dict = {}
    if config_path.exists():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"配置文件 TOML 语法错误：{exc}") from exc
        except (OSError, UnicodeError) as exc:
            raise ConfigError(f"无法读取配置文件 {config_path}：{exc}") from exc
    _validate_config(data)

    # 配置里的 vault 优先；QUIET_MEMO_VAULT 只在未配置时提供默认（也是测试隔离闸）。
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
    engines_raw = search_raw.get("engines", ["bing"])
    if not isinstance(engines_raw, list):
        engines_raw = ["bing"]
    engines = list(
        dict.fromkeys(str(item).strip() for item in engines_raw if str(item).strip())
    ) or ["bing"]
    search = SearchConfig(
        enabled=bool(search_raw.get("enabled", True)),
        providers=[str(item) for item in providers if str(item).strip()] or ["open_websearch"],
        base_url=str(search_raw.get("base_url", "http://127.0.0.1:3000")),
        timeout=float(search_raw.get("timeout", 10.0)),
        max_results=int(search_raw.get("max_results", 5)),
        cache_ttl=int(search_raw.get("cache_ttl", 86400)),
        transport=str(search_raw.get("transport", "http")),
        auto_start=bool(search_raw.get("auto_start", True)),
        engines=engines,
        fetch_timeout=float(search_raw.get("fetch_timeout", 45.0)),
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
vault = {toml_literal(vault.as_posix())}

confidence_threshold = 0.8
auto_push = false

[llm]
provider = {toml_literal("deepseek")}
base_url = {toml_literal(DEFAULT_BASE_URL)}
model = {toml_literal(DEFAULT_MODEL)}
api_key_env = {toml_literal(DEFAULT_API_KEY_ENV)}
reasoning_effort = {toml_literal("")}
fallbacks = {toml_literal([])}
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
method = {toml_literal("hybrid")}

[retrieval.embedding]
# local：fastembed 本地离线推理（uv sync --extra local-embed 安装）
# api：把记忆文本发给 embedding 供应商换取向量（注意隐私）
backend = {toml_literal("local")}
model = {toml_literal(DEFAULT_EMBEDDING_MODEL)}
# backend = "api" 时的示例：
# provider = {toml_literal("siliconflow")}
# base_url = {toml_literal(DEFAULT_EMBEDDING_BASE_URL)}
# api_key_env = {toml_literal("SILICONFLOW_API_KEY")}

[search]
enabled = true
providers = {toml_literal(["open_websearch"])}
base_url = {toml_literal("http://127.0.0.1:3000")}
timeout = 10.0
max_results = 5
cache_ttl = 86400
# http：调用本地 daemon；mcp：每次搜索启动 npx MCP 进程
transport = {toml_literal("http")}
# 打开 TUI 时后台探测并拉起 HTTP daemon，不阻塞界面
auto_start = true
# 上游搜索引擎：bing / baidu / sogou / duckduckgo / brave / startpage 等
engines = {toml_literal(["bing"])}
# 抓取正文的单独超时（秒），比搜索慢
fetch_timeout = 45.0
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
