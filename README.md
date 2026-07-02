# Luclas `v0.1.0`

Luclas is a self-evolving AI agent. It starts empty and grows through use.

Most AI assistants are static — same behavior on day one as day one thousand. Luclas is different: every task it runs, every mistake it makes, every correction you give it gets written into a persistent memory and a self-managed policy file (`core.md`). The agent reads its own history before acting, and can rewrite its own operating rules mid-task when it finds a better way.

The result is an assistant that gets meaningfully better at *your specific work* the more you use it — not better at everything in general, but better at the things you actually ask it to do.

## How growth works

Luclas has three layers of self-improvement:

1. **Experience memory** — after every task, what happened, what worked, and what failed is stored in SQLite and retrieved as context for future similar tasks. The agent learns from its own track record.

2. **Self-updating policy** — `data/core.md` is the agent's operating manual. The agent can rewrite it when it identifies a better strategy. Every version is snapshotted, so you can diff the evolution over time.

3. **Zero pre-loaded knowledge** — the database starts empty. Everything Luclas knows about your domain, your workflows, your preferences, it learned from working with you. This means two Luclas instances raised on different work will behave very differently.

## The risk: drift

Because Luclas writes its own rules, it can go wrong in ways a static assistant cannot. If it develops a bad habit — overcautious, sloppy about a certain task type, optimizing for the wrong outcome — that pattern gets reinforced across future tasks until you correct it.

**You are responsible for steering it.** Luclas grows toward whatever behavior you reward with continued use and corrects away from whatever you explicitly push back on.

Practical safeguards:
- Read `data/core.md` periodically. It's a plain-text file; you can edit it directly.
- When Luclas does something wrong, say so explicitly — "that approach was wrong because X" is more useful than silence or a vague "try again".
- Use `/history` to review what it's been doing and whether the patterns look right.
- Use `core.md` snapshots (`/core history`) to see how its rules have changed.

## How to get the most out of it

Luclas grows faster with real work than with test questions.

- **Give it actual tasks**, not demos. A real failed attempt teaches more than a successful toy example.
- **Correct it in context.** When it makes a mistake mid-task, use Ctrl-C to pause and inject the correction rather than waiting until the end.
- **Don't over-specify.** Luclas is designed to figure out *how* to do things. Tell it *what* you want and let it decide the approach — then correct the approach if it's wrong.
- **Let it fail sometimes.** Failure with explicit feedback is the fastest path to improvement. Don't only give it easy tasks.

## Features

- **Recursive task decomposition** — the LLM decides whether a goal needs subtasks, with no fixed depth limit.
- **Long-term memory** — searchable SQLite store with tags, importance scores, and semantic search (sentence-transformers + cosine similarity, keyword fallback).
- **Episodic memory** — recent tasks injected into context; older ones archived; very old batches compressed into LLM-written summaries.
- **Tool use** — shell, Python (subprocess-isolated), file ops, grep/find, HTTP, web search/fetch, memory read/write, scheduled tasks.
- **Messaging adapters** — receive tasks and push results via WeCom (企业微信); more platforms coming.
- **HTTP API** — submit tasks asynchronously, poll for results, integrate with external systems.
- **Scheduled tasks** — daily/weekly/one-shot tasks set via natural language; results routed back to the channel that created them.
- **i18n** — CLI display language via `LUC_LANG` (`en` default, `zh` supported).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # set LUC_LLM_BASE_URL and LUC_LLM_MODEL
./luclas.sh
```

On first run Luclas generates its own `data/core.md` by asking the LLM to write an initial policy. From that point on, it owns the file.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LUC_LANG` | `en` | CLI display language (`en` / `zh`) |
| `LUC_LLM_BASE_URL` | `http://localhost:8003/v1` | OpenAI-compatible endpoint |
| `LUC_LLM_MODEL` | `qwen3.6-27b-awq-int4` | Model name |
| `LUC_LLM_API_KEY` | `none` | API key if required |
| `LUC_API_KEY` | _(none)_ | Auth key for the HTTP API |
| `LUC_EMBED_MODEL` | language-dependent | sentence-transformers model for memory search |

### Private policy customization

Create `data/core.local.md` to override `data/core.md` without touching the tracked default. This file is gitignored — use it for domain-specific instructions, business workflows, or constraints you don't want in the public repo.

## Project layout

```
luclas.sh                launcher script
luclas/
  luclas.py            CLI entry point, slash commands, bootstrap
  setup.py             interactive setup wizard (luclas setup)
  api.py               HTTP API (FastAPI)
  cron_runner.py       scheduled task runner (crontab-driven)
  config.py            env-driven configuration
  i18n.py              CLI display strings
  llm_client.py        OpenAI-compatible chat client
  loops/
    agent_loop.py      core LLM ↔ tool execution loop
    task_runner.py     recursive decompose/execute/merge
  memory/
    database.py        SQLite schema and migrations
    store.py           long-term memory
    task_memory.py     episodic task history
  tools/               shell/python/file/search/http/web/memory/schedule tools
  adapters/
    wecom.py           WeCom (企业微信) adapter
    whatsapp.py        WhatsApp Business Cloud API adapter
    discord_adapter.py Discord bot adapter
```

## Roadmap

- [ ] **Popular LLM support** — first-class integration with OpenAI, Anthropic Claude, Google Gemini, and other hosted providers
- [x] **Popular messaging platforms** — WeCom, WhatsApp, Discord supported; Telegram, Slack coming
- [ ] **Telegram adapter**
- [ ] **Slack adapter**

## License

MIT — see [LICENSE](LICENSE).
