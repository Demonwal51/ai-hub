"""Scheduled tasks: a request, or autonomous work, that starts again on its own.

A task belongs to a conversation (its folder, its AI, its permission mode)
and says when: every N minutes, every day at a time, or some days of the
week at a time, in the PC's local time. The scheduler (a thread of the
server) launches the tasks that are due. If the conversation is busy, the
task waits a few minutes. Runs missed while the PC was off or asleep are
not all made up for: the task runs once, then goes on with its rule.

A check command of autonomous work is confirmed when the task is created
(the same warning as for a run started by hand), then used at each run.
"""

import json
import re
import threading
from datetime import datetime, timedelta, timezone

from . import autonomous, db, store
from .store import StoreError

EVERY = ("interval", "daily", "weekly")
KINDS = ("single", "autonomous")
DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MIN_INTERVAL, MAX_INTERVAL = 5, 7 * 24 * 60
BUSY_RETRY = timedelta(minutes=5)   # a busy conversation: try again later
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def next_run(rule, after):
    """The first time after `after` (aware) that the rule names, in UTC.

    Daily and weekly times are wall-clock times of the PC: they are built
    as naive local times, which the system converts with its own rules
    (summer time included).
    """
    if rule["every"] == "interval":
        return (after + timedelta(minutes=rule["interval_minutes"])).astimezone(timezone.utc)
    hour, minute = (int(x) for x in rule["at_time"].split(":"))
    local = after.astimezone().replace(tzinfo=None)
    days = {int(d) for d in str(rule.get("weekdays") or "").split(",") if d != ""}
    for offset in range(0, 8):
        candidate = (local + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            continue
        if rule["every"] == "daily" or candidate.weekday() in days:
            return candidate.astimezone().astimezone(timezone.utc)
    raise StoreError("règle de répétition sans jour")


def check_rule(every, interval_minutes=None, at_time=None, weekdays=None):
    """The rule as stored: (every, interval_minutes, at_time, weekdays)."""
    if every not in EVERY:
        raise StoreError(f"répétition inconnue : {every!r} (attendu : {', '.join(EVERY)})")
    if every == "interval":
        if not isinstance(interval_minutes, int) or not MIN_INTERVAL <= interval_minutes <= MAX_INTERVAL:
            raise StoreError(f"intervalle : entre {MIN_INTERVAL} minutes et 7 jours")
        return every, interval_minutes, None, None
    if not isinstance(at_time, str) or not _TIME.match(at_time):
        raise StoreError("heure : au format HH:MM, par exemple 08:30")
    if every == "daily":
        return every, None, at_time, None
    if not isinstance(weekdays, list) or not weekdays or any(
            not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in weekdays):
        raise StoreError("jours : au moins un, de 0 (lundi) à 6 (dimanche)")
    return every, None, at_time, ",".join(str(d) for d in sorted(set(weekdays)))


def describe(rule):
    """« tous les jours à 08:30 », for the interface and the alerts."""
    if rule["every"] == "interval":
        minutes = rule["interval_minutes"]
        return f"toutes les {minutes // 60} h" if minutes % 60 == 0 else f"toutes les {minutes} min"
    if rule["every"] == "daily":
        return f"tous les jours à {rule['at_time']}"
    days = [DAYS[int(d)] for d in rule["weekdays"].split(",")]
    return f"le {', '.join(days)} à {rule['at_time']}"


def create(conn, conversation_id, prompt, *, kind="single", every="daily", interval_minutes=None, at_time=None,
           weekdays=None, max_rounds=autonomous.DEFAULT_ROUNDS, max_minutes=autonomous.DEFAULT_MINUTES,
           check_command=None, confirm_check=False, now=None):
    """Create a task; returns its id. Everything is checked before anything is written."""
    conversation = store.get_conversation(conn, conversation_id)
    if conversation["mode"] != "single":
        raise StoreError("une tâche planifiée se crée dans une conversation à une IA")
    if not conversation["agent_id"] or not conversation["workdir"]:
        raise StoreError("la conversation n'a pas d'IA ou de dossier de travail")
    if not isinstance(prompt, str) or not prompt.strip():
        raise StoreError("demande vide")
    if kind not in KINDS:
        raise StoreError(f"genre de tâche inconnu : {kind!r}")
    rule = dict(zip(("every", "interval_minutes", "at_time", "weekdays"),
                    check_rule(every, interval_minutes, at_time, weekdays)))
    options = {}
    if kind == "autonomous":
        command = autonomous.check_limits(max_rounds, max_minutes, check_command, confirm_check)
        options = {"max_rounds": max_rounds, "max_minutes": max_minutes, "check_command": command}
    now = now or datetime.now(timezone.utc)
    return conn.execute(
        "INSERT INTO schedules (conversation_id, prompt, kind, options_json, every, interval_minutes, at_time, "
        "weekdays, next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conversation_id, prompt.strip(), kind, json.dumps(options), rule["every"], rule["interval_minutes"],
         rule["at_time"], rule["weekdays"], _iso(next_run(rule, now)), db.utc_now())).lastrowid


def get(conn, schedule_id):
    row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise store.NotFound("tâche planifiée introuvable")
    return _public(row)


def _public(row):
    task = dict(row)
    task["options"] = json.loads(task.pop("options_json") or "{}")
    task["enabled"] = bool(task["enabled"])
    task["rule"] = describe(task)
    return task


def list_all(conn, conversation_id=None):
    if conversation_id is None:
        rows = conn.execute("SELECT s.*, c.title AS conversation_title FROM schedules s "
                            "JOIN conversations c ON c.id = s.conversation_id ORDER BY s.next_run_at").fetchall()
    else:
        rows = conn.execute("SELECT s.*, c.title AS conversation_title FROM schedules s "
                            "JOIN conversations c ON c.id = s.conversation_id WHERE s.conversation_id = ? "
                            "ORDER BY s.id", (conversation_id,)).fetchall()
    return [_public(r) for r in rows]


def set_enabled(conn, schedule_id, enabled, now=None):
    task = get(conn, schedule_id)
    # Turned back on: from now, not from a date long past.
    nxt = _iso(next_run(task, now or datetime.now(timezone.utc))) if enabled else task["next_run_at"]
    conn.execute("UPDATE schedules SET enabled = ?, next_run_at = ? WHERE id = ?", (int(bool(enabled)), nxt,
                                                                                   schedule_id))
    return get(conn, schedule_id)


def delete(conn, schedule_id):
    if conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount == 0:
        raise store.NotFound("tâche planifiée introuvable")


def finished(conn, schedule_id, status, error=None):
    """Record how the task's run ended (called when the work is over)."""
    conn.execute("UPDATE schedules SET last_status = ?, last_error = ? WHERE id = ?", (status, error, schedule_id))


class Scheduler:
    """Launches the tasks that are due. `launch(task) -> None` starts one
    (raises store.Busy when the conversation is busy); `tick()` looks once."""

    def __init__(self, db_path, launch, *, period_s=30, clock=None):
        self.db_path = db_path
        self.launch = launch
        self.period_s = period_s
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="aihub-schedule", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a bad task must not stop the others for good
                pass
            self._stop.wait(self.period_s)

    def tick(self):
        """Launch every due task once. Returns the ids launched."""
        now = self.clock()
        conn = db.connect(self.db_path)
        launched = []
        try:
            due = conn.execute("SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
                               (_iso(now),)).fetchall()
            for row in due:
                task = _public(row)
                try:
                    self.launch(task)
                except store.Busy:
                    conn.execute("UPDATE schedules SET next_run_at = ?, last_status = 'reportee' WHERE id = ?",
                                 (_iso(now + BUSY_RETRY), task["id"]))
                    continue
                except Exception as exc:  # refused (no folder, quota...): said, then the rule goes on
                    conn.execute("UPDATE schedules SET last_run_at = ?, last_status = 'error', last_error = ?, "
                                 "next_run_at = ? WHERE id = ?",
                                 (_iso(now), str(exc), _iso(next_run(task, now)), task["id"]))
                    continue
                conn.execute("UPDATE schedules SET last_run_at = ?, last_status = 'running', last_error = NULL, "
                             "next_run_at = ? WHERE id = ?", (_iso(now), _iso(next_run(task, now)), task["id"]))
                launched.append(task["id"])
        finally:
            conn.close()
        return launched
