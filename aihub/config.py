"""The hub's settings: one JSON file, editable from the app.

`data/config.json` by default. It never holds a secret: the password stays
a hash in the database. A missing file means the default values; a setting
missing from the file takes its default too, so an old file keeps working.

Every value is checked before anything is written (`validate`): a refused
change leaves the file as it was. The file is written whole, through a
temporary file, so it is never left half written.
"""

import json
import os
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CLAUDE_MODELS = ("default", "haiku", "sonnet", "opus", "fable")
# "codex": Codex's own default model. The others are offered only when the
# Codex installed lists them (a model can need a newer Codex).
CODEX_MODELS = ("codex", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                "gpt-5.6-terra", "gpt-5.6-luna")
MODELS = CLAUDE_MODELS + CODEX_MODELS


# The alerts (aihub/notify.py lists their labels); kept here so the settings need no crypto.
NTFY_EVENTS = ("permission", "done", "error", "plan", "plan_done", "autonomous_done", "scheduled", "quota")


LOCAL_PREFIX = "local:"  # a model of the local AI (Ollama), e.g. local:qwen3.5:4b


def provider_of(model):
    """The provider of a model: Codex for the GPT ones, the local AI for « local:… », Claude Code otherwise."""
    if isinstance(model, str) and model.startswith(LOCAL_PREFIX):
        return "ollama"
    return "codex" if model in CODEX_MODELS else "claude_code"


class ConfigError(Exception):
    """A refused setting; the message is fit for the user."""


@dataclass
class Settings:
    allowed_roots: list = field(default_factory=list)   # working folders allowed; empty: all
    obsidian_vault: str | None = None                    # vault of the project notes; None: no notes
    backup_enabled: bool = True
    backup_dir: str | None = None                        # None: data/sauvegardes next to the base
    backup_keep: int = 14
    models: list = field(default_factory=lambda: list(MODELS))  # the models offered in the app
    default_model: str = "default"                       # preselected for a new conversation
    default_conductor: str = "opus"                      # preselected conductor of an orchestration
    quota_warning: int = 80                              # % of a quota window from which the app warns
    idle_days: float = 365                               # a session closes after this many days unused
    max_failures: int = 5                                # wrong passwords before the lockout
    lock_minutes: float = 15                             # first lockout, doubled at each new one
    keep_awake: bool = True                              # hold sleep back while an agent works
    summary_model: str = "haiku"                         # writes the summary of a fresh start; "aucun": none
    rag_budget: int = 8000                               # tokens of a fresh start's package (Claude)
    rag_exchanges: int = 2                               # last exchanges given as they are
    rag_budget_local: int = 4000                         # the same cap for the local AI (small context)
    semantic_model: str = "nomic-embed-text"             # the local embedding model of the search by meaning; "": none
    ntfy_url: str | None = None                          # ntfy topic of the alerts; None: no ntfy
    ntfy_events: list = field(default_factory=lambda: list(NTFY_EVENTS))  # the alerts sent there
    ntfy_detail: str = "minimal"                         # minimal: what happened, not where
    tool_servers: list = field(default_factory=list)     # MCP servers a conversation may choose (aihub/tools.py)
    sandbox_cli: str | None = None                      # optional explicit sbx executable
    sandbox_data: str | None = None                     # process-only LOCALAPPDATA override (Windows)

    def to_dict(self):
        return asdict(self)


NAMES = [f.name for f in fields(Settings)]

# Shown in the app: label, and what the setting changes.
LABELS = {
    "allowed_roots": "Dossiers de travail autorisés (vide : tous)",
    "obsidian_vault": "Coffre Obsidian des notes de projet (vide : pas de notes)",
    "backup_enabled": "Sauvegarde quotidienne",
    "backup_dir": "Dossier des sauvegardes (vide : data/sauvegardes)",
    "backup_keep": "Nombre de sauvegardes gardées",
    "models": "Modèles proposés dans l'appli",
    "default_model": "Modèle par défaut d'une nouvelle conversation",
    "default_conductor": "Chef d'orchestre par défaut",
    "quota_warning": "Seuil d'alerte du quota (%)",
    "idle_days": "Fermer une session après (jours sans usage)",
    "max_failures": "Mots de passe erronés avant blocage",
    "lock_minutes": "Durée du premier blocage (minutes)",
    "keep_awake": "Empêcher la mise en veille pendant qu'un agent travaille",
    "summary_model": "IA qui écrit le résumé d'un nouveau départ",
    "rag_budget": "Plafond du paquet de départ (tokens)",
    "rag_exchanges": "Derniers échanges donnés tels quels",
    "rag_budget_local": "Plafond du paquet pour l'IA locale (tokens)",
    "semantic_model": "Modèle local de la recherche par le sens (vide : par mots seulement)",
    "ntfy_url": "Adresse ntfy des alertes (vide : pas de ntfy)",
    "ntfy_events": "Alertes envoyées à ntfy",
    "ntfy_detail": "Ce que disent les alertes ntfy",
    "tool_servers": "Serveurs d'outils (MCP) proposés aux conversations",
    "sandbox_cli": "Docker Sandboxes : chemin de la commande sbx (vide : détection)",
    "sandbox_data": "Docker Sandboxes : stockage Windows (vide : emplacement du moteur)",
}
NO_SUMMARY = "aucun"


def _folder(value, what, *, must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{what} : chemin vide")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{what} : indiquez un chemin complet ({value})")
    if must_exist and not path.is_dir():
        raise ConfigError(f"{what} : dossier introuvable ({path})")
    return str(path.resolve())


def _number(value, what, low, high, kind=int):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{what} : un nombre est attendu")
    if kind is int and value != int(value):
        raise ConfigError(f"{what} : un nombre entier est attendu")
    if not low <= value <= high:
        raise ConfigError(f"{what} : entre {low} et {high}")
    return kind(value)


def _bool(value, what):
    if not isinstance(value, bool):
        raise ConfigError(f"{what} : oui ou non attendu")
    return value


def validate(data, base=None):
    """A checked Settings from `base` (defaults if None) changed by `data`.

    Unknown names are refused rather than ignored: a typo must not pass
    for a change that did not happen.
    """
    if not isinstance(data, dict):
        raise ConfigError("réglages illisibles")
    unknown = sorted(set(data) - set(NAMES))
    if unknown:
        raise ConfigError(f"réglage inconnu : {', '.join(unknown)}")
    merged = (base or Settings()).to_dict()
    merged.update(data)
    out = {}
    roots = merged["allowed_roots"]
    if not isinstance(roots, list):
        raise ConfigError("dossiers autorisés : une liste est attendue")
    out["allowed_roots"] = list(dict.fromkeys(_folder(r, "dossier autorisé") for r in roots))
    vault = merged["obsidian_vault"]
    out["obsidian_vault"] = _folder(vault, "coffre Obsidian") if vault not in (None, "") else None
    out["backup_enabled"] = _bool(merged["backup_enabled"], "sauvegarde quotidienne")
    folder = merged["backup_dir"]
    out["backup_dir"] = (_folder(folder, "dossier des sauvegardes", must_exist=False)
                         if folder not in (None, "") else None)
    out["backup_keep"] = _number(merged["backup_keep"], "sauvegardes gardées", 1, 365)
    models = merged["models"]
    if not isinstance(models, list) or not models:
        raise ConfigError("modèles proposés : choisissez-en au moins un")
    wrong = [m for m in models if m not in MODELS]
    if wrong:
        raise ConfigError(f"modèle inconnu : {', '.join(map(str, wrong))} (attendu : {', '.join(MODELS)})")
    out["models"] = [m for m in MODELS if m in models]  # always in the same order
    for name, what in (("default_model", "modèle par défaut"), ("default_conductor", "chef d'orchestre par défaut")):
        if merged[name] not in out["models"]:
            raise ConfigError(f"{what} : « {merged[name]} » n'est pas parmi les modèles proposés")
        out[name] = merged[name]
    out["quota_warning"] = _number(merged["quota_warning"], "seuil d'alerte du quota", 50, 99)
    out["idle_days"] = _number(merged["idle_days"], "durée d'une session", 1, 3650, float)
    out["max_failures"] = _number(merged["max_failures"], "essais avant blocage", 3, 20)
    out["lock_minutes"] = _number(merged["lock_minutes"], "durée du blocage", 1, 1440, float)
    out["keep_awake"] = _bool(merged["keep_awake"], "empêcher la mise en veille")
    summary = merged["summary_model"]
    local_summary = isinstance(summary, str) and summary.startswith(LOCAL_PREFIX) and len(summary) > len(LOCAL_PREFIX)
    if summary != NO_SUMMARY and not local_summary and summary not in [m for m in MODELS if m != "default"]:
        raise ConfigError(f"IA du résumé : « {summary} » inconnue (un modèle de la liste ou {NO_SUMMARY})")
    out["summary_model"] = summary
    out["rag_budget"] = _number(merged["rag_budget"], "plafond du paquet de départ", 1000, 200000)
    out["rag_exchanges"] = _number(merged["rag_exchanges"], "derniers échanges", 0, 10)
    out["rag_budget_local"] = _number(merged["rag_budget_local"], "plafond de l'IA locale", 1000, 100000)
    semantic_name = merged["semantic_model"]
    if not isinstance(semantic_name, str) or len(semantic_name) > 100 or re.search(r"[^\w.:/-]", semantic_name):
        raise ConfigError("modèle de la recherche par le sens : un nom de modèle Ollama, ou vide")
    out["semantic_model"] = semantic_name.strip()
    url = merged["ntfy_url"]
    if url in (None, ""):
        out["ntfy_url"] = None
    else:
        from .notify import NotifyError, check_ntfy_url
        try:
            out["ntfy_url"] = check_ntfy_url(url)
        except NotifyError as exc:
            raise ConfigError(str(exc)) from None
    events = merged["ntfy_events"]
    if not isinstance(events, list) or any(e not in NTFY_EVENTS for e in events):
        raise ConfigError(f"alertes ntfy inconnues (attendu : {', '.join(NTFY_EVENTS)})")
    out["ntfy_events"] = [e for e in NTFY_EVENTS if e in events]
    if merged["ntfy_detail"] not in ("minimal", "titre"):
        raise ConfigError("alertes ntfy : « minimal » ou « titre » attendu")
    out["ntfy_detail"] = merged["ntfy_detail"]
    from .tools import ToolError, check_catalog
    try:
        out["tool_servers"] = check_catalog(merged["tool_servers"])
    except ToolError as exc:
        raise ConfigError(str(exc)) from None
    cli = merged['sandbox_cli']
    if cli not in (None, ''):
        if not isinstance(cli, str) or not Path(cli).is_absolute() or not Path(cli).is_file():
            raise ConfigError('Docker Sandboxes : indiquez le chemin complet du fichier sbx')
        out['sandbox_cli'] = str(Path(cli).resolve())
    else:
        out['sandbox_cli'] = None
    folder = merged['sandbox_data']
    out['sandbox_data'] = _folder(folder, 'stockage Docker Sandboxes', must_exist=False) if folder not in (None, '') else None
    return Settings(**out)


def load(path):
    """The settings of the file, or the defaults if there is none."""
    path = Path(path)
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"fichier de réglages illisible ({path}) : {exc}") from None
    try:
        return validate(data)
    except ConfigError as exc:
        raise ConfigError(f"{path} : {exc}") from None


def save(path, settings):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(settings.to_dict(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
