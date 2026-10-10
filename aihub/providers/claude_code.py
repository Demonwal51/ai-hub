"""Claude Code provider: drives the official CLI in print mode.

The CLI speaks stream-json both ways. The prompt goes in as a user message,
the reply comes back as events (streamed text, quota, result), and permission
prompts reach us as control requests that we answer on stdin.

The CLI signs in with its own login. This module never reads credentials.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROVIDER = "claude_code"
DISPLAY_NAME = "Claude Code"
# Agent model meaning "whatever Claude Code uses by default": no --model flag.
DEFAULT_MODEL = "default"

# No console window for the CLI on Windows.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# AI Hub permission mode -> Claude Code --permission-mode.
PERMISSION_MODES = {"manual": "manual", "plan": "plan", "bypass": "bypassPermissions"}

_STDERR_KEEP = 4000
WEB_TOOLS = ("WebSearch", "WebFetch")  # Claude Code's own web tools


class ProviderError(Exception):
    """The provider cannot run the request; the message is fit for the user."""


@dataclass
class PermissionRequest:
    tool_name: str
    input: dict
    request_id: str
    suggestions: list = field(default_factory=list)


@dataclass
class Decision:
    allow: bool
    message: str = ""
    updated_input: dict | None = None


@dataclass
class QuotaInfo:
    status: str                    # allowed, allowed_warning or rejected
    limit_type: str | None = None  # five_hour, seven_day, ...
    utilization: float | None = None
    resets_at: str | None = None   # ISO 8601, UTC
    windows: dict = field(default_factory=dict)


@dataclass
class RunResult:
    status: str                    # ok, error or stopped
    text: str = ""
    error: str | None = None
    duration_ms: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    session_id: str | None = None
    quota: QuotaInfo | None = None
    model: str | None = None
    permission_denials: list = field(default_factory=list)
    structured: dict | None = None  # with json_schema: the answer, as validated by the CLI
    num_turns: int | None = None    # model calls: each tool round trip sends the context again
    tool_calls: list = field(default_factory=list)  # names of the tools the agent called, in order
    fresh_start: str | None = None  # "asked" or "automatic": a new session fed by the memory
    usage: dict = field(default_factory=dict)  # the CLI's own token counts, by kind


def _version_key(name):
    try:
        return tuple(int(part) for part in name.split("."))
    except ValueError:
        return None


def find_cli(explicit=None):
    """Return the command that starts Claude Code, as a list.

    Order: the explicit path, `claude` on the PATH, then the copy that the
    Claude desktop app keeps under %APPDATA%\\Claude\\claude-code\\<version>\\,
    newest version first. That folder changes with every update, which is
    why it is searched instead of written in the configuration.
    """
    if explicit:
        if not Path(explicit).is_file():
            raise ProviderError(f"Claude Code introuvable à l'emplacement indiqué : {explicit}")
        return [str(explicit)]
    on_path = shutil.which("claude")
    if on_path:
        return [on_path]
    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata) / "Claude" / "claude-code"
        candidates = []
        if root.is_dir():
            for version_dir in root.iterdir():
                key = _version_key(version_dir.name)
                if key is None or not version_dir.is_dir():
                    continue
                for exe in version_dir.glob("*/claude.exe"):
                    candidates.append((key, exe))
        if candidates:
            return [str(max(candidates)[1])]
    raise ProviderError(
        "Claude Code introuvable : installez-le ou indiquez le chemin de claude.exe dans la configuration"
    )


_HOST_SESSION_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDE_PREVIEW_")
_HOST_SESSION_NAMES = {"CLAUDECODE", "CLAUDE_PID", "ANTHROPIC_BASE_URL"}


def clean_env(base):
    """Environment for the CLI, without the variables of a host Claude session.

    When AI Hub itself is started from inside Claude Code (a terminal of the
    desktop app, for instance), the environment carries that session's
    variables: its id, its messaging socket, and an ANTHROPIC_BASE_URL that
    routes requests through the host. An agent inheriting them would talk
    through the host session instead of using its own login. Outside a host
    session (no CLAUDECODE variable), the environment is left untouched.
    """
    env = dict(base)
    if "CLAUDECODE" not in env:
        return env
    return {
        name: value for name, value in env.items()
        if name not in _HOST_SESSION_NAMES and not name.startswith(_HOST_SESSION_PREFIXES)
    }


def _iso_from_epoch(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def parse_quota(info):
    """Turn the CLI's rate_limit_info into a QuotaInfo, or None if unusable."""
    if not isinstance(info, dict) or info.get("status") not in ("allowed", "allowed_warning", "rejected"):
        return None
    utilization = info.get("utilization")
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool) or utilization < 0:
        utilization = None
    windows = {}
    for name, window in (info.get("unifiedWindows") or {}).items():
        if isinstance(window, dict):
            windows[name] = {
                "utilization": window.get("utilization"),
                "resets_at": _iso_from_epoch(window.get("resetsAt")),
            }
    return QuotaInfo(
        status=info["status"],
        limit_type=info.get("rateLimitType") if isinstance(info.get("rateLimitType"), str) else None,
        utilization=utilization,
        resets_at=_iso_from_epoch(info.get("resetsAt")),
        windows=windows,
    )


_USAGE_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
_WARNING_FROM = 0.8


def _iso(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def parse_usage(response, warning_from=_WARNING_FROM):
    """Turn the CLI's get_usage reply into a QuotaInfo.

    Unlike rate_limit_event (a fraction, 0.29), get_usage reports percents
    (29): windows are stored as fractions, the same scale for both.
    `warning_from`: the fraction from which the quota is "nearly reached"
    (the user's alert threshold).
    """
    if not isinstance(response, dict):
        raise ProviderError("réponse de quota illisible")
    limits = response.get("rate_limits") if response.get("rate_limits_available") else None
    if not isinstance(limits, dict):
        raise ProviderError(f"{DISPLAY_NAME} ne donne pas de quota pour ce compte")
    windows = {}
    for name in _USAGE_WINDOWS:
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        percent = window.get("utilization")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or percent < 0:
            continue
        windows[name] = {"utilization": percent / 100, "resets_at": _iso(window.get("resets_at"))}
    if not windows:
        raise ProviderError(f"{DISPLAY_NAME} ne donne pas de quota pour ce compte")
    fullest = max(windows, key=lambda n: windows[n]["utilization"])
    worst = windows[fullest]["utilization"]
    severities = {item.get("severity") for item in limits.get("limits") or [] if isinstance(item, dict)}
    if worst >= 1:
        status = "rejected"
    elif worst >= warning_from or severities - {"normal", None}:
        status = "allowed_warning"
    else:
        status = "allowed"
    return QuotaInfo(status=status, limit_type=fullest, utilization=worst,
                     resets_at=windows[fullest]["resets_at"], windows=windows)


def read_usage(command, *, workdir, env=None, timeout_s=60, warning_from=_WARNING_FROM):
    """Ask Claude Code for the current quota, without sending anything to the model.

    The CLI answers a get_usage control request on its own: this costs no
    quota, unlike a request (about 26,000 tokens of context each).
    """
    if not Path(workdir).is_dir():
        raise ProviderError(f"dossier introuvable : {workdir}")
    args = list(command) + [
        "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--no-session-persistence",
    ]
    try:
        proc = subprocess.Popen(
            args, cwd=workdir, env=env if env is not None else clean_env(os.environ),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW, start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        raise ProviderError(f"impossible de lancer {DISPLAY_NAME} : {exc}") from None
    timer = threading.Timer(timeout_s, _kill_tree, args=(proc,))
    timer.daemon = True
    timer.start()
    reply = None
    try:
        proc.stdin.write(json.dumps({"type": "control_request", "request_id": "aihub-usage",
                                     "request": {"subtype": "get_usage"}}) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (isinstance(event, dict) and event.get("type") == "control_response"
                    and (event.get("response") or {}).get("request_id") == "aihub-usage"):
                reply = event["response"]
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    if reply is None:
        raise ProviderError(f"{DISPLAY_NAME} n'a pas donné son quota (code {proc.returncode})")
    if reply.get("subtype") != "success":
        detail = reply.get("error") or "erreur sans message"
        if "not logged in" in str(detail).lower():
            return_message = (f"{DISPLAY_NAME} n'est connecté à aucun compte. "
                              "Lancez « claude auth login » dans un terminal.")
            raise ProviderError(return_message)
        raise ProviderError(f"{DISPLAY_NAME} n'a pas donné son quota : {detail}")
    return parse_usage(reply.get("response"), warning_from)


def _local_time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m à %H:%M")
    except (TypeError, ValueError):
        return None


def describe_error(text, *, api_error_status=None, quota=None, model=None):
    """A clear message for a failed run, naming the AI when its quota is reached."""
    low = (text or "").lower()
    if "not logged in" in low or "please run /login" in low:
        return (f"{DISPLAY_NAME} n'est connecté à aucun compte. "
                "Lancez « claude auth login » dans un terminal, puis réessayez.")
    quota_hit = (
        api_error_status == 429
        or (quota is not None and quota.status == "rejected")
        or "usage limit" in low
        or "rate limit" in low
    )
    if quota_hit:
        who = f"{DISPLAY_NAME} ({model})" if model else DISPLAY_NAME
        message = f"Quota de {who} atteint"
        when = _local_time(quota.resets_at) if quota and quota.resets_at else None
        return message + (f" : retour prévu le {when}." if when else ".")
    detail = (text or "").strip() or "erreur sans message"
    return f"{DISPLAY_NAME} a renvoyé une erreur : {detail}"


def _kill_tree(proc):
    """Kill the CLI and every process it started (shells, tools)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True, creationflags=NO_WINDOW,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _allowed(tool_name, auto_allow):
    """A tool of the list, or of a server listed whole (« mcp__nom », as --allowedTools reads it)."""
    if tool_name in auto_allow:
        return True
    return any(entry.startswith("mcp__") and entry.count("__") == 1 and tool_name.startswith(entry + "__")
               for entry in auto_allow)


class ClaudeCodeRun:
    """One request to Claude Code. `run()` blocks; `stop()` may come from any thread.

    Callbacks run in the thread that calls `run()`:
    - `on_text(chunk)` for each piece of streamed text;
    - `ask_permission(PermissionRequest) -> Decision` when the agent needs approval
      (manual and plan modes); without it, every request is denied;
    - `on_quota(QuotaInfo)` when the CLI reports the quota state.
    """

    def __init__(self, command, *, prompt, workdir, permission_mode, model=None,
                 resume_session=None, ask_permission=None, on_text=None, on_quota=None,
                 timeout_s=None, extra_args=(), env=None, tools=None, json_schema=None,
                 mcp_config=None, auto_allow=(), disallowed=()):
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
        self.extra_args = list(extra_args)
        self.env = env
        # tools: names of the only built-in tools the agent may use (e.g. a
        # read-only conductor); json_schema: the shape its answer must have.
        self.tools = list(tools) if tools is not None else None
        self.json_schema = json_schema
        # mcp_config: the hub's own MCP servers (the only ones, with
        # --strict-mcp-config); auto_allow: their read-only tools, allowed
        # without asking anyone.
        self.mcp_config = mcp_config
        self.auto_allow = list(auto_allow)
        self.disallowed = list(disallowed)  # tools taken away (the web search, when not chosen)
        self._lock = threading.Lock()
        self._proc = None
        self._stop_reason = None  # "user" or "timeout"
        self._stderr = []

    def arguments(self):
        args = self.command + [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", PERMISSION_MODES[self.permission_mode],
            # Both are needed: without --permission-prompt-tool stdio the CLI
            # denies every prompt itself and never sends can_use_tool
            # (measured on 2.1.293 against the real CLI).
            "--permission-prompts", "host",
            "--permission-prompt-tool", "stdio",
            # The user's own MCP servers (mail, vaults...) stay out of agents
            # launched from the hub.
            "--strict-mcp-config",
        ]
        if self.model:
            args += ["--model", self.model]
        if self.resume_session:
            args += ["--resume", self.resume_session]
        if self.tools is not None:
            args += ["--tools", ",".join(self.tools)]
        if self.json_schema is not None:
            args += ["--json-schema", json.dumps(self.json_schema, ensure_ascii=False)]
        if self.mcp_config is not None:
            # Only the keys Claude Code knows (Codex's own tool_timeout_sec stays out).
            servers = {name: {k: v for k, v in spec.items() if k in ("type", "command", "args", "env")}
                       for name, spec in (self.mcp_config.get("mcpServers") or {}).items()}
            args += ["--mcp-config", json.dumps({"mcpServers": servers}, ensure_ascii=False)]
        if self.auto_allow:
            args += ["--allowedTools", ",".join(self.auto_allow)]
        if self.disallowed:
            args += ["--disallowedTools", ",".join(self.disallowed)]
        return args + self.extra_args

    def stop(self, reason="user"):
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)

    def _read_stderr(self, stream):
        size = 0
        for line in stream:
            self._stderr.append(line)
            size += len(line)
            while size > _STDERR_KEEP and len(self._stderr) > 1:
                size -= len(self._stderr.pop(0))

    def _send(self, proc, payload):
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass  # the CLI is gone; the end of stdout will tell the rest

    def _answer_permission(self, proc, event, result):
        request = event.get("request") or {}
        request_id = event.get("request_id")
        if request.get("subtype") != "can_use_tool":
            self._send(proc, {"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": f"unsupported control request: {request.get('subtype')}",
            }})
            return
        ask = PermissionRequest(
            tool_name=str(request.get("tool_name") or "?"),
            input=request.get("input") if isinstance(request.get("input"), dict) else {},
            request_id=str(request_id),
            suggestions=request.get("permission_suggestions") or [],
        )
        if _allowed(ask.tool_name, self.auto_allow):
            # The hub's read-only tools, should the CLI still ask for them.
            decision = Decision(True)
        elif self.ask_permission is None:
            decision = Decision(False, "aucune personne n'est là pour valider : refus par défaut")
        else:
            try:
                decision = self.ask_permission(ask)
            except Exception as exc:  # a broken callback must not approve anything
                decision = Decision(False, f"validation impossible : {exc}")
            if not isinstance(decision, Decision):
                decision = Decision(False, "réponse de validation invalide : refus par défaut")
        if decision.allow:
            response = {"behavior": "allow",
                        "updatedInput": decision.updated_input if decision.updated_input is not None
                        else ask.input}
        else:
            response = {"behavior": "deny", "message": decision.message or "refusé par l'utilisateur"}
            result.permission_denials.append({"tool_name": ask.tool_name, "message": response["message"]})
        self._send(proc, {"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": response,
        }})

    def run(self):
        if not self.workdir or not Path(self.workdir).is_dir():
            raise ProviderError(f"dossier de travail introuvable : {self.workdir}")
        result = RunResult(status="error", model=self.model)
        started = time.monotonic()
        with self._lock:
            if self._stop_reason is not None:
                result.status = "stopped"
                return result
            try:
                proc = subprocess.Popen(
                    self.arguments(), cwd=self.workdir,
                    env=self.env if self.env is not None else clean_env(os.environ),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=NO_WINDOW, start_new_session=(os.name != "nt"),
                )
            except OSError as exc:
                raise ProviderError(f"impossible de lancer {DISPLAY_NAME} : {exc}") from None
            self._proc = proc
        stderr_thread = threading.Thread(target=self._read_stderr, args=(proc.stderr,), daemon=True)
        stderr_thread.start()
        timer = None
        if self.timeout_s:
            timer = threading.Timer(self.timeout_s, self.stop, kwargs={"reason": "timeout"})
            timer.daemon = True
            timer.start()
        try:
            self._send(proc, {"type": "user", "message": {"role": "user", "content": self.prompt}})
            final = self._read_events(proc, result)
            if final is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
        finally:
            if timer:
                timer.cancel()
            if proc.poll() is None:
                _kill_tree(proc)
            stderr_thread.join(timeout=5)
            # Each request would otherwise leave three descriptors open.
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self._conclude(proc, result, final)
        return result

    def _read_events(self, proc, result):
        """Read events until the result message; return it (None if none came)."""
        streamed = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                result.session_id = event.get("session_id") or result.session_id
                result.model = event.get("model") or result.model
            elif kind == "stream_event":
                inner = event.get("event") or {}
                delta = inner.get("delta") or {}
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    chunk = delta.get("text") or ""
                    if chunk:
                        streamed.append(chunk)
                        if self.on_text:
                            self.on_text(chunk)
            elif kind == "assistant":
                content = (event.get("message") or {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        result.tool_calls.append(str(block.get("name") or "?"))
            elif kind == "rate_limit_event":
                quota = parse_quota(event.get("rate_limit_info"))
                if quota is not None:
                    result.quota = quota
                    if self.on_quota:
                        self.on_quota(quota)
            elif kind == "control_request":
                self._answer_permission(proc, event, result)
            elif kind == "result":
                result.text = event.get("result") if isinstance(event.get("result"), str) else "".join(streamed)
                result.session_id = event.get("session_id") or result.session_id
                if isinstance(event.get("structured_output"), dict):
                    result.structured = event["structured_output"]
                usage = event.get("usage") or {}
                result.usage = {k: v for k, v in usage.items() if isinstance(v, int)}
                if isinstance(event.get("num_turns"), int):
                    result.num_turns = event["num_turns"]
                counts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                                 "cache_read_input_tokens")]
                if any(isinstance(c, int) for c in counts):
                    result.tokens_in = sum(c for c in counts if isinstance(c, int))
                if isinstance(usage.get("output_tokens"), int):
                    result.tokens_out = usage["output_tokens"]
                return event
        if streamed and not result.text:
            result.text = "".join(streamed)
        return None

    def _conclude(self, proc, result, final):
        with self._lock:
            reason = self._stop_reason
        if reason == "user":
            result.status = "stopped"
            result.error = None
            return
        if reason == "timeout":
            result.status = "error"
            result.error = f"{DISPLAY_NAME} n'a pas fini dans le délai de {self.timeout_s} s : requête arrêtée."
            return
        if final is None:
            tail = "".join(self._stderr).strip()[-500:]
            result.status = "error"
            result.error = (f"{DISPLAY_NAME} s'est arrêté sans réponse (code {proc.returncode})"
                            + (f" : {tail}" if tail else "."))
            return
        if final.get("is_error"):
            result.status = "error"
            text = final.get("result") if isinstance(final.get("result"), str) else ""
            if not text.strip() and isinstance(final.get("errors"), list):
                # error_during_execution (an unknown session to resume, for one):
                # the reason is in "errors", not in "result" (measured on the real CLI).
                text = "; ".join(str(e) for e in final["errors"] if e)
            result.error = describe_error(
                text,
                api_error_status=final.get("api_error_status"),
                quota=result.quota, model=result.model,
            )
            return
        result.status = "ok"
