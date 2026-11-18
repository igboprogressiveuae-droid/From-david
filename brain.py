"""
David's Second Brain — a folder of Markdown files, readable in Obsidian.

    brain/
      MEMORY.md                 index of what he knows, rebuilt on every change
      memory/<category>.md      one bullet per fact:  "- 2026-09-16 · fact"
      wiki/<title>.md           longer pages David writes and reads, with [[links]]
      conversations/<day>.md    what was said, on every channel, day by day
      archive.md                every fact or page David replaced or forgot, word for word

The folder is gitignored: the repository is public and this is personal.
Set "brain_dir" in config.json to keep it somewhere else (an Obsidian vault).
Lines you add by hand that are not "- date · fact" bullets are left alone.
"""

import json
import os
import re
import threading
from datetime import datetime

_LOCK = threading.Lock()      # voice, Telegram and jobs can all write at once
_FACT = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (.+)$")


class Brain:
    def __init__(self, root, legacy_json=None):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.legacy_json = legacy_json

    # ---- paths ----

    def path(self, *parts):
        """A path inside the brain. Anything that would land outside it is refused,
           because titles come from the model."""
        p = os.path.abspath(os.path.join(self.root, *parts))
        if os.path.commonpath([p, self.root]) != self.root:
            raise ValueError("that is outside the brain folder")
        return p

    @staticmethod
    def slug(text):
        s = re.sub(r"[^\w\- ]", "", str(text)).strip().lower()
        return re.sub(r"\s+", "-", s)[:60] or "general"

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)     # never leave a note half-written

    def _archive(self, source, text):
        """Nothing David replaces is lost: the old words go to archive.md.
           Call with _LOCK held."""
        if not text.strip():
            return
        path = self.path("archive.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {datetime.now():%Y-%m-%d %H:%M} — replaced in {source}\n\n{text.strip()}\n")

    # ---- memory: short facts ----

    def facts(self):
        """[(category, date, fact)], oldest first within each category."""
        self._migrate()
        folder = self.path("memory")
        out = []
        if not os.path.isdir(folder):
            return out
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                for line in f:
                    m = _FACT.match(line.rstrip("\n"))
                    if m:
                        out.append((name[:-3], m.group(1), m.group(2)))
        return out

    def _rewrite_category(self, category, keep_line):
        """Rewrite one category file, keeping the lines keep_line(line) says to."""
        path = self.path("memory", category + ".md")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
        kept = [l for l in lines if keep_line(l)]
        self._archive(f"memory/{category}", "\n".join(l for l in lines if not keep_line(l)))
        self._write(path, "\n".join(kept))
        return len(lines) - len(kept)

    def remember(self, fact, category="general", today=None):
        fact = " ".join(str(fact).split())
        category = self.slug(category)
        today = today or datetime.now().strftime("%Y-%m-%d")

        def key_words(text):
            return {w for w in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split() if len(w) > 3}

        words = key_words(fact)

        def same_fact(line):
            m = _FACT.match(line)
            if not m or not words:
                return False
            old = key_words(m.group(2))
            return bool(old) and len(words & old) / min(len(words), len(old)) >= 0.7

        with _LOCK:
            replaced = 0
            for cat in {c for c, _, _ in self.facts()}:
                replaced += self._rewrite_category(cat, lambda l: not same_fact(l))
            path = self.path("memory", category + ".md")
            existing = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    existing = f.read().rstrip("\n")
            if not existing:
                existing = f"# {category}\n"
            self._write(path, f"{existing}\n- {today} · {fact}\n")
            self._index()
        return replaced

    def forget(self, about):
        needle = str(about).lower()
        with _LOCK:
            dropped = 0
            for cat in {c for c, _, _ in self.facts()}:
                if needle in cat:
                    dropped += self._rewrite_category(cat, lambda l: not _FACT.match(l))
                else:
                    dropped += self._rewrite_category(
                        cat, lambda l: not (_FACT.match(l) and needle in l.lower()))
            self._index()
        return dropped

    def for_prompt(self, limit=80):
        """The newest facts, grouped, for the system prompt. Capped so the
           prompt — and its cost — cannot grow without bound."""
        items = sorted(self.facts(), key=lambda x: x[1])[-limit:]
        if not items:
            return ""
        by_cat = {}
        for cat, _, fact in items:
            by_cat.setdefault(cat, []).append(fact)
        lines = []
        for cat, facts in by_cat.items():
            lines.append(f"  {cat}:")
            lines += [f"    - {f}" for f in facts]
        return ("\n\nThings they have told you to remember. Treat these as true and act on them "
                "without being reminded:\n" + "\n".join(lines))

    def _index(self):
        counts = {}
        for cat, _, _ in self.facts():
            counts[cat] = counts.get(cat, 0) + 1
        wiki = sorted(n[:-3] for n in os.listdir(self.path("wiki"))
                      if n.endswith(".md")) if os.path.isdir(self.path("wiki")) else []
        lines = ["# Memory", "", "What David knows. Edit any file — he reads them fresh.", ""]
        lines += [f"- [[memory/{c}|{c}]] — {n} fact{'s' if n != 1 else ''}"
                  for c, n in sorted(counts.items())]
        if wiki:
            lines += ["", "## Wiki", ""] + [f"- [[wiki/{w}|{w}]]" for w in wiki]
        self._write(self.path("MEMORY.md"), "\n".join(lines) + "\n")

    def _migrate(self):
        """memory.json from before the Second Brain becomes Markdown, once.
           The old file is renamed, not deleted."""
        old = self.legacy_json
        if not old or not os.path.exists(old) or os.path.isdir(self.path("memory")):
            return
        try:
            with open(old, encoding="utf-8") as f:
                items = json.load(f)
        except (ValueError, OSError):
            return
        by_cat = {}
        for i in items if isinstance(items, list) else []:
            if isinstance(i, dict) and i.get("fact"):
                learned = i.get("learned") or datetime.now().strftime("%Y-%m-%d")
                by_cat.setdefault(self.slug(i.get("category") or "general"), []).append(
                    f"- {learned} · {' '.join(str(i['fact']).split())}")
        for cat, lines in by_cat.items():
            self._write(self.path("memory", cat + ".md"), f"# {cat}\n" + "\n".join(lines) + "\n")
        os.makedirs(self.path("memory"), exist_ok=True)
        os.replace(old, old + ".migrated")
        self._index()

    # ---- wiki: longer pages ----

    def write_page(self, title, text, append=True):
        path = self.path("wiki", self.slug(title) + ".md")
        with _LOCK:
            old = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    old = f.read().rstrip("\n")
            if append and old:
                body = f"{old}\n\n## {datetime.now():%Y-%m-%d %H:%M}\n\n{text.strip()}\n"
            else:
                self._archive(os.path.relpath(path, self.root), old)
                body = f"# {title}\n\n{text.strip()}\n"
            self._write(path, body)
            self._index()
        return os.path.relpath(path, self.root)

    def read(self, name):
        """A wiki page by title, or any note by its path inside the brain
           ("conversations/2026-11-18"). None if there is no such note."""
        name = str(name).strip()
        for candidate in (self.path("wiki", self.slug(name) + ".md"),
                          self.path(name if name.endswith(".md") else name + ".md")):
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as f:
                    return f.read()
        return None

    def search(self, query, limit=8):
        """[(note path, matching line)] — plain word matching, every word must appear."""
        words = [w for w in str(query).lower().split() if w]
        hits = []
        for folder, _, files in os.walk(self.root):
            for name in sorted(files):
                if not name.endswith(".md"):
                    continue
                full = os.path.join(folder, name)
                with open(full, encoding="utf-8") as f:
                    for line in f:
                        if words and all(w in line.lower() for w in words):
                            hits.append((os.path.relpath(full, self.root)[:-3], line.strip()))
                            if len(hits) >= limit:
                                return hits
        return hits

    # ---- conversations: the raw record ----

    def log_turn(self, channel, who, text, now=None):
        text = " ".join(str(text).split())
        if not text:
            return
        now = now or datetime.now()
        path = self.path("conversations", f"{now:%Y-%m-%d}.md")
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                if new:
                    f.write(f"# Conversations {now:%Y-%m-%d}\n\n")
                f.write(f"- {now:%H:%M} [{channel}] **{who}:** {text}\n")
