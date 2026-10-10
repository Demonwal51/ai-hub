"""MCP server that lets an agent ask another AI for its opinion (delegation).

Started by the agent's CLI (stdio, one JSON-RPC message per line):

    python delegate_mcp.py --db data/aihub.db --conversation 12 --models opus,gpt-6-astra

One tool, ask_ai(model, question): the other AI runs in the conversation's
folder in read-only mode (it may read the files, it changes nothing), with
no delegation tool of its own (no chain of AIs asking each other), and its
answer goes back to the agent. At most MAX_CALLS per request of the agent:
each one spends the other AI's quota. Every delegation is recorded in the
hub's base, and shown in the conversation.
"""

import argparse
import json
import sys
import time
from pathlib import Path

# The hub's package: this file runs with python -I from the agent's folder,
# which must not be able to plant a module (-I); the package's own folder is added.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aihub import config, db, providers  # noqa: E402
from aihub.providers.claude_code import ProviderError  # noqa: E402

SERVER = "deleguer"
VERSION = "1"
PROTOCOL = "2025-06-18"
MAX_CALLS = 3
TIMEOUT_S = 600
QUESTION_MAX = 8000
READ_ONLY_TOOLS = ("Read", "Glob", "Grep")

PROMPT = ("Une autre IA, qui travaille dans ce dossier pour l'utilisateur, te demande ton avis. Réponds-lui "
          "directement et de façon concise. Tu peux lire les fichiers du dossier ; tu ne modifies rien.\n\n"
          "Question :\n{question}")


def tools(models):
    return [{"name": "ask_ai", "description": (
        "Demande l'avis d'une autre IA (relecture, deuxième avis, question où elle est meilleure). Elle lit le "
        f"dossier sans rien modifier et répond. {MAX_CALLS} demandes au plus par requête ; chacune consomme "
        "son quota."),
        "inputSchema": {"type": "object", "required": ["model", "question"], "properties": {
            "model": {"type": "string", "enum": list(models)},
            "question": {"type": "string", "description": "la question, avec le contexte utile"}}}}]


class Refused(Exception):
    pass


class Delegate:
    def __init__(self, db_path, conversation_id, models, commands=None, *, max_calls=MAX_CALLS):
        self.db_path = db_path
        self.conversation_id = conversation_id
        self.models = list(models)
        self.commands = commands
        self.max_calls = max_calls
        self.calls = 0

    def ask(self, model, question):
        if model not in self.models:
            raise Refused(f"modèle non proposé : {model!r} (proposés : {', '.join(self.models)})")
        question = str(question or "").strip()
        if not question:
            raise Refused("question vide")
        if len(question) > QUESTION_MAX:
            raise Refused(f"question trop longue ({QUESTION_MAX} caractères au plus)")
        if self.calls >= self.max_calls:
            raise Refused(f"déjà {self.max_calls} demandes pour cette requête : continue avec ce que tu sais")
        self.calls += 1
        conn = db.connect(self.db_path)
        try:
            conversation = conn.execute("SELECT workdir FROM conversations WHERE id = ?",
                                        (self.conversation_id,)).fetchone()
            if conversation is None or not conversation["workdir"]:
                raise Refused("conversation introuvable")
            provider = config.provider_of(model)
            row = conn.execute(
                "INSERT INTO delegations (conversation_id, provider, model, question, status, created_at) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (self.conversation_id, provider, model, question, db.utc_now())).lastrowid
            started = time.monotonic()
            try:
                run = providers.new_run(provider, providers.command_for(self.commands, provider),
                                        prompt=PROMPT.format(question=question), workdir=conversation["workdir"],
                                        permission_mode="plan", model=providers.default_model(provider, model),
                                        tools=list(READ_ONLY_TOOLS), timeout_s=TIMEOUT_S)
                result = run.run()
            except ProviderError as exc:
                result, error = None, str(exc)
            else:
                error = None if result.status == "ok" else (result.error or result.status)
            ms = int((time.monotonic() - started) * 1000)
            conn.execute("UPDATE delegations SET status = ?, answer = ?, error = ?, tokens_in = ?, tokens_out = ?, "
                         "duration_ms = ? WHERE id = ?",
                         ("ok" if error is None else "error", result.text if error is None else None, error,
                          getattr(result, "tokens_in", None), getattr(result, "tokens_out", None), ms, row))
        finally:
            conn.close()
        if error is not None:
            raise Refused(f"{model} n'a pas pu répondre : {error}")
        return f"Réponse de {model} :\n\n{result.text}"

    def call(self, name, arguments):
        arguments = arguments if isinstance(arguments, dict) else {}
        if name != "ask_ai":
            raise Refused(f"outil inconnu : {name}")
        return self.ask(arguments.get("model"), arguments.get("question"))


def handle(delegate, message):
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        result = {"protocolVersion": asked or PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER, "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools(delegate.models)}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            text, error = delegate.call(params.get("name"), params.get("arguments")), False
        except Refused as exc:
            text, error = str(exc), True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"méthode inconnue : {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--conversation", type=int, required=True)
    parser.add_argument("--models", required=True, help="les modèles proposés, séparés par des virgules")
    parser.add_argument("--commands", help="JSON {fournisseur: commande} (essais) ; sinon chaque CLI est cherchée")
    args = parser.parse_args(argv)
    commands = json.loads(args.commands) if args.commands else None
    delegate = Delegate(args.db, args.conversation, [m for m in args.models.split(",") if m], commands)
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON illisible"}}
        else:
            reply = handle(delegate, message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
