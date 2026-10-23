# quiet-memo

安静的个人记忆库：从日常对话中抽取**你本人**的事实、偏好和计划，默认无感写入本地 Markdown；低置信、冲突或敏感内容才进入待确认队列。

- **本地优先**：Markdown 是唯一事实来源，Git 负责版本与审计。
- **默认静默**：可信内容直接落盘，不逐条打断；只在必要时提醒。
- **完整 Agent**：交互式对话、会话持久化、上下文压缩、工具循环。
- **多模型**：LiteLLM 统一接入 DeepSeek、硅基流动、百炼、OpenRouter、OpenAI 等。
- **可回滚**：每次提交自动生成中文总结，供本人审阅。

## 快速开始

需要 Python 3.12+、uv、git。

```bash
uv sync

# 进入主菜单（新建会话、配置模型、检索记忆）
uv run qm
uv run quietmemo        # 同上，等价别名
```

主菜单里可以选择“配置模型 / API Key”，不需要先记命令。也可以直接运行 `uv run qm setup` 做同样的配置。

`qm setup` 会写入配置文件（默认 `quiet-memo.toml`），初始化记忆库，并可选做一次连接测试。Key 也可以用环境变量提供，环境变量优先于 keyring：

```bash
export DEEPSEEK_API_KEY=sk-...
```

手动配置示例：

```toml
vault = "C:/Users/you/.quiet-memo/vault"
confidence_threshold = 0.8
auto_push = false

[llm]
provider = "deepseek"          # litellm provider：deepseek/siliconflow/dashscope/openrouter/openai/custom
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
api_key_env = "DEEPSEEK_API_KEY"
reasoning_effort = ""         # low / medium / high；仅模型支持时生效
fallbacks = []                 # 例如 ["openai/gpt-4o-mini"]
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
extract_on_exit = true
```

Windows Git Bash 下如果中文输出乱码，加 `export PYTHONUTF8=1`。交互菜单和聊天依赖 Windows 控制台，建议用 Windows Terminal / PowerShell；Git Bash 可尝试 `winpty`。

## 对话 Agent

直接运行 `qm` 或 `quietmemo` 进入主菜单；选择“开始对话”后进入多轮 Agent。也可以 `qm chat` 直接进入。

- 每轮自动检索相关长期记忆，注入上下文。
- 模型可调用 `recall / memorize / correct / forget / review_pending / list_topics`。
- 会话持久化为 `sessions/<id>.jsonl`，可 `qm chat --resume <id>` 恢复。
- 退出时抽取本轮尚未沉淀的用户陈述。
- 记忆条目记录 `session` 和 `message` 来源，可追溯到原始对话。

会话内命令：

| 命令 | 作用 |
| --- | --- |
| `/new` | 结束当前会话并新建 |
| `/sessions` | 列出历史会话 |
| `/resume <id>` | 恢复会话 |
| `/compact` | 立即压缩上下文 |
| `/summary` | 查看当前会话摘要 |
| `/status` | 会话、模型、上下文和待确认状态 |
| `/models` | 在 TUI 内切换模型（显示上下文上限与 reasoning 支持） |
| `/setup` | 完整配置模型 / API Key（含供应商、思考强度、上下文长度） |
| `/rename <title>` | 重命名会话 |
| `/recall <query>` | 直接检索记忆 |
| `/remember <text>` | 直接抽取写入 |
| `/pending` | 查看待确认候选 |
| `/exit` | 退出 |

### 上下文压缩

当估算 token 超过预算的 `compact_threshold` 时：

1. 按 `keep_recent_tokens` 保留最近对话（以用户消息为边界）。
2. 把即将被淘汰的用户陈述送入记忆抽取，先沉淀到 Markdown。
3. 用 LLM 把旧对话压缩成摘要，持久化为 `summary` 记录。
4. 下一轮只带“摘要 + 最近原文 + 新消息”。

摘要记录包含 `covered_records`，恢复会话时不会重复发送已被压缩的历史。压缩失败时回退为抽取式摘要，不阻塞对话。

## 命令行

```bash
# 从一段对话抽取并静默写入
uv run qm memorize "我最近在找市区的房子，预算 4000 以内，不接受合租"

# 直接记录（不调用 LLM）
uv run qm note "喝咖啡偏好清淡、像茶的风味" --topic preferences --title 偏好

# 检索 / 查看待确认 / 确认或拒绝
uv run qm recall "市区"
uv run qm pending
uv run qm approve p-20261022-140312-a1b2
uv run qm reject p-20261022-140312-c3d4

# 会话与提交
uv run qm sessions
uv run qm status
uv run qm commit --no-push
uv run qm history
```

### 接入 MCP

```bash
uv run qm serve --vault ~/.quiet-memo/vault
```

MCP 客户端配置：

```json
{
  "mcpServers": {
    "quiet-memo": {
      "command": "uv",
      "args": ["run", "--directory", "C:/path/to/quiet-memo", "qm", "serve"]
    }
  }
}
```

暴露工具：`memorize`、`note`、`recall`、`review_pending`、`approve`、`reject`、`list_topics`、`history`、`commit`、`status`。

## 写入规则

| 情况 | 行为 |
| --- | --- |
| 置信度 ≥ 阈值、非敏感、无冲突 | 直接写入 |
| 置信度低于阈值 | 进入待确认队列 |
| 数值/否定/高相似度冲突 | 进入待确认队列，保留冲突条目 ID |
| 敏感内容 | 进入待确认队列 |
| 密码、验证码、API Key | 直接拦截，不落盘 |
| 文本完全重复 | 跳过 |

记忆按主题存为 `memories/<topic>.md`，条目带稳定 ID、状态、时间戳、来源和置信度：

```markdown
- <!-- id=mem-20261022-194250-8f1e; status=active --> 2026-10-22T19:42:50 — 在找市区的房子  (source: session:s-... , confidence: 0.95, session: s-..., message: m-...)
```

更正采用“新条目 + `supersedes`”，旧条目状态改为 `superseded`；删除是软删除 `deleted`，操作写入 `.quiet-memo/audit.jsonl`，Git 可回滚。

## 结构

```text
src/quiet_memo/
├── app/          cli.py, server.py              # 入口：Typer CLI / FastMCP
├── agent/        runtime.py, context.py, tools.py, session.py
│                                                   # Agent 循环、压缩、工具、会话
├── memory/       service.py, store.py, models.py, queue.py,
│                 policy.py, detect.py, extractor.py, audit.py
│                                                   # 记忆领域与写入裁决
├── retrieval/    base.py, keyword.py             # Retriever 接口 + BM25
├── llm/          client.py, summarizer.py, commit_summary.py
│                                                   # LiteLLM 适配、会话/提交摘要
└── infra/        config.py, providers.py, gitops.py, tokens.py
                                                   # 配置、供应商、Git、token
```

依赖方向：`app → agent → memory → retrieval → infra`，`memory/agent → llm → infra`。

- 检索当前是 jieba 分词 + rank-bm25；`Retriever` 是接口，后续可接向量/混合检索。
- token 预算用 tiktoken 估算，离线失败时回退字符估算。
- 会话压缩、记忆裁决、来源链是自研核心；模型适配交给 LiteLLM，供应商选择与密码输入用 questionary，Key 用 keyring。

## 测试

```bash
uv run pytest
```

当前 59 个测试，覆盖存储、队列、冲突检测、检索、抽取解析、会话、上下文压缩、Agent 工具循环、MCP 和 CLI。LLM 路径用固定抽取器和假模型测试；未接真实 API Key 时不声称已完成线上验证。

## Roadmap

- 20+ 条抽取/冲突/检索评测用例与指标
- 真实模型 dogfood 与成本统计
- 向量/混合检索实现（替换 `Retriever`）
- 每日定时提交守护进程
