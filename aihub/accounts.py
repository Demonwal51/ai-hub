"""Each person's own AI accounts: who runs a request pays for it.

The owner keeps the accounts of the computer (the CLIs' usual folders).
Anyone else gets folders of their own under <data>/comptes/<id>/:
`claude` (CLAUDE_CONFIG_DIR) and `codex` (CODEX_HOME). A request that
person sends runs with those folders, so it uses their subscription and
their quota, never the owner's: an empty folder is « not signed in » (both
CLIs were checked: no fallback on the computer's account). The local AI
(Ollama) is the computer's and costs no account: it stays common.

Signing in happens from the hub, without a terminal:
- Codex: a code to type at a page of OpenAI (`account/login/start`,
  type chatgptDeviceCode, of its app server), the end comes as the
  `account/login/completed` notification;
- Claude Code: `claude auth login` gives a page whose sign-in shows a code
  to paste back; the hub hands it to the waiting CLI.
The hub never reads what the CLIs keep there: only their own commands
(`auth status`, `login status`, `auth logout`, `logout`) touch it.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import providers
from .providers import ProviderError, claude_code, codex

PROVIDERS = (claude_code.PROVIDER, codex.PROVIDER)
NAMES = {claude_code.PROVIDER: claude_code.DISPLAY_NAME, codex.PROVIDER: codex.DISPLAY_NAME}
LOGIN_TIMEOUT_S = 600   # a sign-in left waiting is given up after that
_URL = re.compile(r"https://\S+")
# Keys that would make a CLI use an account other than the folders' one.
SECRET_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENAI_API_KEY", "CODEX_API_KEY")


class AccountError(Exception):
    """A sign-in that cannot be done; the message is for the person."""


def _who(user):
    """The key of a person's sign-ins (0: the owner of a server without accounts)."""
    return (user or {}).get("id", 0)


def own_accounts(user):
    """True when this person has accounts of their own (not the computer's)."""
    return bool(user) and not user.get("is_owner")


def folders(root, user_id):
    """The person's two folders, created if missing."""
    base = Path(root) / str(int(user_id))
    found = {claude_code.PROVIDER: base / "claude", codex.PROVIDER: base / "codex"}
    for path in found.values():
        path.mkdir(parents=True, exist_ok=True)
    return found


def run_env(base, user, root):
    """The environment a request of `user` runs with. `base`: the hub's (None: this process's)."""
    if not own_accounts(user):
        return base
    env = dict(base if base is not None else claude_code.clean_env(os.environ))
    for name in SECRET_NAMES:
        env.pop(name, None)
    found = folders(root, user["id"])
    env["CLAUDE_CONFIG_DIR"] = str(found[claude_code.PROVIDER])
    env["CODEX_HOME"] = str(found[codex.PROVIDER])
    return env


def account_vars(env):
    """The variables naming the accounts, to pass on to a process a CLI starts (a delegation)."""
    return {k: env[k] for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME") if env and env.get(k)}


def remove(root, user_id):
    """Drop a person's folders (the person is deleted)."""
    shutil.rmtree(Path(root) / str(int(user_id)), ignore_errors=True)


def claude_signed_in(command, env, timeout_s=60):
    proc = subprocess.run(list(command) + ["auth", "status"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout_s, encoding="utf-8", errors="replace",
                          creationflags=claude_code.NO_WINDOW, env=env)
    try:
        return bool(json.loads(proc.stdout).get("loggedIn"))
    except ValueError:
        raise ProviderError("état de connexion de Claude Code illisible") from None


class Login:
    """One sign-in under way, of one person to one provider."""

    def __init__(self, provider):
        self.provider = provider
        self.state = "starting"     # starting, waiting (url/code shown), ok, error, cancelled
        self.url = None
        self.code = None            # Codex: the code to type at the page
        self.needs_code = False     # Claude Code: the page's code is pasted back
        self.error = None
        self.started = time.monotonic()
        self.done = threading.Event()
        self._proc = None
        self._server = None
        self._login_id = None
        self._lock = threading.Lock()

    def view(self):
        return {"provider": self.provider, "state": self.state, "url": self.url, "code": self.code,
                "needs_code": self.needs_code, "error": self.error}

    def finish(self, state, error=None):
        with self._lock:
            if self.done.is_set():
                return
            self.state, self.error = state, error
            self.done.set()

    def cancel(self):
        self.finish("cancelled")
        if self._server is not None and self._login_id:
            self._server.request("account/login/cancel", {"loginId": self._login_id})
        for proc in (self._proc, self._server.proc if self._server is not None else None):
            if proc is not None:
                claude_code._kill_tree(proc)


class Accounts:
    """The sign-ins of the hub's people. `commands`: as for the runs (tests give fakes)."""

    def __init__(self, root, *, commands, env=None, workdir=None):
        self.root = Path(root)
        self.commands = commands
        self.env = env
        self.workdir = str(workdir or self.root)
        self._logins = {}
        self._lock = threading.Lock()

    def _command(self, provider):
        return providers.command_for(self.commands, provider)

    def env_for(self, user):
        env = run_env(self.env, user, self.root)
        return env if env is not None else claude_code.clean_env(os.environ)

    def status(self, user):
        """{provider: {"signed_in", "error", "login"}} for this person's own accounts."""
        env = self.env_for(user)
        out = {}
        for provider in PROVIDERS:
            entry = {"name": NAMES[provider], "signed_in": None, "error": None, "login": None}
            if isinstance(self.commands, dict) and self.commands.get(provider) is False:
                continue  # not offered on this hub
            try:
                command = self._command(provider)
                entry["signed_in"] = (claude_signed_in(command, env) if provider == claude_code.PROVIDER
                                      else codex.login_status(command, env=env))
            except (ProviderError, OSError, subprocess.SubprocessError) as exc:
                entry["error"] = str(exc)
            login = self.login_of(user, provider)
            entry["login"] = login.view() if login else None
            out[provider] = entry
        return out

    def login_of(self, user, provider):
        with self._lock:
            login = self._logins.get((_who(user), provider))
        if login is not None and not login.done.is_set() and time.monotonic() - login.started > LOGIN_TIMEOUT_S:
            login.cancel()
            login.error = "connexion abandonnée : trop longue"
        return login

    def start_login(self, user, provider, wait_s=20):
        """Start a sign-in; returns its view once the page (and code) are known.

        The owner signs in the computer's own accounts (a new server, from its page)."""
        if provider not in PROVIDERS:
            raise AccountError(f"fournisseur inconnu : {provider!r}")
        key = (_who(user), provider)
        with self._lock:
            old = self._logins.get(key)
            if old is not None and not old.done.is_set():
                old.cancel()
            login = self._logins[key] = Login(provider)
        command = self._command(provider)
        env = self.env_for(user)
        target = self._claude_login if provider == claude_code.PROVIDER else self._codex_login
        threading.Thread(target=target, args=(login, command, env), daemon=True).start()
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and login.state == "starting" and not login.done.is_set():
            time.sleep(0.05)
        return login.view()

    def send_code(self, user, provider, code):
        """Hand the code shown by Claude's page to the waiting CLI; wait for its verdict."""
        login = self.login_of(user, provider)
        code = str(code or "").strip()
        if login is None or login.done.is_set() or not login.needs_code or login._proc is None:
            raise AccountError("aucune connexion n'attend de code")
        if not code or len(code) > 512 or any(c.isspace() for c in code):
            raise AccountError("code invalide")
        try:
            login._proc.stdin.write(code + "\n")
            login._proc.stdin.flush()
        except (OSError, ValueError):
            raise AccountError("la connexion s'est arrêtée") from None
        login.done.wait(60)
        return login.view()

    def cancel_login(self, user, provider):
        login = self.login_of(user, provider)
        if login is not None and not login.done.is_set():
            login.cancel()

    def logout(self, user, provider):
        if not own_accounts(user):
            # Claude Code and Codex use them outside the hub too: that is done in a terminal.
            raise AccountError("les comptes de l'ordinateur se déconnectent dans un terminal, pas d'ici")
        if provider not in PROVIDERS:
            raise AccountError(f"fournisseur inconnu : {provider!r}")
        self.cancel_login(user, provider)
        args = ["auth", "logout"] if provider == claude_code.PROVIDER else ["logout"]
        try:
            subprocess.run(self._command(provider) + args, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60, encoding="utf-8", errors="replace",
                           creationflags=claude_code.NO_WINDOW, env=self.env_for(user))
        except (OSError, subprocess.SubprocessError) as exc:
            raise AccountError(f"déconnexion impossible : {exc}") from None

    def forget(self, user_id):
        """The person is deleted: end their sign-ins, drop their folders."""
        with self._lock:
            mine = [k for k in self._logins if k[0] == user_id]
            logins = [self._logins.pop(k) for k in mine]
        for login in logins:
            login.cancel()
        remove(self.root, user_id)

    # --- the two sign-in flows -------------------------------------------------

    def _claude_login(self, login, command, env):
        env = dict(env, BROWSER="none")  # the page is opened by the person, on their device
        try:
            proc = login._proc = subprocess.Popen(
                list(command) + ["auth", "login", "--claudeai"], env=env, cwd=self.workdir,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1, creationflags=claude_code.NO_WINDOW)
        except OSError as exc:
            login.finish("error", f"impossible de lancer Claude Code : {exc}")
            return
        timer = threading.Timer(LOGIN_TIMEOUT_S, claude_code._kill_tree, args=(proc,))
        timer.daemon = True
        timer.start()
        tail = []
        try:
            # The CLI writes its prompt without an end of line: read by character.
            line = ""
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                line += ch
                if ch == "\n" or line.rstrip().endswith(">"):
                    found = _URL.search(line)
                    if found and login.url is None:
                        login.url = found.group(0)
                    if "code" in line.lower() and line.rstrip().endswith(">"):
                        login.needs_code = True
                    if login.url and login.needs_code and login.state == "starting":
                        login.state = "waiting"
                    tail = (tail + [line.strip()])[-5:]
                    line = ""
            proc.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            timer.cancel()
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        if login.done.is_set():
            return
        try:
            ok = proc.returncode == 0 and claude_signed_in(command, {k: v for k, v in env.items() if k != "BROWSER"})
        except (ProviderError, OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            login.finish("ok")
        else:
            # Measured on the real CLI: « Paste code here if prompted > Login failed: … » on one line.
            detail = next((t for t in reversed(tail) if t and not t.endswith(">")), "").split(">")[-1].strip()
            login.finish("error", "connexion refusée" + (f" : {detail[:200]}" if detail else ""))

    def _codex_login(self, login, command, env):
        try:
            home = Path(env["CODEX_HOME"]) if env and env.get("CODEX_HOME") else codex.codex_home()
            server = login._server = codex._Server(command, self.workdir, env, home)
        except ProviderError as exc:
            login.finish("error", str(exc))
            return
        timer = threading.Timer(LOGIN_TIMEOUT_S, claude_code._kill_tree, args=(server.proc,))
        timer.daemon = True
        timer.start()
        try:
            first = server.request("initialize", {"clientInfo": codex._CLIENT})
            start = None
            for message in server.messages():
                if "method" in message:
                    if "id" in message:
                        server.write({"id": message["id"], "error": {"code": -32601, "message": "non pris en charge"}})
                        continue
                    if message["method"] == "account/login/completed":
                        params = message.get("params") or {}
                        if params.get("success"):
                            login.finish("ok")
                        else:
                            login.finish("error", "connexion refusée" + (
                                f" : {params['error'][:200]}" if isinstance(params.get("error"), str) else ""))
                        break
                    continue
                if message.get("id") == first:
                    if "error" in message:
                        login.finish("error", f"{codex.DISPLAY_NAME} n'a pas démarré")
                        break
                    server.notify("initialized")
                    start = server.request("account/login/start", {"type": "chatgptDeviceCode"})
                elif start is not None and message.get("id") == start:
                    result = message.get("result") or {}
                    if "error" in message or not result.get("verificationUrl") or not result.get("userCode"):
                        detail = (message.get("error") or {}).get("message") or "pas de code reçu"
                        login.finish("error", f"{codex.DISPLAY_NAME} a refusé : {detail}")
                        break
                    login._login_id = result.get("loginId")
                    login.url, login.code = result["verificationUrl"], result["userCode"]
                    login.state = "waiting"
        finally:
            timer.cancel()
            server.close()
        if not login.done.is_set():
            login.finish("error", f"{codex.DISPLAY_NAME} s'est arrêté avant la fin de la connexion")
