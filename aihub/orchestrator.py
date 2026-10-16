"""Orchestration: the conductor plans, agents run the subtasks, the conductor sums up.

1. `propose_plan`: the conductor reads the project (read-only tools) and
   answers with a structured plan: subtasks, dependencies, the files each one
   changes, and how many agents should run at once.
2. The user edits the plan (store functions); `adapt_plan` lets the conductor
   rework it after changes such as an agent taken out.
3. `PlanExecution` runs the validated plan: subtasks whose dependencies are
   done start in parallel, up to the conductor's number, with two guards on
   files (see `PlanExecution`). Then the conductor writes the synthesis,
   failures included.
"""

import json
import os
import queue
import threading
from pathlib import Path

from . import access, accounts, config, db, memory, project_access, providers, rag, resumption, routing, store, tools
from .providers import claude_code
from .providers.claude_code import Decision, ProviderError
from .runner import Request
from .store import StoreError

READ_ONLY_TOOLS = ("Read", "Glob", "Grep")
# Tools that write one file, named in their input as file_path.
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
ALL_FILES = "*"
MAX_PARALLEL = 8          # technical ceiling on the conductor's choice
MAX_SUBTASKS = 12
RESULT_EXCERPT = 4000     # characters of a dependency's result passed on
KINDS = routing.KINDS     # the kinds the assignment table knows
# The request recorded when "Demander un plan" is sent with nothing written.
AUTO_PLAN_REQUEST = 'Prépare un plan à partir de notre discussion et du contexte du projet.'
# Every executed plan went through the user's validation (store.validate_plan,
# called by the "Lancer le plan" button only): the agents and the synthesis
# must not take its execution for a departure from the request.
VALIDATED = ("L'utilisateur a validé ce plan avant son exécution : le réaliser est ce qu'il a demandé.")


def _execution_request(request):
    """The request as the agents and the synthesis should read it, once the plan is validated."""
    if str(request or '').strip() == AUTO_PLAN_REQUEST:
        return ("L'utilisateur a demandé un plan à partir de la discussion et du contexte du projet, "
                "puis il l'a validé : il faut maintenant le réaliser.")
    return request


def plan_schema(agent_names=None):
    """JSON schema of the conductor's answer. With agent names, each subtask names its agent."""
    subtask = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "titre court"},
            "description": {"type": "string", "description": "ce que l'agent doit faire, précisément"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "depends_on": {"type": "array", "items": {"type": "integer", "minimum": 1},
                           "description": "numéros (à partir de 1) des sous-tâches à finir avant"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "fichiers créés ou modifiés ; \"*\" si inconnu"},
        },
        "required": ["title", "description", "kind", "depends_on", "files"],
    }
    if agent_names:
        subtask["properties"]["agent"] = {"type": "string", "enum": list(agent_names)}
        subtask["required"].append("agent")
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "max_parallel": {"type": "integer", "minimum": 1, "maximum": MAX_PARALLEL},
            "subtasks": {"type": "array", "items": subtask, "minItems": 1, "maxItems": MAX_SUBTASKS},
        },
        "required": ["summary", "max_parallel", "subtasks"],
    }


_KIND_LINES = "\n".join(f"  - {kind} : {label}" for kind, label in routing.KIND_LABELS.items())

_PLAN_RULES = f"""Règles du plan :
- Entre 1 et {MAX_SUBTASKS} sous-tâches. Une seule suffit si le travail ne se découpe pas.
- Chaque sous-tâche est confiée à un agent qui ne voit que sa description et les résultats des sous-tâches dont elle dépend : écris une description complète et précise.
- depends_on : les numéros des sous-tâches qui doivent être finies avant (à partir de 1). Les sous-tâches indépendantes tourneront en même temps.
- files : les fichiers que la sous-tâche va créer ou modifier, chemins relatifs au dossier de travail. Mets "*" si tu ne peux pas le savoir. Deux sous-tâches qui touchent le même fichier ne tourneront jamais en même temps.
- max_parallel : combien d'agents faire tourner en même temps (1 à {MAX_PARALLEL}). Chaque agent consomme le quota de l'utilisateur : n'en mets pas plus que ce que le parallélisme apporte vraiment.
- kind : le genre de travail, qui sert à choisir l'IA la plus forte pour la sous-tâche :
{_KIND_LINES}
  Découpe de façon que chaque sous-tâche ait un seul genre net quand c'est possible.
- summary : le plan en deux ou trois phrases, pour l'utilisateur.
- Ne modifie rien : tu peux seulement lire le projet pour préparer le plan."""


def planning_prompt(request):
    return f"""Tu es le chef d'orchestre d'une équipe d'agents de code. Prépare un plan pour la demande ci-dessous, sans l'exécuter.

{_PLAN_RULES}

Demande de l'utilisateur :
<demande>
{request}
</demande>"""


def _describe_plan(plan, agents_by_id):
    lines = []
    for st in plan["subtasks"]:
        agent = agents_by_id.get(st["agent_id"]) if st["agent_id"] else None
        who = f"{agent['name']} ({st['model']})" if agent else "aucun agent"
        deps = ", ".join(str(next(s["position"] for s in plan["subtasks"] if s["id"] == d))
                         for d in st["depends_on"]) or "aucune"
        lines.append(f"{st['position']}. {st['title'] or st['description'][:60]} — agent : {who} — "
                     f"dépend de : {deps} — fichiers : {', '.join(st['files']) or 'non précisés'}\n"
                     f"   {st['description']}")
    return "\n".join(lines)


DISCUSSION_EXCERPT = 1500    # characters of each discussion message passed on
DISCUSSION_MESSAGES = 12     # most recent messages passed on


def _describe_discussion(discussion):
    lines = []
    for message in discussion[-DISCUSSION_MESSAGES:]:
        who = "Utilisateur" if message["role"] == "user" else "Toi (chef)"
        lines.append(f"{who} : {(message['content'] or '')[:DISCUSSION_EXCERPT]}")
    return "\n".join(lines)


def adapt_prompt(request, plan, agents, agents_by_id, discussion=()):
    names = "\n".join(f"- {a['name']} : modèle {a['model']}" for a in agents)
    talk = ""
    if discussion:
        talk = f"""
Discussion avec l'utilisateur depuis le plan (applique ce qui y a été décidé) :
{_describe_discussion(discussion)}
"""
    return f"""Tu es le chef d'orchestre d'une équipe d'agents de code. L'utilisateur a modifié ton plan (sous-tâches ajoutées, retirées ou réassignées, agents retirés ou ajoutés) ou en a discuté avec toi. Adapte le plan.

- Garde les sous-tâches et les assignations que l'utilisateur a choisies, sauf si elles sont devenues impossibles ou si la discussion demande de les changer.
- Donne un agent à chaque sous-tâche, parmi les agents disponibles uniquement.
- Réorganise les dépendances et le parallélisme si les changements l'exigent.

{_PLAN_RULES}

Agents disponibles :
{names}

Plan actuel :
{_describe_plan(plan, agents_by_id)}
{talk}
Demande de l'utilisateur :
<demande>
{request}
</demande>"""


def discussion_prompt(text, plan=None, agents_by_id=None):
    context = ""
    if plan is not None:
        context = f"""
Plan en cours, pas encore lancé (l'utilisateur a pu le modifier depuis que tu l'as proposé) :
{plan.get('summary') or ''}
{_describe_plan(plan, agents_by_id or {})}

Tu ne peux pas modifier ce plan toi-même. Si l'utilisateur veut des changements, explique ce que tu ferais, puis dis-lui de cliquer sur « Adapter le plan » : tu referas alors le plan en tenant compte de cette discussion.
"""
    return f"""Discussion avec l'utilisateur. Tu es le chef d'orchestre d'une équipe d'agents de code : réponds à son message, en français. Tu peux lire le projet pour répondre, mais ne modifie rien : le travail se fait par un plan, que l'utilisateur demande avec « Demander un plan ».
{context}
Message de l'utilisateur :
<message>
{text}
</message>"""


def discussion_since(conn, plan):
    """The user's and the conductor's messages after the plan was proposed, plan replies excepted."""
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id = ? AND id > ? AND status = 'ok' "
        "ORDER BY id",
        (plan["conversation_id"], plan["request_message_id"]),
    ).fetchall()
    return [dict(r) for r in rows
            if not (r["role"] == "assistant" and (r["content"] or "").startswith(("Plan proposé", "Plan adapté")))]


def discuss(conn, conversation_id, text, *, on_request=None, **request_kwargs):
    """Talk with the conductor: it answers, reads if it needs to, changes nothing.

    It resumes its own session, so it remembers the project it read and the
    plans it made. An open plan is described to it as the user left it.
    """
    conversation = store.get_conversation(conn, conversation_id)
    if conversation["mode"] != "orchestration":
        raise StoreError("la conversation n'est pas en mode orchestration")
    conductor = _conductor(conn, conversation)
    row = store.open_plan(conn, conversation_id)
    plan = store.get_plan(conn, row["id"]) if row is not None and row["status"] == "proposed" else None
    agents_by_id = {a["id"]: a for a in store.list_agents(conn)}
    store.add_user_message(conn, conversation_id, text, request_kwargs.get("user_id"))
    run = Request(conn, conversation_id, conductor, discussion_prompt(text, plan, agents_by_id),
                  record_prompt=False, resume=True, tools=READ_ONLY_TOOLS, retrieval_prompt=text, **request_kwargs)
    if on_request:
        on_request(run)
    return run.run()


def subtask_prompt(request, plan, subtask, done_dependencies):
    parts = [f"""Tu fais partie d'une équipe d'agents. Fais uniquement la sous-tâche qui t'est confiée, puis termine par un court compte rendu de ce que tu as fait (fichiers créés ou modifiés, commandes lancées, problèmes rencontrés).

Demande d'origine de l'utilisateur (pour le contexte, ne la traite pas en entier) :
<demande>
{_execution_request(request)}
</demande>

Plan d'ensemble : {plan.get('summary') or 'non précisé'}
{VALIDATED}

Ta sous-tâche ({subtask['position']}/{len(plan['subtasks'])}) : {subtask['title'] or ''}
{subtask['description']}"""]
    if subtask["files"] and subtask["files"] != [ALL_FILES]:
        parts.append("Fichiers prévus pour toi : " + ", ".join(subtask["files"])
                     + ". D'autres agents travaillent peut-être en même temps sur d'autres fichiers : "
                       "ne touche pas aux fichiers qui ne te sont pas confiés.")
    for dep in done_dependencies:
        text = (dep["result"] or "")[:RESULT_EXCERPT]
        parts.append(f"Résultat de la sous-tâche {dep['position']} ({dep['title'] or ''}), dont tu dépends :\n{text}")
    return "\n\n".join(parts)


def synthesis_prompt(request, plan):
    lines = []
    for st in plan["subtasks"]:
        status = {"ok": "réussie", "error": "ÉCHEC", "stopped": "non lancée (bloquée)"}.get(st["status"], st["status"])
        detail = st["result"] if st["status"] == "ok" else (st["error"] or "")
        lines.append(f"Sous-tâche {st['position']} — {st['title'] or ''} — {status}\n{(detail or '')[:RESULT_EXCERPT]}")
    return f"""Tu es le chef d'orchestre. Les agents ont terminé. Écris la synthèse finale pour l'utilisateur : ce qui a été fait, ce qui reste à faire, et à vérifier. Si une sous-tâche a échoué ou n'a pas pu tourner, dis-le clairement, avec la raison : ne masque aucune erreur. Tu peux relire les fichiers pour vérifier, mais ne modifie rien.

Demande de l'utilisateur :
<demande>
{_execution_request(request)}
</demande>

Plan d'ensemble : {plan.get('summary') or 'non précisé'}
{VALIDATED} Ne présente pas son exécution comme un écart à la demande.

Résultats des sous-tâches :
{chr(10).join(lines)}"""


def specs_from_answer(answer, agents_by_name=None):
    """Turn the conductor's structured answer into store specs; refuse a malformed plan."""
    if not isinstance(answer, dict) or not isinstance(answer.get("subtasks"), list) or not answer["subtasks"]:
        raise ProviderError("le chef d'orchestre n'a pas rendu de plan exploitable")
    subtasks = answer["subtasks"][:MAX_SUBTASKS]
    specs = []
    for item in subtasks:
        if not isinstance(item, dict) or not str(item.get("description") or "").strip():
            raise ProviderError("le chef d'orchestre a rendu une sous-tâche sans description")
        files = [f for f in item.get("files") or [] if isinstance(f, str) and f.strip()]
        deps = [d for d in item.get("depends_on") or []
                if isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= len(subtasks)]
        kind = item.get("kind") if item.get("kind") in KINDS else "autre"
        spec = {"title": str(item.get("title") or "").strip() or None,
                "description": str(item["description"]).strip(),
                "files": files or [ALL_FILES], "depends_on": deps, "kind": kind}
        if agents_by_name is not None and item.get("agent") in agents_by_name:
            spec["agent_id"] = agents_by_name[item["agent"]]["id"]
        specs.append(spec)
    parallel = answer.get("max_parallel")
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        parallel = 1
    return (str(answer.get("summary") or "").strip() or None,
            min(parallel, MAX_PARALLEL, len(specs)), specs)


def _conductor(conn, conversation):
    agent_id = conversation["conductor_agent_id"] or conversation["agent_id"]
    if not agent_id:
        raise StoreError("aucun chef d'orchestre n'est choisi pour cette conversation")
    return agent_id


def _plan_message(result):
    if result.structured and result.structured.get("summary"):
        return "Plan proposé : " + str(result.structured["summary"])
    return result.text


def propose_plan(conn, conversation_id, prompt, *, on_request=None, **request_kwargs):
    """Record the user's request, ask the conductor for a plan, store it. Returns (plan_id, result).

    `request_kwargs` go to runner.Request (command, env, callbacks...);
    `on_request(request)` receives it before it runs, so it can be stopped.
    When the conductor fails, no plan is created and plan_id is None.
    """
    conversation = store.get_conversation(conn, conversation_id)
    if conversation["mode"] != "orchestration":
        raise StoreError("la conversation n'est pas en mode orchestration")
    conductor = _conductor(conn, conversation)
    if store.open_plan(conn, conversation_id) is not None:
        # Checked before the conductor runs: a refused plan must cost no quota.
        raise store.Busy("un plan est déjà ouvert : lancez-le ou annulez-le, ou discutez-en avec le chef")
    context = ''
    retrieval = prompt
    if not str(prompt or '').strip():
        prompt = AUTO_PLAN_REQUEST
        exchanges = memory.last_exchanges(conn, conversation_id, 6)
        recent, used = [], 0
        for entry in reversed(exchanges):
            excerpt = entry['content'][:1500]
            if used + len(excerpt) > 10000:
                break
            recent.append({'role': entry['role'], 'texte': excerpt})
            used += len(excerpt)
        summary = (memory.get_summary(conn, conversation_id) or {}).get('content', '')[:2000]
        payload = json.dumps({'resume': summary, 'discussion': list(reversed(recent))}, ensure_ascii=False).replace('<', '\\u003c')
        context = ('\n\nContexte de cette conversation (échanges antérieurs) :\n' + payload +
                   '\nBase le plan sur les objectifs discutés. Si aucun objectif précis n’est connu, '
                   'propose une étape de clarification ou d’analyse initiale ; n’invente pas de besoin.')
        retrieval = next((e['content'][:2000] for e in reversed(exchanges) if e['role'] == 'user'), conversation['title'])
    request_id = store.add_user_message(conn, conversation_id, prompt, request_kwargs.get("user_id"))
    parsed = {}

    def check(result):
        try:
            parsed["plan"] = specs_from_answer(result.structured)
        except ProviderError as exc:
            return str(exc)
        return None

    run = Request(conn, conversation_id, conductor, planning_prompt(prompt) + context, record_prompt=False, retrieval_prompt=retrieval,
                  resume=False, tools=READ_ONLY_TOOLS, json_schema=plan_schema(),
                  format_content=_plan_message, check_result=check, **request_kwargs)
    if on_request:
        on_request(run)
    result = run.run()
    if result.status != "ok":
        return None, result
    summary, parallel, specs = parsed["plan"]
    plan_id = store.create_plan(conn, conversation_id, request_id, specs, summary=summary,
                                max_parallel=parallel)
    if conversation["assignment_mode"] == "auto":
        connected = (accounts.available_models(request_kwargs.get("command"), request_kwargs.get("env"),
                                              conversation["workdir"]) if request_kwargs.get("account_id") else None)
        routing.auto_assign(conn, plan_id, connected=connected)
    return plan_id, result


def adapt_plan(conn, plan_id, *, on_request=None, **request_kwargs):
    """Ask the conductor to rework a proposed plan after the user's changes."""
    plan = store.get_plan(conn, plan_id)
    if plan["status"] != "proposed":
        raise StoreError("le plan est validé : il ne peut plus être modifié")
    conversation = store.get_conversation(conn, plan["conversation_id"])
    conductor = _conductor(conn, conversation)
    # Agents the user took out of this plan are not offered back.
    agents = [a for a in store.list_agents(conn, enabled_only=True) if a["id"] not in plan["removed_agents"]]
    if not agents:
        raise StoreError("aucun agent n'est disponible pour le plan")
    agents_by_id = {a["id"]: a for a in store.list_agents(conn)}
    request_text = store.get_message(conn, plan["request_message_id"])["content"]
    by_name = {a["name"]: a for a in agents}
    parsed = {}

    def check(result):
        try:
            parsed["plan"] = specs_from_answer(result.structured, by_name)
        except ProviderError as exc:
            return str(exc)
        return None

    run = Request(conn, plan["conversation_id"], conductor,
                  adapt_prompt(request_text, plan, agents, agents_by_id, discussion_since(conn, plan)),
                  record_prompt=False, retrieval_prompt=request_text,
                  resume=False, tools=READ_ONLY_TOOLS,
                  json_schema=plan_schema([a["name"] for a in agents]),
                  format_content=lambda r: "Plan adapté : " + str((r.structured or {}).get("summary") or ""),
                  check_result=check, **request_kwargs)
    if on_request:
        on_request(run)
    result = run.run()
    if result.status != "ok":
        return result
    summary, parallel, specs = parsed["plan"]
    store.replace_plan(conn, plan_id, specs, summary=summary, max_parallel=parallel)
    if conversation["assignment_mode"] == "auto":
        # The conductor's own assignments stand; the table fills the gaps.
        connected = (accounts.available_models(request_kwargs.get("command"), request_kwargs.get("env"),
                                              conversation["workdir"]) if request_kwargs.get("account_id") else None)
        routing.auto_assign(conn, plan_id, only_unassigned=True, connected=connected)
    return result


def _norm(workdir, path):
    full = Path(path) if Path(path).is_absolute() else Path(workdir) / path
    text = os.path.normpath(str(full))
    return os.path.normcase(text)


class PlanExecution:
    """Run a validated plan. `run()` blocks; `stop()` may come from any thread.

    Two guards keep agents off each other's files:
    - subtasks that declare a common file (or "*", the whole project) never
      run at the same time;
    - at run time, an agent asking to write a file another running agent has
      already been allowed to write waits until that agent is done, before
      the user is even asked (this one works in manual and plan modes; in
      bypass mode nothing is asked, so only the declared files protect).

    Callbacks run in worker threads:
    - `ask_permission(subtask, PermissionRequest) -> Decision`;
    - `on_event(dict)`: plan_started, subtask_started, subtask_text,
      subtask_done, synthesis_started, text, plan_done.
    """

    def __init__(self, db_path, plan_id, *, command=None, env=None, ask_permission=None,
                 on_event=None, timeout_s=None, settings=None, user_id=None, account_id=None):
        self.db_path = db_path
        self.plan_id = plan_id
        self.settings = settings or config.Settings()  # the tool catalog, the vault
        self.user_id = user_id  # who launched it: their accounts, what they may let agents read
        self.account_id = account_id  # whose own AI accounts `env` names (None: the computer's)
        self.command = command
        self.env = env
        self.ask_permission = ask_permission
        self.on_event = on_event or (lambda event: None)
        self.timeout_s = timeout_s
        self._lock = threading.Condition()
        self._stopping = False
        self._runs = {}           # subtask id -> ClaudeCodeRun
        self._claimed = {}        # normalized path -> subtask id allowed to write it
        self._synthesis = None
        self.synthesis_message_id = None

    def stop(self):
        with self._lock:
            self._stopping = True
            runs = list(self._runs.values())
            synthesis = self._synthesis
            self._lock.notify_all()
        for run in runs:
            run.stop()
        if synthesis is not None:
            synthesis.stop()

    def run(self):
        """Run to the end. Returns the plan's final status (done, error or stopped)."""
        conn = db.connect(self.db_path)
        try:
            return self._run(conn)
        finally:
            conn.close()

    def _run(self, conn):
        plan = store.get_plan(conn, self.plan_id)
        conversation = store.get_conversation(conn, plan["conversation_id"])
        request_text = store.get_message(conn, plan["request_message_id"])["content"]
        command = self.command  # each subtask's CLI is found for its own provider
        store.start_plan(conn, self.plan_id)
        self.on_event({"type": "plan_started", "plan_id": self.plan_id})
        limit = max(1, min(plan["max_parallel"] or 1, MAX_PARALLEL))
        done = queue.Queue()
        threads = {}
        running_files = {}

        while True:
            with self._lock:
                stopping = self._stopping
            if stopping:
                break
            plan = store.get_plan(conn, self.plan_id)
            by_id = {s["id"]: s for s in plan["subtasks"]}
            for row in store.ready_subtasks(conn, self.plan_id):
                if len(threads) >= limit:
                    break
                subtask = by_id[row["id"]]
                files = set(subtask["files"] or [ALL_FILES])
                busy = set().union(*running_files.values()) if running_files else set()
                if running_files and (ALL_FILES in files or ALL_FILES in busy or files & busy):
                    continue  # shares a file with a running subtask: later
                store.start_subtask(conn, subtask["id"])
                self.on_event({"type": "subtask_started", "plan_id": self.plan_id,
                               "subtask_id": subtask["id"]})
                deps = [by_id[d] for d in subtask["depends_on"]]
                prompt = subtask_prompt(request_text, plan, subtask, deps)
                thread = threading.Thread(
                    target=self._run_subtask, daemon=True, name=f"aihub-subtask-{subtask['id']}",
                    args=(command, conversation, subtask, prompt, done))
                threads[subtask["id"]] = thread
                running_files[subtask["id"]] = files
                thread.start()
            if not threads:
                break
            sid, result, error = done.get()
            threads.pop(sid).join()
            running_files.pop(sid, None)
            # Record the end before freeing its files: the next agent's request
            # to write one of them then comes after this "done", for everyone.
            self._record(conn, sid, result, error)
            self._release(sid)

        # Stopped: every agent is killed and nothing more runs.
        with self._lock:
            stopping = self._stopping
        if stopping:
            for thread in threads.values():
                thread.join(30)
            while not done.empty():
                done.get_nowait()
            if store.get_plan(conn, self.plan_id)["status"] == "running":
                store.stop_plan(conn, self.plan_id)
            self.on_event({"type": "plan_done", "plan_id": self.plan_id, "status": "stopped"})
            return "stopped"

        store.finish_plan(conn, self.plan_id)
        status = store.get_plan(conn, self.plan_id)["status"]
        self._synthesize(conn, conversation, request_text)
        self.on_event({"type": "plan_done", "plan_id": self.plan_id, "status": status})
        return status

    def _record(self, conn, sid, result, error):
        if error is not None or result is None:
            message = str(error) if error else "erreur inconnue"
            store.finish_subtask(conn, sid, "error", error=message)
            self.on_event({"type": "subtask_done", "subtask_id": sid, "status": "error", "error": message})
            return
        status = result.status
        if status == "stopped":
            # Stopped by the user: stop_plan marks the rest.
            store.finish_subtask(conn, sid, "stopped", duration_ms=result.duration_ms, session_id=result.session_id)
        else:
            store.finish_subtask(conn, sid, status, result=result.text if status == "ok" else None,
                                 error=result.error, duration_ms=result.duration_ms,
                                 tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                                 session_id=result.session_id)
        self.on_event({"type": "subtask_done", "subtask_id": sid, "status": status,
                       "error": result.error})

    def _run_subtask(self, command, conversation, subtask, prompt, done):
        journal = db.connect(self.db_path)  # this thread's own connection
        length = [0]
        try:
            def on_text(chunk):
                journal.execute("UPDATE subtasks SET checkpoint = substr(checkpoint || ?, -200000) WHERE id = ? AND status = 'running'",
                                (chunk, subtask['id']))
                offset = length[0]
                length[0] += len(chunk)
                self.on_event({"type": "subtask_text", "subtask_id": subtask["id"],
                               "offset": offset, "chunk": chunk})

            provider = subtask["provider"] or claude_code.PROVIDER
            project = access.reads_project(journal, self.user_id, conversation)
            given = tools.for_run(journal, conversation, self.settings, commands=command, project=project,
                                  account_env=accounts.account_vars(self.env))
            env = self.env
            if given.env:
                env = {**(env if env is not None else claude_code.clean_env(os.environ)), **given.env}
            from . import isolation
            execution = isolation.for_run(journal, conversation, account_id=self.account_id or self.user_id,
                                          settings=self.settings)
            if execution is not None:
                project = project and execution.policy['memory'] == 'project'
                if tools.chosen(conversation):
                    raise ProviderError('les outils du PC ne sont pas disponibles dans un espace isolé')
                given = tools.RunTools()
            scope = resumption.scope(journal, conversation, self.account_id or self.user_id)
            saved_session = subtask['session_id'] if subtask['session_scope'] == scope else None
            continuing = subtask['attempts'] > 0
            if continuing:
                prompt = resumption.prompt(prompt, subtask['checkpoint'], subtask['error'])
            def on_session(session):
                journal.execute("UPDATE subtasks SET session_id = ?, session_scope = ? WHERE id = ? AND status = 'running'",
                                (session, scope, subtask['id']))
            run = providers.new_run(
                provider, providers.command_for(command, provider, execution=execution),
                prompt=prompt + rag.context(journal, conversation, subtask['title'] + ' ' + subtask['description'], project=project),
                workdir=conversation["workdir"], permission_mode=conversation["permission_mode"],
                model=providers.default_model(provider, subtask["model"]),
                resume_session=saved_session if continuing else None,
                ask_permission=lambda request: self._ask(journal, conversation, subtask, request),
                on_text=on_text, on_session=on_session, timeout_s=self.timeout_s, env=env,
                execution=execution,
                mcp_config=given.mcp_config, auto_allow=given.auto_allow, web=given.web,
                instructions=store.join_instructions(store.person_instructions(journal, conversation),
                                                     project_access.instructions(journal, conversation,
                                                                                 project=project)),
            )
            with self._lock:
                self._runs[subtask["id"]] = run
                stopping = self._stopping
            if stopping:
                run.stop()
            result = run.run()
            if saved_session and result.status == 'error' and providers.lost_session(provider, result.error) and not self._stopping:
                journal.execute('UPDATE subtasks SET session_id = NULL WHERE id = ?', (subtask['id'],))
                fresh = providers.new_run(provider, providers.command_for(command, provider, execution=execution),
                    prompt=prompt, workdir=conversation['workdir'], permission_mode=conversation['permission_mode'],
                    model=providers.default_model(provider, subtask['model']), ask_permission=lambda request: self._ask(journal, conversation, subtask, request),
                    on_text=on_text, on_session=on_session, timeout_s=self.timeout_s, env=env, execution=execution,
                    mcp_config=given.mcp_config, auto_allow=given.auto_allow, web=given.web,
                    instructions=store.person_instructions(journal, conversation))
                with self._lock:
                    self._runs[subtask['id']] = fresh
                    stopping = self._stopping
                if stopping:
                    fresh.stop()
                result = fresh.run()
            if result.text and not length[0]:
                journal.execute("UPDATE subtasks SET checkpoint = substr(checkpoint || ?, -200000) WHERE id = ?", (result.text, subtask['id']))
            journal.execute('UPDATE subtasks SET session_scope = ? WHERE id = ?', (scope, subtask['id']))
            done.put((subtask["id"], result, None))
        except Exception as exc:  # reported as the subtask's failure, never lost
            done.put((subtask["id"], None, exc))
        finally:
            with self._lock:
                self._runs.pop(subtask["id"], None)
            journal.close()

    def _ask(self, journal, conversation, subtask, request):
        paths = []
        if request.tool_name in FILE_TOOLS:
            # Claude Code names one file_path; a Codex file change may name several.
            named = request.input.get("file_paths") or [request.input.get("file_path")]
            paths = [p for p in named if isinstance(p, str) and p and p != "?"]
        keys = sorted({_norm(conversation["workdir"], p) for p in paths})
        claimed_here = []
        if keys:
            with self._lock:
                # Wait while another running agent owns one of these files; then
                # take them all at once (never some held while waiting for others).
                while not self._stopping and any(self._claimed.get(k, subtask["id"]) != subtask["id"]
                                                 for k in keys):
                    self._lock.wait(0.5)
                if self._stopping:
                    return Decision(False, "plan arrêté")
                for key in keys:
                    if key not in self._claimed:
                        self._claimed[key] = subtask["id"]
                        claimed_here.append(key)
        if self.ask_permission is None:
            decision = Decision(False, "aucune personne n'est là pour valider : refus par défaut")
        else:
            decision = self.ask_permission(subtask, request)
        if claimed_here and not decision.allow:
            with self._lock:
                for key in claimed_here:
                    self._claimed.pop(key, None)
                self._lock.notify_all()
        store.record_permission(journal, subtask_id=subtask["id"], tool_name=request.tool_name,
                                input_json=json.dumps(request.input, ensure_ascii=False),
                                decision="allow" if decision.allow else "deny",
                                reason=decision.message or None)
        return decision

    def _release(self, sid):
        with self._lock:
            for key in [k for k, owner in self._claimed.items() if owner == sid]:
                del self._claimed[key]
            self._lock.notify_all()

    def _synthesize(self, conn, conversation, request_text):
        plan = store.get_plan(conn, self.plan_id)
        # Read again: « Passer la suite sur … » may have changed the conductor while the tasks ran.
        conversation = store.get_conversation(conn, conversation["id"])
        conductor = _conductor(conn, conversation)

        def on_start(message_id):
            self.synthesis_message_id = message_id
            store.set_plan_synthesis(conn, self.plan_id, message_id)
            self.on_event({"type": "synthesis_started", "plan_id": self.plan_id, "message_id": message_id})

        length = [0]

        def on_text(chunk):
            offset = length[0]
            length[0] += len(chunk)
            self.on_event({"type": "text", "message_id": self.synthesis_message_id,
                           "offset": offset, "chunk": chunk})

        previous = store.get_message(conn, plan['synthesis_message_id']) if plan['synthesis_message_id'] else None
        synthesis_text = synthesis_prompt(request_text, plan)
        continuing_synthesis = previous is not None and previous['status'] in ('error', 'stopped')
        if continuing_synthesis:
            synthesis_text = resumption.prompt(synthesis_text, previous['content'], previous['error'])
        synthesis = Request(conn, conversation["id"], conductor, synthesis_text,
                            record_prompt=False, resume=continuing_synthesis, tools=READ_ONLY_TOOLS,
                            resume_message_id=previous['id'] if continuing_synthesis else None,
                            on_start=on_start, on_text=on_text, command=self.command, env=self.env,
                            timeout_s=self.timeout_s, settings=self.settings, account_id=self.account_id,
                            user_id=self.user_id, retrieval_prompt=request_text,
                            ask_permission=lambda r: Decision(False, "la synthèse ne fait que lire"))
        with self._lock:
            self._synthesis = synthesis
            stopping = self._stopping
        if stopping:
            synthesis.stop()
        try:
            synthesis.run()
        except (StoreError, ProviderError) as exc:
            self.on_event({"type": "synthesis_failed", "plan_id": self.plan_id, "error": str(exc)})
        finally:
            with self._lock:
                self._synthesis = None
