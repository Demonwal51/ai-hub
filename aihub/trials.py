"""What was never tried for real, and how it went (1.1): the owner's page « Essais à faire ».

Each trial has its steps; the owner says « Ça marche », « Problème » or
« Pas encore », with a remark. Nothing here runs a trial: they are done by
hand, on a phone, another PC, a real account.
"""

from .db import utc_now
from .store import StoreError

STATUSES = ("ok", "probleme", "attente")
MAX_NOTE = 2000

TRIALS = [
    ("telephone-qr", "Lier un téléphone par QR code", [
        "Sur le PC : Menu → Appareils → « Lier un téléphone ».",
        "Scanner le QR code avec l'appareil photo du téléphone.",
        "L'appli s'ouvre sur le téléphone, déjà connectée à votre compte."]),
    ("hors-ligne", "Lire sans réseau sur le téléphone", [
        "Sur le téléphone : Menu → « Hors connexion » → activer.",
        "Ouvrir deux ou trois conversations.",
        "Couper le Wi-Fi et les données (ou éteindre le PC), puis rouvrir l'appli.",
        "Le bandeau « Hors connexion » s'affiche et les conversations se lisent ; l'écriture est bloquée."]),
    ("export-chatgpt", "Importer un vrai export ChatGPT", [
        "chatgpt.com → Paramètres → Gestion des données → Exporter les données.",
        "Télécharger le .zip reçu par mail.",
        "AI Hub : Importer → « Fichier d'export ChatGPT ou Claude », cocher, importer, lire."]),
    ("export-claude", "Importer un vrai export Claude", [
        "claude.ai → Paramètres → Confidentialité → Exporter les données.",
        "Télécharger le .zip reçu par mail.",
        "AI Hub : Importer → « Fichier d'export ChatGPT ou Claude », cocher, importer, lire."]),
    ("invite-claude", "La connexion Claude d'une personne invitée", [
        "Menu → Personnes → inviter quelqu'un.",
        "La personne ouvre le lien et crée son compte.",
        "Chez elle : Menu → « Mes comptes IA » → connecter Claude, coller le code.",
        "Elle envoie une demande : la réponse vient de son compte, pas du vôtre."]),
    ("voix", "La lecture à voix haute", [
        "Sous une réponse : le bouton 🔊.",
        "La réponse est lue en français ; ⏹ arrête la lecture."]),
    ("maj-zip", "La mise à jour en un clic, sur une installation .zip", [
        "Installer une version précédente depuis son .zip, dans un dossier d'essai.",
        "Menu → Mise à jour → installer la nouvelle version.",
        "L'appli revient seule, dans la nouvelle version, avec ses données."]),
    ("restauration", "Restaurer une sauvegarde", [
        "Menu → Serveur → Sauvegardes → « Restaurer… » sur une sauvegarde récente.",
        "Le serveur redémarre et l'appli revient seule.",
        "Les conversations sont celles de la sauvegarde, et le compte rendu s'affiche dans « Serveur »."]),
    ("reprise-quota", "Un plan repris après un vrai quota", [
        "Un plan s'arrête sur « Quota … atteint ».",
        "Choisir « Reprendre automatiquement à … » ou « Confier à cette IA et reprendre ».",
        "Le plan reprend seul (ou tout de suite), sans rejouer les tâches terminées."]),
    ("rag-sens", "La recherche par le sens (Ollama)", [
        "Ollama allumé, avec son modèle d'embeddings.",
        "Dans une conversation d'un projet : ⋯ → Mémoire RAG… → chercher un mot de même sens qu'une note (pas le même mot).",
        "Le passage voulu arrive quand même."]),
    ("linux-kvm", "Les espaces isolés sur un vrai Linux avec KVM", [
        "Installer AI Hub sur un PC Linux (Ubuntu 24.04 ou plus) qui a /dev/kvm, avec Docker Sandboxes.",
        "Créer une conversation en espace isolé et lui donner une copie de quelques fichiers.",
        "L'IA travaille dans l'espace ; les fichiers produits se vérifient puis s'appliquent."]),
]
KEYS = {key for key, _, _ in TRIALS}


def list_trials(conn):
    """Every trial with its steps and what was said of it (« attente » when nothing yet)."""
    said = {row["key"]: row for row in conn.execute("SELECT * FROM trials")}
    out = []
    for key, title, steps in TRIALS:
        row = said.get(key)
        out.append({"key": key, "title": title, "steps": steps,
                    "status": row["status"] if row else "attente",
                    "note": row["note"] if row else "", "updated_at": row["updated_at"] if row else None})
    return out


def set_trial(conn, key, status, note=""):
    if key not in KEYS:
        raise StoreError("essai inconnu")
    if status not in STATUSES:
        raise StoreError("état : ok, probleme ou attente")
    note = str(note or "").strip()
    if len(note) > MAX_NOTE:
        raise StoreError(f"remarque trop longue ({MAX_NOTE} caractères au plus)")
    conn.execute("INSERT INTO trials (key, status, note, updated_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET status = excluded.status, note = excluded.note, "
                 "updated_at = excluded.updated_at", (key, status, note, utc_now()))
