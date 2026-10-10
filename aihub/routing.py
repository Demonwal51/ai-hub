"""Automatic assignment: which AI for which kind of work, from independent benchmarks.

The conductor only classifies each subtask (its kind); this table, drawn
from the readings of 9 October 2026 (coffre aihub, « Comparatif Claude vs
GPT par tâche » and « Maths - detail »), picks the agent. Each rule carries
the figure it rests on, shown to the user as the reason of the choice.

Within a kind, rules are in order of preference. A rule whose provider is
not connected, or whose model the installed CLI does not offer (a GPT model
that needs a newer Codex), is skipped, and the reason says so: the user
sees what the better choice would have been.
Every kind ends with a Claude Code rule, so assignment never fails while
Claude Code is the only provider.
"""

from dataclasses import dataclass

from . import store


@dataclass(frozen=True)
class Rule:
    provider: str
    model: str
    label: str     # how the user calls this AI
    evidence: str  # the measurement the choice rests on


KIND_LABELS = {
    "code": "écrire ou modifier le code d'une application",
    "terminal": "commandes, scripts, installation, automatisation en ligne de commande",
    "migration": "porter du code vers une autre version, bibliothèque ou langage",
    "redaction": "documentation, textes, messages",
    "analyse": "lire et analyser des documents, données ou tableurs",
    "recherche": "chercher des informations sur le web",
    "maths": "calculs et raisonnements mathématiques courants",
    "maths_avancees": "problèmes mathématiques difficiles, preuves",
    "algorithmique": "algorithmes difficiles, optimisation, problèmes de concours",
    "ops": "déploiement, serveurs, fiabilité, dépannage d'infrastructure",
    "securite": "sécurité, audit, vulnérabilités",
    "autre": "rien de tout cela",
}
KINDS = tuple(KIND_LABELS)

_OPUS = ("claude_code", "opus", "Claude Opus")
_SONNET = ("claude_code", "sonnet", "Claude Sonnet")
_FABLE = ("claude_code", "fable", "Claude Fable")
_HAIKU = ("claude_code", "haiku", "Claude Haiku")
_ASTRA = ("codex", "gpt-6-astra", "GPT-6 Astra")
_SOL61 = ("codex", "gpt-6.1-sol", "GPT-6.1 Sol")
_SOL6 = ("codex", "gpt-6-sol", "GPT-6 Sol")


def _rule(who, evidence):
    return Rule(who[0], who[1], who[2], evidence)


RULES = {
    "code": [
        _rule(_OPUS, "n°1 Vibe Code Bench 1-100 (30,4 %) et ProgramBench, Vals AI"),
        _rule(_SONNET, "n°1 Vibe Code Bench v1.1 (92,4 %), Vals AI"),
        _rule(_ASTRA, "27,6 % sur Vibe Code Bench 1-100, Vals AI"),
    ],
    "terminal": [
        _rule(_OPUS, "n°1 Terminal-Bench avec Claude Code (64,8 %), tbench.ai"),
        _rule(_SONNET, "61,8 % sur Terminal-Bench avec Claude Code, tbench.ai"),
        _rule(_SOL61, "58,2 % sur Terminal-Bench avec Codex, six fois moins cher, tbench.ai"),
    ],
    "migration": [
        _rule(_SONNET, "n°1 Code Migration (69,8 %), Vals AI"),
        _rule(_ASTRA, "67,7 % sur Code Migration, Vals AI"),
        _rule(_OPUS, "66,7 % sur Code Migration, Vals AI"),
    ],
    "redaction": [
        _rule(_OPUS, "n°2 Arena écriture créative (1517), devant tous les GPT (meilleur : 1468)"),
        _rule(_FABLE, "n°3 Arena écriture créative (1502)"),
    ],
    "analyse": [
        _rule(_OPUS, "n°1 GDPval-AA, travail sur documents et tableurs (Elo 1866), Artificial Analysis"),
        _rule(_SONNET, "n°1 AA-Briefcase, workflows longs (Elo 1823), Artificial Analysis"),
    ],
    "recherche": [
        _rule(_FABLE, "n°1 Web Search Index, Vals AI"),
        _rule(_OPUS, "n°1 Humanity's Last Exam (61,4 %), Artificial Analysis"),
    ],
    "maths": [
        _rule(_FABLE, "n°2 Arena maths (1524), devant tous les GPT (meilleur : 1500)"),
        _rule(_OPUS, "1521 sur Arena maths (Opus 5)"),
    ],
    "maths_avancees": [
        _rule(_SOL61, "100 % FrontierMath Tier 4, Epoch AI"),
        _rule(_ASTRA, "97,6 % FrontierMath Tier 4, seul à résoudre FrontierMath Erdős, Epoch AI"),
        _rule(_OPUS, "95,0 % FrontierMath Tier 4, meilleur Claude, Epoch AI"),
    ],
    "algorithmique": [
        _rule(_ASTRA, "100 % IOI, Vals AI"),
        _rule(_SOL61, "96,9 % IOI, Vals AI"),
        _rule(_OPUS, "95,1 % IOI, meilleur Claude, Vals AI"),
    ],
    "ops": [
        _rule(_ASTRA, "n°1 SRE Bench (56,9 %), Vals AI"),
        _rule(_SOL61, "50,8 % sur SRE Bench, Vals AI"),
        _rule(_OPUS, "33,6 % sur SRE Bench, meilleur Claude, Vals AI"),
    ],
    "securite": [
        _rule(_SOL6, "n°1 CyberBench (78,0 %), Vals AI"),
        _rule(_HAIKU, "75,5 % sur CyberBench, très économe en quota, Vals AI"),
        _rule(_OPUS, "74,6 % sur CyberBench, Vals AI"),
    ],
    "autre": [
        _rule(_SONNET, "n°2 de l'indice général Artificial Analysis (56), plus économe qu'Opus"),
    ],
}

# What the hub found connected: provider -> the models it offers (None: any).
# Set by the app from the providers' own answers (set_available); until
# then, Claude Code alone, as before Codex.
_available = {"claude_code": None}


def set_available(available):
    """Record the connected providers, {provider: set of models or None}."""
    global _available
    _available = dict(available)


def available():
    return dict(_available)


@dataclass(frozen=True)
class Choice:
    rule: Rule
    reason: str


def route(kind, connected=None, excluded=frozenset(), models=None):
    """The best connected AI for a kind of work, with the reason to show.

    `connected`: the providers to use (default: those recorded by
    set_available); `models`: {provider: models offered}, for a provider
    whose installed CLI does not offer every model (default: recorded too).
    `excluded` holds (provider, model) pairs the user took out of the plan.
    Returns None when every candidate is excluded.
    """
    if connected is None:
        connected, models = frozenset(_available), (_available if models is None else models)
    models = models or {}
    rules = list(RULES.get(kind) or RULES["autre"])
    rules += [r for r in RULES["autre"] if r not in rules]  # last resort
    unusable = []  # (rule, why)
    passed_over = []
    for rule in rules:
        if (rule.provider, rule.model) in excluded:
            passed_over.append(rule)
            continue
        if rule.provider not in connected:
            unusable.append((rule, f"{_provider_name(rule.provider)} n'est pas branché"))
            continue
        offered = models.get(rule.provider)
        if offered is not None and rule.model not in offered:
            unusable.append((rule, f"le {_provider_name(rule.provider)} installé ne le propose pas "
                                   "(mise à jour à faire)"))
            continue
        reason = f"{rule.label} : {rule.evidence}"
        if unusable:
            best, why = unusable[0]
            reason += f". {best.label} serait meilleur ({best.evidence}), mais {why}"
        if passed_over:
            reason += f". {passed_over[0].label} a été retiré du plan"
        return Choice(rule, reason)
    return None


def _provider_name(provider):
    return {"codex": "Codex", "ollama": "l'IA locale", "claude_code": "Claude Code"}.get(provider, provider)


def follow_kind(conn, plan_id, subtask_id, connected=None):
    """After the user changed a subtask's kind: give it the AI of its new kind,
    unless the user had chosen its agent by hand (no automatic reason)."""
    plan = store.get_plan(conn, plan_id)
    subtask = next(s for s in plan["subtasks"] if s["id"] == subtask_id)
    if subtask["agent_id"] is not None and subtask["assignment_reason"] is None:
        return False
    return bool(auto_assign(conn, plan_id, connected=connected, only=subtask_id))


def auto_assign(conn, plan_id, *, only_unassigned=False, connected=None, only=None):
    """Give each subtask of a proposed plan the agent the table recommends.

    Missing agents are created (an agent is only a provider and a model).
    With only_unassigned, subtasks that already have an agent keep it.
    Returns the number of subtasks assigned.
    """
    plan = store.get_plan(conn, plan_id)
    excluded = set()
    for agent_id in plan["removed_agents"]:
        row = conn.execute("SELECT provider, model FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is not None:
            excluded.add((row["provider"], row["model"]))
    count = 0
    for st in plan["subtasks"]:
        if only is not None and st["id"] != only:
            continue
        if only_unassigned and st["agent_id"] is not None:
            continue
        choice = route(st["kind"] or "autre", connected, frozenset(excluded))
        if choice is None:
            continue  # every recommended agent was taken out: left to the user
        agent_id = store.ensure_agent(conn, choice.rule.provider, choice.rule.model)
        store.assign_subtask(conn, st["id"], agent_id, model=choice.rule.model, reason=choice.reason)
        count += 1
    return count
