"""The memory by meaning: « teinte » finds « couleur ».

Every finished message and subtask report gets a vector from an embedding
model of the local AI (nomic-embed-text, in the local Ollama). A search
then looks both by words (FTS5, aihub/memory.py) and by meaning, and the two
rankings are merged (reciprocal rank fusion): a passage that both find comes
first, one that only meaning finds still comes.

Everything stays on the PC. Without the local AI (Ollama stopped, model
missing), the search goes on by words alone. The vectors are made in the
background (Indexer), never while a request waits.

Measured on the RTX 5060: 0.13 s for 5 passages once the model is loaded
(308 MB of video memory); 4 French questions out of 5 matched to the right
passage, with words they did not share.
"""

import array
import logging
import math
import threading

import httpx

from . import db

log = logging.getLogger("aihub.semantic")

MODEL = "nomic-embed-text"
DOCUMENT, QUERY = "search_document: ", "search_query: "  # the prefixes this model was trained with
CHARS = 6000          # of a message given to the model (its context is 2 048 tokens)
BATCH = 16
RRF_K = 60            # reciprocal rank fusion: 1 / (k + rank)


class SemanticError(Exception):
    pass


def _normalized(values):
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return array.array("f", (v / norm for v in values))


def pack(vector):
    return array.array("f", vector).tobytes()


def unpack(blob):
    vector = array.array("f")
    vector.frombytes(blob)
    return vector


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """The embedding model of an Ollama. `embed(texts, query=False)` -> normalized vectors."""

    def __init__(self, url, model=MODEL, timeout_s=60):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def embed(self, texts, query=False):
        prefix = QUERY if query else DOCUMENT
        try:
            response = httpx.post(f"{self.url}/api/embed", timeout=self.timeout_s,
                                  json={"model": self.model, "input": [prefix + t[:CHARS] for t in texts]})
            response.raise_for_status()
            vectors = response.json()["embeddings"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise SemanticError(f"modèle d'embeddings indisponible : {exc}") from None
        if len(vectors) != len(texts):
            raise SemanticError("réponse d'embeddings incomplète")
        return [_normalized(v) for v in vectors]


# The embedder in force, set by the app when the local AI and its model are there (None: words alone).
_embedder = None


def set_embedder(embedder):
    global _embedder
    _embedder = embedder


def embedder():
    return _embedder


def pending(conn, model, limit=BATCH):
    """Messages and reports that have no vector yet for this model."""
    rows = conn.execute(
        """
        SELECT 'message' AS kind, m.id, m.conversation_id, m.content FROM messages m
        WHERE m.content IS NOT NULL AND m.content <> '' AND (m.role = 'user' OR m.status = 'ok')
          AND NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.model = ? AND v.message_id = m.id)
        UNION ALL
        SELECT 'subtask', s.id, p.conversation_id, s.result FROM subtasks s JOIN plans p ON p.id = s.plan_id
        WHERE s.result IS NOT NULL AND s.result <> ''
          AND NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.model = ? AND v.subtask_id = s.id)
        LIMIT ?
        """, (model, model, limit)).fetchall()
    return [dict(r) for r in rows]


def index_pending(conn, embed, model, limit=BATCH):
    """Vectors for up to `limit` waiting items. Returns how many were made."""
    rows = pending(conn, model, limit)
    if not rows:
        return 0
    vectors = embed([r["content"] for r in rows])
    now = db.utc_now()
    for row, vector in zip(rows, vectors):
        conn.execute(
            "INSERT OR IGNORE INTO memory_vectors (conversation_id, message_id, subtask_id, model, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["conversation_id"], row["id"] if row["kind"] == "message" else None,
             row["id"] if row["kind"] == "subtask" else None, model, pack(vector), now))
    return len(rows)


def nearest(conn, text, *, conversation_id=None, limit=20, embed=None, model=None, allowed_keys=None):
    """[(score, message_id, subtask_id, conversation_id)] closest to `text`, or [] without the model."""
    current = _embedder
    embed = embed or (current.embed if current else None)
    model = model or (current.model if current else None)
    if embed is None or not str(text or "").strip():
        return []
    try:
        query = embed([str(text)], query=True)[0]
    except SemanticError as exc:
        log.info("recherche par le sens indisponible : %s", exc)
        return []
    if conversation_id is None:
        rows = conn.execute("SELECT message_id, subtask_id, conversation_id, vector FROM memory_vectors WHERE model = ?",
                            (model,))
    else:
        rows = conn.execute("SELECT message_id, subtask_id, conversation_id, vector FROM memory_vectors "
                            "WHERE model = ? AND conversation_id = ?", (model, conversation_id))
    scored = [(dot(query, unpack(r["vector"])), r["message_id"], r["subtask_id"], r["conversation_id"]) for r in rows
              if allowed_keys is None or (("m", r["message_id"]) if r["message_id"] is not None
                                           else ("s", r["subtask_id"])) in allowed_keys]
    scored.sort(key=lambda s: -s[0])
    return scored[:limit]


def fuse(word_keys, meaning_keys):
    """Reciprocal rank fusion of two rankings of keys, best first."""
    scores = {}
    for ranking in (word_keys, meaning_keys):
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda k: -scores[k])


class Indexer:
    """Makes the missing vectors in the background, a batch at a time."""

    def __init__(self, db_path, *, period_s=20):
        self.db_path = db_path
        self.period_s = period_s
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="aihub-semantic", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)

    def tick(self, max_items=200):
        """Index what is waiting (up to max_items). Returns how many vectors were made."""
        current = _embedder
        if current is None:
            return 0
        conn = db.connect(self.db_path)
        made = 0
        try:
            while made < max_items and not self._stop.is_set():
                from . import rag
                done = index_pending(conn, current.embed, current.model) + rag.index_pending(conn, current)
                if not done:
                    break
                made += done
        except SemanticError as exc:
            log.info("vecteurs remis à plus tard : %s", exc)
        finally:
            conn.close()
        return made

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("indexation par le sens")
            self._stop.wait(self.period_s)
