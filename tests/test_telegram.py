"""Telegram: only the owner gets answers, and the token never reaches a log."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import requests  # noqa: E402
import telegram  # noqa: E402
import tools  # noqa: E402
from brain import Brain  # noqa: E402

TOKEN = "123456:SECRET-token"


class Telegram(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.saved_post, self.saved_brain = telegram.requests.post, telegram.BRAIN
        self.tmp = tempfile.TemporaryDirectory()
        telegram.BRAIN = Brain(self.tmp.name)
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN

        class Ok:
            def json(self_):
                return {"ok": True, "result": []}

        def post(url, json=None, timeout=None):
            self.sent.append((url.rsplit("/", 1)[1], json))
            return Ok()
        telegram.requests.post = post
        self.bot = telegram.Bot()
        self.asked = []
        self.bot.convo.ask = lambda text: self.asked.append(text) or "answer"

    def tearDown(self):
        telegram.requests.post, telegram.BRAIN = self.saved_post, self.saved_brain
        os.environ.pop("TELEGRAM_OWNER_ID", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.tmp.cleanup()

    def texts(self):
        return [p["text"] for m, p in self.sent if m == "sendMessage"]

    def test_without_an_owner_it_only_says_your_id(self):
        os.environ.pop("TELEGRAM_OWNER_ID", None)
        self.bot.handle({"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "delete everything"})
        self.assertEqual(self.asked, [])
        self.assertIn("TELEGRAM_OWNER_ID=42", self.texts()[0])

    def test_strangers_are_ignored(self):
        os.environ["TELEGRAM_OWNER_ID"] = "42"
        self.assertIsNone(self.bot.handle({"chat": {"id": 666, "type": "private"}, "from": {"id": 666}, "text": "hi"}))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.asked, [])

    def test_the_owner_reaches_the_brain_and_is_logged(self):
        os.environ["TELEGRAM_OWNER_ID"] = "42"
        self.assertEqual(self.bot.handle({"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "what's on today"}), "answer")
        self.assertEqual(self.asked, ["what's on today"])
        self.assertEqual(self.bot.gate.last_heard, "what's on today")
        self.assertEqual(len(telegram.BRAIN.search("what's on today")), 1)

    def test_a_typed_yes_clears_a_destructive_action(self):
        os.environ["TELEGRAM_OWNER_ID"] = "42"
        saved = dict(tools.ACTIONS)
        tools.ACTIONS["empty_bin"] = lambda: "emptied"
        try:
            self.assertIn("CONFIRMATION_REQUIRED", tools.run("empty_bin", {}, self.bot.gate))
            self.bot.gate.pending = {k: v - 1 for k, v in self.bot.gate.pending.items()}
            self.bot.handle({"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "yes go ahead"})
            self.assertEqual(tools.run("empty_bin", {}, self.bot.gate), "emptied")
        finally:
            tools.ACTIONS.clear(); tools.ACTIONS.update(saved)

    def test_groups_and_forged_senders_are_ignored(self):
        os.environ["TELEGRAM_OWNER_ID"] = "42"
        self.assertIsNone(self.bot.handle({"chat": {"id": -100, "type": "group"}, "from": {"id": 42}, "text": "hi"}))
        self.assertIsNone(self.bot.handle({"chat": {"id": 42, "type": "private"}, "from": {"id": 7}, "text": "hi"}))
        os.environ["TELEGRAM_OWNER_ID"] = "-100"
        self.assertIsNone(telegram.owner_id())
        self.assertEqual(self.asked, [])

    def test_long_replies_are_split(self):
        telegram.send(42, "x" * 9000)
        self.assertEqual([len(t) for t in self.texts()], [4000, 4000, 1000])

    def test_the_token_never_appears_in_an_error(self):
        def boom(url, json=None, timeout=None):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
        telegram.requests.post = boom
        with self.assertRaises(ConnectionError) as caught:
            telegram._call("getMe")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIn("<token>", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
