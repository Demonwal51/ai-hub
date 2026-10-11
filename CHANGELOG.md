# Changelog

## 0.5.0 beta — not published yet

- Regenerate the last reply (with the same AI or another one); edit a request
  in a new conversation that starts from there (a branch).
- Compare two AIs: another one answers the same request, read only, beside the
  reply; « Garder celle-ci ».
- Instructions for a conversation and for a project, at each request.
- Read a reply aloud; keyboard shortcuts (Ctrl+K, Ctrl+Shift+N, Échap, ?).
- The server's dashboard (the owner's); reading without the PC on a device
  that chose it.

## 0.4.0 beta — not published (part of 0.5)

- Files and images with a request (📎, a pasted screenshot, files dropped):
  a text file's text in the request, images seen by Codex and the local AI,
  read by Claude Code; shown under the request.
- Dictation (🎤), favorite requests (★) kept on the server for each person.
- Export of a conversation: Markdown, AI Hub's JSON (read back by the
  import), a page to print or save as PDF.
- Consumption per person and per AI (owner and admins).
- One-click update from GitHub's releases, the old version kept, undone if
  anything fails.
- Installation: one .zip and installer.bat (a Desktop shortcut).

## 0.3.0 beta — 2026-10-11

- A new server is created from its page, on its own computer: the owner's
  name, and two optional passwords (the owner's, the server's).
- Accounts without a password; the server's own password, asked once per
  device. Adding someone: the owner (or an admin) chooses the name and the
  password, or makes a link and the person chooses.
- Roles: invité (what is shared), membre (also their own conversations, in a
  folder of theirs, within limits), admin (everything but the settings). The
  role and the accesses (projects, conversations, read or write) are chosen
  when the person is added.
- A phone connected to an account by a QR code (once, 15 minutes), named
  (« Téléphone de Lou »); devices named in « Appareils ».
- First login: connect Claude and/or ChatGPT before going on; the owner
  signs in the computer's accounts from the hub too.
- Import: conversations of ChatGPT and Claude (their exports) and of Claude
  Code and Codex (this computer's sessions), one by one or merged into one.
- Interface: the conversation list first, the quota on one line, menus,
  Entrée to send, drafts kept, « Copier », « nouveau » marks, long
  conversations loaded in part.

## 0.2.0 beta — 2026-10-11

Several people on one server, each with their own AI accounts.

- People: the owner invites by a one-use link (7 days); each person has a
  name and a password, and logs in with them.
- Sharing of a conversation or of a whole project, read or read and write.
  What is not shared answers "introuvable"; what is the owner's stays so.
- Each person's own Claude and Codex accounts ("Mes comptes IA", sign-in
  from the hub): their requests, plans, delegations and quota use them,
  never the owner's.
- Authors on the requests; a permission prompt answered only by the person
  whose request it is; alerts only about what is shared with you, quota
  alerts only about your own accounts.
- The owner can also make an account directly (name and password to pass
  on), and rename anyone, themselves too.

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
