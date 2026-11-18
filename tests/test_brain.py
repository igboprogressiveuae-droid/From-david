"""The Second Brain: Markdown memory that survives being edited by hand."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from brain import Brain  # noqa: E402


class SecondBrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Brain(os.path.join(self.tmp.name, "brain"))

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, *parts):
        with open(os.path.join(self.b.root, *parts), encoding="utf-8") as f:
            return f.read()

    def test_a_fact_is_a_bullet_in_its_category_file(self):
        self.b.remember("Prefers Edge for work", "Preferences", today="2026-11-18")
        self.assertIn("- 2026-11-18 · Prefers Edge for work", self.read("memory", "preferences.md"))
        self.assertIn("[[memory/preferences|preferences]] — 1 fact", self.read("MEMORY.md"))
        self.assertIn("Prefers Edge for work", self.b.for_prompt())

    def test_a_restated_fact_replaces_the_old_one_even_across_categories(self):
        self.b.remember("Lives in Abu Dhabi with family", "people")
        self.b.remember("Lives in Abu Dhabi with family now", "home")
        self.assertEqual([f for _, _, f in self.b.facts()], ["Lives in Abu Dhabi with family now"])

    def test_hand_written_lines_survive_remember_and_forget(self):
        os.makedirs(os.path.join(self.b.root, "memory"))
        with open(os.path.join(self.b.root, "memory", "work.md"), "w") as f:
            f.write("# work\n\nMy own note, not a bullet.\n- 2026-01-01 · Works at IPC\n")
        self.b.remember("Meets the board on Mondays", "work")
        self.assertEqual(self.b.forget("board"), 1)
        text = self.read("memory", "work.md")
        self.assertIn("My own note, not a bullet.", text)
        self.assertIn("Works at IPC", text)
        self.assertNotIn("board", text)

    def test_forgetting_a_category_keeps_its_heading(self):
        self.b.remember("Likes gold", "trading")
        self.b.remember("Watches oil", "trading")
        self.assertEqual(self.b.forget("trading"), 2)
        self.assertEqual(self.b.facts(), [])

    def test_old_memory_json_moves_in_once_and_is_kept(self):
        legacy = os.path.join(self.tmp.name, "memory.json")
        with open(legacy, "w") as f:
            json.dump([{"fact": "Name is Uche", "category": "preferences", "learned": "2026-09-16"}], f)
        b = Brain(os.path.join(self.tmp.name, "brain"), legacy_json=legacy)
        self.assertEqual(b.facts(), [("preferences", "2026-09-16", "Name is Uche")])
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy + ".migrated"))

    def test_titles_cannot_escape_the_brain(self):
        with self.assertRaises(ValueError):
            self.b.path("..", "outside.md")
        with self.assertRaises(ValueError):
            self.b.read("../../etc/passwd")
        where = self.b.write_page("../../etc/passwd", "x")
        self.assertTrue(where.startswith("wiki" + os.sep))

    def test_wiki_pages_append_and_are_searchable(self):
        self.b.write_page("Gold", "Bought at 2,400 [[Trading]]")
        self.b.write_page("Gold", "Sold at 2,600")
        page = self.b.read("Gold")
        self.assertIn("Bought at 2,400", page)
        self.assertIn("Sold at 2,600", page)
        self.assertEqual(self.b.search("sold 2,600")[0][0], os.path.join("wiki", "gold"))

    def test_conversations_are_logged_by_day(self):
        self.b.log_turn("telegram", "Uche", "hello\nthere", now=datetime(2026, 11, 18, 9, 5))
        self.assertIn("- 09:05 [telegram] **Uche:** hello there",
                      self.b.read("conversations/2026-11-18"))


if __name__ == "__main__":
    unittest.main()
