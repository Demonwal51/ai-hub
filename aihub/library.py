"""Personal conversation organisation and reusable work templates; never shared implicitly."""
import json
import re

from . import db, store

VARIABLE = re.compile(r"\{\{\s*([\w -]{1,40})\s*\}\}")
BUILTINS = [
    {"id": "builtin:bug", "title": "Corriger un bug", "prompt": "Corrige ce problème : {{probleme}}. "
     "Commence par le reproduire, explique la cause, puis vérifie la correction.",
     "settings": {"model": "default", "mode": "single", "permission_mode": "manual", "tools": [],
                  "instructions": "Préserve les changements existants. Termine par les changements et les vérifications."}},
    {"id": "builtin:analyse", "title": "Analyser un document", "prompt": "Analyse le document joint pour "
     "{{objectif}}. Distingue les faits, les incertitudes et les conclusions.",
     "settings": {"model": "default", "mode": "single", "permission_mode": "plan", "tools": [],
                  "instructions": "Appuie tes conclusions sur des passages précis du document."}},
    {"id": "builtin:release", "title": "Préparer une version", "prompt": "Prépare la version {{version}} : "
     "vérifie les changements et les tests, prépare les notes et le paquet. Attends mon accord avant publication.",
     "settings": {"model": "default", "mode": "orchestration", "permission_mode": "manual", "tools": [],
                  "instructions": "Le plan doit inclure la vérification et la préparation des résultats."}},
    {"id": "builtin:redaction", "title": "Rédiger un texte", "prompt": "Rédige {{texte}} pour {{public}}.",
     "settings": {"model": "default", "mode": "single", "permission_mode": "manual", "tools": [],
                  "instructions": "Écris en français, simplement. Demande les informations réellement manquantes."}},
]


def preferences(conn, user_id):
    return {row["conversation_id"]: {"pinned": bool(row["pinned"]), "archived": bool(row["archived"])}
            for row in conn.execute("SELECT * FROM conversation_preferences WHERE user_id IS ?", (user_id,))}


def set_preferences(conn, user_id, conversation_id, *, pinned=None, archived=None):
    existing = preferences(conn, user_id).get(conversation_id, {"pinned": False, "archived": False})
    for name, value in (("pinned", pinned), ("archived", archived)):
        if value is not None:
            if not isinstance(value, bool):
                raise store.StoreError("épingler ou archiver : oui ou non attendu")
            existing[name] = value
    conn.execute("INSERT INTO conversation_preferences (user_id, conversation_id, pinned, archived) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT DO UPDATE SET pinned = excluded.pinned, archived = excluded.archived",
                 (user_id, conversation_id, int(existing["pinned"]), int(existing["archived"])))
    return existing


def variables(prompt):
    return list(dict.fromkeys(match.strip() for match in VARIABLE.findall(prompt)))


def _view(row):
    return {"id": str(row["id"]), "title": row["title"], "settings": json.loads(row["settings_json"]),
            "prompt": row["prompt"], "variables": variables(row["prompt"]), "builtin": False}


def templates(conn, user_id):
    return [dict(item, variables=variables(item["prompt"]), builtin=True) for item in BUILTINS] + [
        _view(row) for row in conn.execute("SELECT * FROM work_templates WHERE user_id IS ? ORDER BY title COLLATE NOCASE",
                                         (user_id,))]


def template(conn, user_id, template_id):
    return next((item for item in templates(conn, user_id) if item["id"] == str(template_id)), None)


def save_template(conn, user_id, title, prompt, settings):
    title, prompt = title.strip(), prompt.strip()
    if not title or len(title) > 120 or len(prompt) > 8000 or len(variables(prompt)) > 10:
        raise store.StoreError("modèle : nom de 120 caractères, demande de 8 000 caractères et 10 champs au plus")
    if conn.execute("SELECT COUNT(*) FROM work_templates WHERE user_id IS ?", (user_id,)).fetchone()[0] >= 100:
        raise store.StoreError("100 modèles de travail au plus par personne")
    return conn.execute("INSERT INTO work_templates (user_id, title, prompt, settings_json, created_at) VALUES (?, ?, ?, ?, ?)",
                        (user_id, title, prompt, json.dumps(settings, ensure_ascii=False), db.utc_now())).lastrowid


def remove_template(conn, user_id, template_id):
    if not str(template_id).isdigit() or not conn.execute("DELETE FROM work_templates WHERE id = ? AND user_id IS ?",
                                                         (int(template_id), user_id)).rowcount:
        raise store.NotFound("modèle de travail introuvable")


def render(item, values):
    required = item["variables"]
    if any(not str(values.get(name) or "").strip() for name in required):
        raise store.StoreError("complétez les champs du modèle de travail")
    prompt = VARIABLE.sub(lambda match: values[match.group(1).strip()].strip(), item["prompt"])
    if len(prompt) > 20000:
        raise store.StoreError("demande du modèle trop longue (20 000 caractères au plus)")
    return prompt
