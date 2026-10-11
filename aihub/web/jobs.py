"""Work running in the background for the web interface.

Four kinds of work, one at a time per conversation: a single reply, the
conductor's planning, the conductor adapting a plan, and the execution of a
validated plan. Each runs in its own thread with its own database
connection (sqlite3 connections belong to one thread). Text, permission
prompts and the end are published to the conversation's subscribers; a
permission prompt blocks the agent until someone answers it from the
interface, or until the wait limit, which counts as a refusal.
"""

import queue
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import accounts, autonomous, compare, db, obsidian, orchestrator, providers, schedule, store
from ..providers.claude_code import Decision, ProviderError
from ..runner import Request
from ..store import StoreError

ASK_USER_QUESTION = "AskUserQuestion"


class EventHub:
    """Fan-out of events to the subscribers of each conversation."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = {}

    def subscribe(self, conversation_id):
        q = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(conversation_id, set()).add(q)
        return q

    def unsubscribe(self, conversation_id, q):
        with self._lock:
            subs = self._subscribers.get(conversation_id)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    del self._subscribers[conversation_id]

    def publish(self, conversation_id, event):
        with self._lock:
            subs = list(self._subscribers.get(conversation_id, ()))
        for q in subs:
            q.put(event)

    def subscriber_count(self, conversation_id):
        with self._lock:
            return len(self._subscribers.get(conversation_id, ()))


@dataclass
class PendingPermission:
    id: str
    conversation_id: int
    message_id: int | None
    tool_name: str
    input: dict
    subtask_id: int | None = None
    subtask_title: str | None = None
    _answered: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    decision: Decision | None = None

    def to_dict(self):
        return {"id": self.id, "conversation_id": self.conversation_id,
                "message_id": self.message_id, "tool_name": self.tool_name, "input": self.input,
                "subtask_id": self.subtask_id, "subtask_title": self.subtask_title}


class Job:
    def __init__(self, conversation_id, kind):
        self.conversation_id = conversation_id
        self.kind = kind            # single, planning, discussion, adapt, execution
        self.request = None         # what stop() stops: a Request or a PlanExecution
        self.message_id = None
        self.plan_id = None
        self.run_id = None          # autonomous work: its run
        self.schedule_id = None     # launched by a scheduled task
        self.user = None            # who started it (None: the owner): their AI accounts, their prompts
        self.round = None           # autonomous work: the round in progress
        self.length = 0             # characters of the reply streamed so far
        self.subtask_texts = {}     # subtask id -> text streamed so far
        self.pending = {}
        self.stop_requested = False
        self.started = threading.Event()
        self.start_error = None
        self.lock = threading.Lock()


class _Both:
    """A request and the other AI answering it beside it: « Arrêter » stops both."""

    def __init__(self, request, side):
        self.request, self.side = request, side

    def stop(self):
        self.side.stop()
        self.request.stop()


def side_folder(db_path, kind):
    """A folder of one database's own, next to it: data/aihub-comptes for data/aihub.db.
    Two bases in one folder (the real one, a trial's) never share a person's accounts."""
    path = Path(db_path).resolve()
    return path.with_name(f"{path.stem}-{kind}")


class JobManager:
    def __init__(self, db_path, *, command=None, env=None, permission_timeout_s=1800,
                 request_timeout_s=None, obsidian_vault=None):
        self.db_path = db_path
        self.obsidian_vault = obsidian_vault
        self.settings = lambda: None  # the app's settings in force (create_app sets it)
        self.notifier = None          # phone alerts (aihub/notify.py; create_app sets it)
        self.command = command
        self.env = env
        self.accounts_root = side_folder(db_path, "comptes")  # each person's AI accounts
        self.permission_timeout_s = permission_timeout_s
        self.request_timeout_s = request_timeout_s
        self.hub = EventHub()
        self._lock = threading.Lock()
        self._jobs = {}
        self._threads = []
        conn = db.connect(db_path)
        try:
            self.recovered = store.recover_interrupted(conn)
            autonomous.recover(conn)  # a run cut by a stopped server does not stay « running »
        finally:
            conn.close()

    # --- starting and stopping --------------------------------------------

    def start(self, conversation_id, agent_id, prompt, *, wait_s=30, schedule_id=None, user=None, attachments=(),
              compare_with=None):
        """Start a single reply; return its message id once it exists.

        Refusals (a request already running, no working folder...) are
        raised here as StoreError or ProviderError, before anything runs.
        `schedule_id`: the scheduled task that launched it. `attachments`:
        the files sent with it (rows of aihub/attachments.py, checked already).
        """
        def body(job, conn):
            job.schedule_id = schedule_id
            return self._single(job, conn, agent_id, prompt, attachments, compare_with)
        return self._launch(conversation_id, "single", body, wait_s, user)

    def start_planning(self, conversation_id, prompt, *, wait_s=30, user=None):
        """Ask the conductor for a plan; return the conductor's message id."""
        return self._launch(conversation_id, "planning",
                            lambda job, conn: self._planning(job, conn, prompt), wait_s, user)

    def start_discussion(self, conversation_id, text, *, wait_s=30, user=None):
        """Send a message to the conductor; return its reply's message id."""
        return self._launch(conversation_id, "discussion",
                            lambda job, conn: self._discussion(job, conn, text), wait_s, user)

    def start_adapt(self, conversation_id, plan_id, *, wait_s=30, user=None):
        """Ask the conductor to adapt a proposed plan; return its message id."""
        return self._launch(conversation_id, "adapt",
                            lambda job, conn: self._adapt(job, conn, plan_id), wait_s, user)

    def start_autonomous(self, conversation_id, agent_id, goal, *, wait_s=30, user=None, **limits):
        """Start autonomous work toward a goal; return its run id once it exists.

        `limits`: max_rounds, max_minutes, check_command, confirm_check, schedule_id.
        A bad limit or an unconfirmed check command is refused by AutonomousRun
        before anything is written, and raised here.
        """
        seen = {}

        def body(job, conn):
            seen["job"] = job
            job.schedule_id = limits.get("schedule_id")
            return self._autonomous(job, conn, agent_id, goal, limits)
        self._launch(conversation_id, "autonomous", body, wait_s, user)
        return seen["job"].run_id

    def start_execution(self, conversation_id, plan_id, *, wait_s=30, user=None):
        """Run a validated plan; return the plan id once it is running."""
        self._launch(conversation_id, "execution",
                     lambda job, conn: self._execution(job, plan_id), wait_s, user)
        return plan_id

    def _launch(self, conversation_id, kind, body, wait_s, user=None):
        job = Job(conversation_id, kind)
        job.user = user
        with self._lock:
            if conversation_id in self._jobs:
                raise store.Busy("une requête est déjà en cours dans cette conversation")
            self._jobs[conversation_id] = job
        thread = threading.Thread(target=self._work, args=(job, body),
                                  name=f"aihub-{kind}-{conversation_id}", daemon=True)
        self._threads.append(thread)
        thread.start()
        if not job.started.wait(wait_s):
            raise ProviderError("la requête n'a pas démarré à temps")
        if job.start_error is not None:
            raise job.start_error
        return job.message_id

    def stop(self, conversation_id):
        """Stop the running work of a conversation. False if none runs."""
        with self._lock:
            job = self._jobs.get(conversation_id)
        if job is None:
            return False
        with job.lock:
            job.stop_requested = True
            request = job.request
            pending = list(job.pending.values())
        for item in pending:
            self._decide(item, Decision(False, "requête arrêtée"))
        if request is not None:
            request.stop()
        return True

    def running(self, conversation_id):
        with self._lock:
            return conversation_id in self._jobs

    def busy(self):
        """True while any request or plan runs, in any conversation."""
        with self._lock:
            return bool(self._jobs)

    def kind(self, conversation_id):
        with self._lock:
            job = self._jobs.get(conversation_id)
        return job.kind if job else None

    def join(self, timeout=10):
        """Wait for every request thread (tests and shutdown)."""
        for thread in list(self._threads):
            thread.join(timeout)
        return not any(t.is_alive() for t in self._threads)

    def shutdown(self):
        with self._lock:
            ids = list(self._jobs)
        for cid in ids:
            self.stop(cid)
        self.join()

    # --- permissions ------------------------------------------------------

    def pending(self, conversation_id):
        with self._lock:
            job = self._jobs.get(conversation_id)
        if job is None:
            return []
        with job.lock:
            return [p.to_dict() for p in job.pending.values()]

    def may_answer(self, permission_id, user):
        """The person who started the request, or the owner: a guest never answers another's prompt."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                if permission_id in job.pending:
                    return user is None or user.get("is_owner") or (job.user or {}).get("id") == user["id"]
        return False

    def answer(self, permission_id, *, allow, message="", answers=None, user=None):
        """Answer a pending prompt. False if it is unknown, already answered, or not this person's."""
        if not self.may_answer(permission_id, user):
            return False
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            with job.lock:
                item = job.pending.get(permission_id)
            if item is None:
                continue
            if allow:
                updated = None
                if item.tool_name == ASK_USER_QUESTION:
                    if not isinstance(answers, dict) or not answers:
                        raise StoreError("répondez à la question avant de valider")
                    updated = {**item.input, "answers": {str(k): str(v) for k, v in answers.items()}}
                decision = Decision(True, updated_input=updated)
            else:
                decision = Decision(False, message.strip() or "refusé par l'utilisateur")
            return self._decide(item, decision)
        return False

    def _decide(self, item, decision):
        # The user's answer and the wait limit may come at the same time:
        # the first one wins, the other is reported as too late.
        with item._lock:
            if item._answered.is_set():
                return False
            item.decision = decision
            item._answered.set()
        return True

    # --- snapshot for a new subscriber -------------------------------------

    def snapshot(self, conversation_id):
        """What a newly connected interface needs to catch up with running work."""
        with self._lock:
            job = self._jobs.get(conversation_id)
        if job is None:
            return {"running": False, "kind": None, "message_id": None, "content": "",
                    "pending": [], "plan_id": None, "run_id": None, "round": None, "subtask_texts": {}}
        with job.lock:
            pending = [p.to_dict() for p in job.pending.values()]
            message_id = job.message_id
            subtask_texts = dict(job.subtask_texts)
        content = ""
        if message_id is not None:
            conn = db.connect(self.db_path)
            try:
                content = store.get_message(conn, message_id)["content"]
            finally:
                conn.close()
        return {"running": True, "kind": job.kind, "message_id": message_id, "content": content,
                "pending": pending, "plan_id": job.plan_id, "run_id": job.run_id, "round": job.round,
                "subtask_texts": {str(k): v for k, v in subtask_texts.items()}}

    # --- the worker thread -------------------------------------------------

    def _work(self, job, body):
        cid = job.conversation_id
        conn = None
        done = None
        try:
            conn = db.connect(self.db_path)
            done = body(job, conn)
        except (StoreError, ProviderError) as exc:
            if not job.started.is_set():
                job.start_error = exc
            else:
                done = {"message_id": job.message_id, "status": "error", "error": str(exc)}
        except Exception as exc:  # unexpected: replies are closed by the runner
            if not job.started.is_set():
                job.start_error = ProviderError(f"erreur interne : {exc}")
            else:
                done = {"message_id": job.message_id, "status": "error", "error": f"erreur interne : {exc}"}
        finally:
            with job.lock:
                leftovers = list(job.pending.values())
            for item in leftovers:
                self._decide(item, Decision(False, "requête terminée"))
            if done is not None and job.schedule_id is not None:
                self._record_task_end(job, done)  # before the conversation is free: no one sees it unrecorded
            # Free the conversation before announcing the end, so a message
            # sent right after "done" is not refused as still running.
            with self._lock:
                if self._jobs.get(cid) is job:
                    del self._jobs[cid]
            if conn is not None:
                conn.close()
            job.started.set()
            if done is not None:
                self.hub.publish(cid, {"type": "done", "kind": job.kind, **done})
                self._alert_end(job, done)

    def _alert(self, event, conversation_id, detail=None):
        if self.notifier is not None:
            try:
                self.notifier.notify(event, conversation_id, detail)
            except Exception:  # an alert never fails the work it reports
                pass

    def _alert_end(self, job, done):
        status, cid = done.get("status"), job.conversation_id
        if job.schedule_id is not None:
            self._end_of_task(job, done)
            return
        if status == "stopped":
            return  # the user stopped it: they know
        if job.kind == "execution":
            self._alert("plan_done", cid, "terminé" if status == "done" else "terminé avec des échecs")
        elif job.kind == "autonomous":
            said = {"done": "objectif atteint", "limit": "limite atteinte avant la fin",
                    "error": f"arrêté sur une erreur : {done.get('error')}"}
            self._alert("autonomous_done", cid, f"{said.get(status, status)} ({done.get('rounds')} tour(s))")
        elif status != "ok":
            self._alert("error", cid, done.get("error"))
        elif job.kind == "planning" and done.get("plan_id") is not None:
            self._alert("plan", cid)
        elif job.kind != "planning":
            self._alert("done", cid)

    def _record_task_end(self, job, done):
        status = done.get("status")
        conn = db.connect(self.db_path)
        try:
            schedule.finished(conn, job.schedule_id, "ok" if status in ("ok", "done") else status, done.get("error"))
        finally:
            conn.close()

    def _end_of_task(self, job, done):
        """A scheduled task's run is over (recorded by _record_task_end): one alert of its own."""
        status = done.get("status")
        said = {"ok": "réussie", "done": "objectif atteint", "limit": "limite atteinte avant la fin",
                "stopped": "arrêtée"}.get(status, f"en échec : {done.get('error')}")
        self._alert("scheduled", job.conversation_id, said)

    def _attach(self, job, stoppable):
        with job.lock:
            job.request = stoppable
            stop_now = job.stop_requested
        if stop_now:
            stoppable.stop()

    def _reply_callbacks(self, job):
        cid = job.conversation_id

        def on_start(message_id):
            job.message_id = message_id
            job.started.set()
            self.hub.publish(cid, {"type": "started", "kind": job.kind, "message_id": message_id})

        def on_text(chunk):
            with job.lock:
                offset = job.length
                job.length += len(chunk)
            self.hub.publish(cid, {"type": "text", "message_id": job.message_id,
                                   "offset": offset, "chunk": chunk})

        def on_quota(provider, quota):
            settings = self.settings()
            if self.notifier is not None and settings is not None:
                self.notifier.quota(providers.NAMES.get(provider, provider), quota, settings.quota_warning,
                                    user_id=self.account_id(job))

        return {"ask_permission": lambda r: self._ask(job, r), "on_start": on_start,
                "on_text": on_text, "on_quota": on_quota, "command": self.command, "env": self.env_of(job),
                "timeout_s": self.request_timeout_s, "settings": self.settings(),
                "user_id": (job.user or {}).get("id"), "account_id": self.account_id(job)}

    def env_of(self, job):
        """The environment of a job's runs: the AI accounts of the person who started it."""
        return accounts.run_env(self.env, job.user, self.accounts_root)

    @staticmethod
    def account_id(job):
        """Whose own accounts pay for the job (None: the computer's, the owner's)."""
        return job.user["id"] if accounts.own_accounts(job.user) else None

    @staticmethod
    def _result_event(job, result):
        return {"message_id": job.message_id, "status": result.status, "error": result.error,
                "duration_ms": result.duration_ms, "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out, "fresh_start": getattr(result, "fresh_start", None)}

    def _single(self, job, conn, agent_id, prompt, attachments=(), compare_with=None):
        callbacks = self._reply_callbacks(job)
        holder, side_thread = {}, []
        side = None
        if compare_with:
            # Another AI answers the same request beside it, read only (aihub/compare.py).
            cid = job.conversation_id
            side = compare.SideAnswer(self.db_path, cid, compare_with, commands=self.command, env=self.env_of(job),
                                      settings=self.settings(), files=attachments, timeout_s=self.request_timeout_s,
                                      on_done=lambda vid: self.hub.publish(cid, {"type": "variant_done",
                                                                                 "variant_id": vid}))
            main_started = callbacks["on_start"]

            def on_start(message_id):
                main_started(message_id)
                thread = threading.Thread(target=side.run, args=(holder["request"].request_id,),
                                          name="aihub-comparaison", daemon=True)
                side_thread.append(thread)
                thread.start()
            callbacks["on_start"] = on_start
        request = holder["request"] = Request(conn, job.conversation_id, agent_id, prompt, attachments=attachments,
                                              **callbacks)
        self._attach(job, _Both(request, side) if side else request)
        result = request.run()
        for thread in side_thread:
            thread.join()  # the job ends when both have answered
        return self._result_event(job, result)

    def _planning(self, job, conn, prompt):
        plan_id, result = orchestrator.propose_plan(
            conn, job.conversation_id, prompt, on_request=lambda r: self._attach(job, r),
            **self._reply_callbacks(job))
        job.plan_id = plan_id
        if plan_id is not None:
            self.hub.publish(job.conversation_id, {"type": "plan_proposed", "plan_id": plan_id})
        return {**self._result_event(job, result), "plan_id": plan_id}

    def _discussion(self, job, conn, text):
        result = orchestrator.discuss(conn, job.conversation_id, text,
                                      on_request=lambda r: self._attach(job, r),
                                      **self._reply_callbacks(job))
        return self._result_event(job, result)

    def _adapt(self, job, conn, plan_id):
        job.plan_id = plan_id
        result = orchestrator.adapt_plan(conn, plan_id, on_request=lambda r: self._attach(job, r),
                                         **self._reply_callbacks(job))
        if result.status == "ok":
            self.hub.publish(job.conversation_id, {"type": "plan_updated", "plan_id": plan_id})
        return {**self._result_event(job, result), "plan_id": plan_id}

    def _autonomous(self, job, conn, agent_id, goal, limits):
        cid = job.conversation_id
        callbacks = self._reply_callbacks(job)
        started = callbacks.pop("on_start")
        timeout = callbacks.pop("timeout_s")

        def on_run(run_id):
            job.run_id = run_id
            self.hub.publish(cid, {"type": "autonomous_started", "run_id": run_id})

        def on_round_start(message_id):
            with job.lock:
                job.length = 0  # each round's reply is streamed from its start
            started(message_id)

        def on_event(event):
            if event["type"] == "round_started":
                job.round = event["round"]
            self.hub.publish(cid, event)

        work = autonomous.AutonomousRun(conn, cid, agent_id, goal, on_event=on_event, on_run=on_run,
                                        on_start=on_round_start, request_timeout_s=timeout, **limits, **callbacks)
        self._attach(job, work)  # stop() stops the round and the check in progress
        run = work.run()
        return {"run_id": run["id"], "status": run["status"], "error": run["error"],
                "rounds": run["rounds"], "message_id": job.message_id}

    def _execution(self, job, plan_id):
        job.plan_id = plan_id
        cid = job.conversation_id

        def on_event(event):
            kind = event["type"]
            if kind == "plan_started":
                job.started.set()
            elif kind == "synthesis_started":
                job.message_id = event["message_id"]
            elif kind == "subtask_text":
                with job.lock:
                    job.subtask_texts[event["subtask_id"]] = (
                        job.subtask_texts.get(event["subtask_id"], "") + event["chunk"])
            self.hub.publish(cid, event)

        execution = orchestrator.PlanExecution(
            self.db_path, plan_id, command=self.command, env=self.env_of(job),
            ask_permission=lambda subtask, r: self._ask(job, r, subtask),
            on_event=on_event, timeout_s=self.request_timeout_s, settings=self.settings(),
            user_id=(job.user or {}).get("id"), account_id=self.account_id(job))
        self._attach(job, execution)
        status = execution.run()
        note_error = self.note_plan(plan_id)
        if note_error:
            self.hub.publish(cid, {"type": "obsidian_error", "error": note_error})
        return {"plan_id": plan_id, "status": status, "message_id": execution.synthesis_message_id,
                "error": None, "obsidian_error": note_error}

    def rewrite_note(self, conversation_id):
        """Rebuild the whole project note from the database, at the user's request.

        Creation, then every executed plan and every reply the user sent to
        the note, in order, each with its own date. The previous note goes
        to data/obsidian-sauvegardes first. Returns (note path, backup path).
        """
        if not self.obsidian_vault:
            raise obsidian.ObsidianError("aucun coffre Obsidian n'est configuré")
        conn = db.connect(self.db_path)
        try:
            conv = store.get_conversation(conn, conversation_id)
            if not conv["obsidian_note"]:
                raise StoreError("la note Obsidian n'est pas activée pour cette conversation")
            conductor = (store.get_agent(conn, conv["conductor_agent_id"])["model"]
                         if conv["conductor_agent_id"] else None)
            names = {a["id"]: a["name"] for a in store.list_agents(conn)}
            entries = []
            for plan in store.list_plans(conn, conversation_id):
                ran = any(s["started_at"] for s in plan["subtasks"])
                if plan["status"] in ("done", "error", "stopped") and ran:
                    request = store.get_message(conn, plan["request_message_id"])["content"]
                    synthesis = (store.get_message(conn, plan["synthesis_message_id"])["content"]
                                 if plan["synthesis_message_id"] else None)
                    when = plan["finished_at"] or plan["created_at"]
                    entries.append((when, obsidian.plan_section(plan, request, synthesis, names, when=when)))
            messages = store.list_messages(conn, conversation_id)
            for i, message in enumerate(messages):
                if message["noted_at"]:
                    request = next((m["content"] for m in reversed(messages[:i]) if m["role"] == "user"), None)
                    entries.append((message["created_at"],
                                    obsidian.reply_section(request, message["content"], when=message["created_at"])))
            sections = [obsidian.creation_section(conv, conductor, when=conv["created_at"])]
            sections += [section for _, section in sorted(entries, key=lambda e: e[0])]
            backups = Path(self.db_path).resolve().parent / "obsidian-sauvegardes"
            return obsidian.rewrite(self.obsidian_vault, conv["obsidian_note"], conv["title"], sections, backups)
        finally:
            conn.close()

    def note_plan(self, plan_id):
        """Add the executed plan to the conversation's project note, if it is on.

        Returns an error message, or None. A note that cannot be written
        never fails the plan: the work is done, only its record is missing.
        """
        if not self.obsidian_vault:
            return None
        conn = db.connect(self.db_path)
        try:
            plan = store.get_plan(conn, plan_id)
            conversation = store.get_conversation(conn, plan["conversation_id"])
            if not conversation["obsidian_note"]:
                return None
            request = store.get_message(conn, plan["request_message_id"])["content"]
            synthesis = (store.get_message(conn, plan["synthesis_message_id"])["content"]
                         if plan["synthesis_message_id"] else None)
            names = {a["id"]: a["name"] for a in store.list_agents(conn)}
            obsidian.append(self.obsidian_vault, conversation["obsidian_note"], conversation["title"],
                            obsidian.plan_section(plan, request, synthesis, names))
            return None
        except obsidian.ObsidianError as exc:
            return str(exc)
        finally:
            conn.close()

    def _ask(self, job, request, subtask=None):
        item = PendingPermission(
            id=uuid.uuid4().hex, conversation_id=job.conversation_id, message_id=job.message_id,
            tool_name=request.tool_name, input=request.input,
            subtask_id=subtask["id"] if subtask else None,
            subtask_title=(subtask["title"] or subtask["description"][:80]) if subtask else None,
        )
        with job.lock:
            if job.stop_requested:
                return Decision(False, "requête arrêtée")
            job.pending[item.id] = item
        self.hub.publish(job.conversation_id, {"type": "permission", **item.to_dict()})
        asked = f"l'IA demande « {item.tool_name} »"
        self._alert("permission", job.conversation_id,
                    f"{item.subtask_title} : {asked}" if item.subtask_title else asked)
        try:
            if not item._answered.wait(self.permission_timeout_s):
                self._decide(item, Decision(False, "pas de réponse dans le délai : refusé"))
        finally:
            with job.lock:
                job.pending.pop(item.id, None)
        decision = item.decision
        self.hub.publish(job.conversation_id, {"type": "permission_answered", "id": item.id,
                                               "allow": decision.allow})
        return decision
