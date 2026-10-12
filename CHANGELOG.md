# Changelog

## 1.0.1 — 2026-10-12, guided access and diagnostics

- Three editable access presets: discussion without PC files, analysis of
  selected copies, and project work on copies reviewed before application.
- Visible access overview and durable emergency suspension, with owner-only
  reactivation. Suspension preserves project inheritance and private accounts.
- Pending scoped sign-ins and running work are cancelled on suspension;
  new jobs and sign-ins remain refused until reactivation.
- First-space guidance, direct result-review button and created/modified/deleted
  file counts. The composer shows why a suspended space cannot accept requests.
- Cached diagnostics for database, disk and isolation engine, plus scoped
  account status and concrete recovery steps without sending an AI message.


## 1.0.0 — 2026-10-12, prepared for publication

- Isolated conversations and projects with separate file, network and memory
  access; project limits also constrain existing child conversations.
- Docker Sandboxes microVM transport, private account sign-in per person and
  space, no host fallback, host MCP tools or forwarded host AI credentials.
- Selected files copied for read-only or copy-based editing. Review, download,
  and explicitly apply results with original-file conflict checks and backups.
- Local Ollama inference stays on loopback with read-only file tools in a VM.
- Members and guests require isolated execution; revoked accounts stop work.
  Recreated numeric user ids cannot reuse a deleted account's logged-in VM.
  Recreated conversation ids also receive a fresh space identity.
- Installers, launchers and desktop startup support for Linux; executable ZIP
  scripts and a Linux tar.gz package. App installation tested on Debian 13,
  Fedora 44 and Ubuntu 26.04. Docker's local engine requires a supported host.
- Clearer interface, device-specific appearance and density, mobile access
  forms and improved handling of reduced motion. Direct account button and
  links open the right isolated account dialog.
- Claude uses the official CLI image with a neutral shell profile: its OAuth
  login stays inside its own VM instead of being captured as a shared Docker
  credential. Global credentials remain refused.
- Loopback-only installs also reject cross-site API writes and rebinding Host
  headers. Delayed session responses cannot replace another account cache.
- Live access revocation and offline-cache identity checks, including delayed
  responses arriving after a change of account.
- Daily backups include attachment bytes and hashes; restore validates a staged
  copy before replacement. Update helpers preserve the database and check the
  new server before keeping the update, with rollback after failed startup.

Human sign-in and real replies from Claude and Codex have been validated in
separate Windows-hosted microVMs. A native Linux sandbox engine still requires
manual validation. Python dependency changes are not rolled back by updates.



## 0.6.0 beta — not published yet

- Personal pins and archives, without changing anyone else's shared list.
- Search filters by project, AI, dates and archives; permissions and filters
  applied before ranking, for word and semantic results alike.
- Work templates with fields, personal templates from a conversation's
  settings and draft; creating from one never sends the request itself.
- Richer reply rendering: headings, tables, lists, quotes, safe links and
  code copying; keep the reading position while a reply is streamed.
- Fix manual summaries using the server's accounts for guests; account-aware
  model selection and routing, and the sender's access in orchestration synthesis.
- Branches retain instructions, tools and independent copies of attachments.
- Recover interrupted comparisons, delegations and scheduled run status;
  explicit read-only routing when only a local model is available.

## 0.5.0 beta — 2026-10-11

- Regenerate the last reply (with the same AI or another one); edit a request
  in a new conversation that starts from there (a branch).
- Compare two AIs: another one answers the same request, read only, beside the
  reply; « Garder celle-ci ».
- Instructions for a conversation and for a project, at each request.
- Read a reply aloud; keyboard shortcuts (Ctrl+K, Ctrl+Shift+N, Échap, ?).
- The server's dashboard (the owner's); reading without the PC on a device
  that chose it.

## 0.4.0 beta — included in 0.5

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
