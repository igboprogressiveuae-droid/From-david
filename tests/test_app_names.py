"""Opening apps by a name speech-to-text may have misheard."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-real")

import tools  # noqa: E402

APPS = {"orca": "/A/Orca.app", "terminal": "/A/Terminal.app", "localsend": "/A/LocalSend.app",
        "microsoft word": "/A/Microsoft Word.app", "ollama": "/A/Ollama.app"}


class AppNames(unittest.TestCase):
    def setUp(self):
        self.saved = (tools._APP_INDEX, tools._open_web, tools.macos.launch_app)
        tools._APP_INDEX = dict(APPS)
        self.web, self.launched = [], []
        tools._open_web = self.web.append
        tools.macos.launch_app = self.launched.append

    def tearDown(self):
        tools._APP_INDEX, tools._open_web, tools.macos.launch_app = self.saved

    def test_close_spellings_find_the_app(self):
        self.assertEqual(tools.find_installed("Oca"), "/A/Orca.app")      # not LocalSend
        self.assertEqual(tools.find_installed("word"), "/A/Microsoft Word.app")
        self.assertIsNone(tools.find_installed("Oca terminal"))           # could be either — don't guess

    def test_a_misheard_name_asks_instead_of_searching_the_web(self):
        said = tools.smart_open("Oca terminal")
        self.assertIn("Orca", said)
        self.assertIn("Terminal", said)
        self.assertEqual((self.web, self.launched), ([], []))

    def test_a_truly_unknown_name_still_searches_the_web(self):
        self.assertIn("searched the web", tools.smart_open("zzqx"))
        self.assertEqual(len(self.web), 1)

    def test_the_prompt_lists_installed_apps(self):
        self.assertIn("mishears app names", tools._load_system())


if __name__ == "__main__":
    unittest.main()
