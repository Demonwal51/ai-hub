"""Another AI's answer to the same request (« Comparer avec… »), beside the conversation's own.

It runs at the same time as the conversation's AI, read only (two agents
never change the same files): Claude Code in plan mode with its reading
tools only, Codex in its read-only sandbox, the local AI with its read-only
folder. It has no session of its own: it gets the conversation's memory
(summary, last exchanges, passages) and the request, with its files. Its
answer is kept in `variants`; « Garder celle-ci » swaps it with the reply,
and the next request starts from the memory (no session knows that text).
"""

import threading
import time

from . import attachments, config, db, isolation, memory, providers, store
from .providers import ProviderError
from .providers.claude_code import Decision

READ_ONLY_TOOLS = ("Read", "Glob", "Grep")


def _memory_prompt(conn, conversation_id, request_id, prompt, settings):
    summary = (memory.get_summary(conn, conversation_id) or {}).get("content", "")
    package = memory.base_package(conn, conversation_id, summary=summary,
                                  exchanges=memory.last_exchanges(conn, conversation_id, settings.rag_exchanges,
                                                                  before=request_id),
                                  budget=settings.rag_budget)
    if package["over"]:
        package = memory.shrink(package, settings.rag_budget)
    found = memory.passages(conn, conversation_id, prompt, settings.rag_budget - memory.package_tokens(package),
                            exclude={request_id})
    return memory.render(package, found, prompt)


class SideAnswer:
    """One other AI answering one request. `run()` in its own thread; `stop()` from anywhere."""

    def __init__(self, db_path, conversation_id, model, *, commands, env=None, settings=None, files=(),
                 timeout_s=None, on_done=None, account_id=None, user_id=None):
        self.db_path = db_path
        self.conversation_id = conversation_id
        self.model = model
        self.commands = commands
        self.env = env
        self.settings = settings or config.Settings()
        self.files = list(files)
        self.timeout_s = timeout_s
        self.on_done = on_done
        self.account_id = account_id
        self.user_id = user_id
        self.variant_id = None
        self._run = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()

    def stop(self):
        self._stopped.set()
        with self._lock:
            run = self._run
        if run is not None:
            run.stop()

    def run(self, request_id):
        conn = db.connect(self.db_path)
        started = time.monotonic()
        try:
            request = conn.execute("SELECT * FROM messages WHERE id = ?", (request_id,)).fetchone()
            conversation = conn.execute("SELECT * FROM conversations WHERE id = ?", (self.conversation_id,)).fetchone()
            provider = config.provider_of(self.model)
            self.variant_id = conn.execute(
                "INSERT INTO variants (conversation_id, request_id, provider, model, status, created_at) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (self.conversation_id, request_id, provider, self.model, db.utc_now())).lastrowid
            files_text, files = attachments.for_request(self.files)
            prompt = _memory_prompt(conn, self.conversation_id, request_id, request["content"], self.settings)
            status, content, error, tokens_in, tokens_out = "error", "", None, None, None
            try:
                execution = isolation.for_run(conn, conversation, account_id=self.account_id or self.user_id,
                                              read_only=True, settings=self.settings)
                run = providers.new_run(
                    provider, providers.command_for(self.commands, provider, execution=execution), prompt=prompt + files_text,
                    workdir=conversation["workdir"], permission_mode="plan",
                    model=providers.default_model(provider, self.model), env=self.env,
                    tools=list(READ_ONLY_TOOLS) if provider == "claude_code" else None,
                    ask_permission=lambda r: Decision(False, "une comparaison ne fait que lire"),
                    timeout_s=self.timeout_s, attachments=files,
                    execution=execution,
                    instructions=store.person_instructions(conn, conversation))
                with self._lock:
                    self._run = run
                if self._stopped.is_set():
                    run.stop()
                result = run.run()
                status = "ok" if result.status == "ok" else ("stopped" if result.status == "stopped" else "error")
                content, error = result.text or "", None if status == "ok" else (result.error or result.status)
                tokens_in, tokens_out = result.tokens_in, result.tokens_out
            except ProviderError as exc:
                error = str(exc)
            conn.execute("UPDATE variants SET status = ?, content = ?, error = ?, tokens_in = ?, tokens_out = ?, "
                         "duration_ms = ? WHERE id = ?",
                         (status, content, error, tokens_in, tokens_out, int((time.monotonic() - started) * 1000),
                          self.variant_id))
        finally:
            conn.close()
            if self.on_done:
                self.on_done(self.variant_id)


def of_conversation(conn, conversation_id):
    """{request id: [variants]} of a conversation."""
    found = {}
    for row in conn.execute("SELECT id, request_id, provider, model, content, status, error, tokens_in, tokens_out, "
                            "duration_ms, created_at FROM variants WHERE conversation_id = ? ORDER BY id",
                            (conversation_id,)):
        found.setdefault(row["request_id"], []).append(dict(row))
    return found


def keep(conn, variant_id):
    """« Garder celle-ci »: the variant becomes the reply, the reply a variant; the next request
    starts from the memory (the sessions are forgotten: none of them knows the text kept)."""
    variant = conn.execute("SELECT * FROM variants WHERE id = ?", (variant_id,)).fetchone()
    if variant is None or variant["status"] != "ok":
        raise ProviderError("réponse introuvable, ou pas finie")
    reply = conn.execute("SELECT * FROM messages WHERE conversation_id = ? AND id > ? AND role = 'assistant' "
                         "ORDER BY id LIMIT 1", (variant["conversation_id"], variant["request_id"])).fetchone()
    if reply is None or reply["status"] in ("pending", "running"):
        raise ProviderError("la réponse de la conversation n'est pas finie")
    with db.transaction(conn):
        conn.execute("UPDATE messages SET content = ?, provider = ?, model = ?, tokens_in = ?, tokens_out = ?, "
                     "status = 'ok', error = NULL WHERE id = ?",
                     (variant["content"], variant["provider"], variant["model"], variant["tokens_in"],
                      variant["tokens_out"], reply["id"]))
        conn.execute("UPDATE variants SET content = ?, provider = ?, model = ?, tokens_in = ?, tokens_out = ?, "
                     "status = ? WHERE id = ?",
                     (reply["content"], reply["provider"], reply["model"], reply["tokens_in"], reply["tokens_out"],
                      "ok" if reply["status"] == "ok" else "error", variant_id))
        conn.execute("UPDATE messages SET session_id = NULL WHERE conversation_id = ?", (variant["conversation_id"],))
    return variant["conversation_id"]
