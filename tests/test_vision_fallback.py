"""Reading the screen when Gemini is down."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import engines  # noqa: E402
import tools  # noqa: E402


class VisionFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.tmp.write(b"jpeg")
        self.tmp.close()
        self.saved = (tools._look_gemini, tools._look_claude, tools._grab_screen_png,
                      tools.macos.can_see_screen, engines.missing_key, tools.CFG.get("groq_key"))
        tools._VISION_DOWN.clear()
        tools.CFG["groq_key"] = ""
        tools._grab_screen_png = lambda *a, **k: self.tmp.name
        tools.macos.can_see_screen = lambda: True
        tools._look_gemini = lambda q, img, p: (None, "gemini returned 503")

    def tearDown(self):
        (tools._look_gemini, tools._look_claude, tools._grab_screen_png,
         tools.macos.can_see_screen, engines.missing_key, tools.CFG["groq_key"]) = self.saved
        tools._VISION_DOWN.clear()
        os.unlink(self.tmp.name)

    def test_busy_gemini_hands_the_screen_to_claude(self):
        engines.missing_key = lambda p: None
        tools._look_claude = lambda q, img, p: ("A code editor with David's project open.", None)
        self.assertEqual(tools.describe_screen(), "A code editor with David's project open.")

    def test_without_claude_he_says_what_went_wrong(self):
        engines.missing_key = lambda p: "Claude isn't signed in"
        tools._look_claude = lambda q, img, p: self.fail("Claude isn't signed in, so it must not be tried")
        self.assertEqual(tools.describe_screen(), "gemini returned 503")


if __name__ == "__main__":
    unittest.main()
