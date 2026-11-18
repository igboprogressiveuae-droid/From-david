"""Background jobs: when they run, and what they say."""

import os
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import jobs  # noqa: E402
import telegram  # noqa: E402
import tools  # noqa: E402
from brain import Brain  # noqa: E402

WED_0224 = datetime(2026, 9, 16, 2, 24)    # a Wednesday


class Jobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (jobs.FILE, jobs.BRAIN, tools.NOTIFY, telegram.send_owner)
        jobs.FILE = os.path.join(self.tmp.name, "jobs.json")
        jobs.BRAIN = Brain(os.path.join(self.tmp.name, "brain"))
        self.spoken, self.texted = [], []
        tools.NOTIFY = self.spoken.append
        telegram.send_owner = self.texted.append

    def tearDown(self):
        jobs.FILE, jobs.BRAIN, tools.NOTIFY, telegram.send_owner = self.saved
        self.tmp.cleanup()

    def test_clock_job_runs_next_at_that_time(self):
        job = jobs.add("gold news", at="7am", now=WED_0224)
        self.assertEqual(job["next_run"], "2026-09-16T07:00")
        job = jobs.add("late check", at="1am", now=WED_0224)
        self.assertEqual(job["next_run"], "2026-09-17T01:00")

    def test_days_are_respected(self):
        self.assertEqual(jobs.add("weekly", at="9am", days="friday", now=WED_0224)["next_run"], "2026-09-18T09:00")
        self.assertEqual(jobs.add("weekend", at="9am", days="weekends", now=WED_0224)["next_run"], "2026-09-19T09:00")
        self.assertEqual(jobs._days("mon, fri"), [0, 4])
        with self.assertRaises(ValueError):
            jobs._days("someday")

    def test_bad_jobs_are_refused_with_a_reason(self):
        for kwargs in ({"every_minutes": 1}, {}, {"at": "noonish"}, {"at": "7am", "every_minutes": 10}):
            with self.assertRaises(ValueError):
                jobs.add("x", **kwargs)
        with self.assertRaises(ValueError):
            jobs.add("  ", at="7am")

    def test_interval_job_runs_when_due_and_reschedules(self):
        job = jobs.add("btc check", every_minutes=30, now=WED_0224)
        ran = []
        self.assertEqual(jobs.tick(now=WED_0224, run=ran.append), 0)
        later = datetime(2026, 9, 16, 2, 54)
        self.assertEqual(jobs.tick(now=later, run=lambda j: ran.append(j) or "ok"), 1)
        self.assertEqual(ran[0]["id"], job["id"])
        saved = jobs.load()[0]
        self.assertEqual(saved["last_result"], "ok")
        self.assertGreater(datetime.fromisoformat(saved["next_run"]), later)

    def test_a_job_missed_while_off_runs_once(self):
        jobs.add("gold news", at="7am", now=WED_0224)
        ran = []
        self.assertEqual(jobs.tick(now=datetime(2026, 9, 18, 12, 0), run=lambda j: ran.append(1) or ""), 1)
        self.assertEqual(jobs.tick(now=datetime(2026, 9, 18, 12, 1), run=lambda j: ran.append(1) or ""), 0)

    def test_results_are_spoken_texted_and_logged(self):
        job = jobs.add("gold news", at="7am", now=WED_0224)
        self.assertEqual(jobs.run_job(job, ask=lambda p: "Gold is up 2%."), "Gold is up 2%.")
        self.assertIn("Gold is up 2%", self.spoken[0])
        self.assertIn("Gold is up 2%", self.texted[0])
        self.assertTrue(jobs.BRAIN.search("gold is up"))

    def test_nothing_to_report_stays_quiet(self):
        job = jobs.add("btc check", every_minutes=30, now=WED_0224)
        self.assertEqual(jobs.run_job(job, ask=lambda p: "NOTHING."), "")
        self.assertEqual((self.spoken, self.texted), ([], []))

    def test_making_or_removing_a_job_needs_a_yes(self):
        self.assertIn("add_job", tools.DESTRUCTIVE)
        self.assertIn("remove_job", tools.DESTRUCTIVE)
        self.assertIn("needs them to say yes", tools.run("add_job", {"task": "x", "at": "7am"}, gate=None))
        self.assertEqual(jobs.load(), [])

    def test_remove(self):
        job = jobs.add("x", at="7am")
        self.assertTrue(jobs.remove(f"#{job['id']}"))
        self.assertFalse(jobs.remove(job["id"]))


if __name__ == "__main__":
    unittest.main()
