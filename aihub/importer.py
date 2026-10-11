"""Conversations brought in from elsewhere: kept as they were, or merged into one.

Sources:
- the data export of chatgpt.com or of claude.ai (Settings, export: a .zip
  sent by mail, with `conversations.json` inside; a bare conversations.json
  works too). The person downloads it and gives it to the hub: no password
  goes through the hub;
- the sessions Claude Code and Codex keep on this computer
  (~/.claude/projects/*/*.jsonl, ~/.codex/sessions/**/*.jsonl), read as they
  are, without the tool calls, the tool results, nor the context the CLIs
  inject (environment, AGENTS.md…).

Only the text of the requests and of the replies is kept. An imported
conversation has no session: its first request starts from the memory
(summary, last exchanges, passages), like after a change of AI
(aihub/runner.py).
"""

import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db

SOURCES = {"chatgpt": "ChatGPT", "claude": "Claude", "claude_code": "Claude Code", "codex": "Codex"}
MAX_UPLOAD = 300 * 1024 * 1024        # an export, compressed
MAX_JSON = 800 * 1024 * 1024          # its conversations.json, once read
MAX_MESSAGES = 20000                  # in one import
MAX_LOCAL = 300                       # local sessions listed (the most recent)
TITLE_MAX = 120


class ImportFailed(Exception):
    """An import that cannot be done; the message is for the person."""


@dataclass
class Imported:
    key: str                 # unique within one reading
    source: str              # a key of SOURCES
    title: str
    created_at: str          # ISO, UTC
    messages: list = field(default_factory=list)   # [(role, text, created_at)]
    workdir: str | None = None                     # a local session's folder

    def summary(self):
        return {"key": self.key, "source": self.source, "source_name": SOURCES[self.source],
                "title": self.title, "created_at": self.created_at, "count": len(self.messages),
                "workdir": self.workdir}


def _iso(value):
    """An ISO date (UTC) from an epoch number or an ISO text; None if unreadable."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            when = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        elif isinstance(value, str) and value:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _title(text, fallback):
    text = " ".join(str(text or "").split())
    if not text:
        text = fallback
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


def _merge_turns(messages):
    """Consecutive messages of the same role become one (a reply in several blocks)."""
    out = []
    for role, text, at in messages:
        text = text.strip()
        if not text:
            continue
        if out and out[-1][0] == role:
            out[-1] = (role, out[-1][1] + "\n\n" + text, out[-1][2])
        else:
            out.append((role, text, at))
    return out


# --- the sites' exports ------------------------------------------------------------

def _chatgpt(conv, index):
    mapping = conv.get("mapping") or {}
    node_id = conv.get("current_node")
    chain = []
    seen = set()
    # The branch shown last (current_node up to the root); without it, the order of the mapping.
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        chain.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    chain = list(reversed(chain)) if chain else list(mapping.values())
    messages = []
    for node in chain:
        message = node.get("message") or {}
        role = (message.get("author") or {}).get("role")
        content = message.get("content") or {}
        if role not in ("user", "assistant") or content.get("content_type") not in ("text", "multimodal_text"):
            continue
        if (message.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        text = "\n".join(p for p in content.get("parts") or [] if isinstance(p, str))
        messages.append((role, text, _iso(message.get("create_time"))))
    created = _iso(conv.get("create_time")) or _now()
    return Imported(f"chatgpt-{index}", "chatgpt", _title(conv.get("title"), "Conversation ChatGPT"), created,
                    _merge_turns(messages))


def _claude(conv, index):
    messages = []
    for message in conv.get("chat_messages") or []:
        role = {"human": "user", "assistant": "assistant"}.get(message.get("sender"))
        if role is None:
            continue
        blocks = [b.get("text", "") for b in message.get("content") or []
                  if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        text = "\n\n".join(blocks) if blocks else str(message.get("text") or "")
        messages.append((role, text, _iso(message.get("created_at"))))
    created = _iso(conv.get("created_at")) or _now()
    return Imported(f"claude-{index}", "claude", _title(conv.get("name"), "Conversation Claude"), created,
                    _merge_turns(messages))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _conversations_json(data):
    """The conversation lists of an export: a .zip, or a bare conversations.json."""
    if data[:2] == b"PK":
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ImportFailed("fichier .zip illisible") from None
        names = [i for i in archive.infolist() if Path(i.filename).name.startswith("conversations")
                 and i.filename.endswith(".json")]
        if not names:
            raise ImportFailed("pas de conversations.json dans ce .zip : est-ce bien l'export de ChatGPT ou de Claude ?")
        lists = []
        for info in sorted(names, key=lambda i: i.filename):
            if info.file_size > MAX_JSON:
                raise ImportFailed("conversations.json trop gros")
            lists.append(archive.read(info))
        return lists
    return [data]


def parse_export(data):
    """[Imported] of a ChatGPT or Claude export (the format is recognized by itself)."""
    if not data:
        raise ImportFailed("fichier vide")
    if len(data) > MAX_UPLOAD:
        raise ImportFailed(f"fichier trop gros ({MAX_UPLOAD // 1024 // 1024} Mo au plus)")
    found = []
    for raw in _conversations_json(data):
        try:
            items = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise ImportFailed("conversations.json illisible (ce n'est pas du JSON)") from None
        if isinstance(items, dict):
            items = items.get("conversations") or [items]
        if not isinstance(items, list):
            raise ImportFailed("format d'export inconnu")
        for conv in items:
            if not isinstance(conv, dict):
                continue
            index = len(found)
            if "mapping" in conv:
                found.append(_chatgpt(conv, index))
            elif "chat_messages" in conv:
                found.append(_claude(conv, index))
    if not found:
        raise ImportFailed("aucune conversation reconnue : ni l'export de ChatGPT, ni celui de Claude")
    return sorted((c for c in found if c.messages), key=lambda c: c.created_at, reverse=True)


# --- the sessions of this computer -------------------------------------------------------

def _injected(text):
    """Context a CLI adds to a request (environment, instructions, commands), not the person's words."""
    head = text.lstrip()[:60]
    return head.startswith("<") or head.startswith("# AGENTS.md") or head.startswith("Caveat:")


def _claude_code_session(path, index):
    title = custom = None
    messages = []
    created = workdir = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        kind = row.get("type")
        if kind == "custom-title":
            custom = row.get("customTitle") or custom
        elif kind == "ai-title":
            title = row.get("aiTitle") or title
        if kind not in ("user", "assistant") or row.get("isSidechain") or row.get("isMeta"):
            continue
        created = created or row.get("timestamp")
        workdir = workdir or row.get("cwd")
        content = (row.get("message") or {}).get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        else:
            texts = []
        for text in texts:
            if isinstance(text, str) and text.strip() and not (kind == "user" and _injected(text)):
                messages.append(("user" if kind == "user" else "assistant", text, _iso(row.get("timestamp"))))
    messages = _merge_turns(messages)
    first = next((t for r, t, _ in messages if r == "user"), "")
    return Imported(f"claude_code-{index}", "claude_code", _title(custom or title or first, path.stem),
                    _iso(created) or _iso(path.stat().st_mtime) or _now(), messages, workdir)


def _codex_session(path, index):
    messages = []
    created = workdir = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if row.get("type") == "session_meta":
            if (payload.get("source") or {}) and isinstance(payload.get("source"), dict) \
                    and payload["source"].get("subagent"):
                return None  # a sub-agent's thread: part of another session
            created = created or payload.get("timestamp")
            workdir = workdir or payload.get("cwd")
            continue
        if row.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue  # the developer instructions
        wanted = "input_text" if role == "user" else "output_text"
        for block in payload.get("content") or []:
            text = block.get("text") if isinstance(block, dict) and block.get("type") == wanted else None
            if isinstance(text, str) and text.strip() and not (role == "user" and _injected(text)):
                messages.append((role, text, _iso(row.get("timestamp"))))
    messages = _merge_turns(messages)
    first = next((t for r, t, _ in messages if r == "user"), "")
    return Imported(f"codex-{index}", "codex", _title(first, path.stem),
                    _iso(created) or _iso(path.stat().st_mtime) or _now(), messages, workdir)


def local_sessions(home=None, limit=MAX_LOCAL):
    """[Imported] of the Claude Code and Codex sessions of this computer, the most recent first."""
    home = Path(home) if home else Path.home()
    files = [(p, "claude_code") for p in (home / ".claude" / "projects").glob("*/*.jsonl")]
    files += [(p, "codex") for p in (home / ".codex" / "sessions").rglob("*.jsonl")]
    files.sort(key=lambda f: f[0].stat().st_mtime, reverse=True)
    found = []
    for index, (path, kind) in enumerate(files[:limit]):
        try:
            item = (_claude_code_session if kind == "claude_code" else _codex_session)(path, index)
        except OSError:
            continue
        if item is not None and item.messages:
            found.append(item)
    return found


# --- into the hub --------------------------------------------------------------------------

def _header(item):
    day = item.created_at[:10]
    return f"[Importé de {SOURCES[item.source]} — « {item.title} » — {day}]\n\n"


def write_messages(conn, conversation_id, items, *, merged):
    """Record the messages of `items` in a new conversation. Merged: each one is
    headed by where it comes from, in the order of their start."""
    rows = []
    for item in sorted(items, key=lambda i: i.created_at) if merged else items:
        for n, (role, text, at) in enumerate(item.messages):
            if merged and n == 0:
                text = _header(item) + text
            rows.append((role, text, at or item.created_at, SOURCES[item.source]))
    if not rows:
        raise ImportFailed("rien à importer : ces conversations n'ont pas de texte")
    if len(rows) > MAX_MESSAGES:
        raise ImportFailed(f"trop de messages en une fois ({len(rows)}, {MAX_MESSAGES} au plus)")
    now = db.utc_now()
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO messages (conversation_id, role, model, content, status, created_at, finished_at) "
            "VALUES (?, ?, ?, ?, 'ok', ?, ?)",
            [(conversation_id, role, f"{source} (importé)" if role == "assistant" else None, text, at, at)
             for role, text, at, source in rows])
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
    return len(rows)
