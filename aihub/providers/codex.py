"""Codex provider: drives the official CLI through its app server.

`codex exec` cannot ask anyone before running a command, so the hub talks
to `codex app-server` instead (JSON-RPC, one JSON message per line on
stdio), the protocol the Codex desktop app and editor extensions use. It
streams the reply, asks before a command or a file change, and reports the
quota, all measured on codex-cli 0.144.2.

The CLI signs in with its own login (the ChatGPT account of the Codex app).
This module never reads credentials, and answers no request that would
hand it a token.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .claude_code import (NO_WINDOW, Decision, PermissionRequest, ProviderError, QuotaInfo, RunResult,
                          _kill_tree, _local_time)

PROVIDER = "codex"
DISPLAY_NAME = "Codex"
# Agent model meaning "whatever Codex uses by default": no model sent.
DEFAULT_MODEL = "codex"

# AI Hub permission mode -> (sandbox, approval policy).
# manual: reads run in a read-only sandbox, anything else is asked first
#         (a command that writes, a file change: measured, both are asked);
# plan:   read-only, nothing asked, a refused write goes back to the model;
# bypass: no sandbox, nothing asked (the hub warns before this mode).
PERMISSION_MODES = {
    "manual": ("read-only", "on-request"),
    "plan": ("read-only", "never"),
    "bypass": ("danger-full-access", "never"),
}

# The user's own Codex extensions stay out of agents launched from the hub,
# like --strict-mcp-config for Claude Code: apps and plugins are switched
# off here, the MCP servers of config.toml one by one (_user_mcp_servers).
_DISABLED_FEATURES = ("apps", "plugins")

_STDERR_KEEP = 4000
_WARNING_FROM = 0.8
_CLIENT = {"name": "aihub", "version": "1"}


def find_cli(explicit=None, *, home=None, local_appdata=None):
    """Return the command that starts Codex, as a list.

    Order: the explicit path, `codex` on the PATH, then the newest of the
    copies the Codex desktop app keeps: %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<id>\\
    (app 26.1007, CLI 0.162) and ~/.codex/.sandbox-bin (older apps, left
    behind by the update: 0.144.2). The app's own codex.exe under
    WindowsApps cannot be started by another program (access denied).
    """
    if explicit:
        if not Path(explicit).is_file():
            raise ProviderError(f"Codex introuvable à l'emplacement indiqué : {explicit}")
        return [str(explicit)]
    on_path = shutil.which("codex")
    if on_path:
        return [on_path]
    name = "codex.exe" if os.name == "nt" else "codex"
    candidates = [(home or codex_home()) / ".sandbox-bin" / name]
    local = local_appdata or os.environ.get("LOCALAPPDATA")
    if local:
        candidates += Path(local, "OpenAI", "Codex", "bin").glob(f"*/{name}")
    found = [p for p in candidates if p.is_file()]
    if found:
        return [str(max(found, key=lambda p: p.stat().st_mtime))]
    raise ProviderError("Codex introuvable : installez l'application Codex ou indiquez le chemin de codex.exe")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _user_mcp_servers(home=None):
    """Names of the MCP servers of the user's config.toml (only the names are read)."""
    path = (home or codex_home()) / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcp_servers")
    return sorted(servers) if isinstance(servers, dict) else []


def server_args(command, home=None):
    """The app server's command line, without the user's extensions."""
    args = list(command) + ["app-server"]
    for feature in _DISABLED_FEATURES:
        args += ["--disable", feature]
    for name in _user_mcp_servers(home):
        key = name if _BARE_KEY.match(name) else json.dumps(name)
        args += ["-c", f"mcp_servers.{key}.enabled=false"]
    return args


def _iso_from_epoch(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _window_name(minutes):
    return {300: "five_hour", 10080: "seven_day"}.get(minutes, f"{minutes}_min" if minutes else "window")


def parse_rate_limits(snapshot, warning_from=_WARNING_FROM):
    """Turn a RateLimitSnapshot into a QuotaInfo, or None if unusable.

    Codex reports percents (12), stored as fractions (0.12) like Claude's.
    `rateLimitReachedType` set means a limit is reached.
    """
    if not isinstance(snapshot, dict):
        return None
    windows = {}
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        percent = window.get("usedPercent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or percent < 0:
            continue
        windows[_window_name(window.get("windowDurationMins"))] = {
            "utilization": percent / 100, "resets_at": _iso_from_epoch(window.get("resetsAt"))}
    if not windows:
        return None
    fullest = max(windows, key=lambda n: windows[n]["utilization"])
    worst = windows[fullest]["utilization"]
    if snapshot.get("rateLimitReachedType") or worst >= 1:
        status = "rejected"
    elif worst >= warning_from:
        status = "allowed_warning"
    else:
        status = "allowed"
    return QuotaInfo(status=status, limit_type=fullest, utilization=worst,
                     resets_at=windows[fullest]["resets_at"], windows=windows)


def _error_text(error):
    """The readable part of a TurnError: its message may be a whole JSON error."""
    if not isinstance(error, dict):
        return "", None
    message = str(error.get("message") or "")
    info = error.get("codexErrorInfo")
    kind = info if isinstance(info, str) else (next(iter(info)) if isinstance(info, dict) and info else None)
    try:
        inner = json.loads(message)
        message = str(((inner.get("error") or {}).get("message")) or message)
    except (ValueError, AttributeError):
        pass
    details = error.get("additionalDetails")
    if details and str(details) not in message:
        message = f"{message} ({details})" if message else str(details)
    return message, kind


def describe_error(text, *, kind=None, quota=None, model=None):
    """A clear message for a failed run, naming the AI when its quota is reached."""
    low = (text or "").lower()
    if kind == "unauthorized" or "not logged in" in low or ("login" in low and "required" in low):
        return (f"{DISPLAY_NAME} n'est connecté à aucun compte. Ouvrez l'application Codex et "
                "connectez-vous (ou lancez « codex login »), puis réessayez "
                "(une personne invitée : menu « Mes comptes IA »).")
    if kind == "usageLimitExceeded" or (quota is not None and quota.status == "rejected") or "usage limit" in low:
        who = f"{DISPLAY_NAME} ({model})" if model else DISPLAY_NAME
        when = _local_time(quota.resets_at) if quota and quota.resets_at else None
        return f"Quota de {who} atteint" + (f" : retour prévu le {when}." if when else ".")
    if "requires a newer version of codex" in low:
        who = f"Le modèle {model}" if model else "Ce modèle"
        return f"{who} demande une version plus récente de Codex : mettez à jour l'application Codex, puis réessayez."
    detail = (text or "").strip() or "erreur sans message"
    return f"{DISPLAY_NAME} a renvoyé une erreur : {detail}"


def lost_session(error):
    """The app server no longer has the thread to resume (deleted, other account, other PC)."""
    return "no rollout found" in (error or "").lower()


class _Server:
    """One app server process and its JSON-RPC line protocol."""

    def __init__(self, command, workdir, env, home=None):
        if home is None and env and env.get("CODEX_HOME"):
            home = Path(env["CODEX_HOME"])  # a person's own account (aihub/accounts.py)
        try:
            self.proc = subprocess.Popen(
                server_args(command, home), cwd=workdir, env=env if env is not None else dict(os.environ),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=NO_WINDOW, start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise ProviderError(f"impossible de lancer {DISPLAY_NAME} : {exc}") from None
        self.stderr = []
        self._next = 0
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self):
        size = 0
        for line in self.proc.stderr:
            self.stderr.append(line)
            size += len(line)
            while size > _STDERR_KEEP and len(self.stderr) > 1:
                size -= len(self.stderr.pop(0))

    def write(self, message):
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # the server is gone; the end of stdout will tell the rest

    def request(self, method, params=None):
        self._next += 1
        message = {"id": self._next, "method": method}
        if params is not None:
            message["params"] = params
        self.write(message)
        return self._next

    def notify(self, method):
        self.write({"method": method})

    def messages(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                yield message

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(self.proc)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def tail(self):
        return "".join(self.stderr).strip()[-500:]


def _ask_once(command, workdir, method, params=None, *, env=None, timeout_s=60, home=None):
    """Start the app server, make one request that costs no quota, return its result."""
    if not Path(workdir).is_dir():
        raise ProviderError(f"dossier introuvable : {workdir}")
    server = _Server(command, workdir, env, home)
    timer = threading.Timer(timeout_s, _kill_tree, args=(server.proc,))
    timer.daemon = True
    timer.start()
    reply = None
    try:
        first = server.request("initialize", {"clientInfo": _CLIENT})
        wanted = None
        for message in server.messages():
            if "method" in message:
                if "id" in message:  # nothing to ask anyone here
                    server.write({"id": message["id"], "error": {"code": -32601, "message": "non pris en charge"}})
                continue
            if message.get("id") == first:
                if "error" in message:
                    reply = message
                    break
                server.notify("initialized")
                wanted = server.request(method, params)
            elif wanted is not None and message.get("id") == wanted:
                reply = message
                break
    finally:
        timer.cancel()
        server.close()
    if reply is None:
        raise ProviderError(f"{DISPLAY_NAME} n'a pas répondu (code {server.proc.returncode})")
    if "error" in reply:
        detail = (reply.get("error") or {}).get("message") or "erreur sans message"
        raise ProviderError(f"{DISPLAY_NAME} a refusé la demande : {detail}")
    return reply.get("result")


def read_usage(command, *, workdir, env=None, timeout_s=60, warning_from=_WARNING_FROM, home=None):
    """Ask Codex for the current quota, without sending anything to the model."""
    result = _ask_once(command, workdir, "account/rateLimits/read", env=env, timeout_s=timeout_s, home=home)
    info = parse_rate_limits((result or {}).get("rateLimits"), warning_from)
    if info is None:
        raise ProviderError(f"{DISPLAY_NAME} ne donne pas de quota pour ce compte")
    return info


def list_models(command, *, workdir, env=None, timeout_s=60, home=None):
    """The models this Codex offers to this account (free: no model call)."""
    # model/list needs its params, even empty (the real CLI refuses it without:
    # « missing field `params` »); account/rateLimits/read takes none.
    result = _ask_once(command, workdir, "model/list", {}, env=env, timeout_s=timeout_s, home=home)
    data = (result or {}).get("data") if isinstance(result, dict) else None
    return [m["id"] for m in data or [] if isinstance(m, dict) and isinstance(m.get("id"), str)]


def login_status(command, *, env=None, timeout_s=60):
    """True when Codex is signed in. `codex login status` prints the method only."""
    try:
        proc = subprocess.run(list(command) + ["login", "status"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout_s, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW, env=env if env is not None else dict(os.environ))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProviderError(f"état de connexion de {DISPLAY_NAME} illisible : {exc}") from None
    text = (proc.stdout + proc.stderr).lower()
    return proc.returncode == 0 and "logged in" in text and "not logged in" not in text


def _command_text(params):
    """The command as the agent wrote it, without the shell wrapper Codex adds."""
    actions = params.get("commandActions")
    if isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], dict):
        command = actions[0].get("command")
        if isinstance(command, str) and command:
            return command
    return str(params.get("command") or "?")


def _changes_input(changes, reason):
    paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
    diffs = []
    for change in changes:
        if isinstance(change, dict):
            kind = (change.get("kind") or {}).get("type") if isinstance(change.get("kind"), dict) else None
            diffs.append(f"--- {change.get('path')} ({kind or 'modification'})\n{change.get('diff') or ''}")
    data = {"file_path": paths[0] if paths else "?", "file_paths": paths, "content": "\n".join(diffs)}
    if reason:
        data["description"] = reason
    return data


def _tool_name(item):
    kind = item.get("type")
    if kind == "commandExecution":
        return "Bash"
    if kind == "fileChange":
        return "Edit"
    if kind == "mcpToolCall":
        return f"mcp__{item.get('server') or '?'}__{item.get('tool') or '?'}"
    if kind == "webSearch":
        return "WebSearch"
    return None


class CodexRun:
    """One request to Codex. `run()` blocks; `stop()` may come from any thread.

    Same interface as ClaudeCodeRun: `on_text(chunk)`, `ask_permission(
    PermissionRequest) -> Decision`, `on_quota(QuotaInfo)`, and a RunResult.
    `tools` (any list, even empty) means read-only: Codex has no list of
    tools to allow, only its sandbox. `instructions` is added to the
    developer instructions (what --append-system-prompt is to Claude Code).
    """

    def __init__(self, command, *, prompt, workdir, permission_mode, model=None,
                 resume_session=None, ask_permission=None, on_text=None, on_quota=None,
                 timeout_s=None, env=None, tools=None, json_schema=None,
                 mcp_config=None, auto_allow=(), instructions=None, home=None,
                 warning_from=_WARNING_FROM, web=True):
        if permission_mode not in PERMISSION_MODES:
            raise ProviderError(f"mode de permission inconnu : {permission_mode!r}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("demande vide")
        self.command = list(command)
        self.prompt = prompt
        self.workdir = workdir
        self.permission_mode = permission_mode
        self.model = model
        self.resume_session = resume_session
        self.ask_permission = ask_permission
        self.on_text = on_text
        self.on_quota = on_quota
        self.timeout_s = timeout_s
        self.env = env
        self.tools = list(tools) if tools is not None else None
        self.json_schema = json_schema
        self.mcp_config = mcp_config
        self.auto_allow = list(auto_allow)
        self.instructions = instructions
        self.web = web  # Codex searches the web by default (0.162): off unless chosen
        self.home = home
        self.warning_from = warning_from
        self._lock = threading.Lock()
        self._server = None
        self._stop_reason = None  # "user" or "timeout"

    def thread_params(self):
        """Parameters of thread/start (or thread/resume, with the thread id)."""
        mode = "plan" if self.tools is not None else self.permission_mode
        sandbox, approval = PERMISSION_MODES[mode]
        params = {"cwd": str(self.workdir), "sandbox": sandbox, "approvalPolicy": approval}
        if self.model:
            params["model"] = self.model
        if self.instructions:
            params["developerInstructions"] = self.instructions
        servers = (self.mcp_config or {}).get("mcpServers") or {}
        config = {}
        if servers:
            config["mcp_servers"] = {name: {k: v for k, v in spec.items()
                                            if k in ("command", "args", "env", "tool_timeout_sec")}
                                     for name, spec in servers.items()}
        if not self.web:
            config["web_search"] = "disabled"  # measured: no web search tool left (codex-cli 0.162)
        if config:
            params["config"] = config
        if self.resume_session:
            params["threadId"] = self.resume_session
        return params

    def stop(self, reason="user"):
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
            server = self._server
        if server is not None:
            _kill_tree(server.proc)

    def run(self):
        if not self.workdir or not Path(self.workdir).is_dir():
            raise ProviderError(f"dossier de travail introuvable : {self.workdir}")
        result = RunResult(status="error", model=self.model)
        started = time.monotonic()
        with self._lock:
            if self._stop_reason is not None:
                result.status = "stopped"
                return result
            server = _Server(self.command, self.workdir, self.env, self.home)
            self._server = server
        timer = None
        if self.timeout_s:
            timer = threading.Timer(self.timeout_s, self.stop, kwargs={"reason": "timeout"})
            timer.daemon = True
            timer.start()
        outcome = {}
        try:
            self._converse(server, result, outcome)
        finally:
            if timer:
                timer.cancel()
            server.close()  # stdin closed: the server ends; killed if it does not within 10 s
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self._conclude(server, result, outcome)
        return result

    def _converse(self, server, result, outcome):
        """initialize, thread, turn: read every message until the turn ends."""
        ids = {"initialize": server.request("initialize", {"clientInfo": _CLIENT})}
        items = {}        # item id -> item, for the approval of a file change
        messages = []     # (phase, text) of the agent's messages
        streamed = []
        for message in server.messages():
            method = message.get("method")
            if method is not None and "id" in message:
                self._answer(server, message, items, result)
                continue
            if method is not None:
                if self._notification(method, message.get("params") or {}, items, messages, streamed, result,
                                      outcome):
                    break
                continue
            rid = message.get("id")
            if "error" in message:
                outcome["error"] = str((message.get("error") or {}).get("message") or "erreur sans message")
                break
            reply = message.get("result") or {}
            if rid == ids.get("initialize"):
                server.notify("initialized")
                start = "thread/resume" if self.resume_session else "thread/start"
                ids["thread"] = server.request(start, self.thread_params())
            elif rid == ids.get("thread"):
                thread = reply.get("thread") or {}
                result.session_id = thread.get("id") or self.resume_session
                result.model = reply.get("model") or result.model
                turn = {"threadId": result.session_id, "input": [{"type": "text", "text": self.prompt}]}
                if self.json_schema is not None:
                    turn["outputSchema"] = self.json_schema
                ids["turn"] = server.request("turn/start", turn)
        finals = [text for phase, text in messages if phase == "final_answer"]
        result.text = "\n\n".join(finals) if finals else (messages[-1][1] if messages else "".join(streamed))

    def _notification(self, method, params, items, messages, streamed, result, outcome):
        """Handle one notification; True when the turn is over."""
        if method == "item/agentMessage/delta":
            chunk = params.get("delta") or ""
            if chunk:
                if streamed and params.get("itemId") != outcome.get("streaming"):
                    chunk = "\n\n" + chunk  # a new message of the agent
                outcome["streaming"] = params.get("itemId")
                streamed.append(chunk)
                if self.on_text:
                    self.on_text(chunk)
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if isinstance(item, dict) and item.get("id"):
                items[item["id"]] = item
            if method == "item/completed" and isinstance(item, dict):
                if item.get("type") == "agentMessage":
                    messages.append((item.get("phase"), str(item.get("text") or "")))
                else:
                    name = _tool_name(item)
                    if name:
                        result.tool_calls.append(name)
        elif method == "thread/tokenUsage/updated":
            last = (params.get("tokenUsage") or {}).get("last") or {}
            for key, field in (("inputTokens", "input_tokens"), ("cachedInputTokens", "cached_input_tokens"),
                               ("outputTokens", "output_tokens"),
                               ("reasoningOutputTokens", "reasoning_output_tokens")):
                if isinstance(last.get(key), int):
                    result.usage[field] = result.usage.get(field, 0) + last[key]
            result.num_turns = (result.num_turns or 0) + 1
            result.tokens_in = result.usage.get("input_tokens")
            result.tokens_out = result.usage.get("output_tokens")
        elif method == "account/rateLimits/updated":
            quota = parse_rate_limits(params.get("rateLimits"), self.warning_from)
            if quota is not None:
                result.quota = quota
                if self.on_quota:
                    self.on_quota(quota)
        elif method == "error" and not params.get("willRetry"):
            outcome["turn_error"] = params.get("error")
        elif method == "turn/completed":
            outcome["turn"] = params.get("turn") or {}
            return True
        return False

    def _answer(self, server, message, items, result):
        """Answer a request of the server: approvals go to the user, the rest is declined."""
        method, rid, params = message["method"], message["id"], message.get("params") or {}
        if method == "item/commandExecution/requestApproval":
            data = {"command": _command_text(params)}
            if params.get("reason"):
                data["description"] = str(params["reason"])
            decision = self._decide("Bash", data, rid, result)
            server.write({"id": rid, "result": {"decision": "accept" if decision.allow else "decline"}})
        elif method == "item/fileChange/requestApproval":
            changes = (items.get(params.get("itemId")) or {}).get("changes") or []
            decision = self._decide("Edit", _changes_input(changes, params.get("reason")), rid, result)
            server.write({"id": rid, "result": {"decision": "accept" if decision.allow else "decline"}})
        elif method == "mcpServer/elicitation/request":
            # The read-only servers (the hub's, or marked so in the catalog) may be allowed; any other is declined.
            prefix = f"mcp__{params.get('serverName')}"
            allowed = any(name == prefix or name.startswith(prefix + "__") for name in self.auto_allow)
            server.write({"id": rid, "result": {"action": "accept" if allowed else "decline",
                                                **({"content": {}} if allowed else {})}})
        elif method == "item/permissions/requestApproval":
            # Wider sandbox rights: granted to no one (the mode chosen is the limit).
            result.permission_denials.append({"tool_name": "Permissions", "message": "droits supplémentaires refusés"})
            server.write({"id": rid, "result": {"permissions": {}, "scope": "turn"}})
        elif method in ("applyPatchApproval", "execCommandApproval"):
            server.write({"id": rid, "result": {"decision": "denied"}})
        else:
            # Among them account/chatgptAuthTokens/refresh: the hub never handles a token.
            server.write({"id": rid, "error": {"code": -32601, "message": f"non pris en charge : {method}"}})

    def _decide(self, tool_name, data, rid, result):
        ask = PermissionRequest(tool_name=tool_name, input=data, request_id=str(rid))
        if self.ask_permission is None:
            decision = Decision(False, "aucune personne n'est là pour valider : refus par défaut")
        else:
            try:
                decision = self.ask_permission(ask)
            except Exception as exc:  # a broken callback must not approve anything
                decision = Decision(False, f"validation impossible : {exc}")
            if not isinstance(decision, Decision):
                decision = Decision(False, "réponse de validation invalide : refus par défaut")
        if not decision.allow:
            result.permission_denials.append({"tool_name": tool_name,
                                              "message": decision.message or "refusé par l'utilisateur"})
        return decision

    def _conclude(self, server, result, outcome):
        with self._lock:
            reason = self._stop_reason
        if reason == "user":
            result.status, result.error = "stopped", None
            return
        if reason == "timeout":
            result.status = "error"
            result.error = f"{DISPLAY_NAME} n'a pas fini dans le délai de {self.timeout_s} s : requête arrêtée."
            return
        if "error" in outcome:
            result.status = "error"
            text = outcome["error"]
            result.error = text if lost_session(text) else describe_error(text, quota=result.quota,
                                                                          model=result.model)
            return
        turn = outcome.get("turn")
        if turn is None:
            tail = server.tail()
            result.status = "error"
            result.error = (f"{DISPLAY_NAME} s'est arrêté sans réponse (code {server.proc.returncode})"
                            + (f" : {tail}" if tail else "."))
            return
        if turn.get("status") != "completed":
            text, kind = _error_text(turn.get("error") or outcome.get("turn_error"))
            result.status = "error"
            if turn.get("status") == "interrupted" and not text:
                text = "tour interrompu"
            result.error = describe_error(text, kind=kind, quota=result.quota, model=result.model)
            return
        if self.json_schema is not None:
            try:
                parsed = json.loads(result.text)
            except ValueError:
                parsed = None
            result.structured = parsed if isinstance(parsed, dict) else None
        result.status = "ok"
