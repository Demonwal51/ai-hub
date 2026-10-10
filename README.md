# AI Hub

**Version 0.1.0 beta.** A personal web app, run on your own PC, to send
requests to several AI coding agents — Claude Code, Codex, and a local model
through Ollama — alone or orchestrated by a conductor agent, with saved
conversations, a memory that survives a change of AI, and access from a phone.

- One AI per conversation, or an orchestration: a conductor plans, several
  agents run the subtasks in parallel, the conductor sums up.
- Agentic work: autonomous runs toward a goal, scheduled tasks, tools chosen
  per conversation, an agent asking another AI for its opinion.
- Memory (RAG) by words and by meaning, projects whose conversations read
  each other, phone access through Tailscale with a password, encrypted phone
  alerts, daily backups.
- Everything stays on your PC: AI Hub drives the official CLIs with your own
  accounts and never reads their credentials.

This is a beta, made for one person on Windows (it should run elsewhere,
but has only been used on Windows 11). The interface and the messages are in
French. Licence: GPL-3.0 (see `LICENSE`).

## Orchestration

1. In a conversation created in "Orchestration" mode, the request goes to the
   conductor. It reads the project (read-only tools) and answers with a plan:
   subtasks, dependencies, the files each one changes, and how many agents to
   run at once — its own choice.
2. The plan can be edited before anything runs: agent and model of each
   subtask, subtasks added or removed, dependencies, an agent taken out or
   added. "Adapter le plan" has the conductor rework it after such changes.
3. Nothing runs before "Valider et lancer".
4. Subtasks whose dependencies are done run in parallel, up to the
   conductor's number. Subtasks that declare a common file never run at the
   same time, and an agent asking to write a file another running agent is
   writing waits until that one is done, before the user is even asked.
5. The conductor writes the synthesis, failures included.

With automatic assignment (per conversation), the conductor only classifies
each subtask by kind of work (code, terminal, writing, maths...) and
`aihub/routing.py` picks the AI from independent benchmarks, showing the
figure behind each choice. With Codex connected, the kinds where a GPT
model leads go to it (operations, security, hard algorithms and maths); the
reason says when the better model is not connected, or not offered by the
installed Codex. Changing a subtask's kind brings its AI along unless
the user chose the agent by hand; agents taken out of a plan stay out.

At any time but during a plan's execution, "Discuter avec le chef" talks with
the conductor instead of asking for a plan: it resumes its own session,
may read the project, changes nothing. While a plan is proposed, it sees the
plan as the user left it, and "Adapter le plan" then applies what was said.

## Requirements

- Python 3.12 or later
- The web dependencies, in a virtual environment:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```
- Claude Code, signed in with your own account. AI Hub runs the official CLI and
  never reads its credentials. It finds `claude` on the PATH, or the copy kept by
  the Claude desktop app under `%APPDATA%\Claude\claude-code\`.

Check that Claude Code is found and signed in:

```
python -m aihub status
```

If it is not signed in, run the command it prints (`claude auth login`) and sign
in in the browser.

- Optional: Codex (the GPT models of a ChatGPT subscription), signed in in the
  Codex app. AI Hub finds `codex` on the PATH, or the newest copy the Codex
  desktop app keeps (`%LOCALAPPDATA%\OpenAI\Codex\bin\<id>\`, or
  `%USERPROFILE%\.codex\.sandbox-bin\` for older apps; `serve --codex` for
  another path); it never reads `auth.json`.

## Web interface

Set the password once (typed twice, never shown; only its scrypt hash is
kept, and changing it closes every session):

```
.venv\Scripts\python -m aihub mot-de-passe
.venv\Scripts\python -m aihub serve
```

Then open http://127.0.0.1:8770/. `--allow D:\projets` (repeatable) limits
the working folders to those roots. Without a password, `serve` refuses to
start; `--sans-mot-de-passe` runs it for this computer only (requests from
another machine or relayed by a proxy are refused).

- Session: an HttpOnly, SameSite=Strict cookie; the password is typed once
  per device: a session ends only after a year without use
  (`--session-jours`), and when you log a device out ("Appareils") or change
  the password.
- Lockout: after 5 wrong passwords in 15 minutes (`--essais`), the address is
  locked 15 minutes (`--blocage-minutes`), doubled at each new lockout up to a
  day. Above 20 wrong passwords from all addresses, every login is locked.
- Backups: one copy of the database per day in `data/sauvegardes/`
  (`--sauvegardes`), the last 14 kept (`--garder`), `--sans-sauvegarde` to
  turn it off. Deleting a conversation also deletes it from every backup.
  To restore one: stop the server, then `python -m aihub restaurer` (lists
  them) and `python -m aihub restaurer aihub-AAAA-MM-JJ.db` (asks for a typed
  `oui`, keeps the current base as `data/avant-restauration-….db`).
- Devices: "Appareils" in the sidebar lists the open sessions (browser, system,
  address, last use); log one out, or every device but this one.
- Sleep: while an agent works, the PC does not go to sleep (the screen may
  still turn off); `--laisser-dormir` to allow it.
- Start with Windows: `python -m aihub demarrage-auto activer`
  puts a shortcut in the Startup folder (no admin rights): the server then
  starts without a window at each session opening, its messages in
  `data/serveur.log`. `etat` and `desactiver` to check or remove it. A second
  server on the same base is refused (`data/serveur.pid`).

### Settings

"Réglages" in the sidebar edits `data/config.json` (next to the base;
`serve --config` for another file): allowed working folders, Obsidian vault,
models offered and the default ones, quota alert threshold, backups, session
length and lockout, keeping the PC awake. Each value is checked before
anything is written, and a change applies at once, without a restart. With a
password, saving asks for it again (a stolen session alone cannot widen the
folders the agents may touch), and wrong ones count for the lockout. The file
never holds a secret. A `serve` option (`--obsidian`, `--allow`, `--garder`…)
fixes that setting for the run, over the file, and is never written to it.

### Memory (RAG) and search

Every finished message and every subtask report goes into an index of the
base (SQLite FTS5, accents ignored), kept up to date by triggers; a deleted
conversation leaves the index too, its text overwritten (FTS5 secure-delete).

- The search box of the sidebar looks through every conversation (all the
  words, as prefixes) and opens the message found.
- "Repartir de zéro" makes the next request start a new session instead of
  resuming the old one, which resends the whole history at every exchange.
  It gets a starting package: a summary written by the summary AI (Haiku by
  default; only when asked, then kept until new messages come), the last
  exchanges, and the passages closest to the request, within a cap (8 000
  tokens by default, estimated at 3.5 characters a token). Its agent may
  search the rest with `search_this_conversation` / `read_this_conversation`.
  Above the cap, nothing is kept until you choose: raise it (once or for the
  conversation), compress automatically (shorter summary, then shorter
  exchanges), or compress by hand.
- If the CLI no longer has the session to resume (another PC, another
  account), the same happens on its own, compressed automatically if needed.
- Settings: the summary AI (or none), the cap, the number of exchanges kept.

By meaning too (`aihub/semantic.py`): with the embedding model
`nomic-embed-text` in the local Ollama (`ollama pull nomic-embed-text`,
about 270 MB), every finished message gets a vector
in the background, and searches merge the word and meaning rankings
(« teinte » finds « couleur »); without it, words alone
(`tools/essai_sens_reel.py`: 6/6, about 0.05 s a question).

Measured on the real CLI (`tools/essai_memoire_reel.py`, Haiku): the summary
costs about 5 600 tokens (no tool, so a small context), the request after it
about 39 000 in one model call.

### Local AI (Ollama)

`aihub/providers/ollama.py` talks to a local Ollama
(http://127.0.0.1:11434, `AIHUB_OLLAMA_URL` for another address; the hub
never installs or starts it: `ollama serve`). Its chat models are offered as
« local:<name> ». Free,
nothing leaves the PC. Ollama keeps no session: every request carries the
conversation's memory within its own cap (4 000 tokens by default). The
local AI may read the working folder (list, read, search, confined to it)
and never changes anything. With neither Claude nor ChatGPT connected, it
writes the summaries. Measured with qwen3.5:4b: 0.5 to 4 s a reply once
loaded (`tools/essai_local_reel.py`).

### Agentic work

- "Autonome…" (`aihub/autonomous.py`): a goal the AI pursues alone, round
  after round. Each round ends with « ÉTAT : FINI » or « ÉTAT : À SUIVRE »;
  the hub runs the check command if given (confirmed like the bypass mode:
  it runs without the agent's sandbox) and passes its result to the next
  round. It stops when done and the check passes, or at a limit (rounds,
  minutes, stop, failed request).
- "Planifier" (`aihub/schedule.py`): a request or autonomous work that runs
  again on its own (every N minutes, every day, some days), in the PC's
  local time; a busy conversation waits 5 minutes; missed runs are made up
  once.
- "Outils" (`aihub/tools.py`): per conversation, the web search (on by
  default, can be turned off for both AIs), the Obsidian vault read only
  (`aihub/vault_mcp.py`), the MCP servers of the settings' catalog, and the
  delegation.
- Delegation (`aihub/delegate_mcp.py`): the agent may ask another AI for its
  opinion; that AI reads the folder and changes nothing, cannot delegate in
  turn, at most 3 per request; each one is recorded and shown.

### Phone alerts

The person chooses, on each device: "Notifications" in the sidebar turns
the alerts on for this browser and picks them (an AI waits for an answer,
reply ready, a request failed, plan proposed, plan done, quota nearly
reached). "Essayer" sends a test.

- Web Push (`aihub/notify.py`): the alert is encrypted for the device
  (RFC 8291) and signed with the server's key (VAPID, RFC 8292, kept in the
  base); the browser's push service (Google, Mozilla, Apple, Microsoft) only
  relays bytes it cannot read, and only those services' addresses are
  accepted. It needs the https address of Tailscale; on an iPhone, the app
  installed on the home screen (iOS 16.4 or later). A device logged out
  receives nothing more; one the browser dropped is removed.
- ntfy (optional, in "Réglages"): another app, whose server sees the text.
  By default an alert only says what happened, not in which conversation.
- Nothing is shown while the app is in front (it shows the card already).
  A tap opens the conversation. Sending never blocks or fails a request.

### Codex

AI Hub talks to `codex app-server` (JSON-RPC on stdio, the protocol of the
Codex app), not `codex exec`, which cannot ask before acting:

- Modes: manual = read-only sandbox, every write or command that needs more
  is asked first (a file change shows every file and its diff); plan =
  read-only, nothing asked; bypass = no sandbox, nothing asked.
- The user's own Codex extensions (apps, plugins, the MCP servers of
  `config.toml`) stay out of the agents, as `--strict-mcp-config` does for
  Claude Code; the hub's read-only MCP server (projects, memory) is given
  and allowed without asking. A request for a token is never answered.
- The GPT models offered are those the installed Codex lists (a model may
  need a newer Codex: update the app). The quota (5 hours and 7 days) is
  read for free and shown next to Claude's.
- Changing the AI of a conversation (the "IA :" menu of its header): another
  provider cannot resume the session, so the next request starts from the
  memory (summary, last exchanges, passages), as for "Repartir de zéro".
  Between two Claude models, the session goes on.

Measured on the real CLI (`tools/essai_codex_reel.py`, GPT-5.6 Luna): about
13 000 tokens for a short request (Claude Code: about 26 000).

### Projects

A project groups conversations ("+ Projet" in the sidebar). Its folder, if
given, is the default working folder of the conversations created in it.
Deleting a project keeps its conversations (without a project), unless you
choose to delete them with it, for good and from the backups too.

Within a project, conversations can read each other. The agent of a shared
conversation receives a read-only MCP server (`aihub/project_mcp.py`) with
three tools, allowed without a prompt since they only read:
`list_project_conversations`, `search_project` (passages ranked by BM25, each
with the question or answer that goes with it) and `read_conversation` (in
slices, 6 000 characters by default).

Every tool call sends Claude Code's whole context again (about 26 000 tokens),
so the hub saves the calls it can: the instruction names the other
conversations (no listing), a request that names one (`#12` or its title)
gets its closest passages joined by the hub, and a typed request that shares
at least two important words with a passage of the project gets that passage
too. Measured with Haiku (`tools/mesure_consultation.py`): 1 model call and
39 000 tokens instead of 4 calls and 160 000. It sees only the other *shared* conversations of its own project,
never another project, and never itself. "Privée" in the conversation header
closes it both ways: it reads nothing and nobody reads it. "Citer…" in the
composer inserts a reference to another conversation of the project.

### Several people on one server

The hub can be shared by several people, each with their own AI accounts:
what they ask is paid by their own Claude or ChatGPT subscription, never by
the owner's.

- The owner (the server's password) invites from "Personnes": a link valid
  7 days, for one use. The person picks a name and a password, then logs in
  with that name (the owner leaves the name empty).
- A person sees only what is shared with them: a conversation, or a whole
  project, in "Lecture" (read) or "Lecture et écriture" (they may send
  requests). Anything else answers "introuvable". Settings, people, new
  conversations, the folder, the mode, the tools and the scheduled tasks stay
  the owner's.
- "Mes comptes IA": the person signs in to Claude (a page, then the code it
  shows, pasted back) and to Codex (a code to type at OpenAI's page). Their
  sign-ins live in `data/comptes/<id>/` (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`);
  the hub never reads them, only the CLIs' own commands do. An empty folder is
  "not signed in": there is no fallback on the owner's account. Their quota
  and its alerts are theirs too. The local AI (Ollama) stays common.
- Each request shows who wrote it. A permission prompt is answered only by
  the person whose request it is.
- A person with one conversation of a project, not the project, never makes
  the agent read the other conversations.

### From the phone (Tailscale)

1. Install Tailscale on the PC and on the phone, with the same account. In
   the admin console, turn on MagicDNS and HTTPS certificates.
2. On the PC, with `serve` running: `tailscale serve --bg 8770`. The app is
   then at `https://<pc>.<tailnet>.ts.net/`, in HTTPS, for your devices only.
   Never use `tailscale funnel`: it would put the app on the Internet.
3. On the phone, open that address and log in. To install it: Chrome, menu
   "Installer l'application"; Safari, Partager, "Sur l'écran d'accueil".
4. To let only the phone in, restrict the access rules in the Tailscale admin
   console (Access controls) to the phone as source and the PC on port 443.

The server keeps listening on 127.0.0.1: `tailscale serve` relays to it, and
the lockout counts the phone's own address (`X-Forwarded-For` is only
trusted from this computer).

`--obsidian D:\notes\coffre` (or the vault in "Réglages") links the hub to an Obsidian vault. Each
conversation can then keep a project note there (button "Obsidian" in its
header, default path `Projets AI Hub/<title>.md`): a creation section, then an
update after each executed plan (request, table of subtasks and AIs, failures,
synthesis). Conversations themselves never go to the vault; a reply goes there
only with "Noter dans Obsidian" under it. The hub only appends, so your own
edits in the note stay. "Réécrire la note" rebuilds the whole note from the
database, on request only: the previous version is first copied to
`data/obsidian-sauvegardes/`, outside the vault. Obsidian itself does not need
to be running: these are plain Markdown files.

To try the interface without spending quota, on the fake CLI (prompts with
"écris", "question" or "attends" show a permission, a question, a long task):

```
.venv\Scripts\python -B tools/serveur_essai.py
```

With `--mot-de-passe` it shows the login screen, with a throwaway test password
kept in `data/essai-mot-de-passe.txt` and a lockout of 3 tries for 1 minute.

## Command line

```
python -m aihub ask --workdir D:\projets\essai "Explique ce dossier"
python -m aihub ask --conversation 1 "Et maintenant, ajoute un test"
python -m aihub conversations
python -m aihub quota --refresh
```

`quota --refresh` asks Claude Code for the current figures with a `get_usage`
control request, which the CLI answers by itself: it costs no quota. The web
interface does the same when it opens, every 5 minutes, and on "Actualiser".

- `--mode manual` (default): every file change or command asks for approval.
- `--mode plan`: the agent proposes a plan and changes nothing until it is approved.
- `--mode bypass`: the agent acts without asking. A warning is shown and must be
  confirmed by typing `oui`.

Ctrl+C stops the running request, including every process the agent started.
The database is `data/aihub.db` by default (`--db` to change it).

## Tests

```
.venv\Scripts\python -m unittest discover -s tests -t .
```

Without the web dependencies, the web tests are skipped. A watchdog variant
dumps every thread's stack if the suite hangs:

```
.venv\Scripts\python -B tools/tests_avec_garde.py --seconds 240
```

The provider tests use `tests/fake_claude.py`, a stand-in that speaks the same
stream-json protocol as the CLI, and `tests/fake_codex.py`, which speaks the
app server's JSON-RPC like codex-cli 0.144.2: they need neither an account
nor a network.

To check that the tests really guard each rule, break the guards one at a time
in a throwaway copy and see which tests fall:

```
.venv\Scripts\python -B tools/mutations_fragments.py
.venv\Scripts\python -B tools/mutations.py --jobs 12
```

The first one costs no test run: it lists the mutations whose code moved
(a fragment no longer found exactly once guards nothing). `--only MOT`
limits the bench to the mutations whose name contains that word.

`AIHUB_BENCH_PAUSE_IF=Game1,Game2` (program name prefixes) makes it stop by
itself while one of those programs runs, on Windows.

To check the whole chain against the real Claude Code CLI (signed in), with a
throwaway folder and database and the light model:

```
python -B tools/essai_reel.py
.venv\Scripts\python -B tools/essai_web_reel.py
.venv\Scripts\python -B tools/essai_orchestration_reel.py
.venv\Scripts\python -B tools/essai_discussion_reel.py
.venv\Scripts\python -B tools/essai_assignation_reel.py
.venv\Scripts\python -B tools/essai_projets_reel.py
.venv\Scripts\python -B tools/essai_memoire_reel.py
.venv\Scripts\python -B tools/mesure_consultation.py [--cite | --vague | --sans-ia]
.venv\Scripts\python -B tools/essai_codex_reel.py [--sans-claude]
```

The last one runs the real Codex (GPT-5.6 Luna, a little of the ChatGPT quota).

## Layout

- `aihub/db.py` — connection, schema, migrations
- `aihub/store.py` — agents, conversations, messages, plans, quota, permission journal
- `aihub/providers/claude_code.py` — runs the Claude Code CLI (stream-json, permissions, stop)
- `aihub/providers/codex.py` — runs Codex through its app server (JSON-RPC, approvals, quota, stop)
- `aihub/providers/ollama.py` — the local AI (Ollama, HTTP), read-only folder tools
- `aihub/providers/__init__.py` — picks the provider of an agent, finds its CLI
- `aihub/runner.py` — runs a request and records it in the database
- `aihub/orchestrator.py` — conductor prompts and plan schema, plan execution in parallel, synthesis
- `aihub/routing.py` — which AI for which kind of work, from benchmarks (automatic assignment)
- `aihub/project_mcp.py` — read-only MCP server: an agent reads the other conversations of its project
- `aihub/project_access.py` — gives that server to the agents of shared conversations in a project
- `aihub/config.py` — the settings file: defaults, validation, atomic write
- `aihub/memory.py` — the memory (RAG): search, summary, starting package, compression
- `aihub/semantic.py` — the memory by meaning: vectors of the local embedding model, merged rankings
- `aihub/autonomous.py` — autonomous work: rounds, check command, limits
- `aihub/schedule.py` — scheduled tasks and their scheduler
- `aihub/tools.py` — the tools of a conversation; `aihub/vault_mcp.py` (vault, read only), `aihub/delegate_mcp.py` (another AI's opinion)
- `aihub/auth.py` — people, invitations, password hash, sessions, lockout
- `aihub/access.py` — who sees and writes what: shares of conversations and projects
- `aihub/accounts.py` — each person's own AI accounts: their folders, the sign-ins from the hub
- `aihub/notify.py` — phone alerts: Web Push (encryption, VAPID signature, devices' choices) and ntfy
- `aihub/backup.py` — daily backups, deletion from backups
- `aihub/eveil.py` — holds the PC's sleep back while an agent works
- `aihub/obsidian.py` — project notes in an Obsidian vault (paths kept in the vault, append, rewrite with backup)
- `aihub/cli.py` — command line (`python -m aihub`)
- `aihub/web/app.py` — HTTP API and live events (server-sent events)
- `aihub/web/jobs.py` — requests running in the background, permission prompts waiting for an answer
- `aihub/web/static/` — the single-page interface (no framework)
- `tests/` — unit tests and the fake CLI
- `tools/mutations.py` — mutation check of the guards; `tools/mutations_fragments.py` — its fragments still found
- `tools/essai_reel.py`, `tools/essai_web_reel.py`, `tools/essai_orchestration_reel.py` — end-to-end checks against the real CLI
- `tools/serveur_essai.py` — the interface on the fake CLIs (Claude Code and Codex)
- `tools/tests_avec_garde.py` — test suite with a hang watchdog
- `tools/icones.py` — draws the app's icons (PNG, no image library)
