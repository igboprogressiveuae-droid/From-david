"""
Checks for the parts of David that need no microphone, no key and no network.

    python -m unittest discover -s tests

Nothing here clicks, types, speaks or opens anything.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import david_live as d  # noqa: E402
import tools as t  # noqa: E402
import macos  # noqa: E402

t.CFG["spoken_acknowledgement"] = False     # a slow job must not say "On it." mid-test


class RefusalKind(unittest.TestCase):
    """The reconnect loop gave up for good on the message Google sends when a
       saved session has expired, because it reads as "not found" — David went
       on serving his interface with no connection behind it."""

    def test_expired_session_starts_a_fresh_one(self):
        said = ("received 1008 (policy violation) BidiGenerateContent session not found; "
                "then sent 1008 (policy violation) BidiGenerateContent session not found")
        self.assertEqual(d._refusal_kind(said), "stale")

    def test_a_wrong_model_is_still_fatal(self):
        self.assertEqual(d._refusal_kind("models/gemini-nope not found"), "model")
        self.assertEqual(d._refusal_kind("received 1007 (invalid frame payload data)"), "model")

    def test_a_bad_key_is_fatal(self):
        self.assertEqual(d._refusal_kind("API key not valid. Please pass a valid API key."), "key")

    def test_anything_else_is_retried(self):
        self.assertEqual(d._refusal_kind("no close frame received or sent"), "retry")
        self.assertEqual(d._refusal_kind(""), "retry")
        self.assertEqual(d._refusal_kind(None), "retry")


class Confirmation(unittest.TestCase):
    def test_yes_is_whole_words(self):
        for said in ("yes", "Yeah, go ahead", "go ahead now", "sure, do it"):
            self.assertTrue(t._is_yes(said), said)

    def test_anything_doubtful_is_a_no(self):
        for said in ("no", "yes, don’t", "not now", "yes wait", "yesterday", "I know", ""):
            self.assertFalse(t._is_yes(said), said)

    def test_destructive_action_needs_a_later_yes(self):
        live = d.Live()
        args = {"name": "report.pdf"}
        self.assertIn("CONFIRMATION_REQUIRED", live.gate.check("delete_file", args))
        self.assertIn("not answered", live.gate.check("delete_file", args))
        live.gate.heard("yes go ahead", at=time.time() + 1)
        self.assertIsNone(live.gate.check("delete_file", args))

    def test_destructive_action_declined(self):
        live = d.Live()
        live.gate.check("empty_bin", {})
        live.gate.heard("no leave it", at=time.time() + 1)
        self.assertIn("did not confirm", live.gate.check("empty_bin", {}))


class Repeats(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.saved = dict(t.ACTIONS)
        for name in ("scroll", "check_email"):
            t.ACTIONS[name] = lambda _n=name, **kw: self.calls.append((_n, kw)) or "ok"

    def tearDown(self):
        t.ACTIONS.clear()
        t.ACTIONS.update(self.saved)

    def run_calls(self, *calls):
        live = d.Live()

        async def go():
            return [await live.run_one({"id": str(i), "name": n, "args": a})
                    for i, (n, a) in enumerate(calls)]
        return asyncio.run(go())

    def test_scrolling_twice_scrolls_twice(self):
        self.run_calls(("scroll", {"direction": "down"}), ("scroll", {"direction": "up"}))
        self.assertEqual(len(self.calls), 2)

    def test_identical_scroll_in_the_same_breath_is_dropped(self):
        out = self.run_calls(("scroll", {"direction": "down"}), ("scroll", {"direction": "down"}))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("already doing that", out[1]["response"]["result"])

    def test_screen_jobs_still_refuse_a_pile_up(self):
        self.run_calls(("check_email", {}), ("check_email", {"sender": "bank"}))
        self.assertEqual(len(self.calls), 1)


class Clicking(unittest.TestCase):
    def test_point_maps_onto_the_screen_that_was_captured(self):
        # a second screen to the right of a 1792-wide main one
        self.assertEqual(t._point_on_screen([500, 500], (1792, 0, 3712, 1080)), (2752, 540))
        self.assertEqual(t._point_on_screen([0, 1000], (0, 0, 1792, 1120)), (1792, 0))

    def test_missing_permission_is_said_not_hidden(self):
        if macos.can_see_screen():
            self.skipTest("Screen Recording is granted here")
        self.assertEqual(t.describe_screen(), macos.NO_SCREEN)


class Setup(unittest.TestCase):
    def test_every_declared_tool_exists(self):
        declared = {t["name"] for t in t.declarations()}
        self.assertEqual(declared - set(t.ACTIONS), set())
        self.assertEqual(set(t.ACTIONS) - declared, set())
        self.assertEqual(t.REPEATABLE - set(t.ACTIONS), set())

    def test_prompt_is_personalised(self):
        self.assertNotIn("Ibans", t.SYSTEM)
        self.assertNotIn("{name}", t.SYSTEM)
        self.assertIn(t.CFG["owner_name"], t.SYSTEM)

    def test_no_windows_calls_left(self):
        for name in ("david_live.py", "tools.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                source = f.read().lower()
            for word in ("windll", "powershell", "startfile", "taskkill", "imagegrab"):
                self.assertFalse(word in source, f"{word} is still in {name}")

    def test_config_holds_no_keys(self):
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertFalse({"api_key", "groq_key", "email"} & set(cfg))

    def test_gitignore_covers_secrets(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            ignored = set(f.read().split())
        for name in (".env", "credentials.json", "gmail_token.json", "notion_token.json", "memory.json"):
            self.assertIn(name, ignored)

    def test_env_file_is_read_without_overriding_the_shell(self):
        saved = t.HERE
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env"), "w") as f:
                f.write("# a comment\nDAVID_TEST_A='from file'\nDAVID_TEST_B=from file\n")
            os.environ["DAVID_TEST_B"] = "from shell"
            try:
                t.HERE = tmp
                t._load_env_file()
                self.assertEqual(os.environ["DAVID_TEST_A"], "from file")
                self.assertEqual(os.environ["DAVID_TEST_B"], "from shell")
            finally:
                t.HERE = saved
                os.environ.pop("DAVID_TEST_A", None)
                os.environ.pop("DAVID_TEST_B", None)

    def test_research_cache_key_collapses_spaces(self):
        self.assertEqual(t._research_cache_key("Gold   price", " cheap"),
                         t._research_cache_key("gold price", " cheap"))


class Microphone(unittest.TestCase):
    def test_dead_silence_warns_once(self):
        watch = d.SilenceWatch(seconds=4)
        self.assertFalse(watch.feed(0, 100.0))
        self.assertFalse(watch.feed(0, 103.9))
        self.assertTrue(watch.feed(0, 104.0))
        self.assertFalse(watch.feed(0, 120.0))

    def test_any_sound_means_the_mic_works(self):
        watch = d.SilenceWatch(seconds=4)
        watch.feed(0, 100.0)
        watch.feed(12, 101.0)
        self.assertFalse(watch.feed(0, 200.0))


class GeminiCalls(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.saved = d.requests.post

    def tearDown(self):
        d.requests.post = self.saved

    def fake(self, *replies):
        class Reply:
            def __init__(self, code, text=""):
                self.status_code, self.text, self.ok = code, text, code < 400
        queue = [Reply(*r) for r in replies]

        def post(url, headers=None, json=None, timeout=None):
            self.sent.append({"url": url, "headers": headers, "json": json})
            return queue.pop(0)
        d.requests.post = post

    def test_thinking_level_is_dropped_if_the_model_refuses_it(self):
        self.fake((400, "Invalid argument: thinking_level is not supported"), (200,))
        body = {"generationConfig": {"maxOutputTokens": 5, **t._thinking("low")}}
        self.assertEqual(t._gemini_post(body).status_code, 200)
        self.assertIn("thinkingConfig", self.sent[0]["json"]["generationConfig"])
        self.assertEqual(self.sent[1]["json"]["generationConfig"], {"maxOutputTokens": 5})

    def test_other_errors_are_not_retried(self):
        self.fake((400, "API key not valid"))
        body = {"generationConfig": t._thinking("low")}
        self.assertEqual(t._gemini_post(body).status_code, 400)
        self.assertEqual(len(self.sent), 1)

    def test_key_goes_in_a_header_never_the_url(self):
        self.fake((200,))
        t._gemini_post({})
        self.assertNotIn(t.CFG["api_key"], self.sent[0]["url"])
        self.assertEqual(self.sent[0]["headers"]["x-goog-api-key"], t.CFG["api_key"])

    def test_no_thinking_level_means_no_setting(self):
        self.assertEqual(t._thinking(""), {})


class Browser(unittest.TestCase):
    def test_tab_script_escapes_the_address(self):
        script = macos.browser_tab_script("Google Chrome", 'https://x.com/?q="a"', "x.com")
        self.assertIn('set URL of t to "https://x.com/?q=\\"a\\""', script)
        self.assertIn('tell application "Google Chrome"', script)

    def test_safari_is_not_scripted(self):
        self.assertIsNone(macos.show_in_browser_tab("Safari", "https://x.com", "x.com"))


class Reminders(unittest.TestCase):
    # a Wednesday, 2:24 in the morning
    NOW = __import__("datetime").datetime(2026, 9, 16, 2, 24)

    def test_clock_times(self):
        for said, clock in (("9", (9, 0)), ("9am", (9, 0)), ("9:30 p.m.", (21, 30)),
                            ("21:30", (21, 30)), ("12 am", (0, 0)), ("12pm", (12, 0))):
            self.assertEqual(t._parse_clock(said), clock, said)
        for said in ("25:00", "9:75", "noonish", ""):
            self.assertIsNone(t._parse_clock(said), said)

    def test_no_day_means_the_next_time_it_comes_round(self):
        when, _ = t._resolve_when("9am", "", self.NOW)
        self.assertEqual((when.day, when.hour), (16, 9))
        when, _ = t._resolve_when("1am", "", self.NOW)
        self.assertEqual((when.day, when.hour), (17, 1))

    def test_days(self):
        self.assertEqual(t._resolve_when("9am", "tomorrow", self.NOW)[0].day, 17)
        self.assertEqual(t._resolve_when("9am", "Friday", self.NOW)[0].day, 18)
        self.assertEqual(t._resolve_when("1am", "wednesday", self.NOW)[0].day, 23)
        self.assertEqual(t._resolve_when("9am", "2026-10-01", self.NOW)[0].month, 10)

    def test_impossible_requests_say_why(self):
        self.assertIn("already passed", t._resolve_when("1am", "today", self.NOW)[1])
        self.assertIn("which day", t._resolve_when("9am", "someday", self.NOW)[1])
        self.assertIn("what time", t._resolve_when("soon", "", self.NOW)[1])

    def test_reminder_script(self):
        script = macos.reminder_script('Call "Mum"', self.NOW.replace(hour=9, minute=30))
        self.assertIn('name:"Call \\"Mum\\""', script)
        self.assertIn("set month of d to 9", script)
        self.assertIn(f"set time of d to {9 * 3600 + 30 * 60}", script)
        self.assertLess(script.index("set day of d to 1"), script.index("set month of d"))


class Resampling(unittest.TestCase):
    def pcm(self, values):
        import array as a
        return a.array("h", values).tobytes()

    def samples(self, pcm):
        import array as a
        out = a.array("h")
        out.frombytes(pcm)
        return list(out)

    def test_same_rate_is_untouched(self):
        raw = self.pcm([1, 2, 3])
        self.assertIs(d._resample(raw, 24000, 24000), raw)

    def test_doubling_keeps_every_original_sample_in_place(self):
        out = self.samples(d._resample(self.pcm([0, 100, 200, 300]), 24000, 48000))
        self.assertEqual(len(out), 8)
        self.assertEqual(out[::2], [0, 100, 200, 300])
        self.assertEqual(out[1], 50)        # halfway between 0 and 100

    def test_length_follows_the_rate_ratio(self):
        raw = self.pcm(list(range(480)))
        self.assertEqual(len(self.samples(d._resample(raw, 24000, 44100))), 882)
        self.assertEqual(len(self.samples(d._resample(raw, 24000, 16000))), 320)

    def test_empty_and_odd_input_survive(self):
        self.assertEqual(d._resample(b"", 24000, 48000), b"")
        self.assertEqual(len(self.samples(d._resample(b"\x01", 24000, 48000))), 0)

    def test_a_tone_keeps_its_shape(self):
        import math
        tone = [int(12000 * math.sin(2 * math.pi * 200 * n / 24000)) for n in range(240)]
        out = self.samples(d._resample(self.pcm(tone), 24000, 48000))
        self.assertLessEqual(max(out), 12100)
        self.assertGreaterEqual(min(out), -12100)
        # the peaks should land at twice the sample index they did before
        self.assertAlmostEqual(out[60], tone[30], delta=200)


class Sound(unittest.TestCase):
    def test_laptop_speakers_mean_no_talking_over_david(self):
        self.assertFalse(d._interruption_allowed("auto", "MacBook Pro Speakers"))

    def test_headphones_allow_talking_over_david(self):
        for name in ("External Headphones", "AirPods Pro", "USB Headset"):
            self.assertTrue(d._interruption_allowed("auto", name), name)

    def test_unknown_output_plays_safe(self):
        self.assertFalse(d._interruption_allowed("auto", ""))

    def test_config_can_force_either_way(self):
        self.assertTrue(d._interruption_allowed(True, "MacBook Pro Speakers"))
        self.assertFalse(d._interruption_allowed(False, "AirPods Pro"))


class Mac(unittest.TestCase):
    def test_screens(self):
        screens = macos.displays()
        self.assertGreaterEqual(len(screens), 1)
        self.assertTrue(all(s["right"] > s["left"] and s["bottom"] > s["top"] for s in screens))
        self.assertIsNotNone(t._main_screen(screens))

    def test_system_numbers_are_sane(self):
        self.assertGreater(macos.cpu_ticks()[1], 0)
        self.assertTrue(0 < macos.ram_percent() <= 100)
        self.assertTrue(0 < macos.disk_percent() <= 100)
        percent, _plugged = macos.battery_status()
        self.assertTrue(percent is None or 0 <= percent <= 100)

    def test_disks_and_apps(self):
        self.assertIn("GB", t._speech(t.list_drives()))
        self.assertIn("safari", macos.installed_apps())

    def test_applescript_strings_are_escaped(self):
        self.assertEqual(macos._q('say "hi" \\ bye'), '"say \\"hi\\" \\\\ bye"')

    def test_keys(self):
        for key in ("enter", "escape", "tab", "f11", "v"):
            self.assertTrue(macos.has_key(key))
        self.assertFalse(macos.has_key("hyper"))


if __name__ == "__main__":
    unittest.main()
