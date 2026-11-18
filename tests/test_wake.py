"""The wake phrase, and falling back asleep."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import david_live as d  # noqa: E402
import engines  # noqa: E402
import tools  # noqa: E402
import wake  # noqa: E402


class FakeRecognizer:
    """Says what it 'heard' once each script entry's audio arrives."""

    def __init__(self, script):
        self.script = list(script)       # [(finished?, text)] per chunk
        self.last = ""

    def AcceptWaveform(self, pcm):
        done, self.last = self.script.pop(0)
        return done

    def Result(self):
        return json.dumps({"text": self.last})


class Listener(unittest.TestCase):
    def listener(self, script):
        made = []

        def make():
            made.append(FakeRecognizer(script))
            return made[-1]
        return wake.Listener("Wake up, David!", make=make), made

    def test_only_whole_finished_sentences_count(self):
        lst, _ = self.listener([(False, "wake up david"), (True, "wake up daniel"),
                                (True, "wake up davidson"), (True, "okay wake up david please")])
        self.assertEqual([lst.heard(b"x") for _ in range(4)], [False, False, False, True])

    def test_a_wake_starts_a_fresh_recognizer(self):
        lst, made = self.listener([(True, "wake up david")])
        self.assertTrue(lst.heard(b"x"))
        self.assertEqual(len(made), 2)

    def test_no_phrase_never_wakes(self):
        lst = wake.Listener("", make=lambda: FakeRecognizer([(True, "wake up david")]))
        self.assertFalse(lst.heard(b"x"))


@unittest.skipUnless(os.path.isdir(wake.MODEL_DIR) and shutil.which("say"),
                     "needs the downloaded speech model and macOS 'say'")
class RealVoice(unittest.TestCase):
    """Spoken by macOS, heard by Vosk — the same path as a real microphone."""

    @classmethod
    def setUpClass(cls):
        cls.listener = wake.Listener("wake up david")
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def hears(self, sentence):
        path = os.path.join(self.tmp.name, "s.wav")
        subprocess.run(["say", "-o", path, "--data-format=LEI16@16000", sentence], check=True)
        with wave.open(path) as w:
            pcm = w.readframes(w.getnframes()) + b"\0" * 96000   # the big model waits ~1.3s of quiet
        return any(self.listener.heard(pcm[i:i + 2048]) for i in range(0, len(pcm), 2048))

    def test_the_phrase_wakes_him(self):
        self.assertTrue(self.hears("Wake up David"))
        self.assertTrue(self.hears("Wake up David, what's the weather?"))

    def test_near_misses_do_not(self):
        for sentence in ("Wake up Daniel", "Hey, wake up", "David is my friend", "Make up David"):
            self.assertFalse(self.hears(sentence), sentence)


class FallingAsleep(unittest.TestCase):
    def setUp(self):
        self.saved = (tools.CFG.get("wake_phrase"), tools.CFG.get("sleep_after_minutes"), asyncio.sleep)
        tools.SLEEP_NOW.clear()
        real = asyncio.sleep
        asyncio.sleep = lambda _s: real(0)       # the watcher checks every 2s; tests needn't wait

    def tearDown(self):
        tools.CFG["wake_phrase"], tools.CFG["sleep_after_minutes"], asyncio.sleep = self.saved
        tools.SLEEP_NOW.clear()

    def test_go_to_sleep_needs_a_wake_phrase(self):
        tools.CFG["wake_phrase"] = ""
        self.assertIn("no wake phrase", tools.go_to_sleep())
        self.assertFalse(tools.SLEEP_NOW.is_set())
        tools.CFG["wake_phrase"] = "wake up david"
        self.assertIn("wake up david", tools.go_to_sleep())
        self.assertTrue(tools.SLEEP_NOW.is_set())

    def test_asked_to_sleep_he_sleeps_but_not_mid_sentence(self):
        live = d.Live()
        tools.SLEEP_NOW.set()
        live.speaking.set()
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(live.watch_for_sleep(), 0.2))
        live.speaking.clear()
        with self.assertRaises(d.GoToSleep):
            asyncio.run(asyncio.wait_for(live.watch_for_sleep(), 1))
        self.assertFalse(tools.SLEEP_NOW.is_set())

    def test_a_quiet_spell_puts_him_to_sleep(self):
        live = d.Live()
        tools.CFG["sleep_after_minutes"] = 5
        live.last_active -= 5 * 60 + 1
        with self.assertRaises(d.GoToSleep):
            asyncio.run(asyncio.wait_for(live.watch_for_sleep(), 1))

    def test_text_brains_cannot_put_the_voice_to_sleep(self):
        self.assertIn("go_to_sleep", engines.NOT_FOR_ENGINES)


if __name__ == "__main__":
    unittest.main()
