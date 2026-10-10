"""HTTP API and single-page app of AI Hub.

With a password (`auth_policy`), every API call needs a session: see
aihub/auth.py. Without one (`local_only`), the server answers only requests
made on this computer, never one relayed by a proxy such as Tailscale.
"""

import json
import os
import queue
import subprocess
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Request as HttpRequest
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import VERSION_LABEL
from .. import (auth, autonomous, backup, config, db, eveil, memory, notify, obsidian, providers, routing, runner,
                schedule, semantic, store, tools)
from ..providers import claude_code, codex, ollama
from ..providers.claude_code import ProviderError
from ..store import BypassConfirmationRequired, Busy, NotFound, StoreError
from .jobs import JobManager

STATIC = Path(__file__).with_name("static")
MODELS = config.MODELS
_STATUS_CACHE_S = 60
QUOTA_REFRESH_S = 30


class NewConversation(BaseModel):
    title: str
    workdir: str = ""               # empty in a project: the project's folder
    project_id: int | None = None
    permission_mode: str = "manual"
    confirm_bypass: bool = False
    model: str = "default"
    mode: str = "single"            # single or orchestration
    conductor_model: str = "default"
    assignment_mode: str = "manual"  # orchestration: auto (benchmarks) or manual


class NewAgent(BaseModel):
    model: str


class SubtaskChange(BaseModel):
    agent_id: int | None = None
    unassign: bool = False
    model: str | None = None
    title: str | None = None
    description: str | None = None
    files: list[str] | None = None
    kind: str | None = None


class NewSubtask(BaseModel):
    description: str
    title: str | None = None
    agent_id: int | None = None
    model: str | None = None
    files: list[str] | None = None
    depends_on: list[int] = []      # subtask ids


class Dependency(BaseModel):
    depends_on_id: int


class AgentChoice(BaseModel):
    agent_id: int
    model: str | None = None


class ConversationChange(BaseModel):
    title: str | None = None
    permission_mode: str | None = None
    confirm_bypass: bool = False
    assignment_mode: str | None = None
    obsidian: bool | None = None      # turn the project note on or off
    obsidian_note: str | None = None  # its path in the vault (default: from the title)
    project_id: int | None = None     # move into this project
    leave_project: bool = False       # take it out of its project
    shared: bool | None = None        # open or close it to the project's other conversations
    model: str | None = None          # change of AI: the next request starts from the memory
    tools: list[str] | None = None    # the tools chosen for the conversation (aihub/tools.py)


class AutonomousStart(BaseModel):
    goal: str
    max_rounds: int = 5
    max_minutes: int = 60
    check_command: str | None = None
    confirm_check: bool = False


class NewSchedule(BaseModel):
    conversation_id: int
    prompt: str
    kind: str = "single"               # single or autonomous
    every: str = "daily"               # interval, daily or weekly
    interval_minutes: int | None = None
    at_time: str | None = None         # HH:MM, local time
    weekdays: list[int] | None = None  # 0 = Monday
    max_rounds: int = 5
    max_minutes: int = 60
    check_command: str | None = None
    confirm_check: bool = False


class ScheduleChange(BaseModel):
    enabled: bool


class PushSubscription(BaseModel):
    subscription: dict              # what the browser's PushManager gives: endpoint and keys
    events: list[str]               # the alerts this device chose


class PushEndpoint(BaseModel):
    endpoint: str


class NewProject(BaseModel):
    name: str
    description: str = ""
    workdir: str = ""


class ProjectChange(BaseModel):
    name: str | None = None
    description: str | None = None
    workdir: str | None = None        # "" clears it


class NewMessage(BaseModel):
    prompt: str
    # Orchestration only: "plan" asks the conductor for a plan, "discuss" talks with it.
    kind: str = "plan"


class PermissionAnswer(BaseModel):
    allow: bool
    message: str = ""
    answers: dict | None = None


class Login(BaseModel):
    password: str


class FreshStart(BaseModel):
    # None: prepare, and say if the package is over its cap. Over it, the
    # user's choice: "raise" the cap, "auto" compress, or a "custom" package.
    decision: str | None = None
    budget: int | None = None        # with "raise": the new cap, in tokens
    remember: bool = False           # with "raise": keep it for this conversation
    package: dict | None = None      # with "custom": {summary, exchanges}
    allow_over: bool = False         # with "custom": send it although over the cap


class ConfigChange(BaseModel):
    changes: dict
    password: str = ""                # with a password set: needed to change the settings


# Reachable without a session: the page itself (it shows the login form),
# what the phone needs to install it, and the login routes.
PUBLIC_PATHS = {"/", "/manifest.webmanifest", "/sw.js", "/api/auth/session", "/api/auth/login",
                "/api/auth/logout"}
PUBLIC_PREFIXES = ("/static/",)
# A custom header no other site can make a browser send (no CORS here):
# with SameSite=Strict, it keeps another page from acting through the session.
CSRF_HEADER = "x-aihub"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = (b"x-forwarded-for", b"forwarded", b"x-real-ip", b"tailscale-user-login")


def _headers(scope):
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def client_of(scope):
    """The client's address; behind `tailscale serve` (on this computer), the forwarded one."""
    peer = (scope.get("client") or ("?", 0))[0]
    forwarded = _headers(scope).get("x-forwarded-for")
    if peer in _LOOPBACK and forwarded:
        return forwarded.split(",")[-1].strip() or peer
    return peer


def _cookie(scope, name):
    raw = _headers(scope).get("cookie", "")
    for part in raw.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


async def _deny(send, status, detail, code):
    body = json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class Guard:
    """ASGI middleware (not BaseHTTPMiddleware: the live events stream through it)."""

    def __init__(self, app, *, check_session, local_only):
        self.app, self.check_session, self.local_only = app, check_session, local_only

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.local_only:
            peer = (scope.get("client") or ("?", 0))[0]
            relayed = any(k.lower() in _PROXY_HEADERS for k, _ in scope.get("headers", []))
            if peer not in _LOOPBACK or relayed:
                return await _deny(send, 403, "accès refusé : sans mot de passe, l'appli ne répond "
                                   "qu'à cet ordinateur (python -m aihub mot-de-passe)", "local_only")
        if self.check_session is None:
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        import anyio
        if not await anyio.to_thread.run_sync(self.check_session, _cookie(scope, auth.COOKIE)):
            return await _deny(send, 401, "connexion requise", "auth_required")
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and _headers(scope).get(CSRF_HEADER) != "1":
            return await _deny(send, 403, "requête refusée : en-tête X-AIHub manquant", "csrf")
        return await self.app(scope, receive, send)


class FreshStatic(StaticFiles):
    """Static files the browser must check again before reusing (cheap: ETag).

    Without it, the browser keeps an old app.js or style.css after an
    update, by heuristic caching, and the phone's service worker with it.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _custom_package(raw, budget):
    """A package compressed by hand, checked: a summary and exchanges, nothing else."""
    if not isinstance(raw, dict):
        raise StoreError("paquet illisible")
    summary = raw.get("summary") or ""
    exchanges = raw.get("exchanges") or []
    if not isinstance(summary, str) or not isinstance(exchanges, list):
        raise StoreError("paquet illisible")
    clean = []
    for item in exchanges:
        if (not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
                or not isinstance(item.get("content"), str) or not item["content"].strip()):
            raise StoreError("échange illisible dans le paquet")
        clean.append({"role": item["role"], "content": item["content"],
                      "message_id": item.get("message_id") if isinstance(item.get("message_id"), int) else None})
    return memory.base_package(None, None, summary=summary.strip(), exchanges=clean, budget=budget)


def _row(row):
    return dict(row) if row is not None else None


def _quota(row):
    if row is None:
        return None
    data = dict(row)
    try:
        data["windows"] = json.loads(data.pop("windows_json") or "{}")
    except ValueError:
        data["windows"] = {}
    return data


def create_app(db_path, *, command=None, env=None, allowed_roots=(), permission_timeout_s=1800,
               request_timeout_s=None, sse_ping_s=15, obsidian_vault=None, auth_policy=None,
               local_only=False, backup_dir=None, backup_keep=14, clock=None, keep_awake=False,
               awake_setter=None, settings=None, config_path=None, overridden=(), codex_command=None,
               push_post=None, run_scheduler=True, schedule_clock=None, ollama_url=None):
    """Build the application. `command` overrides the Claude Code CLI (tests).

    `codex_command` is the Codex CLI. Left to None, Codex is searched for
    only when `command` is too: a test that gives its own Claude Code does
    not start the real Codex of the computer by surprise.

    `push_post(url, content, headers, timeout) -> status` sends the phone
    alerts (Web Push, ntfy); httpx by default, tests give their own.
    `run_scheduler`: start the thread of the scheduled tasks (tests call
    its tick() themselves); `schedule_clock` gives its UTC time (tests).

    The settings come from `settings` (a config.Settings), else from the
    keyword arguments below. With `config_path`, the app can change them
    (the Réglages window): checked, written to that file, applied at once.
    `overridden`: names a command-line option fixed for this run.

    `obsidian_vault`: the vault where project notes go; without it, notes
    are not offered (the hub does not need Obsidian).
    `auth_policy`: an auth.Policy; every API call then needs a session.
    `local_only`: without a password, refuse what does not come from this computer.
    `backup_dir`: daily backups go there (None: no backup).
    `keep_awake`: hold the PC's sleep back while an agent works (aihub/eveil.py).
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if settings is None:
        policy = auth_policy or auth.Policy()
        settings = config.Settings(
            allowed_roots=[str(Path(r).resolve()) for r in allowed_roots],
            obsidian_vault=str(obsidian_vault) if obsidian_vault else None,
            backup_enabled=backup_dir is not None, backup_dir=str(backup_dir) if backup_dir else None,
            backup_keep=backup_keep, keep_awake=keep_awake, idle_days=policy.idle_days,
            max_failures=policy.max_failures, lock_minutes=policy.lock_minutes)
    live = {"settings": settings}
    settings_lock = threading.Lock()

    def current():
        return live["settings"]

    def backup_folder(s=None):
        s = s or current()
        if not s.backup_enabled:
            return None
        return s.backup_dir or str(Path(db_path).resolve().parent / "sauvegardes")

    def purge_folder():
        """Where copies may lie, even with backups turned off since: a deletion reaches them too."""
        s = current()
        return s.backup_dir or str(Path(db_path).resolve().parent / "sauvegardes")

    def policy_of(s):
        return auth.Policy(idle_days=s.idle_days, max_failures=s.max_failures, lock_minutes=s.lock_minutes)

    # By provider; None: searched for; False: not offered.
    commands = {claude_code.PROVIDER: command,
                codex.PROVIDER: codex_command if codex_command is not None else (None if command is None else False),
                # The local AI: its URL (a test's own); same rule, no real Ollama behind a test's fake Claude.
                ollama.PROVIDER: [ollama_url] if ollama_url else (None if command is None else False)}
    jobs = JobManager(db_path, command=commands, env=env, permission_timeout_s=permission_timeout_s,
                      request_timeout_s=request_timeout_s, obsidian_vault=settings.obsidian_vault)
    status_cache = {}
    refresh_lock = threading.Lock()
    refresh_state = {}
    authority = auth.Auth(policy_of(settings), now=clock) if auth_policy is not None else None
    running = {"started": False}

    def make_backup(s):
        folder = backup_folder(s)
        return backup.DailyBackup(db_path, folder, s.backup_keep) if folder else None

    def make_awake(s):
        return eveil.KeepAwake(jobs.busy, set_state=awake_setter) if s.keep_awake else None

    @asynccontextmanager
    async def lifespan(app):
        with settings_lock:
            running["started"] = True
            for helper in (app.state.backup, app.state.awake):
                if helper is not None:
                    helper.start()
        if run_scheduler:
            app.state.scheduler.start()
            local_status()               # learns whether the local AI is there (the search by meaning)
            app.state.indexer.start()
        yield
        app.state.scheduler.stop()
        app.state.indexer.stop()
        with settings_lock:
            running["started"] = False
            for helper in (app.state.awake, app.state.backup):
                if helper is not None:
                    helper.stop()
        # Stopping the server stops every running agent with it.
        jobs.shutdown()
        app.state.notifier.flush(10)  # the last alerts (the end of those agents) still go out

    app = FastAPI(title="AI Hub", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.backup = make_backup(settings)
    app.state.awake = make_awake(settings)
    app.state.settings = current
    jobs.settings = current
    notifier = notify.Notifier(db_path, current, post=push_post)
    jobs.notifier = app.state.notifier = notifier

    def launch_task(task):
        """Start a due scheduled task (store.Busy if its conversation is busy)."""
        with database() as conn:
            conv = store.get_conversation(conn, task["conversation_id"])
        if task["kind"] == "autonomous":
            opts = task["options"]
            jobs.start_autonomous(conv["id"], conv["agent_id"], task["prompt"], schedule_id=task["id"],
                                  max_rounds=opts["max_rounds"], max_minutes=opts["max_minutes"],
                                  check_command=opts.get("check_command"), confirm_check=True)
        else:
            jobs.start(conv["id"], conv["agent_id"], task["prompt"], schedule_id=task["id"])

    app.state.scheduler = schedule.Scheduler(db_path, launch_task, clock=schedule_clock)
    app.state.indexer = semantic.Indexer(db_path)  # the vectors of the search by meaning, in the background
    app.mount("/static", FreshStatic(directory=STATIC), name="static")

    def apply(new):
        """Put new settings in force at once, the helpers started or stopped as needed."""
        old = current()
        live["settings"] = new
        jobs.obsidian_vault = new.obsidian_vault
        if authority is not None:
            authority.policy = policy_of(new)
        if (backup_folder(old), old.backup_keep) != (backup_folder(new), new.backup_keep):
            if app.state.backup is not None and running["started"]:
                app.state.backup.stop()
            app.state.backup = make_backup(new)
            if app.state.backup is not None and running["started"]:
                app.state.backup.start()  # a new folder gets today's copy at once
        if old.keep_awake != new.keep_awake:
            if app.state.awake is not None and running["started"]:
                app.state.awake.stop()
            app.state.awake = make_awake(new)
            if app.state.awake is not None and running["started"]:
                app.state.awake.start()

    @contextmanager
    def database():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def check_session(token):
        with database() as conn:
            return authority.check(conn, token)

    app.add_middleware(Guard, check_session=check_session if authority else None, local_only=local_only)

    @app.exception_handler(auth.Locked)
    def _locked(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc), "code": "locked", "retry_after_s": exc.retry_after_s},
                            429, headers={"Retry-After": str(exc.retry_after_s)})

    @app.exception_handler(auth.AuthError)
    def _auth_refused(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc), "code": "bad_password"}, 401)

    # --- login -----------------------------------------------------------------

    @app.get("/api/auth/session")
    def session(request: HttpRequest):
        if authority is None:
            return {"required": False, "logged_in": True}
        return {"required": True, "logged_in": check_session(request.cookies.get(auth.COOKIE))}

    @app.post("/api/auth/login")
    def login(body: Login, request: HttpRequest):
        if authority is None:
            return {"logged_in": True}
        with database() as conn:
            token = authority.login(conn, body.password, client_of(request.scope),
                                    request.headers.get("user-agent"))
        response = JSONResponse({"logged_in": True})
        secure = (request.url.scheme == "https"
                  or request.headers.get("x-forwarded-proto", "").lower() == "https")
        response.set_cookie(auth.COOKIE, token, max_age=int(authority.policy.max_days * 86400),
                            httponly=True, samesite="strict", secure=secure, path="/")
        return response

    # Connected devices. Behind the session check (not in PUBLIC_PATHS).
    @app.get("/api/sessions")
    def sessions(request: HttpRequest):
        if authority is None:
            return {"sessions": [], "required": False}
        with database() as conn:
            return {"sessions": authority.sessions(conn, request.cookies.get(auth.COOKIE)), "required": True}

    @app.delete("/api/sessions/{sid}")
    def close_session(sid: int):
        if authority is None:
            raise NotFound("session introuvable")
        with database() as conn:
            if not authority.close_session(conn, sid):
                raise NotFound("session introuvable")
        return {"closed": sid}

    @app.post("/api/sessions/close-others")
    def close_other_sessions(request: HttpRequest):
        if authority is None:
            return {"closed": 0}
        with database() as conn:
            return {"closed": authority.close_others(conn, request.cookies.get(auth.COOKIE))}

    # --- phone notifications (each device chooses its alerts) -------------------

    @app.get("/api/push/key")
    def push_key():
        """The server's public key, which a device subscribes with."""
        with database() as conn:
            _, public = notify.server_key(conn)
        return {"public_key": public, "events": notify.EVENTS}

    @app.post("/api/push/subscription")
    def push_subscribe(body: PushSubscription, request: HttpRequest):
        with database() as conn:
            session_id = authority.session_id(conn, request.cookies.get(auth.COOKIE)) if authority else None
            notify.subscribe(conn, body.subscription, body.events, session_id=session_id,
                             user_agent=request.headers.get("user-agent"))
            return {"subscription": notify.subscription(conn, body.subscription.get("endpoint"))}

    @app.post("/api/push/state")
    def push_state(body: PushEndpoint):
        """This device's alerts (POST: the endpoint stays out of the address and the logs)."""
        with database() as conn:
            return {"subscription": notify.subscription(conn, body.endpoint)}

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe(body: PushEndpoint):
        with database() as conn:
            return {"removed": notify.unsubscribe(conn, body.endpoint)}

    @app.post("/api/push/test")
    def push_test():
        notifier.notify("done", None, "notification d'essai", test=True)
        return {"sent": True}

    @app.post("/api/auth/logout")
    def logout(request: HttpRequest):
        if authority is not None:
            with database() as conn:
                authority.logout(conn, request.cookies.get(auth.COOKIE))
        response = JSONResponse({"logged_in": False})
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    @app.exception_handler(BypassConfirmationRequired)
    def _bypass(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc), "code": "bypass_confirmation_required"}, 409)

    @app.exception_handler(autonomous.CheckConfirmationRequired)
    def _check_confirmation(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc), "code": "check_confirmation_required"}, 409)

    @app.exception_handler(NotFound)
    def _not_found(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(Busy)
    def _busy(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(StoreError)
    @app.exception_handler(ProviderError)
    @app.exception_handler(obsidian.ObsidianError)
    @app.exception_handler(config.ConfigError)
    @app.exception_handler(notify.NotifyError)
    @app.exception_handler(tools.ToolError)
    def _refused(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    def check_workdir(path):
        workdir = Path(path).expanduser()
        if not workdir.is_absolute():
            raise StoreError("indiquez un chemin complet pour le dossier de travail")
        workdir = workdir.resolve()
        if not workdir.is_dir():
            raise StoreError(f"ce dossier de travail n'existe pas : {workdir}")
        roots = [Path(r).resolve() for r in current().allowed_roots]
        if roots and not any(workdir == r or workdir.is_relative_to(r) for r in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise StoreError(f"dossier de travail non autorisé : {workdir} (autorisés : {allowed})")
        return str(workdir)

    def agent_for(conn, model):
        offered = (offered_models(current()) if config.provider_of(model) in (codex.PROVIDER, ollama.PROVIDER)
                   else current().models)
        if model not in offered:
            raise StoreError(f"modèle non proposé : {model!r} (proposés : {', '.join(offered)})")
        return store.ensure_agent(conn, config.provider_of(model), model)

    # --- pages ----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    # The phone installs the app from these two (PWA). The service worker is
    # served from the root so that it covers the whole site.
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    # --- state of the providers -------------------------------------------

    def claude_status():
        result = {"provider": "claude_code", "cli": None, "logged_in": None, "error": None}
        try:
            cli = providers.command_for(commands, claude_code.PROVIDER)
            result["cli"] = " ".join(cli)
            proc = subprocess.run(cli + ["auth", "status"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=60, encoding="utf-8",
                                  errors="replace", creationflags=claude_code.NO_WINDOW,
                                  env=env if env is not None else claude_code.clean_env(os.environ))
            result["logged_in"] = bool(json.loads(proc.stdout).get("loggedIn"))
        except ProviderError as exc:
            result["error"] = str(exc)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result["error"] = f"état de connexion illisible : {exc}"
        return result

    def codex_status():
        """Signed in, and the models this Codex offers (both free: no model call)."""
        result = {"provider": "codex", "cli": None, "logged_in": None, "error": None, "models": None,
                  "offered": commands[codex.PROVIDER] is not False}
        if not result["offered"]:
            return result
        try:
            cli = providers.command_for(commands, codex.PROVIDER)
            result["cli"] = " ".join(cli)
            result["logged_in"] = codex.login_status(cli, env=env)
            if result["logged_in"]:
                result["models"] = codex.list_models(cli, workdir=str(Path(db_path).resolve().parent), env=env)
        except ProviderError as exc:
            result["error"] = str(exc)
        return result

    def local_status():
        """The local Ollama: does it answer, and with which models (never started by the hub)."""
        result = {"provider": "ollama", "offered": commands[ollama.PROVIDER] is not False, "running": False,
                  "models": [], "error": None, "semantic": None}
        if not result["offered"]:
            semantic.set_embedder(None)
            return result
        try:
            url = providers.command_for(commands, ollama.PROVIDER)
            result["models"] = ollama.list_models(url)
            result["running"] = True
            # The search by meaning: on when the settings' embedding model is in this Ollama.
            wanted = current().semantic_model
            found = next((m for m in ollama.embedding_models(url) if wanted and m.split(":")[0] == wanted.split(":")[0]),
                         None)
            semantic.set_embedder(semantic.Embedder(url[0], found) if found else None)
            result["semantic"] = found
        except ProviderError as exc:
            result["error"] = str(exc)
            semantic.set_embedder(None)
        return result

    def provider_status(fresh=False):
        """The providers' state, kept a minute; the routing table learns what is connected."""
        cached = status_cache.get("all")
        if not fresh and cached and time.monotonic() - cached[0] < _STATUS_CACHE_S:
            return cached[1]
        claude, gpt, local = claude_status(), codex_status(), local_status()
        connected = {}
        if claude["logged_in"]:
            connected[claude_code.PROVIDER] = None
        if gpt["logged_in"]:
            connected[codex.PROVIDER] = set(gpt["models"] or ())
        if local["running"] and local["models"]:
            connected[ollama.PROVIDER] = set(local["models"])
        routing.set_available(connected or {claude_code.PROVIDER: None})
        result = {**claude, "codex": gpt, "local": local}
        status_cache["all"] = (time.monotonic(), result)
        return result

    @app.get("/api/status")
    def status():
        return provider_status()

    @app.get("/api/quota")
    def quota(refresh: bool = False):
        """Last known quota; with refresh, ask Claude Code first (costs no quota).

        Refreshes closer than QUOTA_REFRESH_S apart reuse the last reading.
        """
        errors = {}
        gpt = provider_status()["codex"]
        if refresh:
            with refresh_lock:
                last = refresh_state.get("at")
                if last is None or time.monotonic() - last >= QUOTA_REFRESH_S:
                    refresh_state["at"] = time.monotonic()
                    wanted = [claude_code] + ([codex] if gpt["logged_in"] else [])
                    for module in wanted:
                        try:
                            info = module.read_usage(
                                providers.command_for(commands, module.PROVIDER),
                                workdir=str(Path(db_path).resolve().parent),
                                env=env, warning_from=current().quota_warning / 100)
                            with database() as conn:
                                store.record_quota(
                                    conn, module.PROVIDER, info.status, limit_type=info.limit_type,
                                    utilization=info.utilization, resets_at=info.resets_at,
                                    windows_json=json.dumps(info.windows))
                            notifier.quota(module.DISPLAY_NAME, info, current().quota_warning)
                        except ProviderError as exc:
                            errors[module.PROVIDER] = str(exc)
                    refresh_state["errors"] = errors
                else:
                    errors = refresh_state.get("errors") or {}
        with database() as conn:
            return {"claude_code": _quota(store.latest_quota(conn, "claude_code")),
                    "codex": _quota(store.latest_quota(conn, "codex")) if gpt["logged_in"] else None,
                    "codex_connected": bool(gpt["logged_in"]),
                    "error": errors.get(claude_code.PROVIDER), "codex_error": errors.get(codex.PROVIDER)}

    def offered_models(s):
        """The models of the settings this computer can run: a GPT model only
        when Codex is signed in and offers it."""
        state = provider_status()
        gpt = state["codex"]
        usable = set(gpt["models"] or ()) | {codex.DEFAULT_MODEL} if gpt["logged_in"] else set()
        # The local AI's models come from its Ollama, after those of the settings.
        return [m for m in s.models if config.provider_of(m) != codex.PROVIDER or m in usable] + state["local"]["models"]

    @app.get("/api/options")
    def options():
        s = current()
        vault = s.obsidian_vault
        return {"models": offered_models(s), "default_model": s.default_model,
                "default_conductor": s.default_conductor, "quota_warning": s.quota_warning,
                "permission_modes": list(store.PERMISSION_MODES),
                "bypass_warning": store.BYPASS_WARNING, "check_warning": autonomous.CHECK_WARNING,
                "kinds": routing.KIND_LABELS,
                "obsidian": {"enabled": bool(vault), "vault": str(Path(vault).resolve()) if vault else None},
                "settings_editable": config_path is not None, "version": VERSION_LABEL}

    # --- settings (the Réglages window) ----------------------------------------

    @app.get("/api/config")
    def get_config():
        s = current()
        return {"settings": s.to_dict(), "labels": config.LABELS, "all_models": list(config.MODELS),
                "editable": config_path is not None, "password_needed": authority is not None,
                "overridden": list(overridden), "backup_folder": backup_folder(s),
                "file": str(Path(config_path).resolve()) if config_path else None}

    @app.patch("/api/config")
    def change_config(body: ConfigChange, request: HttpRequest):
        if config_path is None:
            raise StoreError("ces réglages sont fixés au lancement : pas de fichier de réglages")
        if authority is not None:
            # A stolen session alone must not widen the folders the agents may touch.
            with database() as conn:
                authority.verify(conn, body.password, client_of(request.scope))
        with settings_lock:
            # The file gets only the user's changes, not the options fixed at
            # launch; the running app gets them on top of those options.
            in_force = config.validate(body.changes, base=current())
            to_file = config.validate(body.changes, base=config.load(config_path))
            config.save(config_path, to_file)  # written first: a refused write applies nothing
            apply(in_force)
        return get_config()

    # --- conversations ----------------------------------------------------

    @app.get("/api/conversations")
    def conversations():
        with database() as conn:
            rows = [dict(r) for r in store.list_conversations(conn)]
        for row in rows:
            row["running"] = jobs.running(row["id"])
        return {"conversations": rows}

    # --- projects ------------------------------------------------------------

    @app.get("/api/projects")
    def projects():
        with database() as conn:
            return {"projects": [dict(p) for p in store.list_projects(conn)]}

    @app.post("/api/projects", status_code=201)
    def create_project(body: NewProject):
        workdir = check_workdir(body.workdir) if body.workdir.strip() else None
        with database() as conn:
            pid = store.create_project(conn, body.name, description=body.description, workdir=workdir)
            return {"project": dict(store.get_project(conn, pid))}

    @app.get("/api/projects/{pid}")
    def project(pid: int):
        with database() as conn:
            row = dict(store.get_project(conn, pid))
            convs = [dict(c) for c in store.project_conversations(conn, pid)]
        for conv in convs:
            conv["running"] = jobs.running(conv["id"])
        return {"project": row, "conversations": convs}

    @app.patch("/api/projects/{pid}")
    def change_project(pid: int, body: ProjectChange):
        workdir = None
        if body.workdir is not None:
            workdir = check_workdir(body.workdir) if body.workdir.strip() else ""
        with database() as conn:
            store.update_project(conn, pid, name=body.name, description=body.description, workdir=workdir)
            return {"project": dict(store.get_project(conn, pid))}

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: int, with_conversations: bool = False):
        with database() as conn:
            for row in store.project_conversations(conn, pid):
                if with_conversations and jobs.running(row["id"]):
                    raise Busy("une conversation du projet travaille : arrêtez-la d'abord")
            deleted = store.delete_project(conn, pid, with_conversations=with_conversations)
        errors = {}
        folder = purge_folder()
        for cid in deleted:
            errors.update(backup.purge_conversation(folder, cid))
        errors.update(backup.purge_project(folder, pid))
        return {"deleted": pid, "conversations": deleted, "backup_errors": errors}

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: NewConversation):
        if body.mode not in store.MODES:
            raise StoreError(f"mode inconnu : {body.mode!r}")
        with database() as conn:
            workdir = body.workdir
            if not workdir.strip() and body.project_id is not None:
                workdir = store.get_project(conn, body.project_id)["workdir"] or ""
            if not workdir.strip():
                raise StoreError("indiquez le dossier de travail (ou donnez-en un au projet)")
            workdir = check_workdir(workdir)
            if body.mode == "orchestration":
                # The conductor plans and sums up; the subtasks get their agents in the plan.
                agent_id = conductor_id = agent_for(conn, body.conductor_model)
            else:
                agent_id, conductor_id = agent_for(conn, body.model), None
            cid = store.create_conversation(
                conn, body.title, mode=body.mode, agent_id=agent_id, conductor_agent_id=conductor_id,
                workdir=workdir, permission_mode=body.permission_mode,
                confirm_bypass=body.confirm_bypass, assignment_mode=body.assignment_mode,
                project_id=body.project_id,
            )
            return {"conversation": _row(store.get_conversation(conn, cid))}

    @app.get("/api/conversations/{cid}")
    def conversation(cid: int):
        with database() as conn:
            conv = dict(store.get_conversation(conn, cid))
            agent = store.get_agent(conn, conv["agent_id"]) if conv["agent_id"] else None
            conductor = (store.get_agent(conn, conv["conductor_agent_id"])
                         if conv["conductor_agent_id"] else None)
            messages = [dict(m) for m in store.list_messages(conn, cid)]
            plans = store.list_plans(conn, cid)
            runs = autonomous.list_runs(conn, cid)
            delegations = [dict(r) for r in conn.execute(
                "SELECT * FROM delegations WHERE conversation_id = ? ORDER BY id", (cid,))]
        conv["model"] = agent["model"] if agent else None
        conv["conductor_model"] = conductor["model"] if conductor else None
        conv["tools"] = tools.chosen(conv)
        return {"conversation": conv, "tools_offered": tools.offered(current()), "messages": messages, "plans": plans, "autonomous": runs,
                "delegations": delegations,
                "running": jobs.running(cid), "running_kind": jobs.kind(cid),
                "pending": jobs.pending(cid)}

    # --- agents -------------------------------------------------------------

    @app.get("/api/agents")
    def agents():
        with database() as conn:
            return {"agents": [dict(a) for a in store.list_agents(conn, enabled_only=True)]}

    @app.post("/api/agents", status_code=201)
    def add_agent(body: NewAgent):
        with database() as conn:
            return {"agent": dict(store.get_agent(conn, agent_for(conn, body.model)))}

    # --- plans ---------------------------------------------------------------

    def editable_plan(conn, plan_id):
        """A plan can be edited only while nothing runs in its conversation."""
        plan = store.get_plan(conn, plan_id)
        if jobs.running(plan["conversation_id"]):
            raise Busy("le chef d'orchestre travaille sur ce plan : attendez qu'il ait fini")
        return plan

    def plan_of_subtask(conn, subtask_id):
        row = conn.execute("SELECT plan_id FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
        if row is None:
            raise NotFound("sous-tâche introuvable")
        return editable_plan(conn, row["plan_id"])

    @app.patch("/api/subtasks/{sid}")
    def change_subtask(sid: int, body: SubtaskChange):
        with database() as conn:
            plan = plan_of_subtask(conn, sid)
            if body.unassign:
                store.assign_subtask(conn, sid, None)
            elif body.agent_id is not None or body.model is not None:
                current = next(s for s in plan["subtasks"] if s["id"] == sid)
                agent_id = body.agent_id if body.agent_id is not None else current["agent_id"]
                if agent_id is None:
                    raise StoreError("choisissez d'abord un agent pour cette sous-tâche")
                store.assign_subtask(conn, sid, agent_id, model=body.model)
            if body.kind is not None and body.kind not in routing.KINDS:
                raise StoreError(f"genre de travail inconnu : {body.kind!r}")
            if any(v is not None for v in (body.title, body.description, body.files, body.kind)):
                store.update_subtask(conn, sid, title=body.title, description=body.description,
                                     files=body.files, kind=body.kind)
            if body.kind is not None:
                conv = store.get_conversation(conn, plan["conversation_id"])
                if conv["assignment_mode"] == "auto":
                    routing.follow_kind(conn, plan["id"], sid)
            return {"plan": store.get_plan(conn, plan["id"])}

    @app.post("/api/plans/{pid}/subtasks", status_code=201)
    def add_subtask(pid: int, body: NewSubtask):
        with database() as conn:
            editable_plan(conn, pid)
            with db.transaction(conn):
                sid = store.add_subtask(conn, pid, body.description, title=body.title,
                                        files=body.files, agent_id=body.agent_id, model=body.model)
                for dep in body.depends_on:
                    store.add_dependency(conn, sid, dep)
            return {"plan": store.get_plan(conn, pid)}

    @app.delete("/api/subtasks/{sid}")
    def remove_subtask(sid: int):
        with database() as conn:
            plan = plan_of_subtask(conn, sid)
            store.remove_subtask(conn, sid)
            return {"plan": store.get_plan(conn, plan["id"])}

    @app.post("/api/subtasks/{sid}/dependencies")
    def add_dependency(sid: int, body: Dependency):
        with database() as conn:
            plan = plan_of_subtask(conn, sid)
            store.add_dependency(conn, sid, body.depends_on_id)
            return {"plan": store.get_plan(conn, plan["id"])}

    @app.delete("/api/subtasks/{sid}/dependencies/{dep}")
    def remove_dependency(sid: int, dep: int):
        with database() as conn:
            plan = plan_of_subtask(conn, sid)
            store.remove_dependency(conn, sid, dep)
            return {"plan": store.get_plan(conn, plan["id"])}

    @app.post("/api/plans/{pid}/remove-agent")
    def remove_agent(pid: int, body: AgentChoice):
        with database() as conn:
            editable_plan(conn, pid)
            store.remove_agent_from_plan(conn, pid, body.agent_id)
            return {"plan": store.get_plan(conn, pid)}

    @app.post("/api/plans/{pid}/assign-all")
    def assign_all(pid: int, body: AgentChoice):
        """Give every subtask without an agent to the chosen one."""
        with database() as conn:
            plan = editable_plan(conn, pid)
            with db.transaction(conn):
                for st in plan["subtasks"]:
                    if st["agent_id"] is None:
                        store.assign_subtask(conn, st["id"], body.agent_id, model=body.model)
            return {"plan": store.get_plan(conn, pid)}

    @app.post("/api/plans/{pid}/auto-assign")
    def auto_assign(pid: int):
        """Give every subtask the AI the benchmark table recommends for its kind."""
        with database() as conn:
            editable_plan(conn, pid)
            with db.transaction(conn):
                routing.auto_assign(conn, pid)
            return {"plan": store.get_plan(conn, pid)}

    @app.post("/api/plans/{pid}/adapt", status_code=202)
    def adapt(pid: int):
        with database() as conn:
            plan = editable_plan(conn, pid)
            if plan["status"] != "proposed":
                raise StoreError("le plan est validé : il ne peut plus être modifié")
        return {"message_id": jobs.start_adapt(plan["conversation_id"], pid)}

    @app.post("/api/plans/{pid}/launch", status_code=202)
    def launch(pid: int):
        """Nothing runs before this: validate the plan, then run it."""
        with database() as conn:
            plan = editable_plan(conn, pid)
            store.validate_plan(conn, pid)
        try:
            jobs.start_execution(plan["conversation_id"], pid)
        except (StoreError, ProviderError):
            with database() as conn:
                if store.get_plan(conn, pid)["status"] == "validated":
                    store.stop_plan(conn, pid)
            raise
        return {"plan_id": pid}

    @app.post("/api/plans/{pid}/cancel")
    def cancel(pid: int):
        with database() as conn:
            plan = editable_plan(conn, pid)
            if plan["status"] != "proposed":
                raise StoreError("seul un plan proposé peut être annulé ; un plan lancé s'arrête avec « Arrêter »")
            store.stop_plan(conn, pid)
            return {"plan": store.get_plan(conn, pid)}

    @app.patch("/api/conversations/{cid}")
    def change_conversation(cid: int, body: ConversationChange):
        with database() as conn:
            if body.title is not None:
                store.rename_conversation(conn, cid, body.title)
            if body.permission_mode is not None:
                if jobs.running(cid):
                    raise Busy("une requête est en cours : arrêtez-la avant de changer de mode")
                store.set_permission_mode(conn, cid, body.permission_mode,
                                          confirm_bypass=body.confirm_bypass)
            if body.assignment_mode is not None:
                store.update_conversation(conn, cid, assignment_mode=body.assignment_mode)
            if body.tools is not None:
                store.set_tools(conn, cid, tools.check_choice(body.tools, current()))
            if body.model is not None:
                # store.update_conversation refuses it while a request runs.
                agent_id = agent_for(conn, body.model)
                if store.get_conversation(conn, cid)["mode"] == "orchestration":
                    store.update_conversation(conn, cid, agent_id=agent_id, conductor_agent_id=agent_id)
                else:
                    store.update_conversation(conn, cid, agent_id=agent_id)
            if body.leave_project:
                store.move_conversation(conn, cid, None)
            elif body.project_id is not None:
                store.move_conversation(conn, cid, body.project_id)
            if body.shared is not None:
                store.set_shared(conn, cid, body.shared)
            if body.obsidian is False:
                # The note stays in the vault; only the hub stops writing to it.
                store.set_obsidian_note(conn, cid, None)
            elif body.obsidian:
                conv = store.get_conversation(conn, cid)
                note = body.obsidian_note or conv["obsidian_note"] or obsidian.default_note_path(conv["title"])
                if note != conv["obsidian_note"]:
                    conductor = (store.get_agent(conn, conv["conductor_agent_id"])["model"]
                                 if conv["conductor_agent_id"] else None)
                    obsidian.append(current().obsidian_vault, note, conv["title"],
                                    obsidian.creation_section(conv, conductor))
                    store.set_obsidian_note(conn, cid, note)
            return {"conversation": _row(store.get_conversation(conn, cid))}

    @app.post("/api/messages/{mid}/obsidian")
    def note_message(mid: int):
        """Add one reply to the project note, at the user's request."""
        with database() as conn:
            message = store.get_message(conn, mid)
            conv = store.get_conversation(conn, message["conversation_id"])
            if not conv["obsidian_note"]:
                raise StoreError("la note Obsidian n'est pas activée pour cette conversation")
            if message["role"] != "assistant" or message["status"] != "ok":
                raise StoreError("seule une réponse terminée peut être notée")
            before = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND id < ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1", (conv["id"], mid)).fetchone()
            path = obsidian.append(current().obsidian_vault, conv["obsidian_note"], conv["title"],
                                   obsidian.reply_section(before["content"] if before else None,
                                                          message["content"]))
            store.mark_noted(conn, mid)
        return {"note": conv["obsidian_note"], "path": str(path)}

    @app.post("/api/conversations/{cid}/obsidian/rewrite")
    def rewrite_note(cid: int):
        """Rebuild the whole project note; the previous one is backed up outside the vault."""
        if jobs.running(cid):
            raise Busy("une requête est en cours : attendez qu'elle ait fini pour réécrire la note")
        path, backup = jobs.rewrite_note(cid)
        return {"path": str(path), "backup": str(backup) if backup else None}

    @app.delete("/api/conversations/{cid}")
    def delete_conversation(cid: int):
        # The store refuses to delete a conversation whose reply is running.
        with database() as conn:
            store.delete_conversation(conn, cid)
        # Deleted for good means from the backups too.
        errors = backup.purge_conversation(purge_folder(), cid)
        return {"deleted": cid, "backup_errors": errors}

    # --- requests ---------------------------------------------------------

    @app.post("/api/conversations/{cid}/messages", status_code=202)
    def send(cid: int, body: NewMessage):
        with database() as conn:
            conv = store.get_conversation(conn, cid)
            if not conv["agent_id"]:
                raise StoreError("aucun agent n'est choisi pour cette conversation")
            agent_id = conv["agent_id"]
        if conv["mode"] == "orchestration":
            if body.kind == "discuss":
                return {"message_id": jobs.start_discussion(cid, body.prompt)}
            if body.kind != "plan":
                raise StoreError(f"genre de message inconnu : {body.kind!r} (attendu : plan, discuss)")
            # The conductor proposes a plan; nothing else runs before it is validated.
            return {"message_id": jobs.start_planning(cid, body.prompt)}
        return {"message_id": jobs.start(cid, agent_id, body.prompt)}

    # --- autonomous work --------------------------------------------------------

    @app.post("/api/conversations/{cid}/autonomous", status_code=202)
    def start_autonomous(cid: int, body: AutonomousStart):
        """The AI pursues the goal alone, round after round, within the limits."""
        with database() as conn:
            conv = store.get_conversation(conn, cid)
        if conv["mode"] != "single":
            raise StoreError("le travail autonome se lance dans une conversation à une IA")
        if not conv["agent_id"]:
            raise StoreError("aucun agent n'est choisi pour cette conversation")
        run_id = jobs.start_autonomous(cid, conv["agent_id"], body.goal, max_rounds=body.max_rounds,
                                       max_minutes=body.max_minutes, check_command=body.check_command,
                                       confirm_check=body.confirm_check)
        return {"run_id": run_id}

    # --- scheduled tasks ----------------------------------------------------------

    @app.get("/api/schedules")
    def schedules(conversation_id: int | None = None):
        with database() as conn:
            return {"schedules": schedule.list_all(conn, conversation_id)}

    @app.post("/api/schedules", status_code=201)
    def add_schedule(body: NewSchedule):
        with database() as conn:
            sid = schedule.create(conn, body.conversation_id, body.prompt, kind=body.kind, every=body.every,
                                  interval_minutes=body.interval_minutes, at_time=body.at_time,
                                  weekdays=body.weekdays, max_rounds=body.max_rounds, max_minutes=body.max_minutes,
                                  check_command=body.check_command, confirm_check=body.confirm_check,
                                  now=app.state.scheduler.clock())
            return {"schedule": schedule.get(conn, sid)}

    @app.patch("/api/schedules/{sid}")
    def change_schedule(sid: int, body: ScheduleChange):
        with database() as conn:
            return {"schedule": schedule.set_enabled(conn, sid, body.enabled, now=app.state.scheduler.clock())}

    @app.delete("/api/schedules/{sid}")
    def delete_schedule(sid: int):
        with database() as conn:
            schedule.delete(conn, sid)
        return {"deleted": sid}

    @app.post("/api/schedules/{sid}/run", status_code=202)
    def run_schedule_now(sid: int):
        """Run a task now, outside its rule (its next run stays as it was)."""
        with database() as conn:
            task = schedule.get(conn, sid)
        launch_task(task)
        return {"started": sid}

    # --- memory: fresh start and search ---------------------------------------

    def summary_ai(conv):
        s = current()
        return runner.summarizer(commands, conv["workdir"], s.summary_model, env=env)

    @app.post("/api/conversations/{cid}/fresh-start")
    def fresh_start(cid: int, body: FreshStart):
        """Prepare the next request to start a new session fed by the memory."""
        if jobs.running(cid):
            raise Busy("une requête est en cours : attendez qu'elle ait fini")
        if body.decision not in (None, "raise", "auto", "custom"):
            # Checked before the summary is written: that one costs quota.
            raise StoreError(f"choix inconnu : {body.decision!r} (attendu : raise, auto, custom)")
        s = current()
        with database() as conn:
            conv = store.get_conversation(conn, cid)
            budget = conv["rag_budget"] or s.rag_budget
            summarize = summary_ai(conv)
            if body.decision == "custom":
                package = _custom_package(body.package, budget)
                if package["over"] and not body.allow_over:
                    raise StoreError(f"le paquet fait {package['tokens']} tokens pour un plafond de {budget}")
            else:
                summary = memory.refresh_summary(conn, cid, summarize)
                if body.decision == "raise":
                    if not body.budget or body.budget < 1000:
                        raise StoreError("indiquez un plafond d'au moins 1 000 tokens")
                    budget = body.budget
                    if body.remember:
                        conn.execute("UPDATE conversations SET rag_budget = ? WHERE id = ?", (budget, cid))
                package = memory.base_package(conn, cid, summary=summary, budget=budget,
                                              exchanges=memory.last_exchanges(conn, cid, s.rag_exchanges))
                if body.decision == "auto" and package["over"]:
                    package = memory.shrink(package, budget, runner.resummarizer(summarize))
            if package["over"] and body.decision != "custom":
                # Nothing is sent above the cap until the user chooses.
                return {"ready": False, "over": True, "package": package, "budget": budget}
            memory.store_package(conn, cid, package)
            return {"ready": True, "over": package["over"], "package": package, "budget": budget}

    @app.delete("/api/conversations/{cid}/fresh-start")
    def cancel_fresh_start(cid: int):
        with database() as conn:
            store.get_conversation(conn, cid)
            memory.store_package(conn, cid, None)
        return {"ready": False}

    @app.get("/api/search")
    def search(q: str = "", limit: int = 30):
        with database() as conn:
            return {"results": memory.search(conn, q, limit=max(1, min(limit, 100)))}

    @app.post("/api/conversations/{cid}/stop")
    def stop(cid: int):
        return {"stopped": jobs.stop(cid)}

    @app.post("/api/permissions/{permission_id}")
    def answer(permission_id: str, body: PermissionAnswer):
        if not jobs.answer(permission_id, allow=body.allow, message=body.message,
                           answers=body.answers):
            return JSONResponse({"detail": "demande introuvable ou déjà traitée"}, 404)
        return {"answered": permission_id}

    @app.get("/api/conversations/{cid}/events")
    def events(cid: int):
        with database() as conn:
            store.get_conversation(conn, cid)
        q = jobs.hub.subscribe(cid)
        snapshot = jobs.snapshot(cid)

        def stream():
            try:
                yield _sse({"type": "snapshot", **snapshot})
                while True:
                    try:
                        event = q.get(timeout=sse_ping_s)
                    except queue.Empty:
                        yield ": ping\n\n"
                        continue
                    yield _sse(event)
            finally:
                jobs.hub.unsubscribe(cid, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _sse(event):
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
