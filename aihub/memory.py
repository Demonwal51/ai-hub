"""The conversations' memory (RAG), on the `memory` index of the base.

Every finished message and every subtask report is indexed by the base
itself (triggers of schema 8): "each message completes the RAG".

A conversation that starts afresh (button « Repartir de zéro », or a
session the CLI no longer has) does not get its whole history again. It
gets a starting package, capped in tokens:
- a summary, written by the summary AI (Haiku by default), only when a
  fresh start is asked for, then kept until new messages come;
- the last exchanges, as they are;
- the passages of the conversation closest to the new request;
and the search tool of the hub's MCP server for the rest.

Above the cap, nothing is sent until the user chooses: raise the cap,
compress automatically, or compress by hand.
"""

import json
import math
import re

from . import db, semantic

# French text: about 3.5 characters per token. An estimate, said as such.
CHARS_PER_TOKEN = 3.5
SUMMARY_INPUT = 40000     # characters of conversation given to the summary AI
SUMMARY_MESSAGE = 3000    # characters of one message in it
EXCERPT = 600             # characters of one passage
_WORD = re.compile(r"\w+", re.UNICODE)
_COMMON = set("""
les des une aux ces mes tes ses nos vos son ont qui que quoi est sur par pas mon ton lui eux toi moi
alors aussi avec avoir avant cela celle celui cette ceux chez comme comment dans depuis donc dont elle elles
encore entre été être fait faire leur leurs mais même moins notre nous où pour pourquoi quand quel quelle
quelles quels sans sont sous tous tout toute toutes très votre vous the and for with that this from
""".split())


class OverBudget(Exception):
    def __init__(self, package):
        super().__init__("le paquet de départ dépasse le plafond")
        self.package = package


def tokens(text):
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def _words(text):
    found = []
    for word in _WORD.findall(str(text or "").casefold()):
        if len(word) >= 3 and word not in _COMMON and not word.isdigit() and word not in found:
            found.append(word)
    return found[:30]


def match_query(text, every=False):
    """An FTS5 query from free text, each word as a prefix.

    Nothing the user types can be query syntax: only \\w+ words are kept
    (no quote, colon, parenthesis or minus sign), and in lower case, where
    FTS5 knows no operator (AND, OR, NOT and NEAR exist only in capitals).
    """
    words = _words(text)
    if not words:
        return None
    return (" AND " if every else " OR ").join(f"{w}*" for w in words)


# --- search across the conversations (the sidebar's search box) ---------------------------

def search(conn, text, limit=30):
    """Messages matching every word, best first, with a highlighted excerpt;
    then those close in meaning only (`by_meaning`), when the local model is there."""
    query = match_query(text, every=True)
    found = []
    if query is not None:
        found = [dict(r, by_meaning=False) for r in conn.execute(
            """
            SELECT m.conversation_id, m.message_id, m.subtask_id, m.role,
                   snippet(memory, 0, '[[', ']]', '…', 24) AS excerpt, c.title, c.project_id
            FROM memory m JOIN conversations c ON c.id = m.conversation_id
            WHERE memory MATCH ? ORDER BY bm25(memory) LIMIT ?
            """, (query, limit)).fetchall()]
    seen = {("m", r["message_id"]) if r["message_id"] is not None else ("s", r["subtask_id"]) for r in found}
    for key in _by_meaning(conn, text, limit=10):
        if key in seen or len(found) >= limit:
            continue
        row = _row_of(conn, key)
        if row is None:
            continue
        title = conn.execute("SELECT title, project_id FROM conversations WHERE id = ?",
                             (row["conversation_id"],)).fetchone()
        content = row["content"]
        found.append({"conversation_id": row["conversation_id"], "message_id": row["message_id"],
                      "subtask_id": row["subtask_id"], "role": row["role"], "by_meaning": True,
                      "excerpt": content[:200] + ("…" if len(content) > 200 else ""),
                      "title": title["title"], "project_id": title["project_id"]})
    return found


# --- what a fresh start receives -------------------------------------------------------------

_NO_BOUND = 2 ** 62


def last_exchanges(conn, conversation_id, count, before=None):
    """The last `count` exchanges (a request and its finished reply), oldest first.

    `before`: only messages older than this one (the request being sent,
    already recorded, is sent apart: it must not be one of them).
    """
    if count <= 0:
        return []
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id = ? AND id < ? AND content IS NOT NULL "
        "AND content <> '' AND (role = 'user' OR status = 'ok') ORDER BY id DESC LIMIT ?",
        (conversation_id, before or _NO_BOUND, count * 2)).fetchall()
    return [{"message_id": r["id"], "role": r["role"], "content": r["content"]} for r in reversed(rows)]


MEANING_MIN = 0.5       # cosine below which a passage is not « close in meaning »
MEANING_SPREAD = 0.9    # and below this share of the best one
MEANING_KEEP = 5


def _by_meaning(conn, text, conversation_id=None, limit=MEANING_KEEP):
    """Keys ("m", id) / ("s", id) of the passages close in meaning (aihub/semantic.py), best first."""
    hits = semantic.nearest(conn, text, conversation_id=conversation_id, limit=limit * 4)
    if not hits:
        return []
    best = hits[0][0]
    keep = [h for h in hits if h[0] >= MEANING_MIN and h[0] >= best * MEANING_SPREAD][:limit]
    return [("m", h[1]) if h[1] is not None else ("s", h[2]) for h in keep]


def _row_of(conn, key):
    column = "message_id" if key[0] == "m" else "subtask_id"
    return conn.execute(f"SELECT message_id, subtask_id, role, content, conversation_id FROM memory "
                        f"WHERE {column} = ?", (key[1],)).fetchone()


def passages(conn, conversation_id, text, budget_tokens, exclude=()):
    """This conversation's passages closest to `text`, within `budget_tokens`.

    By words and by meaning (when the local AI's embedding model is there),
    the two rankings merged."""
    if budget_tokens <= 0:
        return []
    query = match_query(text)
    words = conn.execute(
        "SELECT message_id, subtask_id FROM memory WHERE memory MATCH ? AND conversation_id = ? "
        "ORDER BY bm25(memory) LIMIT 20", (query, conversation_id)).fetchall() if query else []
    word_keys = [("m", r["message_id"]) if r["message_id"] is not None else ("s", r["subtask_id"]) for r in words]
    keys = semantic.fuse(word_keys, _by_meaning(conn, text, conversation_id))
    rows = [row for row in (_row_of(conn, k) for k in keys) if row is not None]
    found, used = [], 0
    for row in rows:
        if row["message_id"] is not None and row["message_id"] in exclude:
            continue
        excerpt = _excerpt(row["content"], text)
        cost = tokens(excerpt) + 8
        if used + cost > budget_tokens:
            break
        found.append({"message_id": row["message_id"], "subtask_id": row["subtask_id"],
                      "role": row["role"], "content": excerpt})
        used += cost
    return found


def _excerpt(content, text):
    """The part of a long message around the first word of the request."""
    if len(content) <= EXCERPT:
        return content
    folded = content.casefold()
    at = min((folded.find(w) for w in _words(text) if folded.find(w) >= 0), default=0)
    begin = max(0, at - EXCERPT // 3)
    return ("…" if begin else "") + content[begin:begin + EXCERPT] + "…"


def get_summary(conn, conversation_id):
    row = conn.execute("SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                       (conversation_id,)).fetchone()
    return dict(row) if row else None


def save_summary(conn, conversation_id, content, up_to, model=None, tokens_in=None, tokens_out=None):
    conn.execute(
        "INSERT INTO conversation_summaries (conversation_id, content, up_to_message, model, tokens_in, tokens_out, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
        "content = excluded.content, up_to_message = excluded.up_to_message, model = excluded.model, "
        "tokens_in = excluded.tokens_in, tokens_out = excluded.tokens_out, created_at = excluded.created_at",
        (conversation_id, content, up_to, model, tokens_in, tokens_out, db.utc_now()))


def summary_prompt(previous, messages, max_words=300):
    """The request to the summary AI: what changed since the last summary, newest kept first."""
    lines, used = [], 0
    for message in reversed(messages):
        who = "Utilisateur" if message["role"] == "user" else "IA"
        block = f"[{who}] {message['content'][:SUMMARY_MESSAGE]}"
        if used + len(block) > SUMMARY_INPUT:
            break
        lines.append(block)
        used += len(block)
    lines.reverse()
    head = (f"Résumé précédent :\n{previous}\n\nMessages depuis :\n" if previous else "Conversation :\n")
    return ("Résume cette conversation pour une IA qui va la reprendre sans son historique. Dis l'objectif, "
            "les décisions prises (avec leurs valeurs exactes), l'état actuel, les fichiers touchés et les "
            f"questions ouvertes. En français, {max_words} mots au plus, sans préambule.\n\n"
            + head + "\n\n".join(lines))


def unsummarized(conn, conversation_id, after, before=None):
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id = ? AND id > ? AND id < ? "
        "AND content IS NOT NULL AND content <> '' AND (role = 'user' OR status = 'ok') ORDER BY id",
        (conversation_id, after, before or _NO_BOUND)).fetchall()
    return [dict(r) for r in rows]


def refresh_summary(conn, conversation_id, summarize, max_words=300, before=None):
    """The conversation's summary, rewritten only if messages came since.

    `summarize(prompt) -> (text, tokens_in, tokens_out, model)`: the summary AI
    (None: no summary). Returns the summary text, or "". `before`: as for
    last_exchanges.
    """
    current = get_summary(conn, conversation_id)
    after = current["up_to_message"] if current else 0
    new = unsummarized(conn, conversation_id, after, before)
    if not new:
        return current["content"] if current else ""
    if summarize is None:
        return current["content"] if current else ""
    text, tokens_in, tokens_out, model = summarize(summary_prompt(current["content"] if current else "",
                                                                  new, max_words))
    text = (text or "").strip()
    if text:
        save_summary(conn, conversation_id, text, new[-1]["id"], model, tokens_in, tokens_out)
    return text


def base_package(conn, conversation_id, *, summary, exchanges, budget):
    package = {"summary": summary, "exchanges": exchanges, "budget": budget}
    package["tokens"] = package_tokens(package)
    package["over"] = package["tokens"] > budget
    return package


def package_tokens(package):
    return tokens(render(package, [], ""))


def shrink(package, budget, resummarize=None):
    """Compress automatically to fit `budget`: shorter summary, then shorter exchanges.

    `resummarize(summary, max_words) -> text` asks the summary AI for a
    shorter summary (it costs a little of its quota); without it, the
    summary is cut.
    """
    out = {"summary": package["summary"], "exchanges": [dict(e) for e in package["exchanges"]], "budget": budget}
    if package_tokens(out) > budget and out["summary"] and resummarize is not None:
        words = max(60, int(len(out["summary"].split()) * 0.5))
        out["summary"] = resummarize(out["summary"], words) or out["summary"]
    # The oldest exchanges are cut first, then shortened from their end.
    while package_tokens(out) > budget and len(out["exchanges"]) > 2:
        out["exchanges"].pop(0)
    for exchange in out["exchanges"]:
        while package_tokens(out) > budget and len(exchange["content"]) > 400:
            exchange["content"] = exchange["content"][: int(len(exchange["content"]) * 0.7)].rstrip() + " […]"
    while package_tokens(out) > budget and len(out["summary"]) > 200:
        out["summary"] = out["summary"][: int(len(out["summary"]) * 0.8)].rstrip() + " […]"
    out["tokens"] = package_tokens(out)
    out["over"] = out["tokens"] > budget
    return out


def render(package, found, prompt):
    """The text sent to the new session: the package, the passages, then the request."""
    parts = ["<memoire_de_la_conversation>",
             "Cette conversation reprend dans une nouvelle session : son historique complet n'est pas joint. "
             "Voici l'essentiel ; l'outil search_this_conversation (serveur aihub) retrouve le reste."]
    if package.get("summary"):
        parts += ["", "## Résumé", package["summary"]]
    if package.get("exchanges"):
        parts += ["", "## Derniers échanges"]
        parts += [f"[{'Utilisateur' if e['role'] == 'user' else 'IA'}] {e['content']}" for e in package["exchanges"]]
    if found:
        parts += ["", "## Passages liés à la demande"]
        parts += [f"- {'utilisateur' if p['role'] == 'user' else 'IA' if p['role'] != 'subtask' else 'sous-tâche'} : "
                  f"{p['content']}" for p in found]
    parts += ["</memoire_de_la_conversation>"]
    text = "\n".join(parts)
    return f"{text}\n\nDemande :\n{prompt}" if prompt else text


def load_package(conversation):
    raw = conversation["fresh_package"]
    if not raw:
        return None
    try:
        package = json.loads(raw)
    except ValueError:
        return None
    return package if isinstance(package, dict) else None


def store_package(conn, conversation_id, package):
    conn.execute("UPDATE conversations SET fresh_package = ? WHERE id = ?",
                 (json.dumps(package, ensure_ascii=False) if package is not None else None, conversation_id))
