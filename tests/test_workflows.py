"""n8n workflows: only listed ones run, with the secret, and only after a yes."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import requests  # noqa: E402
import tools  # noqa: E402


class Workflows(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.saved = (tools.requests.post, dict(tools.CFG), os.environ.get("N8N_WEBHOOK_SECRET"))
        tools.CFG["workflows"] = {"hello": "says hello"}
        tools.CFG["n8n_url"] = "http://127.0.0.1:5678"
        os.environ["N8N_WEBHOOK_SECRET"] = "s3cret"

        class Res:
            status_code, text = 200, "hello Uche"

        def post(url, headers=None, json=None, timeout=None):
            self.calls.append((url, headers, json))
            return Res()
        tools.requests.post = post

    def tearDown(self):
        tools.requests.post = self.saved[0]
        tools.CFG.clear()
        tools.CFG.update(self.saved[1])
        if self.saved[2] is None:
            os.environ.pop("N8N_WEBHOOK_SECRET", None)
        else:
            os.environ["N8N_WEBHOOK_SECRET"] = self.saved[2]

    def test_runs_a_listed_workflow_with_the_secret(self):
        self.assertEqual(tools.run_workflow("Hello", "hi"), "hello Uche")
        url, headers, body = self.calls[0]
        self.assertEqual(url, "http://127.0.0.1:5678/webhook/hello")
        self.assertEqual(headers, {"X-David-Secret": "s3cret"})
        self.assertEqual(body, {"details": "hi"})

    def test_unlisted_workflow_never_reaches_n8n(self):
        self.assertIn("no workflow called", tools.run_workflow("../rest/users"))
        self.assertEqual(self.calls, [])

    def test_missing_secret_is_said(self):
        os.environ.pop("N8N_WEBHOOK_SECRET")
        self.assertIn("N8N_WEBHOOK_SECRET", tools.run_workflow("hello"))
        self.assertEqual(self.calls, [])

    def test_n8n_down_is_said(self):
        def down(*a, **k):
            raise requests.ConnectionError()
        tools.requests.post = down
        self.assertIn("not running", tools.run_workflow("hello"))

    def test_needs_a_yes_first(self):
        self.assertIn("run_workflow", tools.DESTRUCTIVE)
        self.assertIn("needs them to say yes", tools.run("run_workflow", {"name": "hello"}))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
