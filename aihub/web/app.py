"""HTTP API and single-page app of AI Hub.

With a password (`auth_policy`), every API call needs a session: see
aihub/auth.py. Without one (`local_only`), the server answers only requests
made on this computer, never one relayed by a proxy such as Tailscale.
"""

import asyncio
import contextvars
import json
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import segno
from fastapi import Depends, FastAPI, Request as HttpRequest
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import VERSION_LABEL, __version__
from .. import (auth, autonomous, backup, browse, config, db, eveil, memory, notify, obsidian, providers, routing, runner,
                schedule, semantic, store, tools)
from .. import access, accounts, attachments, compare, filesystem, importer, isolation, library, rag, resumption, sandbox_results, updater, usage, vaults
from .. import restore as restoring
from .. import places, project_export, relaunch, trials
from ..providers import claude_code, codex, ollama
from ..providers.claude_code import ProviderError
from ..store import BypassConfirmationRequired, Busy, NotFound, StoreError
from .jobs import JobManager, side_folder

STATIC = Path(__file__).with_name("static")
_NOBODY = object()  # with accounts, a request that came without a session
_ASKER = contextvars.ContextVar("aihub_asker", default=_NOBODY)  # the person of the request being answered
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
    template_id: str | None = None
    variables: dict[str, str] = {}
    isolation: dict | None = None


class ConversationPreferences(BaseModel):
    pinned: bool | None = None
    archived: bool | None = None


class CreatePath(BaseModel):
    path: str
    kind: str = 'directory'
    parents: bool = True
    conversation_id: int | None = None
    password: str = ''


class PcFile(BaseModel):
    """A file of the server PC, chosen in the app's explorer (aihub/places.py judges it)."""
    path: str = Field(min_length=1, max_length=1000)


class SaveToPc(BaseModel):
    """A conversation saved into a folder of the PC: a new file, never over another one."""
    folder: str = Field(min_length=1, max_length=1000)
    format: str = "md"
    password: str = ""


class MemoryDocument(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=rag.MAX_CHARS)


class NewTemplate(BaseModel):
    conversation_id: int
    title: str
    prompt: str = ""


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
    obsidian_existing: str | None = None  # the note's file is there already: "continue" it, or a "new" one
    obsidian_vault: str | None = None  # its own vault, chosen when linking ("": its project's, else the settings')
    project_id: int | None = None     # move into this project
    leave_project: bool = False       # take it out of its project
    shared: bool | None = None        # open or close it to the project's other conversations
    model: str | None = None          # change of AI: the next request starts from the memory
    tools: list[str] | None = None    # the tools chosen for the conversation (aihub/tools.py)
    instructions: str | None = None   # what the AI always gets ("" clears them)


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
    instructions: str = ""            # what the AI always gets in its conversations
    isolation: dict | None = None
    obsidian_vault: str | None = None  # the vault of its notes (the owner's choice; empty: the settings')


class ProjectChange(BaseModel):
    name: str | None = None
    description: str | None = None
    workdir: str | None = None        # "" clears it
    instructions: str | None = None   # what the AI always gets in its conversations ("" clears them)
    isolation: dict | None = None
    obsidian_vault: str | None = None  # "" : back to the settings' vault


class RestoreBackup(BaseModel):
    name: str                # a file of the backup folder, by its name only
    password: str = ""       # the owner's, when the server has accounts


class TrialSaid(BaseModel):
    status: str              # ok, probleme or attente
    note: str = ""


class LinkedNote(BaseModel):
    vault: str               # a vault of the PC (the owner's)
    path: str                # the note, relative to the vault


class AutoResume(BaseModel):
    at: str | None = None    # ISO time; none: when the quota comes back (and a small margin)
    cancel: bool = False


class SwitchPlan(BaseModel):
    model: str               # the AI that does the rest of the plan
    conductor: bool = True   # the conductor too: it writes the synthesis


class GiveTo(BaseModel):
    model: str               # the AI that takes the tasks a quota stopped


class NewMessage(BaseModel):
    prompt: str = ''
    # Orchestration only: "plan" asks the conductor for a plan, "discuss" talks with it.
    kind: str = "plan"
    attachments: list[int] = []   # files sent first (POST …/attachments), taken by this request
    compare_with: str | None = None  # another AI answering beside, read only (aihub/compare.py)


class PermissionAnswer(BaseModel):
    allow: bool
    message: str = ""
    answers: dict | None = None


class Login(BaseModel):
    password: str = ""                # empty: an account without a password
    name: str | None = None           # empty: the owner (a server of one person types no name)
    server_password: str = ""         # when the server has one


class Setup(BaseModel):
    name: str
    password: str = ""                # optional
    server_password: str = ""         # optional


class ServerPassword(BaseModel):
    password: str = ""                # empty: the server goes without


class Grant(BaseModel):
    """One access given with an account: a conversation or a project, read or write."""
    conversation_id: int | None = None
    project_id: int | None = None
    role: str = "read"


class Accept(BaseModel):
    invitation: str
    name: str
    password: str = ""                # optional


class NewInvitation(BaseModel):
    note: str | None = None
    role: str = "invite"              # what the person will be once they come
    grants: list[Grant] = []          # and what they will see


class UserChange(BaseModel):
    disabled: bool | None = None
    name: str | None = None
    role: str | None = None


class DeviceLink(BaseModel):
    label: str | None = None          # « Téléphone de Lou »


class DeviceAccept(BaseModel):
    link: str


class Regenerate(BaseModel):
    model: str | None = None      # another AI for it (the owner of the conversation only)


class Branch(BaseModel):
    prompt: str | None = None     # the corrected request (empty: the same, sent again)


class NewFavorite(BaseModel):
    title: str | None = None
    text: str


class SessionChange(BaseModel):
    label: str | None = None


class NewUser(BaseModel):
    name: str
    password: str = ""                # optional: the name is then enough
    role: str = "invite"
    grants: list[Grant] = []


class Share(BaseModel):
    user_id: int
    role: str = "read"
    conversation_id: int | None = None
    project_id: int | None = None


class PasswordChange(BaseModel):
    current: str
    new: str


class NewImport(BaseModel):
    reading: str                  # what /api/import/file or /api/import/local gave
    keys: list[str]               # the conversations chosen in it
    merge: bool = False           # one new conversation for all, or one each
    title: str | None = None      # merged: its title
    workdir: str = ""             # empty: a local session's own folder, or the project's
    project_id: int | None = None
    model: str = "default"
    permission_mode: str = "manual"


class AccountCode(BaseModel):
    code: str


class SandboxTarget(BaseModel):
    provider: str
    read_only: bool = False


class SandboxCode(SandboxTarget):
    code: str


class SandboxApply(SandboxTarget):
    files: dict[str, str | None]
    confirm_delete: bool = False


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
PUBLIC_PATHS = {"/", "/manifest.webmanifest", "/sw.js", "/api/auth/session", "/api/auth/login", "/api/setup",
                "/api/devices/accept",
                "/api/auth/logout", "/api/invitations/accept"}
PUBLIC_PREFIXES = ("/static/",)
# A custom header no other site can make a browser send (no CORS here):
# with SameSite=Strict, it keeps another page from acting through the session.
CSRF_HEADER = "x-aihub"
# The tools a member may give a conversation of theirs: none touches the server's files or vault.
MEMBER_TOOLS = (tools.WEB, tools.DELEGATE)
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = (b"x-forwarded-for", b"forwarded", b"x-real-ip", b"tailscale-user-login")


_TAILSCALE_CLI = (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe",
                  Path("/usr/bin/tailscale"), Path("/usr/local/bin/tailscale"))
_tailscale_cache = {}


def tailscale_name(port):
    """The Tailscale address (« pc.tailnet.ts.net ») that `tailscale serve` relays to this port, or None;
    read at most every 10 minutes. A link made on this computer (a QR code for the phone) must not
    say 127.0.0.1, nor lead to another server of this computer (a trial's)."""
    at, relays = _tailscale_cache.get("relays", (0, None))
    if relays is None or time.monotonic() - at >= 600:
        relays = {}
        cli = next((str(p) for p in _TAILSCALE_CLI if p.is_file()), None)
        if cli:
            try:
                out = subprocess.run([cli, "serve", "status", "--json"], capture_output=True, text=True,
                                     timeout=10, encoding="utf-8", errors="replace",
                                     creationflags=claude_code.NO_WINDOW)
                relays = serve_relays(json.loads(out.stdout or "{}"))
            except (OSError, ValueError, subprocess.SubprocessError):
                relays = {}
        _tailscale_cache["relays"] = (time.monotonic(), relays)
    return relays.get(port)


def serve_relays(status):
    """{local port: the https address that relays to it}, from `tailscale serve status --json`."""
    found = {}
    for host, web in (status.get("Web") or {}).items():
        name, _, public_port = host.rpartition(":")
        for handler in (web.get("Handlers") or {}).values():
            target = handler.get("Proxy") or ""
            local = target.rsplit(":", 1)[-1].split("/")[0]
            if local.isdigit() and name:
                found[int(local)] = name if public_port == "443" else f"{name}:{public_port}"
    return found


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
            # A remote site's DNS must not turn the loopback-only app into
            # its own same-origin API. ASGI test transports use a fake server.
            if (scope.get('server') or ('?', 0))[0] in _LOOPBACK:
                from urllib.parse import urlsplit
                try:
                    host = urlsplit('//' + _headers(scope).get('host', '')).hostname
                except ValueError:
                    host = None
                if host not in _LOOPBACK:
                    return await _deny(send, 403, 'adresse locale requise', 'local_host')
            if (scope['path'].startswith('/api/') and scope['method'] not in ('GET', 'HEAD', 'OPTIONS')
                    and _headers(scope).get(CSRF_HEADER) != '1'):
                return await _deny(send, 403, 'requête refusée : en-tête X-AIHub manquant', 'csrf')
        if self.check_session is None:
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path == '/api/health' and (scope.get('client') or ('?', 0))[0] in _LOOPBACK and not any(
                key.lower() in _PROXY_HEADERS for key, _ in scope.get('headers', [])):
            return await self.app(scope, receive, send)
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        import anyio
        user = await anyio.to_thread.run_sync(self.check_session, _cookie(scope, auth.COOKIE))
        if not user:
            return await _deny(send, 401, "connexion requise", "auth_required")
        scope.setdefault("state", {})["user"] = user  # read by the endpoints: request.state.user
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
               push_post=None, run_scheduler=True, schedule_clock=None, ollama_url=None, local_home=None,
               update_hooks=None, journal_path=None):
    """Build the application. `command` overrides the Claude Code CLI (tests).

    `codex_command` is the Codex CLI. Left to None, Codex is searched for
    only when `command` is too: a test that gives its own Claude Code does
    not start the real Codex of the computer by surprise.

    `journal_path`: the server's log (serve --journal), whose end the dashboard shows.

    `update_hooks`: tests replace what the update touches (get, spawn, stop,
    restart, root, why_not); the real ones go to GitHub and restart the server.

    `local_home`: where the Claude Code and Codex sessions to import are
    (the person's home folder by default; tests give their own).

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
    people_accounts = accounts.Accounts(jobs.accounts_root, commands=commands, env=env,
                                        workdir=Path(db_path).resolve().parent)
    space_accounts = {}
    space_accounts_lock = threading.RLock()
    space_stops = set()
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
        with database() as conn:
            attachments.purge_unsent(conn)  # files of requests never sent, a day old
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

    async def who_asks(request: HttpRequest):
        # Async on purpose: it runs in the request's own context, which the
        # endpoint's worker thread then copies, so `asker()` sees it there.
        if authority is None:
            _ASKER.set(None)  # no accounts: the owner, on this computer only
        else:
            # With accounts, no person is never « the owner »: public pages only, refused anywhere else.
            _ASKER.set(getattr(request.state, "user", None) or _NOBODY)

    app = FastAPI(title="AI Hub", docs_url=None, redoc_url=None, lifespan=lifespan, dependencies=[Depends(who_asks)])
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

    def resume_planned(plan):
        """A plan's resume time has come: resumed with the accounts and accesses of who planned it."""
        with database() as conn:
            user = auth.get_user(conn, plan["auto_resume_user"]) if plan["auto_resume_user"] else None
            if authority is not None and (user is None or user["disabled"]):
                raise StoreError("le compte qui a programmé la reprise n'existe plus ou est suspendu")
            current_plan = store.get_plan(conn, plan["id"])
            conv = access.require(conn, user, current_plan["conversation_id"], "write")
            if not store.can_resume_plan(conn, current_plan):
                raise StoreError("ce plan ne peut plus être repris")
            check_workdir(conv["workdir"])
            isolation.for_run(conn, conv, account_id=(user or {}).get("id"), settings=current())
        jobs.resume_execution(plan["conversation_id"], plan["id"], user=user)

    def resume_refused(plan, message):
        jobs._alert("error", plan["conversation_id"], f"reprise automatique impossible : {message}")
        jobs.hub.publish(plan["conversation_id"], {"type": "plan_updated", "plan_id": plan["id"]})

    app.state.scheduler = schedule.Scheduler(db_path, launch_task, clock=schedule_clock,
                                             resume=resume_planned, on_resume_error=resume_refused)
    app.state.indexer = semantic.Indexer(db_path)  # the vectors of the search by meaning, in the background
    app.mount("/static", FreshStatic(directory=STATIC), name="static")
    # The page this server started with (1.2): an open page that differs is told to reload.
    import hashlib
    page_build = hashlib.sha256(b"".join((STATIC / name).read_bytes() for name in ("index.html", "app.js", "style.css"))
                                ).hexdigest()[:12]

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

    def me(request=None):
        """The person asking (set by the Guard), or None without accounts: the owner, all rights."""
        user = _ASKER.get()
        if user is _NOBODY:
            raise access.Forbidden("connexion requise")
        return user

    app.add_middleware(Guard, check_session=check_session if authority else None, local_only=local_only)

    @app.get('/api/health')
    def health():
        with database() as conn:
            conn.execute('SELECT 1').fetchone()
        return {'ready': True, 'version': __version__, 'probe': os.environ.get('AIHUB_UPDATE_PROBE')}

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
            return {"required": False, "logged_in": True, "user": None}
        user = check_session(request.cookies.get(auth.COOKIE))
        with database() as conn:
            created = auth.password_set(conn)
            locked = auth.server_password_set(conn)
        return {"required": True, "logged_in": bool(user),
                "user": {"name": user["name"], "is_owner": user["is_owner"], "role": user["role"]} if user else None,
                # A new server: it is created from this computer (the form), not from elsewhere.
                "setup": not created, "setup_here": not created and from_this_computer(request),
                "server_password": locked}

    @app.post("/api/auth/login")
    def login(body: Login, request: HttpRequest):
        if authority is None:
            return {"logged_in": True}
        with database() as conn:
            token = authority.login(conn, body.password, client_of(request.scope),
                                    request.headers.get("user-agent"), name=body.name,
                                    server_password=body.server_password)
        return with_session(JSONResponse({"logged_in": True}), token, request)

    def from_this_computer(request):
        """Straight from this computer (not relayed by Tailscale or anything else)."""
        peer = (request.scope.get("client") or ("?", 0))[0]
        relayed = any(k.lower() in _PROXY_HEADERS for k, _ in request.scope.get("headers", []))
        return peer in _LOOPBACK and not relayed

    @app.post("/api/setup")
    def setup(body: Setup, request: HttpRequest):
        """Create the server: its owner, and its own password if wanted. Once, from this computer only:
        the first one to reach a new server must not become its owner from elsewhere."""
        if authority is None:
            raise StoreError("ce serveur tourne sans comptes (--sans-mot-de-passe)")
        if not from_this_computer(request):
            raise access.Forbidden("le serveur se crée depuis l'ordinateur où il tourne")
        if request.headers.get(CSRF_HEADER) != "1":  # a public path: the guard does not check it here
            raise access.Forbidden("requête refusée : en-tête X-AIHub manquant")
        with database() as conn:
            user = auth.setup(conn, body.name, body.password, body.server_password)
            token = authority._open(conn, user["id"], client_of(request.scope), request.headers.get("user-agent"))
        return with_session(JSONResponse({"logged_in": True}), token, request)

    @app.post("/api/server-password")
    def server_password(body: ServerPassword):
        """The server's own password: set, changed, or (empty) removed. The owner's."""
        access.require_owner(me())
        with database() as conn:
            auth.set_server_password(conn, body.password)
            return {"server_password": auth.server_password_set(conn)}

    def with_session(response, token, request):
        secure = (request.url.scheme == "https"
                  or request.headers.get("x-forwarded-proto", "").lower() == "https")
        response.set_cookie(auth.COOKIE, token, max_age=int(authority.policy.max_days * 86400),
                            httponly=True, samesite="strict", secure=secure, path="/")
        return response

    # --- people (version 0.2: several people on one server) -------------------

    @app.post("/api/invitations/accept")
    def accept_invitation(body: Accept, request: HttpRequest):
        """A person comes with their link, chooses their name and password, and is logged in."""
        if authority is None:
            raise StoreError("ce serveur n'a pas de comptes (pas de mot de passe)")
        with database() as conn:
            token = authority.accept_invitation(conn, body.invitation, body.name, body.password,
                                                client_of(request.scope), request.headers.get("user-agent"))
        return with_session(JSONResponse({"logged_in": True}), token, request)

    @app.get("/api/users")
    def users():
        access.require_admin(me())
        if authority is None:
            return {"users": [], "invitations": [], "accounts": False}
        with database() as conn:
            people = auth.list_users(conn)
            for person in people:  # what each one sees (the owner and the admins: everything), and their devices
                person["grants"] = access.grants_of(conn, person["id"])
                person["devices"] = authority.devices_of(conn, person["id"])
            return {"users": people, "invitations": auth.list_invitations(conn), "accounts": True,
                    "server_password": auth.server_password_set(conn), "roles": auth.ROLE_NAMES}

    @app.get("/api/people")
    def people():
        """Who one may share with (a member shares what is theirs): names and roles, nothing more."""
        user = me()
        access.require_creator(user)
        if authority is None:
            return {"people": []}
        with database() as conn:
            return {"people": [{"id": u["id"], "name": u["name"], "role": u["role"], "disabled": u["disabled"]}
                               for u in auth.list_users(conn)
                               if not u["is_owner"] and u["role"] != "admin" and u["id"] != (user or {}).get("id")]}

    def grants(body_grants):
        return [g.model_dump() for g in body_grants]

    @app.post("/api/invitations", status_code=201)
    def new_invitation(body: NewInvitation, request: HttpRequest):
        access.require_admin(me())
        if authority is None:
            raise StoreError("ce serveur tourne sans comptes (--sans-mot-de-passe)")
        with database() as conn:
            token = auth.invite(conn, body.note, now=authority.now(), role=body.role, shares=grants(body.grants))
        return {"link": f"{public_base(request)}/?invitation={token}", "days": auth.INVITE_DAYS}

    def public_base(request):
        """The address the others reach this server at. The one this page uses (Tailscale's, on the
        phone); on this computer itself (127.0.0.1), Tailscale's name when there is one."""
        if from_this_computer(request):
            name = tailscale_name(request.url.port)
            if name:
                return f"https://{name}"
        base = str(request.base_url).rstrip("/")
        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            base = "https://" + base.split("://", 1)[-1]
        return base

    @app.post("/api/users/{uid}/device-link", status_code=201)
    def new_device_link(uid: int, body: DeviceLink, request: HttpRequest):
        """A QR code that connects a device to an account: for oneself, or (owner, admin) for anyone."""
        user = me()
        if authority is None:
            raise StoreError("ce serveur tourne sans comptes (--sans-mot-de-passe)")
        if not (access.sees_all(user) or (user and user["id"] == uid)):
            raise access.Forbidden("on connecte un appareil à son propre compte (ou, admin, à celui de chacun)")
        with database() as conn:
            token = auth.device_link(conn, uid, body.label, now=authority.now())
        link = f"{public_base(request)}/?appareil={token}"
        qr = segno.make(link, error="m")
        return {"link": link, "minutes": auth.DEVICE_LINK_MINUTES,
                "qr_svg": qr.svg_inline(scale=5, border=2, dark="#111111", light="#ffffff")}

    @app.post("/api/me/device-link", status_code=201)
    def my_device_link(body: DeviceLink, request: HttpRequest):
        """Connect another device of one's own (the phone, from the PC): its QR code."""
        user = me()
        if user is None:
            raise StoreError("ce serveur tourne sans comptes (--sans-mot-de-passe)")
        return new_device_link(user["id"], body, request)

    @app.post("/api/devices/accept")
    def accept_device(body: DeviceAccept, request: HttpRequest):
        """The device that scanned the QR code: connected to that account, under the name given."""
        if authority is None:
            raise StoreError("ce serveur tourne sans comptes (--sans-mot-de-passe)")
        if request.headers.get(CSRF_HEADER) != "1":  # a public path: the guard does not check it here
            raise access.Forbidden("requête refusée : en-tête X-AIHub manquant")
        with database() as conn:
            token = authority.accept_device_link(conn, body.link, client_of(request.scope),
                                                 request.headers.get("user-agent"))
        return with_session(JSONResponse({"logged_in": True}), token, request)

    @app.delete("/api/invitations/{iid}")
    def revoke_invitation(iid: int):
        access.require_admin(me())
        with database() as conn:
            if not auth.revoke_invitation(conn, iid):
                raise NotFound("invitation introuvable ou déjà utilisée")
        return {"revoked": iid}

    @app.patch("/api/users/{uid}")
    def change_user(uid: int, body: UserChange):
        access.require_admin(me())
        with database() as conn:
            if body.name is not None:
                auth.rename_user(conn, uid, body.name)
            if body.role is not None:
                auth.set_role(conn, uid, body.role)
            if body.disabled is not None:
                auth.set_disabled(conn, uid, body.disabled)
            person = auth.get_user(conn, uid)
        if body.disabled or body.role is not None:
            revoke_user_work(uid)
        return {"user": person}

    def revoke_user_work(uid):
        jobs.stop_user(uid)
        people_accounts.cancel_user(uid)
        with space_accounts_lock:
            names = [name for name, manager in space_accounts.items()
                     if getattr(manager.execution, 'user_id', None) == uid]
            managers = [space_accounts.pop(name) for name in names]
        for manager in managers:
            manager.cancel_user(uid)
            manager.execution.stop()

    @app.post("/api/users", status_code=201)
    def create_user(body: NewUser):
        """An account made without a link: the name and password (or none) are passed on to the person."""
        access.require_admin(me())
        if authority is None:
            raise StoreError("ce serveur tourne sans comptes (--sans-mot-de-passe)")
        with database() as conn:
            return {"user": auth.create_user(conn, body.name, body.password, body.role, grants(body.grants))}

    @app.delete("/api/users/{uid}")
    def remove_user(uid: int):
        access.require_admin(me())
        with database() as conn:
            auth.delete_user(conn, uid)
        revoke_user_work(uid)
        people_accounts.forget(uid)  # their AI accounts' folders go with them
        return {"deleted": uid}

    @app.post("/api/shares")
    def add_share(body: Share):
        with database() as conn:
            access.require_sharer(conn, me(), conversation_id=body.conversation_id, project_id=body.project_id)
            access.share(conn, user_id=body.user_id, role=body.role, conversation_id=body.conversation_id,
                         project_id=body.project_id)
            return {"shared_with": access.shares_of(conn, conversation_id=body.conversation_id,
                                                    project_id=body.project_id)}

    @app.post("/api/shares/remove")
    def remove_share(body: Share):
        with database() as conn:
            access.require_sharer(conn, me(), conversation_id=body.conversation_id, project_id=body.project_id)
            access.unshare(conn, user_id=body.user_id, conversation_id=body.conversation_id,
                           project_id=body.project_id)
            return {"shared_with": access.shares_of(conn, conversation_id=body.conversation_id,
                                                    project_id=body.project_id)}

    @app.post("/api/me/password")
    def change_my_password(body: PasswordChange, request: HttpRequest):
        """A person changes their own password (their other devices are logged out)."""
        user = me()
        if authority is None or user is None:
            raise StoreError("ce serveur n'a pas de comptes")
        with database() as conn:
            authority.verify(conn, body.current, client_of(request.scope), name=user["name"])
            # Empty: no password any more (the name is then enough, behind the server's password).
            auth.set_user_password(conn, user["id"], body.new)
            token = authority._open(conn, user["id"], client_of(request.scope), request.headers.get("user-agent"))
        return with_session(JSONResponse({"changed": True}), token, request)

    # --- each person's own AI accounts (« Mes comptes IA ») ----------------------

    def my_accounts():
        """The person whose accounts are signed in: the owner signs in the computer's."""
        return me()

    def account_call(action):
        try:
            return action()
        except accounts.AccountError as exc:
            raise StoreError(str(exc)) from None

    @app.get("/api/accounts")
    def my_account_status():
        """Signed in or not, for each AI: a person's own accounts, or the computer's for the owner.
        `ready`: at least one is (the first login asks for one)."""
        user = me()
        own = accounts.own_accounts(user)
        if not own:
            status_cache.clear()  # the computer's state, as it is now
        found = people_accounts.status(user)
        return {"own": own, "providers": found, "ready": any(p.get("signed_in") for p in found.values())}

    @app.post("/api/accounts/{provider}/login")
    def account_login(provider: str):
        user = my_accounts()
        return account_call(lambda: people_accounts.start_login(user, provider))

    @app.post("/api/accounts/{provider}/code")
    def account_code(provider: str, body: AccountCode):
        user = my_accounts()
        return account_call(lambda: people_accounts.send_code(user, provider, body.code))

    @app.post("/api/accounts/{provider}/cancel")
    def account_cancel(provider: str):
        user = my_accounts()
        people_accounts.cancel_login(user, provider)
        return {"cancelled": provider}

    @app.post("/api/accounts/{provider}/logout")
    def account_logout(provider: str):
        user = my_accounts()
        account_call(lambda: people_accounts.logout(user, provider))
        status_cache.clear()
        return {"logged_out": provider}

    # Connected devices. Behind the session check (not in PUBLIC_PATHS).
    @app.get("/api/sessions")
    def sessions(request: HttpRequest):
        if authority is None:
            return {"sessions": [], "required": False}
        with database() as conn:
            return {"sessions": authority.sessions(conn, request.cookies.get(auth.COOKIE)), "required": True}

    # --- the server's dashboard (the owner's): it only reads ---------------------

    started_at = db.utc_now()
    started_clock = time.monotonic()

    def folder_size(path):
        total, count, newest = 0, 0, None
        root = Path(path) if path else None
        if root is None or not root.is_dir():
            return {"size": 0, "count": 0, "newest": None}
        for item in root.rglob("*"):
            if item.is_file():
                stat = item.stat()
                total, count = total + stat.st_size, count + 1
                newest = max(newest or stat.st_mtime, stat.st_mtime)
        when = datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds") if newest else None
        return {"size": total, "count": count, "newest": when}

    @app.get("/api/server")
    def server_dashboard():
        """Version, uptime, disk, base, backups, AIs, how many people and conversations, the log's end."""
        access.require_owner(me())
        data_dir = Path(db_path).resolve().parent
        disk = shutil.disk_usage(data_dir)
        base = sum(p.stat().st_size for p in data_dir.glob(Path(db_path).name + "*") if p.is_file())
        with database() as conn:
            counts = {name: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for name, table in (("people", "users"), ("devices", "sessions"),
                                          ("conversations", "conversations"), ("messages", "messages"))}
        log = []
        if journal_path and Path(journal_path).is_file():
            with open(journal_path, encoding="utf-8", errors="replace") as fh:
                log = [line.rstrip("\n") for line in fh.readlines()[-50:]]
        state = provider_status()
        return {"version": VERSION_LABEL, "started_at": started_at,
                "uptime_s": int(time.monotonic() - started_clock),
                "disk": {"free": disk.free, "total": disk.total, "folder": str(data_dir)},
                "base": base, "backups": {"folder": backup_folder(), **folder_size(backup_folder())},
                "attachments": folder_size(pieces), "counts": counts,
                "ais": {"claude": bool(state.get("logged_in")), "codex": bool((state.get("codex") or {}).get("logged_in")),
                        "local": bool((state.get("local") or {}).get("running"))},
                "log": log, "log_file": str(journal_path) if journal_path else None}

    # --- restoring a backup (aihub/restore.py), the owner's ------------------------

    app.state.restore_hooks = {}  # tests replace the helper's start and the server's stop

    def backup_files():
        folder = backup_folder() or str(Path(db_path).resolve().parent / "sauvegardes")
        return folder, backup.backups(folder)

    @app.get("/api/server/backups")
    def server_backups():
        access.require_owner(me())
        folder, files = backup_files()
        return {"folder": folder, "last_restore": restoring.last_report(db_path),
                "backups": [{"name": p.name, "size": p.stat().st_size,
                             "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                             "attachments": backup.assets_path(p).is_file()} for p in reversed(files)]}

    @app.post("/api/server/restore", status_code=202)
    def restore_backup(body: RestoreBackup, request: HttpRequest):
        """Check the backup, start the helper that puts it in place, then stop: it restarts the server."""
        user = me(request)
        access.require_owner(user)
        if authority is not None:
            with database() as conn:
                verified = authority.verify(conn, body.password, client_of(request.scope), name=user["name"])
                if verified["id"] != user["id"]:
                    raise access.Forbidden("les droits du compte ont changé : restauration refusée")
                access.require_owner(verified)
        if jobs.busy():
            raise Busy("une IA travaille : attendez qu'elle ait fini avant de restaurer")
        folder, files = backup_files()
        chosen = next((p for p in files if p.name == body.name), None)  # a name of the list, never a path
        if chosen is None:
            raise NotFound("sauvegarde introuvable")
        try:
            check = sqlite3.connect(f"{chosen.resolve().as_uri()}?mode=ro", uri=True)
            try:
                if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("contrôle d'intégrité en échec")
                if check.execute("PRAGMA user_version").fetchone()[0] > db.SCHEMA_VERSION:
                    raise sqlite3.DatabaseError("faite par une version plus récente d'AI Hub")
                conversations = check.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            finally:
                check.close()
        except sqlite3.Error as exc:
            raise StoreError(f"cette sauvegarde est illisible : {exc}") from None
        hooks = app.state.restore_hooks
        restart = [sys.executable, "-m", "aihub"] + sys.argv[1:] if "restart" not in hooks else hooks["restart"]
        if updater.restart_details(restart, updater.install_root()) is None:
            # Not started by « python -m aihub serve » (a trial server, a test): it could not come back by itself.
            raise StoreError("ce serveur ne sait pas se relancer seul : arrêtez-le, puis « python -m aihub restaurer »")
        command = [sys.executable, "-m", "aihub.restore", "--backup", str(chosen), "--db", str(Path(db_path).resolve()),
                   "--root", str(updater.install_root()), "--pid", str(os.getpid()), "--restart", json.dumps(restart)]
        spawn = hooks.get("spawn") or (lambda cmd: subprocess.Popen(
            cmd, cwd=str(updater.install_root()), **updater.detached_options(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True))
        spawn(command)
        (hooks.get("stop") or stop_server)()
        return {"restoring": chosen.name, "conversations": conversations}

    # --- one-click update (aihub/updater.py), the owner's -------------------------

    app.state.update_hooks = update_hooks or {}  # tests replace them there

    def stop_server():
        """After the answer is sent: the server stops like on Ctrl+C (the helper waits for it)."""
        import signal
        threading.Timer(1.0, signal.raise_signal, args=(signal.SIGINT,)).start()
        threading.Timer(20.0, os._exit, args=(0,)).start()  # had it not stopped by then

    # What this server runs (1.2): the code on disk is compared to it, a change there asks for a restart.
    started = relaunch.started_state(updater.install_root())

    def code_root():
        return Path(app.state.update_hooks.get("root") or updater.install_root())

    def code_state():
        hooks = app.state.update_hooks  # tests give the start, the folder and git
        return relaunch.code_state(hooks.get("started", started), code_root(),
                                   **({"git": hooks["git"]} if "git" in hooks else {}))

    @app.get("/api/update")
    def update_state(refresh: bool = False):
        access.require_owner(me())
        if refresh:
            updater._cache.clear()
        hooks = app.state.update_hooks
        found = updater.check(**({"get": hooks["get"]} if "get" in hooks else {}))
        reason = hooks["why_not"]() if "why_not" in hooks else updater.why_not_here()
        return {**found, "can_apply": reason is None, "reason": reason, "code": code_state(),
                "last_restart": relaunch.last_report(db_path)}

    @app.post("/api/update/restart", status_code=202)
    def restart_on_disk_code():
        """The code on disk changed since the start (a development copy): the server restarts on it.

        Tried first in a process of its own (it imports, it migrates a copy of the base): a code
        that does not start is refused while this server still runs."""
        access.require_owner(me())
        if jobs.busy():
            raise Busy("une IA travaille : attendez qu'elle ait fini avant de redémarrer")
        state = code_state()
        if not state["restart_needed"]:
            raise StoreError("le serveur tourne déjà sur le code du disque : rien à redémarrer")
        if state["dirty"]:
            raise StoreError("des fichiers du code sont en train de changer (pas encore enregistrés dans git) : "
                             "attendez la fin du travail en cours, puis redémarrez")
        hooks, root = app.state.update_hooks, code_root()
        restart = [sys.executable, "-m", "aihub"] + sys.argv[1:] if "restart" not in hooks else hooks["restart"]
        if updater.restart_details(restart, root) is None:
            raise StoreError("ce serveur ne sait pas se relancer seul : arrêtez-le, puis relancez-le")
        trial = (hooks.get("preflight") or relaunch.preflight)(db_path, root)
        if not trial.get("ok"):
            raise StoreError(f"le nouveau code ne démarre pas, le serveur continue sur l'ancien : {trial.get('error')}")
        with database() as conn:
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
        version = trial.get("version") or state["disk"]
        # A schema change: the base from before is kept beside it (the helper puts it back on a failure).
        keep = relaunch.keep_copy(db_path, version) if trial.get("schema", schema) != schema else None
        command = [sys.executable, "-m", "aihub.relaunch", "--db", str(Path(db_path).resolve()), "--version", version,
                   "--root", str(root), "--pid", str(os.getpid()), "--restart", json.dumps(restart)]
        if keep:
            command += ["--keep", str(keep)]
        spawn = hooks.get("spawn") or (lambda cmd: subprocess.Popen(
            cmd, cwd=str(root), **updater.detached_options(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True))
        spawn(command)
        (hooks.get("stop") or stop_server)()
        return {"restarting": version, "backup": keep.name if keep else None}

    @app.post("/api/update/apply", status_code=202)
    def apply_update():
        """Download the newest version, check it, then let the helper put it in place and restart."""
        access.require_owner(me())
        if jobs.busy():
            raise Busy("une IA travaille : attendez qu'elle ait fini avant de mettre à jour")
        hooks = app.state.update_hooks
        found = updater.check(**({"get": hooks["get"]} if "get" in hooks else {}))
        if not found["latest"]:
            raise StoreError(found["error"] or "AI Hub est déjà à jour")
        restart = [sys.executable, "-m", "aihub"] + sys.argv[1:] if "restart" not in hooks else hooks["restart"]
        try:
            updater.start(found["latest"], server_pid=os.getpid(), restart=restart,
                          **{k: hooks[k] for k in ("root", "get", "spawn") if k in hooks})
        except updater.UpdateError as exc:
            raise StoreError(str(exc)) from None
        (hooks.get("stop") or stop_server)()
        return {"updating": found["latest"]["version"]}

    @app.get("/api/usage")
    def consumption(days: int = 7):
        """Who used how much (requests, AI runs, tokens, by AI): the owner's and the admins' to see."""
        access.require_admin(me())
        if days not in usage.PERIODS:
            raise StoreError(f"période : {', '.join(map(str, usage.PERIODS))} jours")
        with database() as conn:
            return {"days": days, "people": usage.usage(conn, days)}

    # --- favorite requests (each person's own) --------------------------------

    def my_id():
        user = me()
        return user["id"] if user else None

    @app.get("/api/favorites")
    def favorites():
        with database() as conn:
            return {"favorites": store.list_favorites(conn, my_id())}

    @app.post("/api/favorites", status_code=201)
    def add_favorite(body: NewFavorite):
        with database() as conn:
            store.add_favorite(conn, my_id(), body.title, body.text)
            return {"favorites": store.list_favorites(conn, my_id())}

    @app.delete("/api/favorites/{fid}")
    def delete_favorite(fid: int):
        with database() as conn:
            store.delete_favorite(conn, my_id(), fid)
            return {"favorites": store.list_favorites(conn, my_id())}

    @app.patch("/api/sessions/{sid}")
    def rename_session(sid: int, body: SessionChange, request: HttpRequest):
        """Name one of one's devices (« Téléphone de Lou », « PC du salon »)."""
        if authority is None:
            raise NotFound("session introuvable")
        with database() as conn:
            if not authority.rename_session(conn, sid, body.label, request.cookies.get(auth.COOKIE)):
                raise NotFound("session introuvable")
        return {"renamed": sid, "label": auth.check_label(body.label)}

    @app.delete("/api/sessions/{sid}")
    def close_session(sid: int, request: HttpRequest):
        if authority is None:
            raise NotFound("session introuvable")
        with database() as conn:  # one's own devices only
            if not authority.close_session(conn, sid, request.cookies.get(auth.COOKIE)):
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
    def push_test(request: HttpRequest):
        user = me()
        session_id = None
        if authority is not None:
            with database() as conn:
                session_id = authority.session_id(conn, request.cookies.get(auth.COOKIE))
        # The test goes to the devices of the person who asked, not to everyone's.
        notifier.notify("done", None, "notification d'essai", test=True, session_id=session_id)
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

    @app.exception_handler(access.Forbidden)
    def _forbidden(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc), "code": "forbidden"}, 403)

    @app.exception_handler(NotFound)
    def _not_found(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(Busy)
    def _busy(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(StoreError)
    @app.exception_handler(importer.ImportFailed)
    @app.exception_handler(ProviderError)
    @app.exception_handler(obsidian.ObsidianError)
    @app.exception_handler(config.ConfigError)
    @app.exception_handler(notify.NotifyError)
    @app.exception_handler(tools.ToolError)
    def _refused(request: HttpRequest, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    def allowed_places():
        """The one rule for the PC's files and folders (aihub/places.py): the settings' allowed folders."""
        return places.Places(current().allowed_roots)

    # The memory reads text files only; the size the app took from a device before (800 Ko).
    MEMORY_KINDS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml"}
    MEMORY_FILE = 800_000

    def pc_file(user, raw, **limits):
        """A file of the PC read by the app: the owner's and the admins' only (they already run AIs on
        this PC), inside the allowed folders. Members and guests never read the PC's files."""
        if not access.trusted(user):
            raise access.Forbidden("les fichiers du PC serveur sont réservés au propriétaire et aux administrateurs")
        return allowed_places().file(raw, **limits)

    def confirm_maker(conn, user, password, request):
        """Making something on the PC (a folder, a vault, a saved file) asks the person's password again."""
        if authority is not None:
            verified = authority.verify(conn, password, client_of(request.scope), name=user['name'])
            if verified['id'] != user['id'] or not access.trusted(verified):
                raise access.Forbidden('les droits du compte ont changé : création refusée')

    def check_workdir(path):
        return str(allowed_places().folder(path, "dossier de travail"))

    # --- the app's own folder picker (aihub/browse.py): names only, reading only ----------

    @app.get('/api/filesystem/folders')
    def browse_folders(path: str | None = None, scope: str = 'workdir', files: str | None = None):
        """The app's file explorer: folders, and the files of the kinds asked (`files`: "md,txt", or "*")."""
        user = me()
        kinds = None if files is None else "*" if files.strip() == "*" else \
            {"." + k.strip().lower().lstrip(".") for k in files.split(",") if k.strip()}
        if scope == 'settings':
            # The owner chooses a setting (allowed folders, vault, backups): anywhere on the PC's local disks.
            access.require_owner(user)
            if kinds is not None:
                raise StoreError("les réglages ne choisissent que des dossiers")
            return browse.folders(path)
        if not access.trusted(user):
            raise access.Forbidden('parcourir le PC serveur est réservé au propriétaire et aux administrateurs')
        return browse.folders(path, current().allowed_roots, files=kinds)

    @app.get('/api/filesystem/entries')
    def browse_entries(path: str = '', base: str | None = None, conversation_id: int | None = None):
        """What a working folder holds, to pick the files an isolated space receives."""
        user = me()
        if conversation_id is not None:
            with database() as conn:
                conv = access.require(conn, user, conversation_id, 'owner')
            if not conv['workdir']:
                raise StoreError("cette conversation n'a pas de dossier de travail")
            return browse.entries(conv['workdir'], path)
        if not access.trusted(user):
            raise access.Forbidden('parcourir le PC serveur est réservé au propriétaire et aux administrateurs')
        if not base:
            raise StoreError("indiquez d'abord le dossier de travail")
        # check_workdir is aihub/places.py: a network path is refused before anything resolves it.
        return browse.entries(check_workdir(base), path)

    @app.post('/api/filesystem/create')
    def create_path(body: CreatePath, request: HttpRequest):
        user = me()
        if not access.trusted(user):
            raise access.Forbidden('la création sur le PC serveur est réservée au propriétaire et aux administrateurs')
        base = None
        with database() as conn:
            if body.conversation_id is not None:
                conv = access.require(conn, user, body.conversation_id, 'owner')
                if isolation.suspended(conn, conv['id']):
                    raise StoreError('les accès de cette conversation sont suspendus')
                base = check_workdir(conv['workdir'])
            confirm_maker(conn, user, body.password, request)
        return filesystem.create(body.path, body.kind, roots=current().allowed_roots,
                                 base=base, parents=body.parents)

    @app.post('/api/obsidian/vaults', status_code=201)
    def create_obsidian_vault(body: CreatePath, request: HttpRequest):
        user = me()
        access.require_owner(user)
        if body.conversation_id is not None or body.kind != 'directory':
            raise StoreError('indiquez le chemin complet d’un nouveau coffre')
        with database() as conn:
            if authority is not None:
                verified = authority.verify(conn, body.password, client_of(request.scope), name=user['name'])
                if verified['id'] != user['id']:
                    raise access.Forbidden('les droits du compte ont changé : création refusée')
                access.require_owner(verified)
        result = obsidian.create_vault(body.path, roots=current().allowed_roots, parents=body.parents)
        vaults.record_created(vaults.record_file(db_path), result["path"])
        return result

    # --- the vaults on this PC (aihub/vaults.py), the owner's, reading only ------------

    app.state.vault_hooks = {}  # tests give their own Obsidian list and folders to walk

    @app.get("/api/obsidian/vaults")
    def list_vaults(refresh: bool = False):
        access.require_owner(me())
        hooks = app.state.vault_hooks
        with database() as conn:
            own = [(row["name"], row["obsidian_vault"]) for row in conn.execute(
                "SELECT name, obsidian_vault FROM projects WHERE obsidian_vault IS NOT NULL ORDER BY name")]
        found = vaults.discover(configured=current().obsidian_vault, record=vaults.record_file(db_path),
                               lists=hooks.get("lists"), roots=hooks.get("roots"), projects=own,
                               extra_roots=[(r, 2) for r in current().allowed_roots], refresh=refresh)
        # Which may be linked to a conversation or a project: inside the allowed folders (aihub/places.py).
        rule = allowed_places()
        return {**found, "vaults": [{**v, "allowed": bool(v["exists"]) and rule.allows(Path(v["path"]))}
                                    for v in found["vaults"]]}

    def notes_vaults(vault):
        """The folder asked for, when AI Hub writes notes there (the settings' or a project's vault)."""
        try:
            browse.local_path(str(vault or "").strip())  # a network path is not resolved, even to compare
        except StoreError:
            return None
        if current().obsidian_vault and vaults._key(vault) == vaults._key(current().obsidian_vault):
            return vault
        with database() as conn:
            mine = [row[0] for row in conn.execute(
                "SELECT obsidian_vault FROM projects WHERE obsidian_vault IS NOT NULL "
                "UNION SELECT obsidian_vault FROM conversations WHERE obsidian_vault IS NOT NULL")]
        return vault if any(vaults._key(vault) == vaults._key(m) for m in mine) else None

    def readable_vault(vault):
        """A vault read here: one AI Hub writes notes to, else one inside the allowed folders (aihub/places.py)."""
        configured = notes_vaults(vault)
        if configured is None:
            allowed_places().folder(str(vault or ""), "coffre")
        return configured

    @app.get("/api/obsidian/vault/notes")
    def vault_notes(vault: str):
        access.require_owner(me())
        return vaults.list_notes(vault, configured=readable_vault(vault))

    @app.get("/api/obsidian/vault/search")
    def vault_search(vault: str, q: str):
        access.require_owner(me())
        return vaults.search(vault, q, configured=readable_vault(vault))

    @app.get("/api/obsidian/vault/note")
    def vault_note(vault: str, path: str):
        access.require_owner(me())
        return vaults.read_note(vault, path, configured=readable_vault(vault))

    def agent_for(conn, model, *, isolated=False):
        offered = (isolated_models(current()) if isolated and config.provider_of(model) in ('claude_code', 'codex') else
                   offered_models(current()) if config.provider_of(model) in (codex.PROVIDER, ollama.PROVIDER)
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
        # Each person sees the quota of the accounts that pay for their requests.
        user = me()
        account = user["id"] if accounts.own_accounts(user) else None
        run_env = people_accounts.env_for(user)
        state = refresh_state.setdefault(account, {})
        if account is None:
            gpt_in = bool(provider_status()["codex"]["logged_in"])
        else:
            gpt_in = state.get("codex_in")
            if gpt_in is None or refresh:
                try:
                    gpt_in = codex.login_status(providers.command_for(commands, codex.PROVIDER), env=run_env) \
                        if commands[codex.PROVIDER] is not False else False
                except ProviderError:
                    gpt_in = False
                state["codex_in"] = gpt_in
        if refresh:
            with refresh_lock:
                last = state.get("at")
                if last is None or time.monotonic() - last >= QUOTA_REFRESH_S:
                    state["at"] = time.monotonic()
                    wanted = [claude_code] + ([codex] if gpt_in else [])
                    for module in wanted:
                        try:
                            info = module.read_usage(
                                providers.command_for(commands, module.PROVIDER),
                                workdir=str(Path(db_path).resolve().parent),
                                env=run_env, warning_from=current().quota_warning / 100)
                            with database() as conn:
                                store.record_quota(
                                    conn, module.PROVIDER, info.status, limit_type=info.limit_type,
                                    utilization=info.utilization, resets_at=info.resets_at,
                                    windows_json=json.dumps(info.windows), user_id=account)
                            notifier.quota(module.DISPLAY_NAME, info, current().quota_warning, user_id=account)
                        except ProviderError as exc:
                            errors[module.PROVIDER] = str(exc)
                    state["errors"] = errors
                else:
                    errors = state.get("errors") or {}
        with database() as conn:
            return {"claude_code": _quota(store.latest_quota(conn, "claude_code", account)),
                    "codex": _quota(store.latest_quota(conn, "codex", account)) if gpt_in else None,
                    "codex_connected": bool(gpt_in), "own_accounts": account is not None,
                    "error": errors.get(claude_code.PROVIDER), "codex_error": errors.get(codex.PROVIDER)}

    def connected_for(user):
        if accounts.own_accounts(user):
            return accounts.available_models(commands, people_accounts.env_for(user), str(Path(db_path).parent))
        provider_status()
        return routing.available()

    def offered_models(s):
        """The models of the settings this computer can run: a GPT model only
        when Codex is signed in and offers it."""
        user = me()
        if accounts.own_accounts(user):
            connected = connected_for(user)
            return [m for m in s.models if config.provider_of(m) in connected and
                    (connected[config.provider_of(m)] is None or m in connected[config.provider_of(m)])] + \
                   sorted(connected.get(ollama.PROVIDER) or ())
        state = provider_status()
        gpt = state["codex"]
        usable = set(gpt["models"] or ()) | {codex.DEFAULT_MODEL} if gpt["logged_in"] else set()
        # The local AI's models come from its Ollama, after those of the settings.
        return [m for m in s.models if config.provider_of(m) != codex.PROVIDER or m in usable] + state["local"]["models"]

    def isolated_models(s):
        return [m for m in s.models if config.provider_of(m) in ('claude_code', 'codex')
                and commands.get(config.provider_of(m)) is not False] + local_status()['models']

    @app.get("/api/options")
    def options():
        s = current()
        vault = s.obsidian_vault
        return {"models": offered_models(s), "isolated_models": isolated_models(s), "default_model": s.default_model,
                "default_conductor": s.default_conductor, "quota_warning": s.quota_warning,
                "permission_modes": list(store.PERMISSION_MODES),
                "bypass_warning": store.BYPASS_WARNING, "check_warning": autonomous.CHECK_WARNING,
                "kinds": routing.KIND_LABELS,
                "obsidian": {"enabled": bool(vault), "vault": str(Path(vault).resolve()) if vault else None},
                "settings_editable": config_path is not None, "version": VERSION_LABEL, "build": page_build}

    # --- settings (the Réglages window) ----------------------------------------

    @app.get("/api/config")
    def get_config():
        access.require_owner(me())
        s = current()
        return {"settings": s.to_dict(), "labels": config.LABELS, "all_models": list(config.MODELS),
                "editable": config_path is not None, "password_needed": authority is not None,
                "overridden": list(overridden), "backup_folder": backup_folder(s),
                "file": str(Path(config_path).resolve()) if config_path else None}

    @app.patch("/api/config")
    def change_config(body: ConfigChange, request: HttpRequest):
        access.require_owner(me(request))
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
    def conversations(request: HttpRequest):
        user = me(request)
        with database() as conn:
            visible = access.visible_conversation_ids(conn, user)
            rows = [dict(r) for r in store.list_conversations(conn) if visible is None or r["id"] in visible]
            prefs = library.preferences(conn, (user or {}).get("id"))
            for row in rows:
                row["running"] = jobs.running(row["id"])
                row["my_role"] = access.conversation_role(conn, user, row)
                row.update(prefs.get(row["id"], {"pinned": False, "archived": False}))
                row["model"] = (store.get_agent(conn, row["agent_id"])["model"] if row["agent_id"] else None)
        return {"conversations": rows}

    @app.patch("/api/conversations/{cid}/preferences")
    def conversation_preferences(cid: int, body: ConversationPreferences):
        user = me()
        with database() as conn:
            access.require(conn, user, cid)
            return library.set_preferences(conn, (user or {}).get("id"), cid, pinned=body.pinned, archived=body.archived)

    @app.get("/api/templates")
    def work_templates():
        with database() as conn:
            return {"templates": library.templates(conn, my_id())}

    @app.post("/api/templates", status_code=201)
    def save_work_template(body: NewTemplate):
        user = me()
        access.require_creator(user)
        with database() as conn:
            conv = access.require(conn, user, body.conversation_id, "owner")
            settings = {"model": store.get_agent(conn, conv["agent_id"])["model"] if conv["agent_id"] else "default",
                        "mode": conv["mode"], "permission_mode": "manual" if conv["permission_mode"] == "bypass"
                        else conv["permission_mode"], "tools": tools.chosen(conv), "instructions": conv["instructions"] or ""}
            library.save_template(conn, (user or {}).get("id"), body.title, body.prompt, settings)
            return {"templates": library.templates(conn, (user or {}).get("id"))}

    @app.delete("/api/templates/{tid}")
    def delete_work_template(tid: str):
        with database() as conn:
            library.remove_template(conn, my_id(), tid)
            return {"templates": library.templates(conn, my_id())}

    # --- projects ------------------------------------------------------------

    @app.get("/api/projects")
    def projects(request: HttpRequest):
        user = me(request)
        with database() as conn:
            visible = access.visible_project_ids(conn, user)
            return {"projects": [dict(p, isolation=isolation.get(conn, project_id=p['id']) or isolation.DEFAULT.copy())
                                 for p in store.list_projects(conn) if visible is None or p["id"] in visible]}

    @app.post("/api/projects", status_code=201)
    def create_project(body: NewProject, request: HttpRequest):
        user = me(request)
        access.require_creator(user)
        # A member's project has no folder of the server's: its conversations get theirs.
        workdir = check_workdir(body.workdir) if body.workdir.strip() and access.trusted(user) else None
        with database() as conn:
            if body.isolation is not None:
                isolation.check(body.isolation)
            vault = None
            if body.obsidian_vault:
                access.require_owner(user)  # a folder of the owner's PC
                vault = obsidian.check_project_vault(body.obsidian_vault, current().allowed_roots)
            pid = store.create_project(conn, body.name, description=body.description, workdir=workdir)
            conn.execute("UPDATE projects SET instructions = ?, obsidian_vault = ? WHERE id = ?",
                         (store.check_instructions(body.instructions), vault, pid))
            set_owner(conn, "projects", pid, user)
            if body.isolation is not None:
                isolation.save(conn, body.isolation, project_id=pid)
            return {"project": dict(store.get_project(conn, pid))}

    def set_owner(conn, table, row_id, user):
        """Whose it is: NULL for the server's owner (as before accounts), else the person's id."""
        if user is not None and not user.get("is_owner"):
            conn.execute(f"UPDATE {table} SET owner_id = ? WHERE id = ?", (user["id"], row_id))

    def require_project(conn, user, pid):
        role = access.project_role(conn, user, pid)
        if role is None:
            raise NotFound("projet introuvable")
        if role != "owner":
            raise access.Forbidden("seul celui qui a créé le projet (ou un admin) le modifie")

    @app.post('/api/projects/{pid}/memory/documents', status_code=201)
    def add_memory_document(pid: int, body: MemoryDocument):
        with database() as conn:
            store.get_project(conn, pid)
            require_project(conn, me(), pid)
            did = rag.add(conn, pid, body.name, body.content)
            return {'document': next(d for d in rag.documents(conn, pid) if d['id'] == did)}

    @app.post('/api/projects/{pid}/memory/file', status_code=201)
    def add_memory_file(pid: int, body: PcFile):
        """A text file of the PC, chosen in the app's explorer, becomes a document of the project's memory."""
        user = me()
        with database() as conn:
            store.get_project(conn, pid)
            require_project(conn, user, pid)
        path = pc_file(user, body.path, extensions=MEMORY_KINDS, max_size=MEMORY_FILE)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise StoreError("ce fichier n'est pas un texte lisible (UTF-8)") from None
        if len(content) > rag.MAX_CHARS:
            raise StoreError(f"fichier trop long pour la mémoire du projet ({rag.MAX_CHARS} caractères au plus)")
        with database() as conn:
            did = rag.add(conn, pid, path.name[:120], content)
            return {'document': next(d for d in rag.documents(conn, pid) if d['id'] == did)}

    @app.post('/api/projects/{pid}/memory/notes', status_code=201)
    def link_memory_note(pid: int, body: LinkedNote):
        """A vault note becomes a document of the project's memory, and follows its file when it changes."""
        user = me()
        access.require_owner(user)  # the vaults are folders of the owner's PC
        note = vaults.read_note(body.vault, body.path, configured=readable_vault(body.vault))
        if note["cut"]:
            raise StoreError("note trop longue pour la mémoire du projet (200 000 caractères au maximum)")
        source = Path(note["vault"]) / note["path"]
        with database() as conn:
            store.get_project(conn, pid)  # the owner (checked above) manages every project
            did = rag.link(conn, pid, Path(note["path"]).name, note["content"], source, source.stat().st_mtime)
            return {'document': next(d for d in rag.documents(conn, pid) if d['id'] == did)}

    @app.delete('/api/projects/{pid}/memory/documents/{did}')
    def remove_memory_document(pid: int, did: int):
        with database() as conn:
            store.get_project(conn, pid)
            require_project(conn, me(), pid)
            if not rag.remove(conn, pid, did):
                raise NotFound('document introuvable dans ce projet')
        return {'removed': did}

    @app.get('/api/conversations/{cid}/memory')
    def conversation_memory(cid: int, q: str = ''):
        if len(q) > 2000:
            raise StoreError('recherche trop longue (2 000 caractères au maximum)')
        user = me()
        with database() as conn:
            conversation = access.require(conn, user, cid, 'read')
            policy = isolation.effective(conn, conversation)
            project_allowed = (conversation['project_id'] is not None
                               and access.reads_project(conn, (user or {}).get('id'), conversation)
                               and (not policy['isolated'] or policy['memory'] == 'project')
                               and not isolation.suspended(conn, cid))
            ids = {cid}
            if project_allowed and conversation['shared']:
                visible = access.visible_conversation_ids(conn, user)
                ids.update(row['id'] for row in store.project_conversations(conn, conversation['project_id'])
                           if row['shared'] and (visible is None or row['id'] in visible))
            pid = conversation['project_id'] if project_allowed else None
            return {'project_id': pid,
                    'can_manage': pid is not None and access.project_role(conn, user, pid) == 'owner',
                    'semantic_model': semantic.embedder().model if semantic.embedder() else None,
                    'documents': rag.documents(conn, pid) if pid is not None else [],
                    'conversations': memory.search(conn, q, limit=12, conversation_ids=ids) if q.strip() else [],
                    'passages': rag.search(conn, pid, q) if q.strip() else [],
                    'indexed': conn.execute('SELECT COUNT(*) FROM memory WHERE conversation_id = ?', (cid,)).fetchone()[0]}

    @app.get("/api/projects/{pid}")
    def project(pid: int, request: HttpRequest):
        user = me(request)
        with database() as conn:
            visible = access.visible_project_ids(conn, user)
            if visible is not None and pid not in visible:
                raise NotFound("projet introuvable")
            row = dict(store.get_project(conn, pid))
            row['isolation'] = isolation.get(conn, project_id=pid) or isolation.DEFAULT.copy()
            seen = access.visible_conversation_ids(conn, user)
            convs = [dict(c) for c in store.project_conversations(conn, pid) if seen is None or c["id"] in seen]
            mine = access.project_role(conn, user, pid) == "owner"
            shared_with = access.shares_of(conn, project_id=pid) if mine and authority is not None else None
        for conv in convs:
            conv["running"] = jobs.running(conv["id"])
        return {"project": row, "conversations": convs, "shared_with": shared_with, "is_owner": mine}

    @app.patch("/api/projects/{pid}")
    def change_project(pid: int, body: ProjectChange, request: HttpRequest):
        user = me(request)
        workdir = None
        if body.workdir is not None and access.trusted(user):
            workdir = check_workdir(body.workdir) if body.workdir.strip() else ""
        with database() as conn:
            require_project(conn, user, pid)
            if body.isolation is not None:
                if any(jobs.running(row['id']) for row in store.project_conversations(conn, pid)):
                    raise Busy('arrêtez les conversations du projet avant de changer leurs accès')
                isolation.save(conn, body.isolation, project_id=pid)
            if body.obsidian_vault is not None:
                access.require_owner(user)  # a folder of the owner's PC
                conn.execute("UPDATE projects SET obsidian_vault = ? WHERE id = ?",
                             (obsidian.check_project_vault(body.obsidian_vault, current().allowed_roots), pid))
            store.update_project(conn, pid, name=body.name, description=body.description, workdir=workdir)
            if body.instructions is not None:
                conn.execute("UPDATE projects SET instructions = ? WHERE id = ?",
                             (store.check_instructions(body.instructions), pid))
            return {"project": dict(store.get_project(conn, pid))}

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: int, request: HttpRequest, with_conversations: bool = False):
        with database() as conn:
            require_project(conn, me(request), pid)
            for row in store.project_conversations(conn, pid):
                if with_conversations and jobs.running(row["id"]):
                    raise Busy("une conversation du projet travaille : arrêtez-la d'abord")
            deleted = store.delete_project(conn, pid, with_conversations=with_conversations)
        errors = {}
        folder = purge_folder()
        for cid in deleted:
            attachments.forget_conversation(pieces, cid)
            errors.update(backup.purge_conversation(folder, cid))
        errors.update(backup.purge_project(folder, pid))
        return {"deleted": pid, "conversations": deleted, "backup_errors": errors}

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: NewConversation, request: HttpRequest):
        user = me(request)
        access.require_creator(user)  # a guest takes part in shared conversations, creates none
        with database() as conn:
            item, prompt, template_tools = None, "", []
            if body.template_id:
                item = library.template(conn, (user or {}).get("id"), body.template_id)
                if item is None:
                    raise NotFound("modèle de travail introuvable")
                prompt = library.render(item, body.variables)
                template_tools = tools.check_choice(item["settings"].get("tools", []), current())
                if not access.trusted(user) and set(template_tools) - set(MEMBER_TOOLS):
                    raise access.Forbidden("ce modèle utilise des outils réservés au propriétaire")
            with db.transaction(conn):
                cid = new_conversation(conn, body, user, auto_folder=True)
                if body.isolation is not None:
                    isolation.save(conn, body.isolation, conversation_id=cid)
                if item:
                    if template_tools and isolation.effective(conn, store.get_conversation(conn, cid))['isolated']:
                        raise StoreError('ce modèle utilise des outils du PC : choisissez un modèle sans outils')
                    conn.execute("UPDATE conversations SET instructions = ? WHERE id = ?",
                                 (store.check_instructions(item["settings"].get("instructions", "")), cid))
                    store.set_tools(conn, cid, template_tools)
            return {"conversation": _row(store.get_conversation(conn, cid)), "prompt": prompt}

    @app.get('/api/conversations/{cid}/isolation')
    def isolation_state(cid: int):
        with database() as conn:
            conv = access.require(conn, me(), cid)
            parent = isolation.get(conn, project_id=conv['project_id']) or isolation.DEFAULT.copy()
            return {'policy': isolation.effective(conn, conv), 'parent': parent,
                    'inherited': isolation.get(conn, conversation_id=cid) is None}

    @app.post('/api/conversations/{cid}/isolation/pause')
    def isolation_pause(cid: int):
        # Registration reads the switch while holding its own lock. A job
        # registered before this commit is caught by stop; later jobs refuse.
        with space_accounts_lock:
            with database() as conn:
                access.require(conn, me(), cid, 'owner')
                isolation.suspend(conn, cid)
            if cid in space_stops:
                return {'paused': True, 'stopping': True, 'warnings': []}
            space_stops.add(cid)
            names = [name for name, m in space_accounts.items()
                     if m.execution is not None and m.execution.conversation['id'] == cid]
            managers = [(name, space_accounts.pop(name)) for name in names]
        failures = []
        try:
            for name, manager in managers:
                # All participants of this conversation, not just its owner.
                if not manager.cancel_all():
                    failures.append('une connexion ou son espace n’a pas confirmé son arrêt')
                    with space_accounts_lock:
                        space_accounts.setdefault(name, manager)  # allow an explicit retry
            try:
                jobs.stop(cid)
            except (ProviderError, accounts.AccountError, OSError):
                failures.append('le travail en cours n’a pas confirmé son arrêt')
        finally:
            with space_accounts_lock:
                space_stops.discard(cid)
        return {'paused': True, 'warnings': failures}

    @app.post('/api/conversations/{cid}/isolation/resume')
    def isolation_resume(cid: int):
        with space_accounts_lock:
            with database() as conn:
                conv = access.require(conn, me(), cid, 'owner')
                if cid in space_stops or jobs.running(cid):
                    raise Busy('attendez la fin de l’arrêt avant de réactiver les accès')
                isolation.suspend(conn, cid, False)
                return {'policy': isolation.effective(conn, conv)}

    diagnostics_lock = threading.Lock()
    diagnostics_cache = {'until': 0, 'data': None}

    @app.get('/api/diagnostics')
    def diagnostics():
        me()  # Guard enforces the session; results contain no participant data.
        with diagnostics_lock:
            if time.monotonic() < diagnostics_cache['until']:
                return diagnostics_cache['data']
            result = diagnostic_checks()
            diagnostics_cache.update(until=time.monotonic() + 10, data=result)
            return result

    def diagnostic_checks():
        # No logs, credential files, account names or server paths leave here.
        from .. import sandbox
        checks = []
        try:
            with database() as conn:
                conn.execute('SELECT id FROM conversations LIMIT 1').fetchone()
            database_ok = True
        except (sqlite3.Error, db.SchemaError, OSError):
            database_ok = False
        checks.append({'name': 'Base de données', 'ok': database_ok,
                       'detail': 'Lecture disponible.' if database_ok else 'La base ne peut pas être lue.',
                       'action': '' if database_ok else 'Vérifiez le disque et le dossier de données avant de restaurer une sauvegarde.'})
        try:
            free = shutil.disk_usage(Path(db_path).resolve().parent).free
        except OSError:
            free = None
        checks.append({'id': 'disk', 'name': 'Espace disque', 'ok': free is not None and free >= 1024**3,
                       'detail': f'{free // (1024**2)} Mo libres.' if free is not None else 'Lecture du disque indisponible.',
                       'action': 'Vérifiez ou libérez le disque du serveur.' if free is None or free < 1024**3 else ''})
        try:
            cli = sandbox.find_cli(current().sandbox_cli)
            probe_env = {k: v for k, v in os.environ.items() if k.upper() in
                         ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA')}
            if current().sandbox_data:
                probe_env['LOCALAPPDATA'] = current().sandbox_data
            result = subprocess.run([cli, 'daemon', 'status'], env=probe_env, capture_output=True,
                                    timeout=5, creationflags=claude_code.NO_WINDOW)
            engine = result.returncode == 0
        except (ProviderError, OSError, subprocess.TimeoutExpired):
            engine = False
        checks.append({'id': 'engine', 'name': 'Moteur des espaces isolés', 'ok': engine,
                       'detail': 'Le moteur répond ; les accès seront contrôlés au lancement.' if engine else 'Le moteur ne répond pas.',
                       'action': '' if engine else 'Sur le PC du serveur, démarrez Docker Sandboxes ou vérifiez son chemin dans Réglages.'})
        return {'checks': checks, 'version': __version__}

    @app.put('/api/conversations/{cid}/isolation')
    def isolation_change(cid: int, body: dict):
        if jobs.running(cid):
            raise Busy('arrêtez les agents avant de changer leurs accès')
        with database() as conn:
            access.require(conn, me(), cid, 'owner')
            return {'policy': isolation.save(conn, body, conversation_id=cid)}

    @app.delete('/api/conversations/{cid}/isolation')
    def isolation_inherit(cid: int):
        if jobs.running(cid):
            raise Busy('arrêtez les agents avant de changer leurs accès')
        with database() as conn:
            conv = access.require(conn, me(), cid, 'owner')
            old = isolation.effective(conn, conv)
            conn.execute('DELETE FROM execution_policies WHERE conversation_id = ?', (cid,))
            if old['isolated'] and not isolation.effective(conn, conv)['isolated']:
                conn.execute("UPDATE conversations SET permission_mode = 'manual' WHERE id = ? AND permission_mode = 'bypass'", (cid,))
            conn.execute('UPDATE messages SET session_id = NULL WHERE conversation_id = ?', (cid,))
            return {'policy': isolation.effective(conn, conv)}

    def space_account(cid, provider, read_only=False, *, prepare=False):
        if provider not in ('claude_code', 'codex') or commands.get(provider) is False:
            raise StoreError('fournisseur isolé non proposé sur ce serveur')
        user = me()
        with database() as conn:
            conv = access.require(conn, user, cid, 'write')
            context = isolation.for_run(conn, conv, account_id=(user or {}).get('id'),
                                        read_only=read_only, settings=current())
        if context is None:
            raise StoreError('cette conversation utilise le PC, pas un espace isolé')
        name, _ = context.identity(provider)
        with space_accounts_lock:
            manager = space_accounts.get(name)
        if manager is None:
            if prepare and jobs.running(cid):
                raise Busy('attendez la fin du travail avant de préparer une connexion')
            if context.prepare(provider, create=prepare) is None:
                return None
            manager = accounts.Accounts(jobs.accounts_root, commands=commands, execution=context,
                                        workdir=Path(db_path).resolve().parent)
            with space_accounts_lock:
                with database() as conn:
                    access.require(conn, user, cid, 'write')
                    paused = isolation.suspended(conn, cid)
                if not paused:
                    manager = space_accounts.setdefault(name, manager)
            if paused:
                manager.cancel_all()
                raise ProviderError('accès suspendus : le propriétaire doit réactiver cet espace')
        return manager

    @app.get('/api/conversations/{cid}/sandbox/accounts')
    def sandbox_accounts(cid: int, provider: str, read_only: bool = False):
        manager = space_account(cid, provider, read_only)
        if manager is None:
            return {'prepared': False, 'provider': provider, 'login': None}
        return {'prepared': True, **manager.status(me())[provider]}

    @app.post('/api/conversations/{cid}/sandbox/login')
    def sandbox_login(cid: int, body: SandboxTarget):
        if jobs.running(cid):
            raise Busy('attendez la fin du travail avant de connecter cet espace')
        manager = space_account(cid, body.provider, body.read_only, prepare=True)
        with space_accounts_lock:
            # Suspension may have been committed while preparation was running.
            with database() as conn:
                access.require(conn, me(), cid, 'write')
                if isolation.suspended(conn, cid):
                    raise ProviderError('accès suspendus : le propriétaire doit réactiver cet espace')
            return account_call(lambda: manager.start_login(me(), body.provider, wait_s=0))

    @app.post('/api/conversations/{cid}/sandbox/code')
    def sandbox_code(cid: int, body: SandboxCode):
        manager = space_account(cid, body.provider, body.read_only)
        if manager is None:
            raise StoreError('aucune connexion n’attend de code')
        with space_accounts_lock:
            with database() as conn:
                access.require(conn, me(), cid, 'write')
                if isolation.suspended(conn, cid):
                    raise ProviderError('accès suspendus : le propriétaire doit réactiver cet espace')
            return account_call(lambda: manager.send_code(me(), body.provider, body.code, wait_s=0))

    @app.post('/api/conversations/{cid}/sandbox/cancel-login')
    def sandbox_cancel_login(cid: int, body: SandboxTarget):
        manager = space_account(cid, body.provider, body.read_only)
        if manager is not None:
            manager.cancel_login(me(), body.provider)
        return {'cancelled': True}

    @app.post('/api/conversations/{cid}/sandbox/logout')
    def sandbox_logout(cid: int, body: SandboxTarget):
        if jobs.running(cid):
            raise Busy('arrêtez le travail avant de déconnecter cet espace')
        manager = space_account(cid, body.provider, body.read_only)
        if manager is not None:
            account_call(lambda: manager.logout(me(), body.provider))
        return {'signed_in': False}

    def result_context(cid, provider, read_only=False, *, apply=False):
        if provider not in ('claude_code', 'codex'):
            raise StoreError('fournisseur isolé non proposé')
        if jobs.running(cid):
            raise Busy('attendez la fin du travail avant de récupérer ses fichiers')
        with database() as conn:
            conv = access.require(conn, me(), cid, 'owner' if apply else 'write')
            context = isolation.for_run(conn, conv, account_id=(me() or {}).get('id'),
                                        read_only=read_only, settings=current())
        if context is None:
            raise StoreError('cette conversation n’utilise pas d’espace isolé')
        return context

    @app.get('/api/conversations/{cid}/sandbox/results')
    def sandbox_results_list(cid: int, provider: str, read_only: bool = False):
        context = result_context(cid, provider, read_only)
        sandbox_results.collect(context, provider)
        return {'files': sandbox_results.review(context, provider),
                'can_apply': not read_only and context.policy['files'] == 'write',
                'workdir': context.conversation['workdir']}

    @app.get('/api/conversations/{cid}/sandbox/results/download')
    def sandbox_results_download(cid: int, provider: str, read_only: bool = False):
        context = result_context(cid, provider, read_only)
        sandbox_results.collect(context, provider)
        return Response(sandbox_results.download(context, provider), media_type='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="resultats-espace-{cid}-{provider}.zip"',
                                 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/conversations/{cid}/sandbox/results/preview')
    def sandbox_result_preview(cid: int, provider: str, name: str, read_only: bool = False):
        context = result_context(cid, provider, read_only)
        return sandbox_results.preview(context, provider, name)

    @app.post('/api/conversations/{cid}/sandbox/results/apply')
    def sandbox_results_apply(cid: int, body: SandboxApply):
        context = result_context(cid, body.provider, body.read_only, apply=True)
        context.prepare(body.provider, create=False)
        context.stop()  # no guest background process can write during review/apply
        return sandbox_results.apply(context, body.provider, body.files, confirm_delete=body.confirm_delete)

    # --- import (aihub/importer.py) --------------------------------------------
    # A reading is kept 30 minutes on the server, for the person who made it:
    # the page gets the list (titles, dates), never the whole texts.

    readings = {}
    readings_lock = threading.Lock()
    READING_S = 30 * 60

    def keep_reading(items, user):
        token = secrets.token_urlsafe(16)
        with readings_lock:
            now = time.monotonic()
            for old in [k for k, (at, _, _) in readings.items() if now - at > READING_S]:
                del readings[old]
            while len(readings) >= 4:  # a few at most: an export can be large
                del readings[min(readings, key=lambda k: readings[k][0])]
            readings[token] = (now, (user or {}).get("id"), {i.key: i for i in items})
        return {"reading": token, "conversations": [i.summary() for i in items]}

    @app.post("/api/import/file")
    async def import_file(request: HttpRequest):
        """The export of chatgpt.com or claude.ai (.zip or conversations.json), as the request's body."""
        user = me(request)
        access.require_creator(user)
        size = int(request.headers.get("content-length") or 0)
        if size > importer.MAX_UPLOAD:
            raise importer.ImportFailed(f"fichier trop gros ({importer.MAX_UPLOAD // 1024 // 1024} Mo au plus)")
        data = await request.body()
        items = await asyncio.to_thread(importer.parse_export, data)
        return keep_reading(items, user)

    @app.post("/api/import/pc")
    def import_pc_file(body: PcFile, request: HttpRequest):
        """The same export, as a file of the PC chosen in the app's explorer."""
        user = me(request)
        access.require_creator(user)
        path = pc_file(user, body.path, extensions={".zip", ".json"}, max_size=importer.MAX_UPLOAD)
        return keep_reading(importer.parse_export(path.read_bytes()), user)

    @app.get("/api/import/local")
    def import_local(request: HttpRequest):
        """The Claude Code and Codex sessions of this computer (the owner's: they are on their PC)."""
        user = me(request)
        access.require_owner(user)
        return keep_reading(importer.local_sessions(local_home), user)

    @app.post("/api/import", status_code=201)
    def import_conversations(body: NewImport, request: HttpRequest):
        user = me(request)
        access.require_creator(user)
        with readings_lock:
            reading = readings.get(body.reading)
        # Someone else's reading is as unknown as an expired one.
        if reading is None or reading[1] != (user or {}).get("id"):
            raise importer.ImportFailed("lecture expirée : choisissez de nouveau le fichier ou les sessions")
        items = [reading[2][k] for k in dict.fromkeys(body.keys) if k in reading[2]]
        if not items:
            raise importer.ImportFailed("cochez au moins une conversation")
        groups = [items] if body.merge else [[i] for i in items]
        made = []
        with database() as conn:
            for group in groups:
                first = group[0]
                title = (body.title or "").strip() if body.merge else first.title
                if not title:
                    title = "Fusion : " + " + ".join(i.title for i in group)
                    title = title if len(title) <= importer.TITLE_MAX else title[:importer.TITLE_MAX - 1] + "…"
                workdir = body.workdir.strip()
                if not workdir and body.project_id is None:
                    # A local session's own folder, when it still exists.
                    workdir = next((i.workdir for i in group if i.workdir and Path(i.workdir).is_dir()), "")
                cid = new_conversation(conn, NewConversation(
                    title=title, workdir=workdir, project_id=body.project_id, model=body.model,
                    permission_mode=body.permission_mode if body.permission_mode != "bypass" else "manual"), user)
                importer.write_messages(conn, cid, group, merged=body.merge)
                made.append(cid)
        return {"conversations": made}

    spaces = side_folder(db_path, "espaces")

    def own_space(user_id, title, fresh=False):
        """A member's conversation works in a folder of theirs: data/espaces/<id>/<title>.

        `fresh`: never one that exists, even empty (another conversation may already use it)."""
        safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in title).strip()[:60] or "conversation"
        folder = spaces / str(int(user_id)) / safe
        n = 1
        while folder.exists() and (fresh or any(folder.iterdir())):
            n += 1
            folder = spaces / str(int(user_id)) / f"{safe} ({n})"
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    def new_conversation(conn, body, user=None, auto_folder=False):
        """Create a conversation from the « Nouvelle conversation » choices (also used by an import).

        A member's: in their own space on the server, never without permission,
        and only in a project of theirs. `auto_folder`: without a folder (none
        given, none in its project), the owner's gets one of its own too."""
        if body.mode not in store.MODES:
            raise StoreError(f"mode inconnu : {body.mode!r}")
        if body.project_id is not None and access.project_role(conn, user, body.project_id) != "owner":
            raise access.Forbidden("une conversation se crée dans un projet à soi")
        parent = isolation.get(conn, project_id=body.project_id) or isolation.DEFAULT
        profile = isolation.narrower(isolation.check(body.isolation), parent) if body.isolation is not None else parent
        if access.trusted(user):
            workdir = body.workdir
            if not workdir.strip() and body.project_id is not None:
                workdir = store.get_project(conn, body.project_id)["workdir"] or ""
            made = False  # a folder the app makes for the conversation: inside its data, never checked
            if not workdir.strip():
                parent = isolation.get(conn, project_id=body.project_id) or isolation.DEFAULT
                profile = isolation.check(body.isolation) if body.isolation is not None else parent
                if (profile['isolated'] and profile['files'] == 'none') or auto_folder:
                    # « Nouvelle conversation » without a folder (1.2): an empty one of its own, like a member's.
                    workdir, made = own_space((user or {}).get('id') or 0, body.title, fresh=auto_folder), True
                else:
                    raise StoreError("indiquez le dossier de travail (ou donnez-en un au projet)")
            if not made:
                workdir = check_workdir(workdir)
        else:
            if not profile['isolated']:
                if body.isolation is not None:
                    raise access.Forbidden('les membres créent leurs conversations dans un espace isolé')
                profile = isolation.check({'isolated': True})
            if body.permission_mode == "bypass" and not profile['isolated']:
                raise access.Forbidden("le mode sans permission est réservé au propriétaire et aux admins")
            workdir = own_space(user["id"], body.title)
        if body.mode == "orchestration":
            # The conductor plans and sums up; the subtasks get their agents in the plan.
            agent_id = conductor_id = agent_for(conn, body.conductor_model, isolated=profile['isolated'])
        else:
            agent_id, conductor_id = agent_for(conn, body.model, isolated=profile['isolated']), None
        cid = store.create_conversation(
            conn, body.title, mode=body.mode, agent_id=agent_id, conductor_agent_id=conductor_id,
            workdir=workdir, permission_mode=body.permission_mode,
            confirm_bypass=body.confirm_bypass, assignment_mode=body.assignment_mode,
            project_id=body.project_id,
        )
        set_owner(conn, "conversations", cid, user)
        if not access.trusted(user):
            isolation.save(conn, profile, conversation_id=cid)
        if isolation.effective(conn, store.get_conversation(conn, cid))['isolated']:
            store.set_tools(conn, cid, [])
        return cid

    def export_parts(cid):
        """The conversation (whoever sees it may take it away), its messages, and who wrote each."""
        with database() as conn:
            conv = dict(access.require(conn, me(), cid))
            names = {u["id"]: u["name"] for u in auth.list_users(conn)}
            messages = [dict(m) for m in store.list_messages(conn, cid)]
        author = lambda m: (names.get(m["user_id"]) or "Vous") if m["role"] == "user" else (m["model"] or "IA")
        return conv, messages, author

    def exported(cid, format):
        """(text, media type, file name) of the export in Markdown or in AI Hub's JSON."""
        conv, messages, author = export_parts(cid)
        safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in conv["title"]).strip()[:80] or "conversation"
        if format == "json":
            body = json.dumps(importer.export_json(conv, messages, author), ensure_ascii=False, indent=2)
            return body, "application/json", f"{safe}.json"
        shown = [m for m in messages if m["content"] and m["status"] in ("ok", "stopped")]
        parts = [f"# {conv['title']}", "", f"_Exporté d'AI Hub le {db.utc_now()[:10]}._", ""]
        for m in shown:
            parts += [f"## {author(m)} — {m['created_at'][:16].replace('T', ' ')}", "", m["content"], ""]
        return "\n".join(parts), "text/markdown", f"{safe}.md"

    @app.post("/api/conversations/{cid}/save", status_code=201)
    def save_conversation(cid: int, body: SaveToPc, request: HttpRequest):
        """The export saved into a folder of the PC chosen in the app's explorer: a new file, never over one."""
        user = me(request)
        if body.format not in ("md", "json"):
            raise StoreError("format : md ou json")
        if not access.trusted(user):
            raise access.Forbidden("enregistrer sur le PC serveur est réservé au propriétaire et aux administrateurs")
        text, _, name = exported(cid, body.format)
        with database() as conn:
            confirm_maker(conn, user, body.password, request)
        target = places.free(allowed_places().child(body.folder, name))
        try:
            with open(target, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except FileExistsError:
            raise StoreError("un fichier de ce nom vient d'apparaître : réessayez") from None
        except OSError as exc:
            raise StoreError(f"enregistrement impossible : {exc.strerror or exc}") from None
        return {"path": str(target)}

    @app.get("/api/conversations/{cid}/export")
    def export_conversation(cid: int, format: str = "md"):
        """A conversation to keep or to send: Markdown, AI Hub's JSON (the import reads it back), or a page
        to print (« Enregistrer en PDF »). Whoever sees the conversation may export it."""
        if format not in ("md", "json", "html"):
            raise StoreError("format : md, json ou html")
        if format != "html":
            return export_response(*exported(cid, format))
        conv, messages, author = export_parts(cid)
        shown = [m for m in messages if m["content"] and m["status"] in ("ok", "stopped")]
        import html
        rows = "".join(f'<section class="{m["role"]}"><h2>{html.escape(author(m))} '
                       f'<small>{html.escape(m["created_at"][:16].replace("T", " "))}</small></h2>'
                       f'<div>{html.escape(m["content"])}</div></section>' for m in shown)
        page = (f'<!doctype html><html lang="fr"><meta charset="utf-8"><title>{html.escape(conv["title"])}</title>'
                '<style>body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#1d1d1b}'
                'h1{font-size:22px}h2{font-size:14px;margin:0 0 4px;color:#555}small{font-weight:400}'
                'section{padding:10px 14px;border-radius:10px;margin:10px 0;break-inside:avoid}'
                '.user{background:#e6ecfb}.assistant{border:1px solid #ddd}div{white-space:pre-wrap;overflow-wrap:anywhere}'
                '.print{margin:12px 0}@media print{.print{display:none}}</style>'
                f'<h1>{html.escape(conv["title"])}</h1>'
                '<p class="print"><button onclick="print()">Imprimer ou enregistrer en PDF</button></p>'
                f'{rows}</html>')
        return HTMLResponse(page)

    def export_response(body, mime, filename):
        from urllib.parse import quote
        return Response(body, media_type=f"{mime}; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    # --- « Essais à faire » (aihub/trials.py), the owner's ----------------------------

    @app.get("/api/trials")
    def list_trials():
        access.require_owner(me())
        with database() as conn:
            return {"trials": trials.list_trials(conn)}

    @app.put("/api/trials/{key}")
    def say_trial(key: str, body: TrialSaid):
        access.require_owner(me())
        with database() as conn:
            trials.set_trial(conn, key, body.status, body.note)
            return {"trials": trials.list_trials(conn)}

    # --- a whole project in one .zip (aihub/project_export.py) -------------------------

    @app.get("/api/projects/{pid}/export")
    def export_project(pid: int):
        """The project, the conversations the person sees, its documents, and the notes when they may read them."""
        user = me()
        with database() as conn:
            if access.project_role(conn, user, pid) is None:
                raise NotFound("projet introuvable")
            project = dict(store.get_project(conn, pid))
            policy = isolation.get(conn, project_id=pid)
            visible = access.visible_conversation_ids(conn, user)
            names = {u["id"]: u["name"] for u in auth.list_users(conn)}
            author = lambda m: (names.get(m["user_id"]) or "Vous") if m["role"] == "user" else (m["model"] or "IA")
            conversations = []
            for conv in reversed(store.project_conversations(conn, pid)):  # oldest first
                if visible is not None and conv["id"] not in visible:
                    continue
                messages = [dict(m) for m in store.list_messages(conn, conv["id"])]
                note = None
                if conv["obsidian_note"] and (user is None or user.get("is_owner")):  # the vault is the owner's
                    try:
                        vault = obsidian.vault_of(conn, conv, current().obsidian_vault)
                        note = obsidian.resolve(vault, conv["obsidian_note"]).read_text(encoding="utf-8")
                    except (obsidian.ObsidianError, OSError):
                        note = None
                conversations.append((importer.export_json(dict(conv), messages, author), conv["instructions"], note))
            documents = [(r["name"], r["content"]) for r in conn.execute(
                "SELECT name, content FROM rag_documents WHERE project_id = ? ORDER BY id", (pid,))]
        data = project_export.build(project, policy, conversations, documents)
        safe = "".join(c if c.isalnum() or c in " -_" else "-" for c in project["name"]).strip()[:80] or "projet"
        from urllib.parse import quote
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('projet ' + safe + '.zip')}"})

    def import_project_from(bundle, user, workdir, copy_notes):
        """All or nothing in the base (one transaction); the notes are written in the vault after it."""
        made = {"conversations": 0, "documents": 0, "notes": 0, "skipped": []}
        # Without a folder, the owner's conversations are refused (« indiquez le dossier de travail ») and the
        # transaction below takes everything back.
        folder = check_workdir(workdir) if workdir.strip() and access.trusted(user) else None
        notes = []
        with database() as conn:
            with db.transaction(conn):
                name, n = bundle["name"], 1
                existing = {r["name"].lower() for r in store.list_projects(conn)}
                while name.lower() in existing:  # the same name is already a project here
                    name = f"{bundle['name']} (importé)" if n == 1 else f"{bundle['name']} (importé {n})"
                    n += 1
                pid = store.create_project(conn, name, description=bundle["description"], workdir=folder)
                conn.execute("UPDATE projects SET instructions = ? WHERE id = ?",
                             (store.check_instructions(bundle["instructions"][:4000]), pid))
                set_owner(conn, "projects", pid, user)
                if bundle["isolation"] is not None:
                    try:
                        isolation.check(bundle["isolation"])
                        isolation.save(conn, bundle["isolation"], project_id=pid)
                    except (StoreError, ValueError, TypeError, KeyError) as exc:
                        made["skipped"].append(f"accès du projet non repris : {exc}")
                for index, entry in enumerate(bundle["conversations"], 1):
                    item = importer._aihub(entry["export"], index)
                    if not item.messages:
                        made["skipped"].append(f"« {item.title} » : aucun message")
                        continue
                    cid = new_conversation(conn, NewConversation(title=item.title, workdir="", project_id=pid,
                                                                 model="default", permission_mode="manual"), user)
                    importer.write_messages(conn, cid, [item], merged=False)
                    conn.execute("UPDATE conversations SET instructions = ? WHERE id = ?",
                                 (store.check_instructions(entry["instructions"][:4000]), cid))
                    made["conversations"] += 1
                    if entry["note"] and copy_notes and (user is None or user.get("is_owner")):
                        notes.append((cid, item.title, entry["note"]))
                for doc_name, content in bundle["documents"]:
                    try:
                        rag.add(conn, pid, doc_name, content)
                        made["documents"] += 1
                    except StoreError as exc:
                        made["skipped"].append(f"document « {doc_name} » : {exc}")
            # The base is saved: now the notes, each one never over a note that is there.
            for cid, title, body in notes:
                try:
                    vault = obsidian.vault_of(conn, store.get_conversation(conn, cid), current().obsidian_vault)
                    if not vault:
                        made["skipped"].append(f"note de « {title} » : aucun coffre configuré")
                        continue
                    note = obsidian.default_note_path(title)
                    if obsidian.resolve(vault, note).exists():
                        note = obsidian.free_note_path(vault, note)
                    path = obsidian.resolve(vault, note)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "x", encoding="utf-8", newline="\n") as fh:
                        fh.write(body)
                    store.set_obsidian_note(conn, cid, note)
                    made["notes"] += 1
                except (obsidian.ObsidianError, OSError) as exc:
                    made["skipped"].append(f"note de « {title} » : {exc}")
            made["project"] = dict(store.get_project(conn, pid))
        return made

    @app.post("/api/projects/import", status_code=201)
    async def import_project(request: HttpRequest, workdir: str = "", notes: bool = False):
        """A project's .zip (made by « Exporter le projet »), as the request's body."""
        user = me(request)
        access.require_creator(user)
        if int(request.headers.get("content-length") or 0) > project_export.MAX_ZIP:
            raise StoreError(f"fichier trop gros ({project_export.MAX_ZIP // 1024 // 1024} Mo au plus)")
        data = await request.body()
        try:
            bundle = await asyncio.to_thread(project_export.read, data)
        except project_export.ProjectFileError as exc:
            raise StoreError(str(exc)) from None
        return await asyncio.to_thread(import_project_from, bundle, user, workdir, notes)

    @app.post("/api/projects/import/pc", status_code=201)
    def import_project_pc(body: PcFile, request: HttpRequest, workdir: str = "", notes: bool = False):
        """The same .zip, as a file of the PC chosen in the app's explorer."""
        user = me(request)
        access.require_creator(user)
        path = pc_file(user, body.path, extensions={".zip"}, max_size=project_export.MAX_ZIP)
        try:
            bundle = project_export.read(path.read_bytes())
        except project_export.ProjectFileError as exc:
            raise StoreError(str(exc)) from None
        return import_project_from(bundle, user, workdir, notes)

    @app.get("/api/conversations/{cid}")
    def conversation(cid: int, request: HttpRequest, last: int | None = None):
        """`last`: only the last messages (`older` says how many come before them)."""
        if last is not None and not 1 <= last <= 5000:
            raise StoreError("last : de 1 à 5000 messages")
        user = me(request)
        with database() as conn:
            conv = dict(access.require(conn, user, cid))
            conv['isolation'] = isolation.effective(conn, conv)
            conv["my_role"] = access.conversation_role(conn, user, conv)
            conv["notes_vault"] = obsidian.vault_of(conn, conv, current().obsidian_vault)
            conv.update(library.preferences(conn, (user or {}).get("id")).get(cid, {"pinned": False, "archived": False}))
            names = {u["id"]: u["name"] for u in auth.list_users(conn)}
            agent = store.get_agent(conn, conv["agent_id"]) if conv["agent_id"] else None
            conductor = (store.get_agent(conn, conv["conductor_agent_id"])
                         if conv["conductor_agent_id"] else None)
            messages = [dict(m) for m in store.list_messages(conn, cid, last)]
            older = store.count_messages(conn, cid) - len(messages) if last is not None else 0
            for m in messages:  # who asked, when several people share the conversation
                m["author"] = names.get(m.get("user_id")) if m.get("user_id") else None
            plans = store.list_plans(conn, cid)
            runs = autonomous.list_runs(conn, cid)
            delegations = [dict(r) for r in conn.execute(
                "SELECT * FROM delegations WHERE conversation_id = ? ORDER BY id", (cid,))]
            shared_with = access.shares_of(conn, conversation_id=cid) if conv["my_role"] == "owner" else None
            files = attachments.of_conversation(conn, cid)
            others = compare.of_conversation(conn, cid)
            for m in messages:  # the files each request took, the other AIs' answers to it
                m["attachments"] = files.get(m["id"], [])
                m["variants"] = others.get(m["id"], [])
        conv["model"] = agent["model"] if agent else None
        conv["conductor_model"] = conductor["model"] if conductor else None
        conv["tools"] = tools.chosen(conv)
        pending = [dict(p, can_answer=jobs.may_answer(p["id"], user)) for p in jobs.pending(cid)]
        return {"conversation": conv, "tools_offered": tools.offered(current()), "messages": messages, "older": older,
                "plans": plans,
                "autonomous": runs, "delegations": delegations, "shared_with": shared_with,
                "running": jobs.running(cid), "running_kind": jobs.kind(cid), "pending": pending}

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
        """A plan can be edited only while nothing runs in its conversation, by who may write there."""
        try:
            plan = store.get_plan(conn, plan_id)
        except NotFound:
            raise NotFound("plan introuvable") from None
        try:
            access.require(conn, me(), plan["conversation_id"], "write")
        except NotFound:
            raise NotFound("plan introuvable") from None
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
                    routing.follow_kind(conn, plan["id"], sid, connected=connected_for(me()))
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
                routing.auto_assign(conn, pid, connected=connected_for(me()))
            return {"plan": store.get_plan(conn, pid)}

    @app.post("/api/plans/{pid}/adapt", status_code=202)
    def adapt(pid: int):
        with database() as conn:
            plan = editable_plan(conn, pid)
            if plan["status"] != "proposed":
                raise StoreError("le plan est validé : il ne peut plus être modifié")
        return {"message_id": jobs.start_adapt(plan["conversation_id"], pid, user=me())}

    @app.post("/api/plans/{pid}/launch", status_code=202)
    def launch(pid: int):
        """Nothing runs before this: validate the plan, then run it."""
        with database() as conn:
            plan = editable_plan(conn, pid)
            store.validate_plan(conn, pid)
        try:
            jobs.start_execution(plan["conversation_id"], pid, user=me())  # runs with the launcher's accounts
        except (StoreError, ProviderError):
            with database() as conn:
                if store.get_plan(conn, pid)["status"] == "validated":
                    store.stop_plan(conn, pid)
            raise
        return {"plan_id": pid}

    @app.post('/api/plans/{pid}/resume', status_code=202)
    def resume_plan(pid: int):
        user = me()
        with database() as conn:
            plan = editable_plan(conn, pid)
            conv = access.require(conn, user, plan['conversation_id'], 'write')
            if not store.can_resume_plan(conn, plan):
                raise StoreError('ce plan ne peut pas être repris')
            check_workdir(conv['workdir'])
            isolation.for_run(conn, conv, account_id=(user or {}).get('id'), settings=current())
        return {'plan_id': jobs.resume_execution(plan['conversation_id'], pid, user=user)}

    @app.post('/api/plans/{pid}/auto-resume')
    def auto_resume(pid: int, body: AutoResume):
        """A plan a quota stopped: resumed by itself later (when the quota comes back), or not any more."""
        user = me()
        with database() as conn:
            plan = editable_plan(conn, pid)
            if body.cancel:
                store.cancel_resume(conn, pid)
            else:
                at = body.at
                if at is None:
                    back = (plan["quota"] or {}).get("resets_at")
                    if back is None:
                        raise StoreError("l'heure de retour du quota n'est pas connue : choisissez une heure")
                    at = (datetime.fromisoformat(back) + store.RESUME_MARGIN).isoformat()
                store.schedule_resume(conn, pid, at, (user or {}).get("id"))
            plan = store.get_plan(conn, pid)
        jobs.hub.publish(plan["conversation_id"], {"type": "plan_updated", "plan_id": pid})
        return {"plan": plan}

    @app.post('/api/plans/{pid}/switch')
    def switch_plan(pid: int, body: SwitchPlan):
        """The rest of a validated, running or interrupted plan on one AI (« Passer la suite sur … »).

        Tasks running or done keep their AI; an interrupted plan resumes at once."""
        user = me()
        with database() as conn:
            try:
                plan = store.get_plan(conn, pid)
            except NotFound:
                raise NotFound("plan introuvable") from None
            conv = access.require(conn, user, plan["conversation_id"], "write")
            agent = agent_for(conn, body.model, isolated=isolation.effective(conn, conv)["isolated"])
            moved = store.switch_plan(conn, pid, agent)
            if body.conductor:
                store.set_conductor(conn, conv["id"], agent)
            interrupted = plan["status"] != "running" and store.can_resume_plan(conn, store.get_plan(conn, pid))
            if interrupted:
                check_workdir(conv["workdir"])
                isolation.for_run(conn, conv, account_id=(user or {}).get("id"), settings=current())
        resume_now = interrupted and not jobs.running(plan["conversation_id"])
        if resume_now:
            jobs.resume_execution(plan["conversation_id"], pid, user=user)
        jobs.hub.publish(plan["conversation_id"], {"type": "plan_updated", "plan_id": pid})
        with database() as conn:
            return {"plan": store.get_plan(conn, pid), "moved": moved, "resumed": resume_now}

    @app.post('/api/plans/{pid}/give', status_code=202)
    def give_plan(pid: int, body: GiveTo):
        """The tasks a quota stopped go to another AI, and the plan resumes at once."""
        user = me()
        with database() as conn:
            plan = editable_plan(conn, pid)
            conv = access.require(conn, user, plan["conversation_id"], "write")
            agent = agent_for(conn, body.model, isolated=isolation.effective(conn, conv)["isolated"])
            moved = store.give_to(conn, pid, agent)
            check_workdir(conv["workdir"])
            isolation.for_run(conn, conv, account_id=(user or {}).get("id"), settings=current())
        jobs.resume_execution(plan["conversation_id"], pid, user=user)
        return {"plan_id": pid, "moved": moved}

    @app.post("/api/plans/{pid}/cancel")
    def cancel(pid: int):
        with database() as conn:
            plan = editable_plan(conn, pid)
            if plan["status"] != "proposed":
                raise StoreError("seul un plan proposé peut être annulé ; un plan lancé s'arrête avec « Arrêter »")
            store.stop_plan(conn, pid)
            stopped = store.get_plan(conn, pid)
        jobs.hub.publish(stopped["conversation_id"], {"type": "plan_updated", "plan_id": pid})
        return {"plan": stopped}

    @app.patch("/api/conversations/{cid}")
    def change_conversation(cid: int, body: ConversationChange):
        with database() as conn:
            user = me()
            access.require(conn, user, cid, "owner")  # its folder, mode, AI, tools, sharing: the owner's
            if not access.trusted(user):
                # A member, in a conversation of theirs: nothing that reaches beyond their space.
                if body.permission_mode == "bypass" and not isolation.effective(conn, store.get_conversation(conn, cid))['isolated']:
                    raise access.Forbidden("le mode sans permission est réservé au propriétaire et aux admins")
                if body.tools is not None and set(body.tools) - set(MEMBER_TOOLS):
                    raise access.Forbidden("seuls la recherche web et l'avis d'une autre IA sont permis ici")
                if body.obsidian is not None:
                    raise access.Forbidden("le coffre Obsidian est celui du propriétaire")
                if body.project_id is not None and access.project_role(conn, user, body.project_id) != "owner":
                    raise access.Forbidden("une conversation va dans un projet à soi")
            if body.title is not None:
                store.rename_conversation(conn, cid, body.title)
            if body.instructions is not None:
                conn.execute("UPDATE conversations SET instructions = ? WHERE id = ?",
                             (store.check_instructions(body.instructions), cid))
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
                agent_id = agent_for(conn, body.model, isolated=isolation.effective(conn, store.get_conversation(conn, cid))['isolated'])
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
            if body.obsidian_vault is not None:
                # A folder of the owner's PC, like a project's vault: the owner's choice only.
                access.require_owner(user)
                vault = obsidian.check_project_vault(body.obsidian_vault, current().allowed_roots)
                if vault != store.get_conversation(conn, cid)["obsidian_vault"]:
                    store.set_conversation_vault(conn, cid, vault)
                    # The note of the former vault stays there; one is made in the new vault when linked.
                    store.set_obsidian_note(conn, cid, None)
            if body.obsidian is False:
                # The note stays in the vault; only the hub stops writing to it.
                store.set_obsidian_note(conn, cid, None)
            elif body.obsidian:
                conv = store.get_conversation(conn, cid)
                note = body.obsidian_note or conv["obsidian_note"] or obsidian.default_note_path(conv["title"])
                if note != conv["obsidian_note"]:
                    vault = obsidian.vault_of(conn, conv, current().obsidian_vault)
                    if obsidian.resolve(vault, note).exists():
                        # Another conversation's note, or an old one: the person says which.
                        if body.obsidian_existing == "new":
                            note = obsidian.free_note_path(vault, note)
                        elif body.obsidian_existing != "continue":
                            raise StoreError("une note de ce nom existe déjà : continuez-la ou créez-en une nouvelle")
                    conductor = (store.get_agent(conn, conv["conductor_agent_id"])["model"]
                                 if conv["conductor_agent_id"] else None)
                    obsidian.append(vault, note, conv["title"],
                                    obsidian.creation_section(conv, conductor))
                    store.set_obsidian_note(conn, cid, note)
            return {"conversation": _row(store.get_conversation(conn, cid))}

    @app.post("/api/messages/{mid}/obsidian")
    def note_message(mid: int):
        """Add one reply to the project note, at the user's request."""
        with database() as conn:
            message = store.get_message(conn, mid)
            conv = access.require(conn, me(), message["conversation_id"], "owner")
            if not access.trusted(me()):
                raise access.Forbidden("le coffre Obsidian est celui du propriétaire")
            if not conv["obsidian_note"]:
                raise StoreError("la note Obsidian n'est pas activée pour cette conversation")
            if message["role"] != "assistant" or message["status"] != "ok":
                raise StoreError("seule une réponse terminée peut être notée")
            if message["noted_at"]:
                raise StoreError("cette réponse est déjà dans la note")
            if conn.execute("SELECT 1 FROM plans WHERE synthesis_message_id = ?", (mid,)).fetchone():
                # The plan's own section already holds it, written when the plan ended.
                raise StoreError("la synthèse d'un plan va dans la note avec son plan ; "
                                 "« Réécrire la note » l'y remet si elle manque")
            before = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND id < ? AND role = 'user' "
                "ORDER BY id DESC LIMIT 1", (conv["id"], mid)).fetchone()
            path = obsidian.append(obsidian.vault_of(conn, conv, current().obsidian_vault), conv["obsidian_note"], conv["title"],
                                   obsidian.reply_section(before["content"] if before else None,
                                                          message["content"]))
            store.mark_noted(conn, mid)
        return {"note": conv["obsidian_note"], "path": str(path)}

    @app.get("/api/conversations/{cid}/obsidian/check")
    def check_note(cid: int, note: str | None = None):
        """Whether the note's file exists, which other conversations write to it, and a free name."""
        with database() as conn:
            conv = access.require(conn, me(), cid, "owner")
            if not access.trusted(me()):
                raise access.Forbidden("le coffre Obsidian est celui du propriétaire")
            vault = obsidian.vault_of(conn, conv, current().obsidian_vault)
            path = note or conv["obsidian_note"] or obsidian.default_note_path(conv["title"])
            exists = obsidian.resolve(vault, path).exists()
            users = [row["title"] for row in conn.execute(
                "SELECT id, title, project_id, obsidian_vault FROM conversations WHERE obsidian_note = ? AND id <> ?",
                (path, cid))
                if obsidian.vault_of(conn, row, current().obsidian_vault) == vault]
        return {"note": path, "vault": vault, "exists": exists, "used_by": users,
                "free": obsidian.free_note_path(vault, path) if exists else path}

    @app.post("/api/conversations/{cid}/obsidian/rewrite")
    def rewrite_note(cid: int):
        """Rebuild the whole project note; the previous one is backed up outside the vault."""
        with database() as conn:
            access.require(conn, me(), cid, "owner")
            if not access.trusted(me()):
                raise access.Forbidden("le coffre Obsidian est celui du propriétaire")
        if jobs.running(cid):
            raise Busy("une requête est en cours : attendez qu'elle ait fini pour réécrire la note")
        path, backup = jobs.rewrite_note(cid)
        return {"path": str(path), "backup": str(backup) if backup else None}

    @app.delete("/api/conversations/{cid}")
    def delete_conversation(cid: int):
        # The store refuses to delete a conversation whose reply is running.
        with database() as conn:
            access.require(conn, me(), cid, "owner")
            store.delete_conversation(conn, cid)
        attachments.forget_conversation(pieces, cid)  # its files go with it
        # Deleted for good means from the backups too.
        errors = backup.purge_conversation(purge_folder(), cid)
        return {"deleted": cid, "backup_errors": errors}

    # --- requests ---------------------------------------------------------

    @app.post("/api/conversations/{cid}/messages", status_code=202)
    def send(cid: int, body: NewMessage):
        user = me()
        with database() as conn:
            conv = access.require(conn, user, cid, "write")  # runs with the sender's own AI accounts
            if not conv["agent_id"]:
                raise StoreError("aucun agent n'est choisi pour cette conversation")
            agent_id = conv["agent_id"]
            files = attachments.pending(conn, cid, (user or {}).get("id"), body.attachments)
            isolation.for_run(conn, conv, account_id=(user or {}).get('id'), settings=current())
        if files and conv["mode"] == "orchestration":
            raise StoreError("les pièces jointes vont dans une conversation à une IA (pas encore à un chef d'orchestre)")
        if body.compare_with:
            if conv["mode"] != "single":
                raise StoreError("on compare deux IA dans une conversation à une IA")
            if body.compare_with not in offered_models(current()):
                raise StoreError(f"IA non proposée ici : {body.compare_with!r}")
        if conv["mode"] == "orchestration":
            if body.kind == "discuss":
                return {"message_id": jobs.start_discussion(cid, body.prompt, user=user)}
            if body.kind != "plan":
                raise StoreError(f"genre de message inconnu : {body.kind!r} (attendu : plan, discuss)")
            # The conductor proposes a plan; nothing else runs before it is validated.
            return {"message_id": jobs.start_planning(cid, body.prompt, user=user)}
        return {"message_id": jobs.start(cid, agent_id, body.prompt, user=user, attachments=files,
                                         compare_with=body.compare_with)}

    @app.post("/api/variants/{vid}/keep")
    def keep_variant(vid: int):
        """« Garder celle-ci »: the other AI's answer becomes the reply (the next request starts from the memory)."""
        with database() as conn:
            row = conn.execute("SELECT conversation_id FROM variants WHERE id = ?", (vid,)).fetchone()
            if row is None:
                raise NotFound("réponse introuvable")
            access.require(conn, me(), row["conversation_id"], "write")
            if jobs.running(row["conversation_id"]):
                raise Busy("une requête est en cours : attendez qu'elle ait fini")
            compare.keep(conn, vid)
        return {"kept": vid}

    # --- regenerate the last reply, branch at a request ------------------------

    @app.post("/api/conversations/{cid}/regenerate", status_code=202)
    def regenerate(cid: int, body: Regenerate):
        """The last request sent again (its files too), the previous reply gone; with another AI if chosen."""
        user = me()
        with database() as conn:
            conv = access.require(conn, user, cid, "write")
            if isolation.suspended(conn, cid):
                raise StoreError('accès suspendus : réactivez cet espace avant de régénérer une réponse')
            if conv["mode"] != "single":
                raise StoreError("on régénère dans une conversation à une IA")
            if jobs.running(cid):
                raise Busy("une requête est en cours : attendez qu'elle ait fini")
            last = store.last_exchange(conn, cid)
            if last is None:
                raise StoreError("rien à régénérer : la conversation ne finit pas par une réponse")
            request, reply = last
            if conn.execute("SELECT 1 FROM autonomous_rounds WHERE message_id = ?", (reply["id"],)).fetchone():
                raise StoreError("une réponse du travail autonome ne se régénère pas")
            if body.model and body.model != (store.get_agent(conn, conv["agent_id"])["model"] if conv["agent_id"] else None):
                if access.conversation_role(conn, user, conv) != "owner":
                    raise access.Forbidden("seul celui qui possède la conversation change son IA")
                store.update_conversation(conn, cid, agent_id=agent_for(conn, body.model))
            agent_id = store.get_conversation(conn, cid)["agent_id"]
            files = [dict(r) for r in conn.execute("SELECT * FROM attachments WHERE message_id = ?", (request["id"],))]
            conn.execute("UPDATE attachments SET message_id = NULL, user_id = ? WHERE message_id = ?",
                         ((user or {}).get("id"), request["id"]))
            store.forget_exchange(conn, request["id"], reply["id"])
            files = attachments.pending(conn, cid, (user or {}).get("id"), [f["id"] for f in files])
        return {"message_id": jobs.start(cid, agent_id, request["content"], user=user, attachments=files)}

    @app.post('/api/messages/{mid}/resume', status_code=202)
    def resume_message(mid: int):
        user = me()
        with database() as conn:
            reply = store.get_message(conn, mid)
            conv = access.require(conn, user, reply['conversation_id'], 'write')
            latest = conn.execute('SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1', (conv['id'],)).fetchone()
            if reply['role'] != 'assistant' or reply['status'] not in ('stopped', 'error') or latest['id'] != mid:
                raise StoreError('seule la dernière réponse interrompue peut être reprise')
            request = conn.execute("SELECT * FROM messages WHERE conversation_id = ? AND id < ? AND role = 'user' ORDER BY id DESC LIMIT 1", (conv['id'], mid)).fetchone()
            if request is None:
                raise StoreError('demande initiale introuvable')
            check_workdir(conv['workdir'])
            isolation.for_run(conn, conv, account_id=(user or {}).get('id'), settings=current())
            prompt = resumption.prompt(request['content'], reply['content'], reply['error'])
            if conv['mode'] == 'orchestration':
                if conn.execute('SELECT 1 FROM plans WHERE synthesis_message_id = ?', (mid,)).fetchone():
                    raise StoreError('reprenez la synthèse avec le bouton Reprendre de son plan')
                agent_id = None
                files = []
            else:
                agent_id = conv['agent_id']
                files = [dict(row) for row in conn.execute('SELECT * FROM attachments WHERE message_id = ?', (request['id'],))]
        if agent_id is None and conv['mode'] == 'orchestration':
            return {'message_id': jobs.start_planning(conv['id'], prompt, user=user)}
        return {'message_id': jobs.start(conv['id'], agent_id, prompt, user=user, attachments=files,
                                         preserve_attachments=True, resume_message_id=mid)}

    @app.post("/api/messages/{mid}/branch", status_code=201)
    def branch(mid: int, body: Branch):
        """A new conversation from a request: what came before it copied, then the corrected request sent.
        The original stays as it was."""
        user = me()
        access.require_creator(user)
        with database() as conn:
            message = store.get_message(conn, mid)
            conv = dict(access.require(conn, user, message["conversation_id"], "write"))
            if isolation.suspended(conn, conv['id']):
                raise StoreError('accès suspendus : réactivez cet espace avant de créer une branche')
            if message["role"] != "user":
                raise StoreError("on modifie une demande, pas une réponse")
            if conv["mode"] != "single":
                raise StoreError("on modifie une demande dans une conversation à une IA")
            model = store.get_agent(conn, conv["agent_id"])["model"] if conv["agent_id"] else "default"
            title = f"{conv['title']} (branche)"[:120]
            mode = conv["permission_mode"] if conv["permission_mode"] != "bypass" or access.trusted(user) else "manual"
            cid = new_conversation(conn, NewConversation(
                title=title, workdir=conv["workdir"], model=model, permission_mode=mode,
                confirm_bypass=mode == "bypass",
                project_id=conv["project_id"] if conv["project_id"] is not None
                and access.project_role(conn, user, conv["project_id"]) == "owner" else None), user)
            try:
                with db.transaction(conn):
                    policy = isolation.effective(conn, conv)
                    if policy['isolated']:
                        isolation.save(conn, policy, conversation_id=cid)
                    copied = store.copy_messages(conn, message["conversation_id"], cid, mid, with_ids=True)
                    conn.execute("UPDATE conversations SET instructions = ?, tools_json = ?, rag_budget = ?, "
                                 "shared = ? WHERE id = ?",
                                 (conv["instructions"], '[]' if policy['isolated'] else conv["tools_json"] if access.trusted(user) else
                                  json.dumps([name for name in tools.chosen(conv) if name in MEMBER_TOOLS]),
                                  conv["rag_budget"], conv["shared"], cid))
                    files = attachments.copy_for_branch(conn, pieces, message["conversation_id"], cid, mid,
                                                        copied, (user or {}).get("id"))
            except Exception:
                store.delete_conversation(conn, cid)
                attachments.forget_conversation(pieces, cid)
                raise
            agent_id = store.get_conversation(conn, cid)["agent_id"]
        prompt = (body.prompt or "").strip() or message["content"]
        jobs.start(cid, agent_id, prompt, user=user, attachments=files)
        return {"conversation": cid}

    # --- files and images sent with a request (aihub/attachments.py) -------------

    pieces = side_folder(db_path, "pieces")

    @app.post("/api/conversations/{cid}/attachments", status_code=201)
    async def upload_attachment(cid: int, request: HttpRequest):
        """A file for the next request (the body is the file; X-Filename its name, URL-encoded)."""
        from urllib.parse import unquote
        user = me(request)
        size = int(request.headers.get("content-length") or 0)
        if size > attachments.MAX_SIZE:
            raise StoreError(f"fichier trop gros ({attachments.MAX_SIZE // 1024 // 1024} Mo au plus)")
        with database() as conn:
            access.require(conn, user, cid, "write")
        data = await request.body()
        name = unquote(request.headers.get("x-filename") or "fichier")
        declared = request.headers.get("content-type")

        def keep():
            with database() as conn:
                return attachments.view(attachments.save(conn, pieces, cid, (user or {}).get("id"), name, data,
                                                         declared))
        return await asyncio.to_thread(keep)

    @app.post("/api/conversations/{cid}/attachments/pc", status_code=201)
    def attach_pc_file(cid: int, body: PcFile):
        """A file of the PC for the next request, chosen in the app's explorer (not sent from a device)."""
        user = me()
        with database() as conn:
            access.require(conn, user, cid, "write")
        path = pc_file(user, body.path, max_size=attachments.MAX_SIZE)
        data = path.read_bytes()
        with database() as conn:
            return attachments.view(attachments.save(conn, pieces, cid, (user or {}).get("id"), path.name, data))

    @app.delete("/api/attachments/{aid}")
    def remove_attachment(aid: int):
        with database() as conn:
            attachments.remove(conn, aid, my_id())
        return {"removed": aid}

    @app.get("/api/attachments/{aid}")
    def attachment_file(aid: int):
        """A file, to whoever sees its conversation: a picture shown, anything else downloaded."""
        from urllib.parse import quote
        with database() as conn:
            row = attachments.get(conn, aid)
            access.require(conn, me(), row["conversation_id"])
        if not Path(row["stored"]).is_file():
            raise NotFound("fichier introuvable")
        image = row["mime"] in attachments.IMAGES
        disposition = "inline" if image else "attachment"
        return FileResponse(row["stored"], media_type=row["mime"] if image else "application/octet-stream",
                            headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row['name'])}",
                                     "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})

    # --- autonomous work --------------------------------------------------------

    @app.post("/api/conversations/{cid}/autonomous", status_code=202)
    def start_autonomous(cid: int, body: AutonomousStart):
        """The AI pursues the goal alone, round after round, within the limits."""
        user = me()
        with database() as conn:
            conv = access.require(conn, user, cid, "write")
            if body.check_command and not access.trusted(user):
                # It runs on the server without the agent's sandbox: the owner's alone.
                raise access.Forbidden("une commande de vérification est réservée au propriétaire et aux admins")
        if conv["mode"] != "single":
            raise StoreError("le travail autonome se lance dans une conversation à une IA")
        if not conv["agent_id"]:
            raise StoreError("aucun agent n'est choisi pour cette conversation")
        run_id = jobs.start_autonomous(cid, conv["agent_id"], body.goal, max_rounds=body.max_rounds,
                                       max_minutes=body.max_minutes, check_command=body.check_command,
                                       confirm_check=body.confirm_check, user=user)
        return {"run_id": run_id}

    # --- scheduled tasks ----------------------------------------------------------

    @app.get("/api/schedules")
    def schedules(conversation_id: int | None = None):
        # Scheduled tasks run with the owner's accounts: the owner's to see and to set.
        access.require_owner(me())
        with database() as conn:
            return {"schedules": schedule.list_all(conn, conversation_id)}

    @app.post("/api/schedules", status_code=201)
    def add_schedule(body: NewSchedule):
        access.require_owner(me())
        with database() as conn:
            sid = schedule.create(conn, body.conversation_id, body.prompt, kind=body.kind, every=body.every,
                                  interval_minutes=body.interval_minutes, at_time=body.at_time,
                                  weekdays=body.weekdays, max_rounds=body.max_rounds, max_minutes=body.max_minutes,
                                  check_command=body.check_command, confirm_check=body.confirm_check,
                                  now=app.state.scheduler.clock())
            return {"schedule": schedule.get(conn, sid)}

    @app.patch("/api/schedules/{sid}")
    def change_schedule(sid: int, body: ScheduleChange):
        access.require_owner(me())
        with database() as conn:
            return {"schedule": schedule.set_enabled(conn, sid, body.enabled, now=app.state.scheduler.clock())}

    @app.delete("/api/schedules/{sid}")
    def delete_schedule(sid: int):
        access.require_owner(me())
        with database() as conn:
            schedule.delete(conn, sid)
        return {"deleted": sid}

    @app.post("/api/schedules/{sid}/run", status_code=202)
    def run_schedule_now(sid: int):
        """Run a task now, outside its rule (its next run stays as it was)."""
        access.require_owner(me())
        with database() as conn:
            task = schedule.get(conn, sid)
        launch_task(task)
        return {"started": sid}

    # --- memory: fresh start and search ---------------------------------------

    def summary_ai(conv, conn):
        s = current()
        return runner.summarizer(commands, conv["workdir"], s.summary_model, env=people_accounts.env_for(me()),
                                 execution=isolation.for_run(conn, conv, account_id=(me() or {}).get('id'),
                                                             read_only=True, settings=s))

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
            conv = access.require(conn, me(), cid, "write")
            budget = conv["rag_budget"] or s.rag_budget
            if isolation.suspended(conn, cid):
                raise StoreError('accès suspendus : réactivez cet espace avant de préparer une nouvelle session')
            summarize = summary_ai(conv, conn)
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
            access.require(conn, me(), cid, "write")
            memory.store_package(conn, cid, None)
        return {"ready": False}

    @app.get("/api/search")
    def search(q: str = "", limit: int = 30, project_id: int | None = None, model: str | None = None,
               after: str | None = None, before: str | None = None, archived: bool | None = None):
        try:
            start = datetime.strptime(after, "%Y-%m-%d").date().isoformat() if after else None
            end = (datetime.strptime(before, "%Y-%m-%d") + timedelta(days=1)).date().isoformat() if before else None
        except ValueError:
            raise StoreError("dates : au format AAAA-MM-JJ") from None
        if start and end and start >= end:
            raise StoreError("la date de début doit précéder la date de fin")
        with database() as conn:
            user = me()
            visible = access.visible_conversation_ids(conn, user)
            if archived is not None:
                prefs = library.preferences(conn, (user or {}).get("id"))
                visible = {row["id"] for row in store.list_conversations(conn)
                           if (visible is None or row["id"] in visible) and
                           prefs.get(row["id"], {}).get("archived", False) == archived}
            found = memory.search(conn, q, limit=max(1, min(limit, 100)), conversation_ids=visible,
                                  project_id=project_id, model=model, after=start, before=end)
        return {"results": found}

    @app.post("/api/conversations/{cid}/stop")
    def stop(cid: int):
        with database() as conn:
            access.require(conn, me(), cid, "write")
        return {"stopped": jobs.stop(cid)}

    @app.post("/api/permissions/{permission_id}")
    def answer(permission_id: str, body: PermissionAnswer):
        # The person whose request it is answers it (or the owner); nobody else.
        if not jobs.answer(permission_id, allow=body.allow, message=body.message,
                           answers=body.answers, user=me()):
            return JSONResponse({"detail": "demande introuvable ou déjà traitée"}, 404)
        return {"answered": permission_id}

    @app.get("/api/conversations/{cid}/events")
    def events(cid: int, request: HttpRequest):
        with database() as conn:
            access.require(conn, me(), cid, "read")
        q = jobs.hub.subscribe(cid)
        snapshot = jobs.snapshot(cid)
        token = request.cookies.get(auth.COOKIE) if authority else None

        def can_read():
            with database() as conn:
                user = authority.check(conn, token) if authority else None
                if authority and user is None:
                    return False
                try:
                    access.require(conn, user, cid, 'read')
                    return True
                except (access.Forbidden, NotFound):
                    return False

        def stream():
            try:
                from .events import stream as live_stream
                yield from live_stream(q, snapshot, can_read, ping_s=sse_ping_s)
            finally:
                jobs.hub.unsubscribe(cid, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _sse(event):
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
