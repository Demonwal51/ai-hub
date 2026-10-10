"""Project notes in an Obsidian vault.

One note per conversation, when the user turns it on: a "Création" section,
then a "Mise à jour" section after each executed plan, or when the user
sends a reply there by hand. Conversations themselves never go to the vault
(decision of the project's owner).

The hub only ever appends to the end of a note: what the user wrote in it
stays as it is. Every path is kept inside the vault. Obsidian itself is not
needed: these are plain Markdown files.
"""

import os
import re
from datetime import datetime
from pathlib import Path

FOLDER = "Projets AI Hub"
EXCERPT = 6000          # characters of a synthesis or reply kept in a note
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ObsidianError(Exception):
    """The note cannot be written; the message is fit for the user."""


def default_note_path(title):
    """'Projets AI Hub/<title>.md', with characters Windows refuses in a name replaced."""
    name = _UNSAFE.sub("-", title).strip(" .") or "Projet"
    return f"{FOLDER}/{name[:120]}.md"


def resolve(vault, note_path):
    """The note's absolute path, refused if it leaves the vault or is not Markdown."""
    if not vault:
        raise ObsidianError("aucun coffre Obsidian n'est configuré")
    root = Path(vault).resolve()
    if not root.is_dir():
        raise ObsidianError(f"coffre Obsidian introuvable : {root}")
    if not isinstance(note_path, str) or not note_path.strip():
        raise ObsidianError("chemin de note vide")
    if Path(note_path).is_absolute() or note_path.strip().startswith(("/", "\\")):
        raise ObsidianError("le chemin de la note doit être relatif au coffre")
    path = (root / note_path.strip()).resolve()
    if path != root and root not in path.parents:
        raise ObsidianError("la note doit rester dans le coffre Obsidian")
    if path.suffix.lower() != ".md":
        raise ObsidianError("la note doit être un fichier .md")
    return path


def _stamp(when=None):
    if isinstance(when, str):  # an ISO time from the database (UTC)
        try:
            when = datetime.fromisoformat(when).astimezone()
        except ValueError:
            when = None
    return (when or datetime.now()).strftime("%d/%m/%Y à %H:%M")


_HEADER = ("Note de projet tenue par AI Hub : création, puis une mise à jour "
           "après chaque plan exécuté. Les conversations n'y figurent pas.")


def rewrite(vault, note_path, title, sections, backup_dir):
    """Replace the whole note with the given sections, at the user's request.

    What the note held before (the user's own edits included) is first
    copied to `backup_dir`, outside the vault. Returns (note path, backup
    path or None if there was no note yet).
    """
    path = resolve(vault, note_path)
    text = f"# {title}\n\n{_HEADER}\n" + "".join("\n" + s.strip() + "\n" for s in sections)
    backup = None
    try:
        if path.exists():
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup = Path(backup_dir) / f"{path.stem} ({stamp}).md"
            backup.write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".aihub-tmp")
        with open(temporary, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)  # the note is never left half written
    except OSError as exc:
        raise ObsidianError(f"réécriture impossible de {path} : {exc}") from None
    return path, backup


def append(vault, note_path, title, section):
    """Append a section to the note, creating the note (and its folder) if needed."""
    path = resolve(vault, note_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            if new:
                fh.write(f"# {title}\n\n{_HEADER}\n")
            fh.write("\n" + section.strip() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise ObsidianError(f"écriture impossible dans {path} : {exc}") from None
    return path


def creation_section(conversation, conductor_model=None, when=None):
    mode = "orchestration" if conversation["mode"] == "orchestration" else "une IA"
    lines = [f"## Création — {_stamp(when)}", "",
             f"- Conversation : {conversation['title']}",
             f"- Dossier de travail : `{conversation['workdir']}`",
             f"- Fonctionnement : {mode}"]
    if conversation["mode"] == "orchestration":
        lines.append(f"- Chef d'orchestre : {conductor_model or 'par défaut'}")
        lines.append("- Choix des IA : " + ("automatique, selon les benchmarks"
                                            if conversation["assignment_mode"] == "auto" else "manuel"))
    permission = {"manual": "manuel", "plan": "plan", "bypass": "sans permission"}
    lines.append(f"- Mode de permission : {permission.get(conversation['permission_mode'], conversation['permission_mode'])}")
    return "\n".join(lines)


_STATUS = {"ok": "réussie", "error": "ÉCHEC", "stopped": "arrêtée", "pending": "non lancée", "running": "en cours"}
_PLAN_STATUS = {"done": "terminé", "error": "terminé avec des échecs", "stopped": "arrêté"}


def plan_section(plan, request_text, synthesis_text=None, agent_names=None, when=None):
    agent_names = agent_names or {}
    lines = [f"## Mise à jour — {_stamp(when)} — plan {_PLAN_STATUS.get(plan['status'], plan['status'])}", "",
             f"**Demande :** {request_text.strip()}", ""]
    if plan.get("summary"):
        lines += [f"**Plan :** {plan['summary']}", ""]
    lines += ["| # | Sous-tâche | IA | Statut | Fichiers prévus |", "|---|---|---|---|---|"]
    for st in plan["subtasks"]:
        who = agent_names.get(st["agent_id"], "") if st["agent_id"] else ""
        if who and st["model"] and f"({st['model']})" not in who:
            who = f"{who}, IA {st['model']}"  # a model other than the agent's own
        who = who or st["model"] or "—"
        files = ", ".join(f"`{f}`" for f in st["files"]) or "—"
        title = (st["title"] or st["description"][:60]).replace("|", "/")
        lines.append(f"| {st['position']} | {title} | {who} | {_STATUS.get(st['status'], st['status'])} | {files} |")
    failures = [st for st in plan["subtasks"] if st["status"] == "error"]
    if failures:
        lines += ["", "**Échecs :**"]
        lines += [f"- Sous-tâche {st['position']} : {(st['error'] or 'sans message').strip()}" for st in failures]
    if synthesis_text:
        lines += ["", "**Synthèse du chef :**", "", synthesis_text.strip()[:EXCERPT]]
    return "\n".join(lines)


def reply_section(request_text, reply_text, when=None):
    lines = [f"## Mise à jour — {_stamp(when)}", ""]
    if request_text:
        lines += [f"**Demande :** {request_text.strip()}", ""]
    lines += [reply_text.strip()[:EXCERPT]]
    return "\n".join(lines)
