# Changelog

## 0.1.0 beta — 2026-10-10

First published version.

- Providers: Claude Code (stream-json), Codex (`codex app-server`, JSON-RPC),
  a local model through Ollama. Three permission modes: manual, plan,
  bypass (with a warning to confirm).
- Conversations with one AI, or orchestrated: the conductor plans, agents run
  the subtasks in parallel without writing the same file at the same time,
  the conductor sums up. Automatic assignment from independent benchmarks.
- Changing the AI of a conversation: the next one starts from the memory.
- Memory (RAG): an index of every message, by words (SQLite FTS5) and by
  meaning (local embeddings), fresh starts within a token cap.
- Projects: conversations that read each other (read-only MCP server).
- Agentic work: autonomous runs with a check command, scheduled tasks, tools
  per conversation (web search, Obsidian vault, MCP servers), delegation to
  another AI.
- Web interface, installable on a phone (PWA), through Tailscale, with a
  password (scrypt), sessions of a year per device, lockout, encrypted phone
  alerts (Web Push) or ntfy.
- Daily backups, deletion for good (also from the backups), settings window,
  project notes in an Obsidian vault.
