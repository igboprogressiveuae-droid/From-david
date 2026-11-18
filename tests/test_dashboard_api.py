"""The dashboard's endpoints for chat, brain, jobs, notes and Telegram."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import dashboard_api as api  # noqa: E402
import jobs  # noqa: E402
import tools  # noqa: E402
from brain import Brain  # noqa: E402


class DashboardApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (jobs.FILE, jobs.BRAIN, api.BRAIN, api._chat, tools.CFG.get("brain"), tools._update_config)
        jobs.FILE = os.path.join(self.tmp.name, "jobs.json")
        api.BRAIN = jobs.BRAIN = Brain(os.path.join(self.tmp.name, "brain"))
        api._chat = None
        tools._update_config = lambda **kw: None      # never rewrite the real config.json
        tools.CFG["brain"] = "gemini"

    def tearDown(self):
        jobs.FILE, jobs.BRAIN, api.BRAIN, api._chat, tools.CFG["brain"], tools._update_config = self.saved
        self.tmp.cleanup()

    def test_brain_lists_every_provider_and_switches(self):
        code, data = api.handle("GET", "/api/brain")
        self.assertEqual((code, data["current"]), (200, "gemini"))
        self.assertEqual([b["name"] for b in data["brains"]], ["gemini", "claude", "openrouter"])
        code, data = api.handle("POST", "/api/brain", {"name": "nonsense"})
        self.assertIn("only know", data["result"])
        self.assertEqual(tools.CFG["brain"], "gemini")

    def test_jobs_add_list_remove(self):
        code, data = api.handle("POST", "/api/jobs/add", {"task": "gold news", "at": "7am", "days": "weekdays"})
        self.assertEqual(code, 200)
        listed = api.handle("GET", "/api/jobs")[1]["jobs"]
        self.assertEqual(len(listed), 1)
        self.assertIn("gold news", listed[0]["text"])
        self.assertTrue(api.handle("POST", "/api/jobs/remove", {"id": listed[0]["id"]})[1]["ok"])
        self.assertEqual(api.handle("GET", "/api/jobs")[1]["jobs"], [])

    def test_bad_jobs_get_a_reason_not_a_crash(self):
        for body in ({"task": "x"}, {"task": "x", "every_minutes": "lots"}, {"task": "", "at": "7am"}, "junk"):
            code, data = api.handle("POST", "/api/jobs/add", body)
            self.assertEqual(code, 400)
            self.assertTrue(data["error"])

    def test_chat_keeps_the_yes_rule_and_is_logged(self):
        asked = []
        real = api.engines.Conversation.ask
        api.engines.Conversation.ask = lambda self, text: asked.append(text) or "hello"
        try:
            self.assertEqual(api.handle("POST", "/api/chat", {"text": "hi there"})[1]["reply"], "hello")
            self.assertEqual(api.handle("POST", "/api/chat", {"text": "  "})[0], 400)
        finally:
            api.engines.Conversation.ask = real
        self.assertEqual(asked, ["hi there"])
        self.assertIsNotNone(api._chat.gate)
        self.assertEqual(api._chat.gate.last_heard, "hi there")
        self.assertTrue(api.BRAIN.search("hi there"))

    def test_notes_and_telegram(self):
        api.BRAIN.remember("likes jollof rice", "preferences")
        api.BRAIN.write_page("Gold notes", "watch the Fed")
        data = api.handle("GET", "/api/notes")[1]
        self.assertEqual(data["fact_count"], 1)
        self.assertIn("likes jollof rice", data["recent"][0])
        self.assertEqual(len(data["pages"]), 1)
        self.assertIn("enabled", api.handle("GET", "/api/telegram")[1])

    def test_only_our_own_page_may_post(self):
        import david_live
        ok = {"Content-Type": "application/json", "Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}
        self.assertTrue(david_live.dashboard_post_ok(ok))
        for bad in ({"Content-Type": "text/plain"}, {"Host": "evil.example:8765"},
                    {"Origin": "https://evil.example"}, {"Content-Type": "application/x-www-form-urlencoded"}):
            self.assertFalse(david_live.dashboard_post_ok({**ok, **bad}), bad)

    def test_unknown_paths_are_left_alone(self):
        self.assertIsNone(api.handle("GET", "/api/nope"))
        self.assertIsNone(api.handle("POST", "/api/jobs"))


if __name__ == "__main__":
    unittest.main()
