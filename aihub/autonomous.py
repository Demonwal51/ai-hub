"""Autonomous work: the AI pursues a goal alone, round after round.

One run: the goal is recorded as the user's message; then, each round, the
agent works (an ordinary reply of the conversation, in its own session),
ends its reply with « ÉTAT : FINI » or « ÉTAT : À SUIVRE », and the hub
runs the check command if there is one (tests, a build...). The next
round gets the check's result. The run ends when the agent says it is
done and the check passes, or at a limit: rounds, minutes, a stop, a
failed request.

The check command is the person's own: it runs in the conversation's
folder, without the agent's sandbox, so it must be confirmed when the run
is started (like the « sans permission » mode).
"""

import os
import re
import signal
import subprocess
import threading
import time

from . import db, store
from .providers.claude_code import NO_WINDOW, ProviderError
from .runner import Request
from .store import StoreError

MAX_ROUNDS, MAX_MINUTES = 50, 1440
DEFAULT_ROUNDS, DEFAULT_MINUTES = 5, 60
CHECK_TIMEOUT_S = 600
CHECK_KEEP = 3000          # characters of the check's output kept (its end: where errors are)
CHECK_WARNING = ("La commande de vérification est lancée par AI Hub dans le dossier de la conversation, "
                 "sans le bac à sable de l'agent, après chaque tour.")

class CheckConfirmationRequired(StoreError):
    """A check command was given without its warning being confirmed."""

    def __init__(self):
        super().__init__(CHECK_WARNING)


_STATE = re.compile(r"[ÉE]TAT\s*:\s*\**\s*(FINI|[ÀA] SUIVRE)", re.IGNORECASE)


def agent_state(text):
    """fini, suivre or inconnu: the last « ÉTAT : … » line of a reply."""
    found = _STATE.findall(text or "")
    if not found:
        return "inconnu"
    return "fini" if found[-1].upper() == "FINI" else "suivre"


def first_prompt(goal, max_rounds, check_command):
    check = (f"Après chaque tour, AI Hub lance la vérification « {check_command} » et te donne son résultat."
             if check_command else "Il n'y a pas de commande de vérification : vérifie toi-même ton travail.")
    return (f"{goal}\n\n<mode_autonome>\nTu travailles seul vers cet objectif, en {max_rounds} tours au plus, "
            f"sans attendre de réponse de l'utilisateur. {check} À chaque tour : avance le plus possible, "
            "vérifie, puis termine ta réponse par un court compte rendu et une dernière ligne exactement "
            "« ÉTAT : FINI » si l'objectif est atteint, ou « ÉTAT : À SUIVRE » s'il reste du travail.\n"
            "</mode_autonome>")


def next_prompt(number, max_rounds, state, check):
    parts = [f"<mode_autonome>\nTour {number} sur {max_rounds}."]
    if check is not None:
        code, output = check
        verdict = "réussie" if code == 0 else f"en échec (code {code})"
        parts.append(f"Vérification {verdict}. Fin de sa sortie :\n{output or '(rien)'}")
    if state == "fini" and check is not None and check[0] != 0:
        parts.append("Tu as dit avoir fini, mais la vérification échoue : corrige.")
    elif state == "inconnu":
        parts.append("Ta réponse précédente ne finissait pas par « ÉTAT : FINI » ou « ÉTAT : À SUIVRE ».")
    parts.append("Continue vers l'objectif, puis termine par « ÉTAT : FINI » ou « ÉTAT : À SUIVRE ».\n"
                 "</mode_autonome>")
    return "\n".join(parts)


def check_limits(max_rounds, max_minutes, check_command, confirm_check):
    if not isinstance(max_rounds, int) or not 1 <= max_rounds <= MAX_ROUNDS:
        raise StoreError(f"nombre de tours : entre 1 et {MAX_ROUNDS}")
    if not isinstance(max_minutes, int) or not 1 <= max_minutes <= MAX_MINUTES:
        raise StoreError(f"durée : entre 1 et {MAX_MINUTES} minutes")
    command = (check_command or "").strip() or None
    if command and not confirm_check:
        raise CheckConfirmationRequired()
    if command and ("\n" in command or len(command) > 1000):
        raise StoreError("commande de vérification : une seule ligne, 1 000 caractères au plus")
    return command


def _kill(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)


def recover(conn):
    """Runs left « running » by a stopped server are marked stopped. Returns how many."""
    return conn.execute("UPDATE autonomous_runs SET status = 'stopped', finished_at = ? WHERE status = 'running'",
                        (db.utc_now(),)).rowcount


def list_runs(conn, conversation_id):
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM autonomous_runs WHERE conversation_id = ? ORDER BY id", (conversation_id,))]
    for run in runs:
        run["round_list"] = [dict(r) for r in conn.execute(
            "SELECT round, message_id, agent_state, check_code, check_output, created_at "
            "FROM autonomous_rounds WHERE run_id = ? ORDER BY round", (run["id"],))]
    return runs


class AutonomousRun:
    """One autonomous run. Build it, keep it to call `stop()`, then `run()`.

    `request_kwargs` go to each round's Request (on_start, on_text,
    ask_permission, command, env, settings); `on_event(dict)` sees
    round_started and round_done.
    """

    def __init__(self, conn, conversation_id, agent_id, goal, *, max_rounds=DEFAULT_ROUNDS,
                 max_minutes=DEFAULT_MINUTES, check_command=None, confirm_check=False, on_event=None,
                 on_run=None, request_timeout_s=None, schedule_id=None, clock=time.monotonic,
                 **request_kwargs):
        if not isinstance(goal, str) or not goal.strip():
            raise StoreError("objectif vide")
        self.check_command = check_limits(max_rounds, max_minutes, check_command, confirm_check)
        self.conn = conn
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.goal = goal.strip()
        self.max_rounds = max_rounds
        self.max_minutes = max_minutes
        self.on_event = on_event or (lambda event: None)
        self.on_run = on_run  # on_run(run_id) once the run is recorded
        self.request_timeout_s = request_timeout_s
        self.schedule_id = schedule_id
        self.clock = clock
        self.request_kwargs = request_kwargs
        self.run_id = None
        self._lock = threading.Lock()
        self._stopped = False
        self._request = None
        self._check = None

    def stop(self):
        with self._lock:
            self._stopped = True
            request, check = self._request, self._check
        if request is not None:
            request.stop()
        if check is not None:
            _kill(check)

    def _stopping(self):
        with self._lock:
            return self._stopped

    def run(self):
        """Run to the end; returns the run's row (status done, limit, stopped or error)."""
        conversation = store.get_conversation(self.conn, self.conversation_id)
        if not conversation["workdir"]:
            raise StoreError("aucun dossier de travail n'est défini pour cette conversation")
        if store.active_request(self.conn, self.conversation_id) is not None:
            raise store.Busy("une requête est en cours dans cette conversation")
        goal_id = store.add_user_message(self.conn, self.conversation_id, self.goal)
        self.run_id = self.conn.execute(
            "INSERT INTO autonomous_runs (conversation_id, goal_message_id, agent_id, check_command, max_rounds, "
            "max_minutes, status, schedule_id, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (self.conversation_id, goal_id, self.agent_id, self.check_command, self.max_rounds, self.max_minutes,
             self.schedule_id, db.utc_now())).lastrowid
        if self.on_run:
            self.on_run(self.run_id)
        deadline = self.clock() + self.max_minutes * 60
        status, error, summary, state, check = "limit", None, None, None, None
        try:
            # A stop is seen after each round (below), or by the round itself (stop_now).
            for number in range(1, self.max_rounds + 1):
                left = deadline - self.clock()
                if left <= 0:
                    break
                prompt = (first_prompt(self.goal, self.max_rounds, self.check_command) if number == 1
                          else next_prompt(number, self.max_rounds, state, check))
                self.on_event({"type": "round_started", "run_id": self.run_id, "round": number})
                timeout = int(left) if self.request_timeout_s is None else min(int(left), self.request_timeout_s)
                request = Request(self.conn, self.conversation_id, self.agent_id, prompt, record_prompt=False,
                                  timeout_s=max(1, timeout), **self.request_kwargs)
                with self._lock:
                    self._request = request
                    stop_now = self._stopped
                if stop_now:
                    request.stop()
                result = request.run()
                with self._lock:
                    self._request = None
                if result.status == "stopped":
                    status = "stopped"
                    self._round(number, request.message_id, "inconnu", None)
                    break
                if result.status != "ok":
                    status, error = "error", result.error
                    self._round(number, request.message_id, "inconnu", None)
                    break
                state = agent_state(result.text)
                summary = result.text
                check = None
                if self.check_command:
                    self.on_event({"type": "check_started", "run_id": self.run_id, "round": number})
                    check = self._run_check(conversation["workdir"], deadline)
                self._round(number, request.message_id, state, check)
                self.on_event({"type": "round_done", "run_id": self.run_id, "round": number, "state": state,
                               "check_code": check[0] if check else None})
                if self._stopping():
                    status = "stopped"
                    break
                if state == "fini" and (check is None or check[0] == 0):
                    status = "done"
                    break
        except (StoreError, ProviderError) as exc:
            status, error = "error", str(exc)
        except BaseException:
            self._finish("stopped", None, summary)
            raise
        return self._finish(status, error, summary)

    def _run_check(self, workdir, deadline):
        """(exit code, end of the output) of the check command; 124 when it takes too long."""
        timeout = max(1, min(CHECK_TIMEOUT_S, int(deadline - self.clock()) + 60))
        try:
            proc = subprocess.Popen(self.check_command, shell=True, cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                                    start_new_session=(os.name != "nt"))
        except OSError as exc:
            return 127, f"impossible de lancer la vérification : {exc}"
        with self._lock:
            self._check = proc
            stopped = self._stopped
        if stopped:  # a stop that came just before the check started
            _kill(proc)
        try:
            output, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill(proc)
            output, _ = proc.communicate()
            code = 124
            output = (output or "") + f"\n(vérification arrêtée après {timeout} s)"
        finally:
            with self._lock:
                self._check = None
        return code, (output or "").strip()[-CHECK_KEEP:]

    def _round(self, number, message_id, state, check):
        self.conn.execute(
            "INSERT INTO autonomous_rounds (run_id, round, message_id, agent_state, check_code, check_output, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, number, message_id, state, check[0] if check else None, check[1] if check else None,
             db.utc_now()))
        self.conn.execute("UPDATE autonomous_runs SET rounds = ? WHERE id = ?", (number, self.run_id))

    def _finish(self, status, error, summary):
        self.conn.execute("UPDATE autonomous_runs SET status = ?, error = ?, summary = ?, finished_at = ? "
                          "WHERE id = ?", (status, error, (summary or "")[-2000:] or None, db.utc_now(),
                                           self.run_id))
        return dict(self.conn.execute("SELECT * FROM autonomous_runs WHERE id = ?", (self.run_id,)).fetchone())
