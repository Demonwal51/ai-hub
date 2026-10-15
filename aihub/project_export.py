"""A whole project in one .zip (1.1): exported here, imported on another AI Hub.

    projet.json            the project (name, description, instructions, accesses) and the list below
    conversations/001.json each conversation, in AI Hub's export format (the import reads it back)
    documents/<name>       the project's memory documents (text)
    notes/001.md           a conversation's project note, when the one exporting may read it

The files of the working folder are not in it: they are the PC's. Reading a
.zip checks everything before anything is created: its size, its number of
files, the size each one says (a zip bomb is refused), and that every name is
one the list announces, in one of the three folders.
"""

import io
import json
import re
import zipfile
from datetime import datetime, timezone

FORMAT = 1
MAX_ZIP = 100 * 1024 * 1024        # the .zip itself
MAX_FILES = 2000
MAX_FILE = 20 * 1024 * 1024        # one file, once read
MAX_TOTAL = 200 * 1024 * 1024      # all of them, once read
_CONVERSATION = re.compile(r"^conversations/\d{3,4}\.json$")
_NOTE = re.compile(r"^notes/\d{3,4}\.md$")
_DOCUMENT = re.compile(r"^documents/[^/\\:\x00-\x1f]{1,120}$")


class ProjectFileError(Exception):
    """The .zip cannot be read as a project; the message is for the person."""


def build(project, isolation, conversations, documents):
    """`conversations`: [(export dict, instructions, note text or None)]; `documents`: [(name, text)]."""
    listing = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for n, (exported, instructions, note) in enumerate(conversations, 1):
            entry = {"file": f"conversations/{n:03d}.json", "instructions": instructions or "", "note": None}
            archive.writestr(entry["file"], json.dumps(exported, ensure_ascii=False, indent=1))
            if note:
                entry["note"] = f"notes/{n:03d}.md"
                archive.writestr(entry["note"], note)
            listing.append(entry)
        names = []
        for name, text in documents:
            names.append(f"documents/{name}")
            archive.writestr(names[-1], text)
        archive.writestr("projet.json", json.dumps({
            "aihub_project": FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "name": project["name"], "description": project["description"] or "",
            "instructions": project["instructions"] or "", "isolation": isolation,
            "conversations": listing, "documents": names}, ensure_ascii=False, indent=1))
    return buffer.getvalue()


def read(data):
    """The project a .zip holds: {name, description, instructions, isolation, conversations, documents}."""
    if len(data) > MAX_ZIP:
        raise ProjectFileError(f"fichier trop gros ({MAX_ZIP // 1024 // 1024} Mo au plus)")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ProjectFileError("ce fichier n'est pas un .zip de projet AI Hub") from None
    with archive:
        infos = {i.filename: i for i in archive.infolist()}
        if len(infos) > MAX_FILES:
            raise ProjectFileError("trop de fichiers dans ce .zip")
        if sum(i.file_size for i in infos.values()) > MAX_TOTAL or any(i.file_size > MAX_FILE for i in infos.values()):
            raise ProjectFileError("ce .zip est trop gros une fois ouvert")
        if "projet.json" not in infos:
            raise ProjectFileError("ce .zip n'est pas un projet AI Hub (projet.json manque)")

        def text(name):
            info = infos.get(name)
            if info is None:
                raise ProjectFileError(f"fichier annoncé mais absent du .zip : {name}")
            raw = archive.read(info)
            if len(raw) > MAX_FILE:  # the size said in the .zip can lie
                raise ProjectFileError("ce .zip est trop gros une fois ouvert")
            return raw.decode("utf-8", errors="replace")

        try:
            head = json.loads(text("projet.json"))
        except ValueError:
            raise ProjectFileError("projet.json illisible") from None
        if not isinstance(head, dict) or head.get("aihub_project") != FORMAT:
            raise ProjectFileError("ce .zip n'est pas un projet AI Hub de ce format")
        name = str(head.get("name") or "").strip()
        if not name:
            raise ProjectFileError("ce projet n'a pas de nom")
        conversations = []
        for entry in head.get("conversations") or []:
            if not isinstance(entry, dict) or not _CONVERSATION.match(str(entry.get("file"))):
                raise ProjectFileError("conversation au nom inattendu dans le .zip")
            note = entry.get("note")
            if note is not None and not _NOTE.match(str(note)):
                raise ProjectFileError("note au nom inattendu dans le .zip")
            try:
                exported = json.loads(text(entry["file"]))
            except ValueError:
                raise ProjectFileError(f"conversation illisible : {entry['file']}") from None
            if not isinstance(exported, dict):
                raise ProjectFileError(f"conversation illisible : {entry['file']}")
            conversations.append({"export": exported, "instructions": str(entry.get("instructions") or ""),
                                  "note": text(note) if note else None})
        documents = []
        for path in head.get("documents") or []:
            if not _DOCUMENT.match(str(path)) or ".." in str(path):
                raise ProjectFileError("document au nom inattendu dans le .zip")
            documents.append((str(path)[len("documents/"):], text(path)))
        isolation = head.get("isolation")
        return {"name": name, "description": str(head.get("description") or ""),
                "instructions": str(head.get("instructions") or ""),
                "isolation": isolation if isinstance(isolation, dict) else None,
                "conversations": conversations, "documents": documents}
