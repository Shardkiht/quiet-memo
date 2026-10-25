**English** | [中文](README.md)

[![test](https://github.com/Shardkiht/quiet-memo/actions/workflows/test.yml/badge.svg)](https://github.com/Shardkiht/quiet-memo/actions/workflows/test.yml) [![PyPI](https://img.shields.io/pypi/v/quiet-memo)](https://pypi.org/project/quiet-memo/)

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

# Optional: enable local vector retrieval (fastembed + bge-small-zh; ~100MB model download on first run)
uv sync --extra local-embed

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
recall_rewrite = true         # Rewrite follow-up queries with the LLM; falls back without a key
recall_context_turns = 2      # Recent user turns used for follow-up queries
extract_on_exit = true

[retrieval]
method = "hybrid"             # keyword / vector / hybrid; falls back to keyword when the embedding backend is unavailable

[retrieval.embedding]
backend = "local"             # local: offline fastembed inference; api: sends memory text to a provider (privacy note)
model = "BAAI/bge-small-zh-v1.5"
# Example for backend = "api":
# provider = "siliconflow"
# base_url = "https://api.siliconflow.cn/v1"
# api_key_env = "SILICONFLOW_API_KEY"
```

If Chinese output is garbled in Windows Git Bash, add `export PYTHONUTF8=1`. Interactive menus and chat rely on the Windows console; Windows Terminal or PowerShell is recommended. Git Bash users can try `winpty`.

## Chat agent

Run `qm` or `quietmemo` to enter the full-screen chat UI (built on Textual; the whole screen reflows when you resize the window); a session is created when you send the first message. `qm chat` is equivalent. The collapsible left sidebar lists sessions: arrow keys to browse, Enter to resume, Del to delete with confirmation; click « at the sidebar's top-right corner (or Ctrl+B) to collapse, and » appears at the chat's top-left corner to expand. The main pane is a wide two-column layout: user messages appear as right-aligned bubbles, assistant replies span the left with speaker headers and times. Typing `/` opens a vertical command palette above the input (arrows to select, Enter to run, ESC to dismiss, rows clickable); the bottom status bar shows the model with retrieval mode, context usage as a progress line with used/total numbers, and the pending count, warning past the compaction threshold. Tool and retrieval events stream in as cards, turns are separated by a divider, and exiting (Ctrl+Q or `/exit`) distills the turn's statements with a summary.

- Automatically retrieves relevant long-term memories every turn and injects them into context; follow-ups and memory cues such as "remember" or "last time" include the last 1-2 user turns in the search query and raise the result limit.
- The model can call `recall / memorize / correct / forget / review_pending`.
- No session is created when entering chat; `sessions/<id>.jsonl` is created after the first message.
- Resuming a session replays previous user messages and assistant replies; use `qm chat --resume <id>`.
- On exit, extracts user statements from the turn that have not yet been persisted.
- Memory entries record `session` and `message` sources, so they can be traced back to the original conversation.

In-session commands:

| Command | Action |
| --- | --- |
| `/new` | End the current session and start a new one (created after the first message) |
| `/sessions` | Jump to the left session list (arrows to browse, Enter to resume, Del to delete) |
| `/resume [id\|query]` | Resume a session; without arguments jumps to the session list, an id/keyword resumes the match directly |
| `/compact` | Compact the context immediately |
| `/summary` | View the current session summary |
| `/status` | Session, model, retrieval mode, context, and pending status |
| `/models` | Switch models inside the TUI (only lists providers with configured keys, showing readable name/context/reasoning; the list is cached for 12 hours; `/models refresh` forces a refresh) |
| `/think` | Switch reasoning effort: default / low / medium / high, only when the model supports it; applies immediately and persists to the config |
| `/context` | Switch the context window: presets generated from the current model's limit, custom value allowed; effective on the next turn |
| `/setup` | Reminds you to run `qm setup` in the terminal (the setup wizard is not in the chat UI) |
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

### Custom persona

Put a `SOUL.md` at the vault root (default `~/.quiet-memo/vault/SOUL.md`) with your persona description; it takes effect on the next turn, no restart needed:

```markdown
You are Ache, my old friend. Keep replies concise, occasionally dry-humored, and address me by name.
```

- `qm init` generates a `SOUL.md` pre-filled with the default persona; edit it freely. Deleting or emptying the file falls back to the built-in default.
- The persona only replaces the persona section of the system prompt; tool descriptions and memory discipline (only record self-stated facts, never fabricate on empty recall, etc.) are always appended after it and cannot be overridden.
- Content beyond roughly 2000 tokens is truncated.
- SOUL.md is an instruction file, not a memory: it never enters extraction or retrieval; it lives in the vault, is versioned by Git, and changes are traceable.

## Command line

```bash
# Extract from a conversation and write silently
uv run qm memorize "I'm looking for an apartment downtown, budget under 4000, no shared housing"

# Record directly (no LLM call)
uv run qm note "I prefer light, tea-like coffee" --topic preferences --title Preferences

# Import existing Markdown (AI restructures into entries under a strict contract,
# the program adjudicates and writes; dry-run by default, --apply to write)
uv run qm import ~/personal-memory/profile
uv run qm import ~/personal-memory/profile --apply

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
├── app/          cli.py, server.py, tui.py, chat_app.py, chat_widgets.py,
│                 actions.py, ui.py, session_ui.py   # CLI / Textual TUI / FastMCP entry points
├── agent/        runtime.py, prompts.py, context.py, tools.py,
│                 session.py, recall.py              # Agent loop, system prompt, compaction, tools, sessions
├── memory/       service.py, store.py, models.py, queue.py,
│                 policy.py, detect.py, extractor.py, audit.py
│                                                   # Memory domain and write decisions
├── retrieval/    base.py, keyword.py, vector.py, hybrid.py
│                                                   # Retriever interface + BM25/vector/hybrid
├── llm/          client.py, summarizer.py, commit_summary.py
│                                                   # LiteLLM adapter, session/commit summaries
└── infra/        config.py, providers.py, gitops.py, tokens.py,
                  layout.py, prompts.py             # Config, providers, Git, tokens, terminal
```

Dependency direction: `app → agent → memory → retrieval → infra`, `memory/agent → llm → infra`.
`retrieval/keyword.py` reads entries from `memory/store.py`; this is the only back-reference across `memory ↔ retrieval`.
Development conventions (code style, storage invariants, testing, and docs sync) are in [AGENTS.md](AGENTS.md).

- Retrieval defaults to hybrid: jieba + rank-bm25 and vector cosine runs are fused with RRF. Vectors come from local fastembed inference (optional extra) or an embedding API, cached as derived data in `.quiet-memo/vectors.npz` (gitignored inside the vault; delete it to force a rebuild; switching the embedding model invalidates it automatically). When the embedding backend is unavailable it falls back to plain BM25; `/status` and `qm status` show the active retrieval mode.
- Token budgets are estimated with tiktoken, falling back to character estimation when offline.
- Session compaction, memory decisions, and source chains are custom-built; model adapters are delegated to LiteLLM, provider selection and password input use questionary, and keys use keyring.

## Testing

```bash
uv run pytest
```

Tests cover storage, queue, conflict detection, retrieval, extraction parsing, sessions, context compaction, the agent tool loop, MCP, and the CLI. LLM paths are tested with a static extractor and fake models; no claim of live verification is made without a real API key.

## Retrieval evaluation

`evals/` ships a reproducible synthetic-corpus benchmark (456 entries / 27 cases, no personal data):

```bash
uv run python evals/retrieval_eval.py
```

| Retrieval | Recall@3 | Recall@5 | MRR@10 |
| --- | --- | --- | --- |
| keyword | 67% | 67% | 0.667 |
| vector | **89%** | **89%** | 0.815 |
| hybrid | 85% | 85% | 0.815 |

On a real 797-entry personal vault (anonymized, aggregate metrics only), after fixing a vector-matrix misalignment caused by duplicate entry IDs from bulk import, hybrid's MRR@10 rose from 0.569 to 0.745 — the best of the three. Methodology, fix details, and known weaknesses are in [evals/README.md](evals/README.md).

## Roadmap

- 20+ extraction/conflict evaluation cases and metrics (retrieval eval lives in `evals/`)
- Real-model dogfooding and cost tracking
- Daily scheduled commit daemon
