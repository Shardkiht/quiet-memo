**English** | [中文](README.md)

# quiet-memo

A quiet personal memory store: it extracts facts, preferences, and plans about **you** from everyday conversations and silently writes them to local Markdown by default. Only low-confidence, conflicting, or sensitive content goes to a confirmation queue.

- **Local-first**: Markdown is the single source of truth; Git handles versioning and audit.
- **Silent by default**: Trusted content is written directly without interrupting you item by item; it only prompts when necessary.
- **Full agent**: interactive chat, persisted sessions, context compaction, and a tool loop.
- **Multi-model**: LiteLLM integration for DeepSeek, SiliconFlow, DashScope, OpenRouter, OpenAI, and more.
- **Reversible**: Each commit automatically generates a Chinese summary for your review.

## Quick start

Requires Python 3.12+, uv, and git.

```bash
uv sync

# Enter chat directly (a session is created when you send the first message)
uv run qm
uv run quietmemo        # Same as above; equivalent alias
```

On first use, run `uv run qm setup` to configure the model and API key, or type `/setup` in chat.

`qm setup` writes the config file (default: `quiet-memo.toml`), initializes the vault, and optionally runs a connection test. Keys can also be provided through environment variables, which take precedence over keyring:

```bash
export DEEPSEEK_API_KEY=sk-...
```

Manual configuration example:

```toml
vault = "C:/Users/you/.quiet-memo/vault"
confidence_threshold = 0.8
auto_push = false

[llm]
provider = "deepseek"          # litellm provider: deepseek/siliconflow/dashscope/openrouter/openai/custom
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"
reasoning_effort = ""         # low / medium / high; only effective when supported by the model
fallbacks = []                 # e.g. ["openai/gpt-4o-mini"]
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

If Chinese output is garbled in Windows Git Bash, add `export PYTHONUTF8=1`. Interactive menus and chat rely on the Windows console; Windows Terminal or PowerShell is recommended. Git Bash users can try `winpty`.

## Chat agent

Run `qm` or `quietmemo` to enter the chat UI; a session is created when you send the first message. `qm chat` is equivalent. Type `/` to open the command menu with descriptions. Selection, input, and confirmation screens can all be cancelled with `ESC`. Chat content is centered and occupies about 36% of the window; the bottom status bar shows the current model and context usage.

- Automatically retrieves relevant long-term memories every turn and injects them into context.
- The model can call `recall / memorize / correct / forget / review_pending`.
- No session is created when entering chat; `sessions/<id>.jsonl` is created after the first message.
- Resuming a session replays previous user messages and assistant replies; use `qm chat --resume <id>`.
- On exit, extracts user statements from the turn that have not yet been persisted.
- Memory entries record `session` and `message` sources, so they can be traced back to the original conversation.

In-session commands:

| Command | Action |
| --- | --- |
| `/new` | End the current session and start a new one (created after the first message) |
| `/sessions` | Session manager: Enter to resume, Delete to delete (with confirmation), ESC to go back, search supported |
| `/resume [id|query]` | Resume a session; with no argument, opens a sorted session picker; with a keyword, filters first |
| `/compact` | Compact the context immediately |
| `/summary` | View the current session summary |
| `/status` | Session, model, context, and pending status |
| `/models` | Switch models inside the TUI (only lists providers with configured keys, showing readable name/context/reasoning; the list is cached for 12 hours; `/models refresh` forces a refresh) |
| `/setup` | Full model / API key setup (provider, reasoning effort, context length) |
| `/rename <title>` | Rename the session |
| `/recall <query>` | Search memories directly |
| `/remember <text>` | Extract and persist directly |
| `/pending` | View pending candidates |
| `/exit` | Exit |

`/help` shows command descriptions; `/quit` is the same as `/exit`; `/model` is the same as `/models`.

### Context compaction

When estimated tokens exceed `compact_threshold` of the budget:

1. Keep the most recent conversation according to `keep_recent_tokens` (boundary at user messages).
2. Feed user statements that are about to be evicted into memory extraction and persist them to Markdown first.
3. Compress older conversation into a summary with the LLM and persist it as a `summary` record.
4. The next turn includes only the summary + recent raw messages + new messages.

The summary record includes `covered_records`, so resuming a session does not resend compacted history. If compaction fails, it falls back to an extractive summary and does not block the chat.

## Command line

```bash
# Extract from a conversation and write silently
uv run qm memorize "I'm looking for an apartment downtown, budget under 4000, no shared housing"

# Record directly (no LLM call)
uv run qm note "I prefer light, tea-like coffee" --topic preferences --title Preferences

# Initialize / list topics
uv run qm init
uv run qm topics

# Search / review pending / approve or reject
uv run qm recall "downtown"
uv run qm pending
uv run qm approve p-20261022-140312-a1b2
uv run qm reject p-20261022-140312-c3d4

# Sessions and commits
uv run qm sessions
uv run qm status
uv run qm commit --no-push
uv run qm history
```

### MCP integration

```bash
uv run qm serve --vault ~/.quiet-memo/vault
```

MCP client configuration:

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

Exposed tools: `memorize`, `note`, `recall`, `review_pending`, `approve`, `reject`, `list_topics`, `status`.

## Write rules

| Situation | Behavior |
| --- | --- |
| Confidence ≥ threshold, not sensitive, no conflict | Write directly |
| Confidence below threshold | Queue for confirmation |
| Numeric / negation / high-similarity conflict | Queue for confirmation and keep the conflicting entry IDs |
| Sensitive content | Queue for confirmation |
| Passwords, verification codes, API keys | Blocked outright, never written |
| Exact duplicate text | Skipped |

Memories are stored per topic as `memories/<topic>.md`, and entries carry a stable ID, status, timestamp, source, and confidence:

```markdown
- 2026-10-22T19:42:50 — Looking for an apartment downtown  `id=mem-20261022-194250-8f1e` `status=active` `source=session:s-...` `confidence=0.95` `session=s-...` `message=m-...`
```

Corrections use a "new entry + `supersedes`" pattern, and the old entry's status becomes `superseded`; deletion is a soft delete with status `deleted`. Operations are written to `.quiet-memo/audit.jsonl`, and Git can roll them back.

## Structure

```text
src/quiet_memo/
├── app/          cli.py, server.py, tui.py, ui.py, model_ui.py,
│                 session_ui.py                    # CLI / TUI / FastMCP entry points
├── agent/        runtime.py, context.py, tools.py, session.py
│                                                   # Agent loop, compaction, tools, sessions
├── memory/       service.py, store.py, models.py, queue.py,
│                 policy.py, detect.py, extractor.py, audit.py
│                                                   # Memory domain and write decisions
├── retrieval/    base.py, keyword.py             # Retriever interface + BM25
├── llm/          client.py, summarizer.py, commit_summary.py
│                                                   # LiteLLM adapter, session/commit summaries
└── infra/        config.py, providers.py, gitops.py, tokens.py,
                  layout.py, prompts.py             # Config, providers, Git, tokens, terminal
```

Dependency direction: `app → agent → memory → retrieval → infra`, `memory/agent → llm → infra`.
`retrieval/keyword.py` reads entries from `memory/store.py`; this is the only back-reference across `memory ↔ retrieval`.
Development conventions (code style, storage invariants, testing, and docs sync) are in [AGENTS.md](AGENTS.md).

- Retrieval currently uses jieba tokenization + rank-bm25; `Retriever` is an interface, so vector or hybrid retrieval can be added later.
- Token budgets are estimated with tiktoken, falling back to character estimation when offline.
- Session compaction, memory decisions, and source chains are custom-built; model adapters are delegated to LiteLLM, provider selection and password input use questionary, and keys use keyring.

## Testing

```bash
uv run pytest
```

Tests cover storage, queue, conflict detection, retrieval, extraction parsing, sessions, context compaction, the agent tool loop, MCP, and the CLI. LLM paths are tested with a static extractor and fake models; no claim of live verification is made without a real API key.

## Roadmap

- 20+ extraction/conflict/retrieval evaluation cases and metrics
- Real-model dogfooding and cost tracking
- Vector/hybrid retrieval implementation (replacing `Retriever`)
- Daily scheduled commit daemon
