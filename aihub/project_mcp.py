"""MCP server that lets an agent read the other conversations of its project.

Claude Code starts it (stdio, one JSON-RPC message per line) with the hub's
database and the conversation the agent works for:

    python project_mcp.py --db data/aihub.db --conversation 12

What it may read is decided again at every call, from the database:
- the conversation must belong to a project and be shared;
- only the other *shared* conversations of that same project are visible;
- a conversation that is not shared neither reads nor is read.
The database is opened read-only: the agent can look, never change.

Standalone on purpose (standard library only): it runs in the agent's
working folder, where the hub's package is not importable.
"""

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SERVER = "aihub"
VERSION = "1"
PROTOCOL = "2025-06-18"
READ_DEFAULT, READ_MAX = 6000, 20000   # characters returned by one read
SEARCH_DEFAULT, SEARCH_MAX = 5, 20
SEARCH_BUDGET = 5000                   # characters of one search answer
SNIPPET = 500                          # around the best word of a passage
PAIR = 250                             # of the question or answer that goes with it
# Words too common to tell passages apart.
_STOP = set("""
les des une aux ces mes tes ses nos vos son ont qui que quoi est sur par pas mon ton lui eux toi moi
alors aussi avec avoir avant cela celle celui cette ceux chez comme comment dans depuis donc dont elle elles
encore entre est été être fait faire leur leurs mais même moins notre nous où par pas peu peut plus pour
pourquoi quand que quel quelle quelles quels qui quoi sans ses son sont sous sur tes ton tous tout toute
toutes très une vos votre vous the and for with that this from
conversation conversations projet message messages
""".split())
_WORD = re.compile(r"\w+", re.UNICODE)


def terms(text):
    """The words that count in a search: lower case, no short or common word, no plural s/x."""
    found = []
    for word in _WORD.findall(str(text or "").casefold()):
        if len(word) < 3 or word in _STOP or word.isdigit():
            continue
        if len(word) > 4 and word[-1] in "sx":
            word = word[:-1]
        if len(word) > 4 and word[-1] == "e":  # choisie / choisi, décidée / décidé
            word = word[:-1]
        found.append(word)
    return found


_K1, _B = 1.2, 0.75  # BM25: a word repeated 20 times barely counts more than twice; long passages weigh less
RELATIVE = 0.5       # passages kept: at least half the score of the best one

TOOLS = [
    {"name": "list_project_conversations",
     "description": "Liste les autres conversations du même projet AI Hub (numéro, titre, date, "
                    "nombre de messages, première demande). Rarement utile : leurs titres sont déjà dans "
                    "les consignes ; pour une question, search_project va droit au but.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "search_project",
     "description": "Cherche dans les autres conversations du projet les passages les plus proches d'une "
                    "question, classés par pertinence. Chaque passage vient avec sa question ou sa réponse, "
                    "le numéro de la conversation et du message : le plus souvent, un seul appel suffit.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "la question ou ses mots importants"},
         "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX,
                   "description": f"nombre d'extraits (défaut {SEARCH_DEFAULT})"}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "read_conversation",
     "description": "Lit une autre conversation du projet, dans l'ordre, par tranches. Pour économiser "
                    "des tokens, chercher d'abord avec search_project puis lire à partir du message utile.",
     "inputSchema": {"type": "object", "properties": {
         "conversation_id": {"type": "integer", "description": "numéro de la conversation"},
         "start": {"type": "integer", "minimum": 1, "description": "premier message à lire (défaut 1)"},
         "max_chars": {"type": "integer", "minimum": 500, "maximum": READ_MAX,
                       "description": f"taille maximale de la réponse (défaut {READ_DEFAULT})"}},
         "required": ["conversation_id"], "additionalProperties": False}},
    # The conversation's own past: after a fresh start its history is not
    # resent, the agent fetches what it needs.
    {"name": "search_this_conversation",
     "description": "Cherche dans l'historique de CETTE conversation (ce qui précède le nouveau départ) "
                    "les passages les plus proches d'une question. Lecture seule.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "la question ou ses mots importants"},
         "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX,
                   "description": f"nombre de passages (défaut {SEARCH_DEFAULT})"}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "read_this_conversation",
     "description": "Lit l'historique de CETTE conversation par tranches, à partir d'un message.",
     "inputSchema": {"type": "object", "properties": {
         "start": {"type": "integer", "minimum": 1, "description": "premier message à lire (défaut 1)"},
         "max_chars": {"type": "integer", "minimum": 500, "maximum": READ_MAX,
                       "description": f"taille maximale de la réponse (défaut {READ_DEFAULT})"}},
         "additionalProperties": False}},
]
TOOL_NAMES = [tool["name"] for tool in TOOLS]


class Refused(Exception):
    """An answer for the agent: shown to it as a tool error."""


def _local(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso or "?"


class Access:
    def __init__(self, db_path, conversation_id):
        self.db_path, self.conversation_id = db_path, conversation_id

    def _connect(self):
        # as_uri: drive letter, backslashes and spaces made valid for a URI.
        conn = sqlite3.connect(Path(self.db_path).resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _visible(self, conn):
        """The conversations this one may read (never itself), by id."""
        me = conn.execute("SELECT project_id, shared FROM conversations WHERE id = ?",
                          (self.conversation_id,)).fetchone()
        if me is None:
            raise Refused("cette conversation n'existe plus")
        if me["project_id"] is None:
            raise Refused("cette conversation n'est dans aucun projet : elle ne voit aucune autre conversation")
        if not me["shared"]:
            raise Refused("le partage est coupé pour cette conversation : elle ne voit pas les autres")
        rows = conn.execute(
            "SELECT * FROM conversations WHERE project_id = ? AND shared = 1 AND id <> ? "
            "ORDER BY updated_at DESC, id DESC", (me["project_id"], self.conversation_id)).fetchall()
        return {row["id"]: row for row in rows}

    @staticmethod
    def _messages(conn, cid):
        # Finished messages only: a reply being written, or one that failed, says nothing reliable.
        return conn.execute(
            "SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? "
            "AND content IS NOT NULL AND content <> '' AND (role = 'user' OR status = 'ok') ORDER BY id",
            (cid,)).fetchall()

    def list(self):
        conn = self._connect()
        try:
            visible = self._visible(conn)
            if not visible:
                return "Aucune autre conversation partagée dans ce projet."
            lines = []
            for cid, row in visible.items():
                messages = self._messages(conn, cid)
                first = next((m["content"] for m in messages if m["role"] == "user"), "")
                first = " ".join(first.split())[:160]
                lines.append(f"- #{cid} « {row['title']} » — {len(messages)} message(s), "
                             f"mise à jour le {_local(row['updated_at'])}"
                             + (f"\n  première demande : {first}" if first else ""))
            return f"{len(visible)} autre(s) conversation(s) dans le projet :\n" + "\n".join(lines)
        finally:
            conn.close()

    @staticmethod
    def _rank(passages, query_terms):
        """Passages (conversation, row, position, message, list) best first, by BM25."""
        wanted = set(query_terms)
        counts, lengths = [], []
        for _, _, _, message, _ in passages:
            words = terms(message["content"])
            lengths.append(len(words) or 1)
            counts.append({t: words.count(t) for t in wanted})
        total = len(passages)
        average = sum(lengths) / total if total else 1
        frequency = {t: sum(1 for c in counts if c[t]) for t in wanted}
        scored = []
        for i, passage in enumerate(passages):
            norm = _K1 * (1 - _B + _B * lengths[i] / average)
            score = sum(math.log(1 + (total - frequency[t] + 0.5) / (frequency[t] + 0.5)) * n * (_K1 + 1) / (n + norm)
                        for t, n in counts[i].items() if n)
            if score > 0:
                scored.append((score, i, passage))
        scored.sort(key=lambda s: (-s[0], s[1]))
        # Far behind the best, a passage only shares a common word: it would
        # cost tokens and say nothing.
        best = scored[0][0] if scored else 0
        return [p for score, _, p in scored if score >= RELATIVE * best]

    @staticmethod
    def _excerpt(messages, index, query_terms):
        """The passage around its best word, with the question it answers or the answer it got."""
        message = messages[index]
        text = message["content"]
        folded = text.casefold()
        at = min((folded.find(t) for t in query_terms if folded.find(t) >= 0), default=0)
        begin = max(0, at - SNIPPET // 3)
        body = ("…" if begin else "") + " ".join(text[begin:begin + SNIPPET].split()) \
            + ("…" if begin + SNIPPET < len(text) else "")
        who = "utilisateur" if message["role"] == "user" else "IA"
        lines = [f"message {index + 1} ({who}) : {body}"]
        def short(content):
            return " ".join(content[:PAIR].split()) + ("…" if len(content) > PAIR else "")

        if message["role"] == "user" and index + 1 < len(messages) and messages[index + 1]["role"] != "user":
            lines.append(f"  → réponse : {short(messages[index + 1]['content'])}")
        elif message["role"] != "user" and index > 0 and messages[index - 1]["role"] == "user":
            lines.append(f"  ← en réponse à : {short(messages[index - 1]['content'])}")
        return "\n".join(lines)

    def _passages(self, conn, conversations):
        passages = []
        for cid, row in conversations.items():
            messages = self._messages(conn, cid)
            for index, message in enumerate(messages):
                passages.append((cid, row, index, message, messages))
        return passages

    def _render(self, ranked, query_terms, limit, budget):
        parts, used = [], 0
        for cid, row, index, _, messages in ranked[:limit]:
            block = f"- conversation #{cid} « {row['title']} », " + self._excerpt(messages, index, query_terms)
            if parts and used + len(block) > budget:
                break
            parts.append(block)
            used += len(block)
        return parts

    def search(self, query, limit=SEARCH_DEFAULT):
        query_terms = terms(query)
        if not query_terms:
            raise Refused("indiquez au moins un mot important à chercher")
        limit = max(1, min(int(limit or SEARCH_DEFAULT), SEARCH_MAX))
        conn = self._connect()
        try:
            ranked = self._rank(self._passages(conn, self._visible(conn)), query_terms)
            if not ranked:
                return f"Rien trouvé pour « {query} » dans les autres conversations du projet."
            parts = self._render(ranked, query_terms, limit, SEARCH_BUDGET)
            return f"{len(parts)} passage(s) pour « {query} », du plus proche au moins proche :\n" + "\n".join(parts)
        finally:
            conn.close()

    def closest(self, query, budget, min_terms=2, limit=2):
        """Without a named conversation: the best passages of the project, only
        if they share at least `min_terms` different words with the request
        (one common word says nothing). "" otherwise."""
        query_terms = terms(query)
        if len(set(query_terms)) < min_terms:
            return ""
        conn = self._connect()
        try:
            try:
                visible = self._visible(conn)
            except Refused:
                return ""
            ranked = self._rank(self._passages(conn, visible), query_terms)
            wanted = set(query_terms)
            strong = [p for p in ranked if len(wanted & set(terms(p[3]["content"]))) >= min_terms][:limit]
            parts = self._render(strong, query_terms, limit, budget)
            return "\n".join(parts)
        finally:
            conn.close()

    def cited(self, prompt):
        """The visible conversations a request names: « #12 », or its title."""
        conn = self._connect()
        try:
            try:
                visible = self._visible(conn)
            except Refused:
                return {}
            folded = str(prompt or "").casefold()
            ids = {int(n) for n in re.findall(r"#(\d+)", folded)}
            return {cid: row for cid, row in visible.items()
                    if cid in ids or (len(row["title"]) >= 4 and row["title"].casefold() in folded)}
        finally:
            conn.close()

    def relevant(self, conversation_id, query, budget):
        """The passages of one visible conversation closest to a request (its end if none)."""
        conn = self._connect()
        try:
            visible = self._visible(conn)
            if conversation_id not in visible:
                return ""
            row = visible[conversation_id]
            messages = self._messages(conn, conversation_id)
            if not messages:
                return ""
            query_terms = terms(query)
            ranked = self._rank(self._passages(conn, {conversation_id: row}), query_terms) if query_terms else []
            if ranked:
                parts = [self._excerpt(messages, index, query_terms) for _, _, index, _, _ in ranked[:4]]
            else:  # nothing in common with the request: its last exchanges
                parts = [self._excerpt(messages, i, []) for i in range(max(0, len(messages) - 2), len(messages))]
            text, kept = "", []
            for part in parts:
                if kept and len(text) + len(part) > budget:
                    break
                kept.append(part)
                text = "\n".join(kept)
            return f"Conversation #{conversation_id} « {row['title']} » ({len(messages)} messages) :\n{text[:budget]}"
        finally:
            conn.close()

    def read(self, conversation_id, start=1, max_chars=READ_DEFAULT):
        try:
            cid = int(conversation_id)
        except (TypeError, ValueError):
            raise Refused("conversation_id doit être un numéro") from None
        start = max(1, int(start or 1))
        budget = max(500, min(int(max_chars or READ_DEFAULT), READ_MAX))
        conn = self._connect()
        try:
            visible = self._visible(conn)
            if cid == self.conversation_id:
                raise Refused("c'est la conversation en cours : son contenu est déjà dans votre contexte")
            if cid not in visible:
                # The same answer whether it does not exist, is in another
                # project or is not shared: nothing leaks about the others.
                raise Refused(f"conversation #{cid} introuvable parmi les conversations partagées du projet")
            return self._slice(conn, cid, visible[cid], start, budget, "read_conversation")
        finally:
            conn.close()

    def _slice(self, conn, cid, row, start, budget, tool):
        messages = self._messages(conn, cid)
        parts, used, last = [], 0, start - 1
        for position, message in enumerate(messages, start=1):
            if position < start:
                continue
            who = "Utilisateur" if message["role"] == "user" else "IA"
            block = f"[{position}] {who} ({_local(message['created_at'])}) :\n{message['content']}"
            if used + len(block) > budget:
                if not parts:  # one long message: cut it rather than return nothing
                    parts.append(block[:budget] + " […coupé]")
                    last = position
                break
            parts.append(block)
            used += len(block)
            last = position
        header = f"Conversation #{cid} « {row['title']} » — messages {start} à {last} sur {len(messages)}"
        more = (f"\n\n(Suite : {tool} avec start={last + 1}.)" if last < len(messages) else "")
        if not parts:
            return f"{header} : aucun message à partir du numéro {start}."
        return header + "\n\n" + "\n\n".join(parts) + more

    # --- the conversation's own past (after a fresh start) ---

    def _own(self, conn):
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (self.conversation_id,)).fetchone()
        if row is None:
            raise Refused("cette conversation n'existe plus")
        return row

    def search_self(self, query, limit=SEARCH_DEFAULT):
        query_terms = terms(query)
        if not query_terms:
            raise Refused("indiquez au moins un mot important à chercher")
        limit = max(1, min(int(limit or SEARCH_DEFAULT), SEARCH_MAX))
        conn = self._connect()
        try:
            row = self._own(conn)
            ranked = self._rank(self._passages(conn, {self.conversation_id: row}), query_terms)
            if not ranked:
                return f"Rien trouvé pour « {query} » dans l'historique de cette conversation."
            parts = self._render(ranked, query_terms, limit, SEARCH_BUDGET)
            return f"{len(parts)} passage(s) de cette conversation pour « {query} » :\n" + "\n".join(parts)
        finally:
            conn.close()

    def read_self(self, start=1, max_chars=READ_DEFAULT):
        start = max(1, int(start or 1))
        budget = max(500, min(int(max_chars or READ_DEFAULT), READ_MAX))
        conn = self._connect()
        try:
            return self._slice(conn, self.conversation_id, self._own(conn), start, budget, "read_this_conversation")
        finally:
            conn.close()

    def call(self, name, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        if name == "search_this_conversation":
            return self.search_self(arguments.get("query"), arguments.get("limit"))
        if name == "read_this_conversation":
            return self.read_self(arguments.get("start"), arguments.get("max_chars"))
        if name == "list_project_conversations":
            return self.list()
        if name == "search_project":
            return self.search(arguments.get("query"), arguments.get("limit"))
        if name == "read_conversation":
            return self.read(arguments.get("conversation_id"), arguments.get("start"),
                             arguments.get("max_chars"))
        raise Refused(f"outil inconnu : {name}")


def handle(access, message):
    """The reply to one JSON-RPC message, or None for a notification."""
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled...) need no answer
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
            text, error = access.call(params.get("name"), params.get("arguments")), False
        except Refused as exc:
            text, error = str(exc), True
        except (sqlite3.Error, ValueError, TypeError) as exc:
            text, error = f"lecture impossible : {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--conversation", type=int, required=True)
    args = parser.parse_args(argv)
    access = Access(args.db, args.conversation)
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
            reply = handle(access, message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
