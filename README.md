**中文** | [English](README.en.md)

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

# 可选：启用本地向量检索（fastembed + bge-small-zh，首次运行下载约 100MB 模型）
uv sync --extra local-embed

# 直接进入对话（发送第一条消息时才创建会话）
uv run qm
uv run quietmemo        # 同上，等价别名
```

首次使用先运行 `uv run qm setup` 配置模型和 API Key，也可以在对话里输入 `/setup`。

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
model = "deepseek-flash"
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
recall_rewrite = true         # 用 LLM 改写追问；无 Key 自动回退到拼接
recall_context_turns = 2      # 追问时参考最近几轮用户消息
extract_on_exit = true

[retrieval]
method = "hybrid"             # keyword / vector / hybrid；向量后端不可用时自动回退 keyword

[retrieval.embedding]
backend = "local"             # local：fastembed 离线推理；api：发送记忆文本给供应商（注意隐私）
model = "BAAI/bge-small-zh-v1.5"
# backend = "api" 时的示例：
# provider = "siliconflow"
# base_url = "https://api.siliconflow.cn/v1"
# api_key_env = "SILICONFLOW_API_KEY"
```

Windows Git Bash 下如果中文输出乱码，加 `export PYTHONUTF8=1`。交互菜单和聊天依赖 Windows 控制台，建议用 Windows Terminal / PowerShell；Git Bash 可尝试 `winpty`。

## 对话 Agent

直接运行 `qm` 或 `quietmemo` 进入对话界面；会话在发送第一条消息时创建。`qm chat` 等价。输入 `/` 会弹出命令菜单和说明。选择、输入和确认界面都可以按 `ESC` 取消。对话内容居中显示，约占窗口 36%；底部状态栏显示当前模型和已用上下文。

- 每轮自动检索相关长期记忆，注入上下文；追问和“还记得 / 之前”等线索会带上最近 1-2 轮用户消息构造查询，并提高召回数量。
- 模型可调用 `recall / memorize / correct / forget / review_pending`。
- 进入对话时不立即建会话，发送第一条消息后才创建 `sessions/<id>.jsonl`。
- 恢复会话会回放此前的用户消息和助手回复；可 `qm chat --resume <id>` 恢复。
- 退出时抽取本轮尚未沉淀的用户陈述。
- 记忆条目记录 `session` 和 `message` 来源，可追溯到原始对话。

会话内命令：

| 命令 | 作用 |
| --- | --- |
| `/new` | 结束当前会话，进入新会话（发送首条消息后创建） |
| `/sessions` | 会话管理：Enter 恢复、Delete 删除（弹确认）、ESC 返回、支持搜索 |
| `/resume [id|query]` | 恢复会话；无参数打开排序后的会话选择器，带关键词则先过滤 |
| `/compact` | 立即压缩上下文 |
| `/summary` | 查看当前会话摘要 |
| `/status` | 会话、模型、检索模式、上下文和待确认状态 |
| `/models` | 在 TUI 内切换模型（只列已配置 Key 的供应商，显示可读名称/上下文/reasoning；列表缓存 12 小时，`/models refresh` 强制刷新） |
| `/think` | 切换思考强度：默认 / low / medium / high，仅模型支持时可选，实时生效并写回配置 |
| `/context` | 切换上下文长度：档位按当前模型上限生成，也可自定义，下一轮即生效 |
| `/setup` | 完整配置模型 / API Key（含供应商、思考强度、上下文长度） |
| `/rename <title>` | 重命名会话 |
| `/recall <query>` | 直接检索记忆 |
| `/remember <text>` | 直接抽取写入 |
| `/pending` | 查看待确认候选 |
| `/exit` | 退出 |

`/help` 查看命令说明；`/quit` 同 `/exit`；`/model` 同 `/models`。

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

# 导入已有 Markdown（AI 按契约整理成条目，程序裁决写入；默认 dry-run，--apply 落盘）
uv run qm import ~/personal-memory/profile
uv run qm import ~/personal-memory/profile --apply

# 初始化 / 查看主题
uv run qm init
uv run qm topics

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

暴露工具：`memorize`、`note`、`recall`、`review_pending`、`approve`、`reject`、`list_topics`、`status`。

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
- 2026-10-22T19:42:50 — 在找市区的房子  `id=mem-20261022-194250-8f1e` `status=active` `source=session:s-...` `confidence=0.95` `session=s-...` `message=m-...`
```

更正采用“新条目 + `supersedes`”，旧条目状态改为 `superseded`；删除是软删除 `deleted`，操作写入 `.quiet-memo/audit.jsonl`，Git 可回滚。

## 结构

```text
src/quiet_memo/
├── app/          cli.py, server.py, tui.py, ui.py, model_ui.py,
│                 session_ui.py                    # CLI / TUI / FastMCP 入口
├── agent/        runtime.py, context.py, tools.py, session.py
│                                                   # Agent 循环、压缩、工具、会话
├── memory/       service.py, store.py, models.py, queue.py,
│                 policy.py, detect.py, extractor.py, audit.py
│                                                   # 记忆领域与写入裁决
├── retrieval/    base.py, keyword.py, vector.py, hybrid.py
│                                                   # Retriever 接口 + BM25/向量/混合
├── llm/          client.py, summarizer.py, commit_summary.py
│                                                   # LiteLLM 适配、会话/提交摘要
└── infra/        config.py, providers.py, gitops.py, tokens.py,
                  layout.py, prompts.py             # 配置、供应商、Git、token、终端
```

依赖方向：`app → agent → memory → retrieval → infra`，`memory/agent → llm → infra`。
`retrieval/keyword.py` 读取 `memory/store.py` 的条目，是 `memory ↔ retrieval` 之间唯一的跨层回读。
开发约定（代码风格、存储不变量、测试与文档同步）见 [AGENTS.md](AGENTS.md)。

- 检索默认 hybrid：jieba + rank-bm25 与向量余弦两路结果做 RRF 融合；向量由 fastembed 本地推理（可选 extra）或 embedding API 计算，缓存为 `.quiet-memo/vectors.npz` 派生数据（vault 内已 gitignore，删除后自动重建；换 embedding 模型自动失效）。向量后端不可用时回退纯 BM25，`/status` 和 `qm status` 显示当前检索模式。
- token 预算用 tiktoken 估算，离线失败时回退字符估算。
- 会话压缩、记忆裁决、来源链是自研核心；模型适配交给 LiteLLM，供应商选择与密码输入用 questionary，Key 用 keyring。

## 测试

```bash
uv run pytest
```

测试覆盖存储、队列、冲突检测、检索、抽取解析、会话、上下文压缩、Agent 工具循环、MCP 和 CLI。LLM 路径用固定抽取器和假模型测试；未接真实 API Key 时不声称已完成线上验证。

## 检索评测

`evals/` 提供可复现的合成语料基准（456 条 / 27 例，无任何个人数据）：

```bash
uv run python evals/retrieval_eval.py
```

| 检索方式 | Recall@3 | Recall@5 | MRR@10 |
| --- | --- | --- | --- |
| keyword | 67% | 67% | 0.667 |
| vector | **89%** | **89%** | 0.815 |
| hybrid | 85% | 85% | 0.815 |

在一个 797 条的真实个人记忆库上（脱敏，仅汇总指标），修复批量导入的条目 ID 碰撞导致的向量错位后，hybrid 的 MRR@10 从 0.569 升到 0.745，成为三路最佳。方法、修复细节与已知弱点见 [evals/README.md](evals/README.md)。

## Roadmap

- 20+ 条抽取/冲突评测用例与指标（检索评测已落地 `evals/`）
- 真实模型 dogfood 与成本统计
- 每日定时提交守护进程
