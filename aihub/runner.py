"""Run a request in a conversation and record everything in the database.

`Request` joins the store and a provider: it writes the user message, opens
the reply, streams text into it, journals every permission and quota report,
and closes the reply with its measurements, whatever way the run ends.
"""

import json
import os
import threading
from pathlib import Path

from . import access, accounts, config, memory, project_access, providers, routing, store, tools
from .providers import claude_code
from .providers.claude_code import ProviderError


# Codex's lightest models, in order: « Fast and affordable model for easier
# tasks » (codex-cli 0.162), then « Older fast and efficient model » (0.144).
LIGHT_CODEX = ("gpt-6-luna", "gpt-5.6-luna")


def summary_model(model):
    """The summary AI actually used (cahier des charges, section 5).

    The setting names one (Haiku by default); when its provider is not
    connected but Codex is (ChatGPT alone), Codex's lightest model writes it.
    """
    if not model or model == config.NO_SUMMARY:
        return model
    available = routing.available()
    if config.provider_of(model) in available:
        return model
    if "codex" not in available:
        # Neither Claude nor ChatGPT: the local AI, if it runs (cahier des charges, section 5).
        local = sorted(available.get("ollama") or ())
        return local[0] if local else model
    offered = available["codex"]
    if offered is None:
        return LIGHT_CODEX[0]
    return next((m for m in LIGHT_CODEX if m in offered), "codex")


def summarizer(commands, workdir, model, *, env=None, timeout_s=300):
    """summarize(prompt) -> (text, tokens_in, tokens_out, model) with the summary AI; None without one.

    A run of its own, recorded nowhere as a message, with no tool at all:
    it only reads the text it is given. `commands`: as for Request.
    """
    model = summary_model(model)
    if not model or model == config.NO_SUMMARY:
        return None
    provider = config.provider_of(model)

    def summarize(prompt):
        command = providers.command_for(commands, provider)
        result = providers.new_run(provider, command, prompt=prompt, workdir=workdir, permission_mode="plan",
                                   model=providers.default_model(provider, model), tools=[], env=env,
                                   timeout_s=timeout_s).run()
        if result.status != "ok":
            raise ProviderError(f"résumé impossible : {result.error}")
        return result.text, result.tokens_in, result.tokens_out, model
    return summarize


def resummarizer(summarize):
    """resummarize(summary, max_words) -> shorter text, for the automatic compression."""
    if summarize is None:
        return None

    def resummarize(summary, max_words):
        text, *_ = summarize(f"Raccourcis ce résumé à {max_words} mots au plus, sans perdre les décisions "
                             f"ni leurs valeurs exactes. En français, sans préambule.\n\n{summary}")
        return (text or "").strip()
    return resummarize


class Request:
    """One request. Build it, keep it to call `stop()` from another thread, then `run()`.

    `ask_permission(PermissionRequest) -> Decision` answers the agent's
    permission prompts; `on_text(chunk)` sees the reply as it is streamed.
    """

    def __init__(self, conn, conversation_id, agent_id, prompt, *, ask_permission=None,
                 on_text=None, on_start=None, command=None, timeout_s=None, env=None,
                 record_prompt=True, resume=True, tools=None, json_schema=None,
                 format_content=None, check_result=None, settings=None, on_quota=None, user_id=None,
                 account_id=None):
        """`record_prompt=False` and `resume=False` serve the conductor: its
        planning and synthesis prompts are built by the hub, not typed by the
        user, and each starts a fresh session. `tools` and `json_schema` are
        passed to the CLI (read-only tools, structured plan).

        `check_result(result)` may refuse a successful run (a malformed plan)
        by returning an error message: the reply is then closed as an error.
        `format_content(result)` gives the text stored for a successful reply."""
        self.conn = conn
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.prompt = prompt
        self.ask_permission = ask_permission
        self.on_text = on_text
        self.on_start = on_start
        self.command = command
        self.timeout_s = timeout_s
        self.env = env
        self.record_prompt = record_prompt
        self.resume = resume
        self.tools = tools
        self.json_schema = json_schema
        self.format_content = format_content
        self.check_result = check_result
        self.settings = settings or config.Settings()  # summary AI, cap and exchanges of a fresh start
        self.on_quota = on_quota  # on_quota(provider, QuotaInfo) after it is recorded (phone alerts)
        self.user_id = user_id    # who sends it (None: the owner)
        self.account_id = account_id  # whose own AI accounts `env` names (None: the computer's)
        self.message_id = None
        self._run = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()

    def stop(self):
        """Ask the request to stop. Safe from any thread, before or during `run()`."""
        self._stopped.set()
        with self._lock:
            run = self._run
        if run is not None:
            run.stop()

    def _prepare(self):
        conversation = store.get_conversation(self.conn, self.conversation_id)
        agent = store.get_agent(self.conn, self.agent_id)
        providers.module(agent["provider"])  # Ollama: not connected yet
        if not conversation["workdir"]:
            raise ProviderError("aucun dossier de travail n'est défini pour cette conversation")
        if not Path(conversation["workdir"]).is_dir():
            raise ProviderError(f"dossier de travail introuvable : {conversation['workdir']}")
        return conversation, agent

    def run(self):
        """Run to the end and return the provider's RunResult.

        Refusals before anything starts (unknown agent, no working folder,
        a request already running) raise StoreError or ProviderError and
        leave no reply behind.
        """
        conversation, agent = self._prepare()
        self.provider = agent["provider"]
        command = providers.command_for(self.command, self.provider)
        self.request_id = None
        if self.record_prompt:
            self.request_id = store.add_user_message(self.conn, self.conversation_id, self.prompt, self.user_id)
        self.message_id = store.start_request(self.conn, self.conversation_id, self.agent_id)
        try:
            store.mark_message_running(self.conn, self.message_id)
            if self.on_start:
                self.on_start(self.message_id)
            # A fresh start asked for (« Repartir de zéro »): no resume, the
            # starting package instead of the history.
            package = memory.load_package(conversation) if self.resume else None
            last = store.last_session(self.conn, self.conversation_id) if self.resume and package is None else None
            resume_id = last[1] if last and last[0] == self.provider else None
            switched = last is not None and resume_id is None
            # Replies already there but no session at all: an imported conversation
            # (aihub/importer.py). The AI starts from its memory, as after a change of AI.
            # (A request alone, or a goal recorded before its first round, is not one.)
            imported = (package is None and self.resume and last is None
                        and store.has_replies(self.conn, self.conversation_id))
            if switched:
                # Another AI answered last: its session is of no use to this one,
                # which starts from the memory instead (« il va relire le RAG »).
                package = self._automatic_package(conversation)
            elif package is None and self.resume and (providers.stateless(self.provider) or imported):
                # The local AI keeps no session: the memory goes with every request.
                package = self._automatic_package(conversation)
                if not package["summary"] and not package["exchanges"]:
                    package = None  # the first request: nothing to remember yet
            result = self._attempt(conversation, agent, command, resume_id, package)
            if (resume_id and result.status == "error" and providers.lost_session(self.provider, result.error)
                    and not self._stopped.is_set()):
                # The CLI no longer has the session: start again from the memory,
                # with no one to ask, so compressed automatically if needed.
                package = self._automatic_package(conversation)
                result = self._attempt(conversation, agent, command, None, package)
                result.fresh_start = "automatic"
            elif switched:
                result.fresh_start = "switched"
            elif imported and package is not None and not providers.stateless(self.provider):
                result.fresh_start = "imported"
            elif package is not None and not providers.stateless(self.provider):
                result.fresh_start = "asked"
            if package is not None and result.status == "ok":
                memory.store_package(self.conn, self.conversation_id, None)  # done: resume from now on
        except ProviderError as exc:
            store.finish_message(self.conn, self.message_id, "error", error=str(exc))
            raise
        except BaseException:
            # Ctrl+C or a bug: the reply must not stay "running" for ever,
            # or the conversation would be locked.
            if self._run is not None:
                self._run.stop()
            store.finish_message(self.conn, self.message_id, "stopped")
            raise
        if result.status == "ok" and self.check_result is not None:
            refusal = self.check_result(result)
            if refusal:
                result.status, result.error = "error", refusal
        content = None
        if result.status == "ok":
            content = self.format_content(result) if self.format_content else result.text
        store.finish_message(
            self.conn, self.message_id, result.status,
            content=content,
            error=result.error,
            duration_ms=result.duration_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            session_id=result.session_id,
        )
        return result

    def _attempt(self, conversation, agent, command, resume_id, package):
        # Passages of the conversations the request names, joined to what
        # is sent (not to what is recorded): no tool round trip for them.
        project = access.reads_project(self.conn, self.user_id, conversation)
        joined = project_access.prefetch(self.conn, conversation, self.prompt, automatic=self.record_prompt,
                                         project=project)
        if package is not None:
            used = memory.package_tokens(package)
            # Already in the package, or the request itself (indexed as soon as recorded).
            seen = {e["message_id"] for e in package.get("exchanges", [])} | {self.request_id}
            found = memory.passages(self.conn, self.conversation_id, self.prompt,
                                    package.get("budget", self.settings.rag_budget) - used, exclude=seen)
            sent = memory.render(package, found, self.prompt) + joined
        else:
            sent = self.prompt + joined
        given = tools.for_run(self.conn, conversation, self.settings, own=package is not None, commands=self.command,
                              project=project, account_env=accounts.account_vars(self.env))
        env = self.env
        if given.env:
            env = {**(env if env is not None else claude_code.clean_env(os.environ)), **given.env}
        run = providers.new_run(
            self.provider,
            command,
            prompt=sent,
            workdir=conversation["workdir"],
            permission_mode=conversation["permission_mode"],
            model=providers.default_model(self.provider, agent["model"]),
            resume_session=resume_id,
            ask_permission=self._ask,
            on_text=self._text,
            on_quota=self._quota,
            timeout_s=self.timeout_s,
            env=env,
            tools=self.tools,
            json_schema=self.json_schema,
            # In a project, the agent may read the other shared conversations;
            # after a fresh start, it may search its own past too; plus the
            # tools chosen for the conversation (aihub/tools.py).
            mcp_config=given.mcp_config,
            auto_allow=given.auto_allow,
            web=given.web,
            instructions=project_access.instructions(self.conn, conversation, project=project),
        )
        with self._lock:
            self._run = run
        if self._stopped.is_set():
            run.stop()
        return run.run()

    def _automatic_package(self, conversation):
        s = self.settings
        local = providers.stateless(self.provider)
        summarize = summarizer(self.command, conversation["workdir"], s.summary_model, env=self.env)
        if local:
            # Sent with every request: no summary while the last exchanges hold everything
            # (writing one each time would cost a model call per request).
            before = len(memory.unsummarized(self.conn, self.conversation_id, 0, self.request_id))
            if before <= 2 * s.rag_exchanges:
                summarize = None
        try:
            # The request being sent is already recorded: it goes after the memory, not in it.
            summary = memory.refresh_summary(self.conn, self.conversation_id, summarize, before=self.request_id)
        except ProviderError:
            # No summary (quota, network): the exchanges, the passages and the search tool remain.
            summarize, summary = None, (memory.get_summary(self.conn, self.conversation_id) or {}).get("content", "")
        budget = conversation["rag_budget"] or (s.rag_budget_local if local else s.rag_budget)
        package = memory.base_package(self.conn, self.conversation_id, summary=summary,
                                      exchanges=memory.last_exchanges(self.conn, self.conversation_id,
                                                                      s.rag_exchanges, before=self.request_id),
                                      budget=budget)
        if package["over"]:
            package = memory.shrink(package, budget, resummarizer(summarize))
        return package

    def _text(self, chunk):
        store.append_message_content(self.conn, self.message_id, chunk)
        if self.on_text:
            self.on_text(chunk)

    def _quota(self, quota):
        store.record_quota(
            self.conn, self.provider, quota.status,
            limit_type=quota.limit_type, utilization=quota.utilization,
            resets_at=quota.resets_at,
            windows_json=json.dumps(quota.windows) if quota.windows else None,
            user_id=self.account_id,
        )
        if self.on_quota:
            self.on_quota(self.provider, quota)

    def _ask(self, request):
        if self.ask_permission is None:
            decision = claude_code.Decision(False, "aucune personne n'est là pour valider : refus par défaut")
        else:
            decision = self.ask_permission(request)
        if isinstance(decision, claude_code.Decision):
            store.record_permission(
                self.conn, message_id=self.message_id, tool_name=request.tool_name,
                input_json=json.dumps(request.input, ensure_ascii=False),
                decision="allow" if decision.allow else "deny",
                reason=decision.message or None,
            )
        return decision
