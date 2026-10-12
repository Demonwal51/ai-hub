"""Files and images sent with a request (the 📎 of the composer, a photo from the phone).

The file is kept next to the base, in <base>-pieces/<conversation>/, never in
the conversation's working folder, and goes with its conversation when that is
deleted. What the AI gets:
- a text file (code, notes, CSV, JSON…) of 100 KB at most: its text, in the
  request itself, so every AI reads it, the local one too;
- an image: Codex and the local AI (a vision model) see it themselves; Claude
  Code reads it with its own tool, its folder being added to what it may read;
- anything else (a PDF…): its path, for the AI to read with its tools.
The hub's own pages never show a file inline but a picture: whatever comes in
is downloaded, never opened as a page of the hub (an HTML or SVG file stays
inert).
"""

import mimetypes
import shutil
from pathlib import Path

from . import db
from .store import NotFound, StoreError

MAX_SIZE = 20 * 1024 * 1024        # one file
MAX_PER_REQUEST = 10
INLINE_TEXT = 100 * 1024           # a text file joined to the request as text, up to this
IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".log", ".py", ".js",
                 ".ts", ".tsx", ".jsx", ".html", ".css", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".rb",
                 ".php", ".sh", ".ps1", ".bat", ".sql", ".kt", ".swift", ".lua", ".r", ".tex", ".svg"}
NAME_MAX = 120


def safe_name(name):
    """A file name of ours: no folder, no character Windows refuses, never empty."""
    # The part after the last separator, by hand: Path would read « a: » as a drive on Windows.
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    if name in (".", ".."):
        name = ""
    name = "".join("-" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
    return (name or "fichier")[:NAME_MAX]


def kind_of(name, declared=None):
    guessed = mimetypes.guess_type(name)[0]
    mime = declared if declared and declared != "application/octet-stream" else guessed
    return (mime or "application/octet-stream").split(";")[0].strip().lower()


def save(conn, root, conversation_id, user_id, name, data, declared=None):
    """Keep a file sent for the next request of a conversation. Returns its row."""
    if not data:
        raise StoreError("fichier vide")
    if len(data) > MAX_SIZE:
        raise StoreError(f"fichier trop gros ({MAX_SIZE // 1024 // 1024} Mo au plus)")
    name = safe_name(name)
    mime = kind_of(name, declared)
    folder = Path(root) / str(int(conversation_id))
    folder.mkdir(parents=True, exist_ok=True)
    with db.transaction(conn):
        aid = conn.execute("INSERT INTO attachments (conversation_id, user_id, name, stored, mime, size, created_at) "
                           "VALUES (?, ?, ?, '', ?, ?, ?)",
                           (conversation_id, user_id, name, mime, len(data), db.utc_now())).lastrowid
        stored = folder / f"{aid}-{name}"
        stored.write_bytes(data)
        conn.execute("UPDATE attachments SET stored = ? WHERE id = ?", (str(stored), aid))
    return get(conn, aid)


def get(conn, attachment_id):
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise NotFound("pièce jointe introuvable")
    return dict(row)


def view(row):
    """What the page knows of a file: never its path on the server."""
    return {"id": row["id"], "name": row["name"], "mime": row["mime"], "size": row["size"],
            "image": row["mime"] in IMAGES, "message_id": row["message_id"]}


def pending(conn, conversation_id, user_id, ids):
    """The files a request takes: sent by this person, for this conversation, not taken yet."""
    ids = list(dict.fromkeys(int(i) for i in ids or []))
    if len(ids) > MAX_PER_REQUEST:
        raise StoreError(f"{MAX_PER_REQUEST} pièces jointes au plus par demande")
    rows = []
    for aid in ids:
        row = conn.execute("SELECT * FROM attachments WHERE id = ? AND conversation_id = ? AND user_id IS ? "
                           "AND message_id IS NULL", (aid, conversation_id, user_id)).fetchone()
        if row is None:
            raise NotFound("pièce jointe introuvable (déjà envoyée, ou à quelqu'un d'autre)")
        rows.append(dict(row))
    return rows


def attach(conn, rows, message_id):
    for row in rows:
        conn.execute("UPDATE attachments SET message_id = ? WHERE id = ?", (message_id, row["id"]))


def remove(conn, attachment_id, user_id):
    """Take back a file not sent yet (one's own)."""
    row = conn.execute("SELECT * FROM attachments WHERE id = ? AND user_id IS ? AND message_id IS NULL",
                       (attachment_id, user_id)).fetchone()
    if row is None:
        raise NotFound("pièce jointe introuvable")
    Path(row["stored"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))


def of_conversation(conn, conversation_id):
    """{message_id: [view]} of the files sent in a conversation."""
    found = {}
    for row in conn.execute("SELECT * FROM attachments WHERE conversation_id = ? AND message_id IS NOT NULL "
                            "ORDER BY id", (conversation_id,)):
        found.setdefault(row["message_id"], []).append(view(row))
    return found


def purge_unsent(conn, days=1):
    """Files sent for a request that never left (the page was closed): gone after a day."""
    from datetime import datetime, timedelta, timezone
    before = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")
    rows = conn.execute("SELECT id, stored FROM attachments WHERE message_id IS NULL AND created_at < ?",
                        (before,)).fetchall()
    for row in rows:
        Path(row["stored"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM attachments WHERE id = ?", (row["id"],))
    return len(rows)


def forget_conversation(root, conversation_id):
    """The conversation is deleted: its files go too."""
    shutil.rmtree(Path(root) / str(int(conversation_id)), ignore_errors=True)


def copy_for_branch(conn, root, source_id, target_id, before_id, message_ids, user_id):
    """Independent copies: deleting the source never breaks a branch's files."""
    pending = []
    rows = conn.execute("SELECT * FROM attachments WHERE conversation_id = ? AND message_id <= ? ORDER BY id",
                        (source_id, before_id)).fetchall()
    for row in rows:
        target_message = message_ids.get(row["message_id"])
        if row["message_id"] != before_id and target_message is None:
            continue
        try:
            content = Path(row["stored"]).read_bytes()
        except OSError:
            raise StoreError(f"le fichier « {row['name']} » manque : la branche n'a pas été créée") from None
        saved = save(conn, root, target_id, user_id, row["name"], content, row["mime"])
        if target_message is not None:
            attach(conn, [saved], target_message)
        else:
            pending.append(saved)
    return pending


def for_request(rows):
    """(text to join to the request, [{"path", "image"}] for the run) of the files it takes."""
    if not rows:
        return "", []
    parts, files = [], []
    for row in rows:
        path = Path(row["stored"])
        text = None
        if row["size"] <= INLINE_TEXT and (row["mime"].startswith("text/") or path.suffix.lower() in TEXT_SUFFIXES):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = None
        if text is not None:
            fence = "````" if "```" in text else "```"
            parts.append(f'<piece_jointe nom="{row["name"]}">\n{fence}\n{text}\n{fence}\n</piece_jointe>')
        else:
            image = row["mime"] in IMAGES
            what = "image" if image else row["mime"]
            parts.append(f'<piece_jointe nom="{row["name"]}" type="{what}" chemin="{path}">'
                         "Lis ce fichier avec ton outil de lecture.</piece_jointe>")
            files.append({"path": str(path), "image": image})
    return "\n\nPièces jointes à cette demande :\n" + "\n".join(parts), files
