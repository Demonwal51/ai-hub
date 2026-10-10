"""Local AI provider: a model of a local Ollama (http://127.0.0.1:11434 by default).

Free and offline: nothing leaves the PC. Ollama keeps no session, so every
request is sent with the conversation's memory (summary, last exchanges,
passages: aihub/runner.py, within the local cap). The local AI may read
the conversation's folder with three tools run here, confined to that
folder; it never changes anything, whatever the permission mode.

The hub does not install or start Ollama: it uses the one already running
(`ollama serve`). Models are « local:<name> » in the hub.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from .claude_code import ProviderError, RunResult

PROVIDER = "ollama"
DISPLAY_NAME = "IA locale"
DEFAULT_MODEL = None       # there is no "Ollama's own default": a model is always named
PREFIX = "local:"
STATELESS = True           # no session to resume: the memory goes with every request
DEFAULT_URL = "http://127.0.0.1:11434"
MAX_TOOL_ROUNDS = 8
READ_DEFAULT, READ_MAX = 6000, 20000
LIST_MAX = 300
SEARCH_MAX = 30

SYSTEM = ("Tu es l'IA locale d'AI Hub, sur l'ordinateur de l'utilisateur. Réponds en français, directement. "
          "Tu peux lire les fichiers du dossier de travail avec les outils list_files, read_file et search_files ; "
          "tu ne peux rien modifier ni lancer de commande. Si on te demande de modifier quelque chose, explique "
          "ce qu'il faudrait faire.")

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "Liste les fichiers d'un dossier du dossier de travail.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "sous-dossier, « . » pour la racine"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Lit un fichier texte du dossier de travail, par tranches.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "start": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "search_files", "description": "Cherche un texte dans les fichiers du dossier de travail.",
        "parameters": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}}},
]
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".idea", ".vscode"}


def api_name(model):
    return model[len(PREFIX):] if isinstance(model, str) and model.startswith(PREFIX) else model


def find_cli(explicit=None):
    """The base URL of Ollama, as a one-item list (the hub's « command » of a provider)."""
    url = (explicit or os.environ.get("AIHUB_OLLAMA_URL") or DEFAULT_URL).rstrip("/")
    return [url]


def _tags(command, timeout_s):
    try:
        data = httpx.get(f"{command[0]}/api/tags", timeout=timeout_s).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("L'IA locale ne répond pas : démarrez Ollama "
                            f"(ollama serve). Détail : {exc}") from None
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("name")]


def _is_embedding(model):
    family = str((model.get("details") or {}).get("family") or "").lower()
    return "bert" in family or "embed" in model["name"].lower()


def list_models(command, *, timeout_s=5):
    """The chat models of this Ollama, as « local:<name> »; ProviderError when it does not answer."""
    return [PREFIX + m["name"] for m in _tags(command, timeout_s) if not _is_embedding(m)]


def embedding_models(command, *, timeout_s=5):
    """The embedding models of this Ollama (their plain names: nomic-embed-text:latest)."""
    return [m["name"] for m in _tags(command, timeout_s) if _is_embedding(m)]


def lost_session(error):
    return False


class Folder:
    """The three read-only tools, confined to the working folder."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _inside(self, relative):
        target = (self.root / str(relative or ".")).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("chemin hors du dossier de travail : refusé")
        return target

    def _files(self, base):
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(self.root)
            if any(part in _SKIP_DIRS or part.startswith(".") for part in relative.parts):
                continue
            if path.is_file() and path.resolve().is_relative_to(self.root):
                yield path

    def list_files(self, path="."):
        base = self._inside(path)
        if not base.is_dir():
            raise ValueError(f"dossier introuvable : {path}")
        found = [p.relative_to(self.root).as_posix() for p in self._files(base)]
        return "\n".join(found[:LIST_MAX]) + (f"\n(et {len(found) - LIST_MAX} autres)" if len(found) > LIST_MAX
                                               else "") if found else "Aucun fichier."

    def read_file(self, path, start=0):
        target = self._inside(path)
        if not target.is_file():
            raise ValueError(f"fichier introuvable : {path}")
        if any(part.startswith(".") for part in target.relative_to(self.root).parts):
            raise ValueError("fichier caché : refusé")
        raw = target.read_bytes()
        if b"\x00" in raw[:4096]:
            raise ValueError("fichier binaire : illisible")
        text = raw.decode("utf-8", errors="replace")
        start = max(0, int(start or 0))
        part = text[start:start + READ_DEFAULT]
        more = f"\n(suite : read_file avec start={start + READ_DEFAULT})" if start + READ_DEFAULT < len(text) else ""
        return part + more

    def search_files(self, query):
        query = str(query or "").strip()
        if len(query) < 3:
            raise ValueError("cherchez au moins 3 caractères")
        low, hits = query.lower(), []
        for path in self._files(self.root):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if low in line.lower():
                    hits.append(f"{path.relative_to(self.root).as_posix()}:{number}: {line.strip()[:200]}")
                    if len(hits) >= SEARCH_MAX:
                        return "\n".join(hits)
        return "\n".join(hits) or f"Rien trouvé pour « {query} »."

    def call(self, name, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "list_files":
                return self.list_files(arguments.get("path", "."))
            if name == "read_file":
                return self.read_file(arguments.get("path"), arguments.get("start"))
            if name == "search_files":
                return self.search_files(arguments.get("query"))
            return f"outil inconnu : {name}"
        except (ValueError, OSError) as exc:
            return f"refusé : {exc}"


class OllamaRun:
    """One request to the local AI. Same interface as ClaudeCodeRun (`run()`, `stop()`)."""

    def __init__(self, command, *, prompt, workdir, permission_mode, model=None, resume_session=None,
                 ask_permission=None, on_text=None, on_quota=None, timeout_s=None, env=None, tools=None,
                 json_schema=None, mcp_config=None, auto_allow=(), instructions=None, web=True):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("demande vide")
        if not model:
            raise ProviderError("choisissez un modèle de l'IA locale")
        self.url = command[0]
        self.prompt = prompt
        self.workdir = workdir
        self.model = api_name(model)
        self.on_text = on_text
        self.timeout_s = timeout_s
        self.json_schema = json_schema
        self.instructions = instructions
        self.read_files = tools is None or bool(tools)  # tools=[]: nothing at all (a summary)
        self._lock = threading.Lock()
        self._stop_reason = None
        self._response = None

    def stop(self, reason="user"):
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
            response = self._response
        if response is not None:
            response.close()  # the stream ends at once; Ollama stops generating

    def _stopped(self):
        with self._lock:
            return self._stop_reason

    def run(self):
        if not self.workdir or not Path(self.workdir).is_dir():
            raise ProviderError(f"dossier de travail introuvable : {self.workdir}")
        result = RunResult(status="error", model=PREFIX + self.model)
        started = time.monotonic()
        if self._stopped():
            result.status = "stopped"
            return result
        timer = None
        if self.timeout_s:
            timer = threading.Timer(self.timeout_s, self.stop, kwargs={"reason": "timeout"})
            timer.daemon = True
            timer.start()
        folder = Folder(self.workdir)
        system = SYSTEM + (f"\n\n{self.instructions}" if self.instructions else "")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": self.prompt}]
        result.usage = {"input_tokens": 0, "output_tokens": 0}
        text = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS + 1):
                content, calls = self._turn(messages, result)
                text = content
                if not calls:
                    break
                messages.append({"role": "assistant", "content": content, "tool_calls": calls})
                for call in calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "?")
                    result.tool_calls.append(name)
                    messages.append({"role": "tool", "tool_name": name,
                                     "content": folder.call(name, function.get("arguments"))})
            else:
                raise ProviderError(f"l'IA locale a enchaîné plus de {MAX_TOOL_ROUNDS} appels d'outils : arrêtée")
            result.text = text
            result.status = "ok"
        except (httpx.HTTPError, httpx.StreamError, ValueError, ProviderError) as exc:
            reason = self._stopped()
            if reason == "user":
                result.status = "stopped"
            elif reason == "timeout":
                result.error = f"{DISPLAY_NAME} n'a pas fini dans le délai de {self.timeout_s} s : requête arrêtée."
            elif isinstance(exc, httpx.ConnectError):
                result.error = ("L'IA locale ne répond pas : démarrez Ollama (ollama serve).")
            else:
                result.error = f"{DISPLAY_NAME} a renvoyé une erreur : {exc}"
        finally:
            if timer:
                timer.cancel()
        result.tokens_in = result.usage.get("input_tokens")
        result.tokens_out = result.usage.get("output_tokens")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if result.status == "ok" and self.json_schema is not None:
            try:
                parsed = json.loads(result.text)
            except ValueError:
                parsed = None
            result.structured = parsed if isinstance(parsed, dict) else None
        return result

    def _turn(self, messages, result):
        """One model call, streamed: (its text, its tool calls)."""
        body = {"model": self.model, "messages": messages, "stream": True, "think": False}
        if self.read_files:
            body["tools"] = TOOLS
        if self.json_schema is not None:
            body["format"] = self.json_schema
        parts, calls = [], []
        with httpx.Client(timeout=httpx.Timeout(30.0, read=None)) as client:
            with client.stream("POST", f"{self.url}/api/chat", json=body) as response:
                with self._lock:
                    self._response = response
                if self._stopped():
                    response.close()
                if response.status_code != 200:
                    response.read()
                    detail = _error_of(response.text)
                    if response.status_code == 404:
                        raise ProviderError(f"le modèle {self.model} n'est pas installé dans Ollama (ollama pull {self.model})")
                    raise ProviderError(f"Ollama a répondu {response.status_code} : {detail}")
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise ProviderError(str(event["error"]))
                    message = event.get("message") or {}
                    chunk = message.get("content") or ""
                    if chunk:
                        parts.append(chunk)
                        if self.on_text:
                            self.on_text(chunk)
                    calls += [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
                    if event.get("done"):
                        result.num_turns = (result.num_turns or 0) + 1
                        result.usage["input_tokens"] += int(event.get("prompt_eval_count") or 0)
                        result.usage["output_tokens"] += int(event.get("eval_count") or 0)
            with self._lock:
                self._response = None
        if self._stopped():
            raise ProviderError("arrêtée")
        return "".join(parts), calls


def _error_of(text):
    try:
        return json.loads(text).get("error") or text
    except (ValueError, AttributeError):
        return re.sub(r"\s+", " ", text or "")[:300]
