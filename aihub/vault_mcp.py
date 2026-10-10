"""MCP server that lets an agent read an Obsidian vault (read only).

Started by the CLI (stdio, one JSON-RPC message per line):

    python vault_mcp.py --vault D:\\notes\\coffre

Three tools: list the notes, read one, search them. Only the vault's .md
files are read; a path that leads out of it (« .. », a link to elsewhere)
is refused, and the hidden folders (.obsidian, .trash) stay out. Nothing is
ever written.

Standalone on purpose (standard library only), like project_mcp.py.
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

SERVER = "coffre"
VERSION = "1"
PROTOCOL = "2025-06-18"
READ_DEFAULT, READ_MAX = 8000, 30000
LIST_MAX = 300
SEARCH_DEFAULT, SEARCH_MAX = 8, 30
SNIPPET = 300

TOOLS = [
    {"name": "list_notes", "description": "Liste les notes du coffre Obsidian (les plus récentes d'abord).",
     "inputSchema": {"type": "object", "properties": {
         "folder": {"type": "string", "description": "sous-dossier du coffre, vide pour tout"}}}},
    {"name": "read_note", "description": "Lit une note du coffre, par tranches.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "chemin de la note dans le coffre, par exemple Projets/Site.md"},
         "start": {"type": "integer", "minimum": 0, "description": "caractère de départ"},
         "max_chars": {"type": "integer", "minimum": 500, "maximum": READ_MAX}}}},
    {"name": "search_notes", "description": "Cherche des mots dans les notes du coffre (titres et contenu).",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX}}}},
]
TOOL_NAMES = [t["name"] for t in TOOLS]


class Refused(Exception):
    """A refused call; the message goes back to the agent."""


def _fold(text):
    text = unicodedata.normalize("NFKD", str(text or "").casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


class Vault:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _notes(self, folder=""):
        base = self._inside(folder or ".", must_exist=True)
        if not base.is_dir():
            raise Refused(f"dossier introuvable dans le coffre : {folder}")
        found = []
        for path in base.rglob("*.md"):
            relative = path.relative_to(self.root)
            if any(part.startswith(".") for part in relative.parts):
                continue  # .obsidian, .trash, hidden folders
            try:
                if not path.resolve().is_relative_to(self.root):
                    continue  # a link that leads out of the vault
            except OSError:
                continue
            found.append(path)
        return found

    def _inside(self, relative, must_exist=False):
        target = (self.root / str(relative)).resolve()
        if not target.is_relative_to(self.root):
            raise Refused("chemin hors du coffre : refusé")
        if must_exist and not target.exists():
            raise Refused(f"introuvable dans le coffre : {relative}")
        return target

    def list_notes(self, folder=""):
        notes = sorted(self._notes(folder), key=lambda p: p.stat().st_mtime, reverse=True)
        lines = [p.relative_to(self.root).as_posix() for p in notes[:LIST_MAX]]
        more = f"\n(et {len(notes) - LIST_MAX} autres)" if len(notes) > LIST_MAX else ""
        return (f"{len(notes)} note(s) :\n" + "\n".join(lines) + more) if notes else "Aucune note."

    def read_note(self, path, start=0, max_chars=READ_DEFAULT):
        target = self._inside(path, must_exist=True)
        if target.suffix.lower() != ".md" or not target.is_file():
            raise Refused("seules les notes (.md) se lisent")
        if any(part.startswith(".") for part in target.relative_to(self.root).parts):
            raise Refused("dossier caché du coffre : refusé")
        text = target.read_text(encoding="utf-8", errors="replace")
        start = max(0, int(start or 0))
        size = max(500, min(int(max_chars or READ_DEFAULT), READ_MAX))
        part = text[start:start + size]
        more = (f"\n\n(Suite : read_note avec start={start + size}.)" if start + size < len(text) else "")
        return f"{target.relative_to(self.root).as_posix()} — caractères {start} à {start + len(part)} " \
               f"sur {len(text)} :\n\n{part}{more}"

    def search_notes(self, query, limit=SEARCH_DEFAULT):
        words = [w for w in re.findall(r"\w+", _fold(query)) if len(w) >= 3]
        if not words:
            raise Refused("indiquez au moins un mot de 3 lettres")
        limit = max(1, min(int(limit or SEARCH_DEFAULT), SEARCH_MAX))
        scored = []
        for path in self._notes():
            name = _fold(path.stem)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            folded = _fold(text)
            hits = [folded.count(w) for w in words]
            if not all(hits) and not all(w in name for w in words):
                continue  # every word, in the text or in the title
            score = sum(min(h, 5) for h in hits) + 5 * sum(w in name for w in words)
            at = min((folded.find(w) for w in words if w in folded), default=0)
            snippet = text[max(0, at - SNIPPET // 3):at + SNIPPET].replace("\n", " ").strip()
            scored.append((score, path.relative_to(self.root).as_posix(), snippet))
        scored.sort(key=lambda s: (-s[0], s[1]))
        if not scored:
            return f"Rien trouvé pour « {query} » dans le coffre."
        return "\n\n".join(f"- {p} : …{s}…" for _, p, s in scored[:limit])

    def call(self, name, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        if name == "list_notes":
            return self.list_notes(arguments.get("folder", ""))
        if name == "read_note":
            return self.read_note(arguments.get("path", ""), arguments.get("start"), arguments.get("max_chars"))
        if name == "search_notes":
            return self.search_notes(arguments.get("query", ""), arguments.get("limit"))
        raise Refused(f"outil inconnu : {name}")


def handle(vault, message):
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        result = {"protocolVersion": asked or PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER, "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            text, error = vault.call(params.get("name"), params.get("arguments")), False
        except Refused as exc:
            text, error = str(exc), True
        except (OSError, ValueError, TypeError) as exc:
            text, error = f"lecture impossible : {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True)
    args = parser.parse_args(argv)
    vault = Vault(args.vault)
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON illisible"}}
        else:
            reply = handle(vault, message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
