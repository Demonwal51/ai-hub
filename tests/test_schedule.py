"""Scheduled tasks: the rules, the scheduler, and the conversation they run in."""

import time
import unittest
from datetime import datetime, timedelta, timezone

from aihub import autonomous, db, schedule, store
from aihub.store import StoreError

from .helpers import TempDirMixin


def local(year, month, day, hour, minute=0):
    """An aware UTC moment from a local wall-clock time of this PC."""
    return datetime(year, month, day, hour, minute).astimezone().astimezone(timezone.utc)


def wall(moment):
    return moment.astimezone().replace(tzinfo=None)


class RuleTest(unittest.TestCase):
    def test_daily(self):
        rule = {"every": "daily", "at_time": "08:30"}
        self.assertEqual(wall(schedule.next_run(rule, local(2026, 10, 12, 7, 0))), datetime(2026, 10, 12, 8, 30))
        self.assertEqual(wall(schedule.next_run(rule, local(2026, 10, 12, 8, 30))), datetime(2026, 10, 13, 8, 30))
        self.assertEqual(wall(schedule.next_run(rule, local(2026, 10, 12, 9, 0))), datetime(2026, 10, 13, 8, 30))

    def test_across_the_change_of_time(self):
        # The last Sunday of October: still 8:30 on the wall clock the next day.
        rule = {"every": "daily", "at_time": "08:30"}
        self.assertEqual(wall(schedule.next_run(rule, local(2026, 10, 25, 9, 0))), datetime(2026, 10, 26, 8, 30))

    def test_weekly(self):
        rule = {"every": "weekly", "at_time": "18:00", "weekdays": "0,4"}  # Monday and Friday
        after = local(2026, 10, 13, 12, 0)  # a Tuesday
        self.assertEqual(wall(schedule.next_run(rule, after)), datetime(2026, 10, 16, 18, 0))  # Friday
        self.assertEqual(wall(schedule.next_run(rule, local(2026, 10, 16, 19, 0))), datetime(2026, 10, 19, 18, 0))

    def test_interval(self):
        rule = {"every": "interval", "interval_minutes": 90}
        start = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule.next_run(rule, start), start + timedelta(minutes=90))

    def test_checks_and_words(self):
        self.assertEqual(schedule.check_rule("weekly", at_time="07:05", weekdays=[4, 0, 4]),
                         ("weekly", None, "07:05", "0,4"))
        for args in (("hourly",), ("interval", 2), ("interval", None), ("daily", None, "8:30"),
                     ("daily", None, "24:00"), ("weekly", None, "08:00", []), ("weekly", None, "08:00", [7]),
                     ("weekly", None, "08:00", [True])):
            with self.subTest(args=args), self.assertRaises(StoreError):
                schedule.check_rule(*args)
        self.assertEqual(schedule.describe({"every": "interval", "interval_minutes": 120}), "toutes les 2 h")
        self.assertEqual(schedule.describe({"every": "interval", "interval_minutes": 45}), "toutes les 45 min")
        self.assertEqual(schedule.describe({"every": "weekly", "at_time": "18:00", "weekdays": "0,4"}),
                         "le lundi, vendredi à 18:00")


class StoreTestCase(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "Veille", agent_id=self.agent, workdir=str(self.tmp))
        self.now = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)


class CreateTest(StoreTestCase):
    def test_create_and_list(self):
        sid = schedule.create(self.conn, self.cid, " résume les nouvelles ", every="interval", interval_minutes=60,
                              now=self.now)
        task = schedule.get(self.conn, sid)
        self.assertEqual((task["prompt"], task["kind"], task["enabled"], task["rule"]),
                         ("résume les nouvelles", "single", True, "toutes les 1 h"))
        self.assertEqual(task["next_run_at"], "2026-10-12T07:00:00+00:00")
        self.assertEqual([t["conversation_title"] for t in schedule.list_all(self.conn)], ["Veille"])

    def test_refusals(self):
        orchestra = store.create_conversation(self.conn, "o", mode="orchestration", agent_id=self.agent,
                                              conductor_agent_id=self.agent, workdir=str(self.tmp))
        with self.assertRaisesRegex(StoreError, "une IA"):
            schedule.create(self.conn, orchestra, "x", at_time="08:00")
        with self.assertRaisesRegex(StoreError, "demande vide"):
            schedule.create(self.conn, self.cid, " ", at_time="08:00")
        with self.assertRaises(autonomous.CheckConfirmationRequired):
            schedule.create(self.conn, self.cid, "x", kind="autonomous", at_time="08:00", check_command="pytest")
        with self.assertRaisesRegex(StoreError, "genre"):
            schedule.create(self.conn, self.cid, "x", kind="tout", at_time="08:00")
        self.assertEqual(schedule.list_all(self.conn), [])

    def test_autonomous_options_are_kept(self):
        sid = schedule.create(self.conn, self.cid, "corrige les tests", kind="autonomous", at_time="02:00",
                              max_rounds=3, max_minutes=20, check_command="pytest", confirm_check=True)
        self.assertEqual(schedule.get(self.conn, sid)["options"],
                         {"max_rounds": 3, "max_minutes": 20, "check_command": "pytest"})

    def test_turned_back_on_from_now(self):
        sid = schedule.create(self.conn, self.cid, "x", every="interval", interval_minutes=30, now=self.now)
        schedule.set_enabled(self.conn, sid, False)
        later = self.now + timedelta(days=3)
        task = schedule.set_enabled(self.conn, sid, True, now=later)
        self.assertEqual(task["next_run_at"], (later + timedelta(minutes=30)).isoformat(timespec="seconds"))

    def test_deleted_with_its_conversation(self):
        schedule.create(self.conn, self.cid, "x", at_time="08:00")
        store.delete_conversation(self.conn, self.cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)


class SchedulerTest(StoreTestCase):
    def scheduler(self, launch):
        clock = {"now": self.now}
        return schedule.Scheduler(self.db_path, launch, clock=lambda: clock["now"]), clock

    def task(self, **kwargs):
        kwargs.setdefault("every", "interval")
        kwargs.setdefault("interval_minutes", 60)
        return schedule.create(self.conn, self.cid, "fais-le", now=self.now, **kwargs)

    def test_due_tasks_run_once_and_move_on(self):
        launched = []
        sid = self.task()
        scheduler, clock = self.scheduler(lambda t: launched.append(t["id"]))
        self.assertEqual(scheduler.tick(), [])  # not yet
        clock["now"] = self.now + timedelta(hours=5)  # four runs missed (PC asleep)
        self.assertEqual(scheduler.tick(), [sid])  # run once, not four times
        self.assertEqual(scheduler.tick(), [])
        task = schedule.get(self.conn, sid)
        self.assertEqual(task["last_status"], "running")
        self.assertEqual(task["next_run_at"], (clock["now"] + timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(launched, [sid])

    def test_a_busy_conversation_waits(self):
        sid = self.task()

        def busy(task):
            raise store.Busy("une requête est déjà en cours")
        scheduler, clock = self.scheduler(busy)
        clock["now"] = self.now + timedelta(hours=1)
        scheduler.tick()
        task = schedule.get(self.conn, sid)
        self.assertEqual(task["last_status"], "reportee")
        self.assertEqual(task["next_run_at"], (clock["now"] + timedelta(minutes=5)).isoformat(timespec="seconds"))

    def test_a_refused_launch_is_recorded_and_the_rule_goes_on(self):
        sid = self.task()

        def refused(task):
            raise StoreError("dossier de travail introuvable")
        scheduler, clock = self.scheduler(refused)
        clock["now"] = self.now + timedelta(hours=1)
        scheduler.tick()
        task = schedule.get(self.conn, sid)
        self.assertEqual((task["last_status"], task["last_error"]), ("error", "dossier de travail introuvable"))
        self.assertGreater(task["next_run_at"], clock["now"].isoformat())

    def test_disabled_tasks_wait(self):
        sid = self.task()
        schedule.set_enabled(self.conn, sid, False)
        launched = []  # recorded, not raised: the scheduler catches what a launch raises
        scheduler, clock = self.scheduler(launched.append)
        clock["now"] = self.now + timedelta(days=1)
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(launched, [])
        self.assertIsNone(schedule.get(self.conn, sid)["last_status"])

    def test_the_thread_starts_and_stops(self):
        launched = []
        self.task()
        scheduler, clock = self.scheduler(launched.append)
        clock["now"] = self.now + timedelta(hours=2)
        scheduler.period_s = 0.05
        scheduler.start()
        try:
            deadline = datetime.now() + timedelta(seconds=10)
            while not launched and datetime.now() < deadline:
                time.sleep(0.02)
        finally:
            scheduler.stop()
        self.assertEqual(len(launched), 1)
        self.assertFalse(scheduler._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
