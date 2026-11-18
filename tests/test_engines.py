"""The swappable brain: each provider's tool loop, against fake servers."""

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import engines  # noqa: E402
import tools  # noqa: E402


class Reply:
    def __init__(self, data, code=200):
        self.status_code, self.ok, self._data, self.text = code, code < 400, data, str(data)

    def json(self):
        return self._data


class Engines(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(tools.CFG), dict(tools.ACTIONS), tools._gemini_post,
                      engines.requests.post, tools.build_system)
        self.ran = []
        tools.ACTIONS["what_time"] = lambda: self.ran.append("what_time") or "9:05 AM"
        tools.ACTIONS["empty_bin"] = lambda: self.ran.append("empty_bin") or "emptied"
        tools.build_system = lambda: "system"

    def tearDown(self):
        cfg, actions, tools._gemini_post, engines.requests.post, tools.build_system = self.saved
        tools.CFG.clear(); tools.CFG.update(cfg)
        tools.ACTIONS.clear(); tools.ACTIONS.update(actions)
        sys.modules.pop("anthropic", None)

    def test_schema_becomes_plain_json_schema(self):
        g = {"type": "OBJECT", "properties": {"n": {"type": "NUMBER", "description": "type"}}}
        self.assertEqual(engines.json_schema(g),
                         {"type": "object", "properties": {"n": {"type": "number", "description": "type"}}})

    def test_claude_without_a_key_says_so(self):
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            self.assertIn("ANTHROPIC_API_KEY", engines.missing_key("claude") or "")
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_a_brain_cannot_call_itself_or_switch_brains(self):
        names = {d["name"] for d in engines._declarations()}
        self.assertFalse({"ask_brain", "use_brain"} & names)
        tools.CFG["brain"] = "gemini"
        replies = [
            Reply({"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "use_brain", "args": {"name": "claude"}}}]}}]}),
            Reply({"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]}),
        ]
        tools._gemini_post = lambda body, model=None, timeout=90: replies.pop(0)
        tools.ACTIONS["use_brain"] = lambda **kw: self.ran.append("use_brain") or "switched"
        convo = engines.Conversation("a test")
        convo.ask("switch")
        self.assertEqual(self.ran, [])
        self.assertIn("not available", convo.history[2]["parts"][0]["functionResponse"]["response"]["result"])

    def test_gemini_calls_a_tool_then_answers_keeping_its_parts_whole(self):
        tools.CFG["brain"] = "gemini"
        replies = [
            Reply({"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"id": "c1", "name": "what_time", "args": {}}, "thoughtSignature": "sig"}]}}]}),
            Reply({"candidates": [{"content": {"role": "model", "parts": [
                {"text": "thinking...", "thought": True}, {"text": "It's 9:05."}]}}]}),
        ]
        sent = []
        tools._gemini_post = lambda body, model=None, timeout=90: sent.append(body) or replies.pop(0)
        convo = engines.Conversation("a test")
        self.assertEqual(convo.ask("time?"), "It's 9:05.")
        self.assertEqual(self.ran, ["what_time"])
        self.assertEqual(convo.history[1]["parts"][0]["thoughtSignature"], "sig")
        self.assertEqual(convo.history[2]["parts"][0]["functionResponse"],
                         {"name": "what_time", "id": "c1", "response": {"result": "9:05 AM"}})

    def test_destructive_tools_are_refused_where_nobody_can_say_yes(self):
        self.assertIn("needs them to say yes", tools.run("empty_bin", {}, gate=None))
        self.assertEqual(self.ran, [])

    def test_destructive_tools_on_a_channel_with_a_gate_wait_for_yes(self):
        gate = tools.Gate()
        self.assertIn("CONFIRMATION_REQUIRED", tools.run("empty_bin", {}, gate))
        gate.heard("yes do it", at=__import__("time").time() + 1)
        self.assertEqual(tools.run("empty_bin", {}, gate), "emptied")

    def test_openrouter_tool_loop(self):
        tools.CFG["brain"] = "openrouter"
        os.environ["OPENROUTER_API_KEY"] = "test"
        replies = [
            Reply({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "what_time", "arguments": "{}"}}]}}]}),
            Reply({"choices": [{"message": {"role": "assistant", "content": "9:05"}}]}),
        ]
        sent = []
        engines.requests.post = lambda url, headers=None, json=None, timeout=None: sent.append(json) or replies.pop(0)
        convo = engines.Conversation("a test")
        self.assertEqual(convo.ask("time?"), "9:05")
        self.assertEqual(sent[0]["tools"][0]["type"], "function")
        self.assertEqual(sent[1]["messages"][-1], {"role": "tool", "tool_call_id": "t1", "content": "9:05 AM"})

    def test_claude_tool_loop_sends_results_together_and_uses_fallbacks(self):
        tools.CFG["brain"] = "claude"
        tools.CFG["brain_models"] = {"claude": "claude-opus-5-5"}
        block = lambda **kw: types.SimpleNamespace(**kw)
        responses = [
            types.SimpleNamespace(stop_reason="tool_use", content=[
                block(type="tool_use", id="u1", name="what_time", input={})]),
            types.SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="9:05")]),
        ]
        calls = []

        class Messages:
            def create(self, **kw):
                calls.append({**kw, "messages": list(kw["messages"])})
                return responses.pop(0)

        fake = types.ModuleType("anthropic")
        fake.Anthropic = lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=Messages()))
        sys.modules["anthropic"] = fake
        real = engines.missing_key
        engines.missing_key = lambda provider: None    # the real package need not be installed
        try:
            self.assertEqual(engines.Conversation("a test").ask("time?"), "9:05")
        finally:
            engines.missing_key = real
        self.assertEqual(calls[0]["fallbacks"], "default")
        self.assertEqual(calls[0]["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(calls[1]["messages"][-1]["content"][0],
                         {"type": "tool_result", "tool_use_id": "u1", "content": "9:05 AM"})

    def test_a_failure_clears_the_half_finished_exchange(self):
        tools.CFG["brain"] = "gemini"
        tools._gemini_post = lambda body, model=None, timeout=90: Reply({"error": "x"}, code=500)
        convo = engines.Conversation("a test")
        self.assertIn("hit a problem", convo.ask("hi"))
        self.assertEqual(convo.history, [])

    def test_switching_brain_starts_a_fresh_conversation(self):
        tools.CFG["brain"] = "gemini"
        tools._gemini_post = lambda body, model=None, timeout=90: Reply(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}]})
        convo = engines.Conversation("a test")
        convo.ask("hello")
        self.assertEqual(len(convo.history), 2)
        os.environ["OPENROUTER_API_KEY"] = "test"
        tools.CFG["brain"] = "openrouter"
        engines.requests.post = lambda *a, **k: Reply({"choices": [{"message": {"role": "assistant", "content": "yo"}}]})
        convo.ask("hello")
        self.assertEqual(convo.history[0], {"role": "user", "content": "hello"})


if __name__ == "__main__":
    unittest.main()
