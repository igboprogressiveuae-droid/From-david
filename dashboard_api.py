"""
The dashboard's endpoints for the newer parts of David: the chat box, the
thinking brain, background jobs, the Second Brain and Telegram.

david_live's interface server hands any /api/ path it doesn't know to
handle() here. Clicking a button on the dashboard is the owner acting
directly, so nothing here waits for a spoken yes — except what the chat box
asks the brain to do, which goes through the same yes-first rule as Telegram.
"""

import os
import threading

import engines
import jobs
import telegram
import tools
from tools import BRAIN, CFG

_chat = None
_chat_lock = threading.Lock()


def chat(text):
    global _chat
    with _chat_lock:          # one typed conversation; a second message waits for the first answer
        if _chat is None:
            _chat = engines.Conversation("the dashboard chat box", gate=tools.Gate())
        _chat.gate.heard(text)
        BRAIN.log_turn("dashboard", CFG.get("owner_name") or "you", text)
        reply = _chat.ask(text)
        BRAIN.log_turn("dashboard", "David", reply)
        return reply


def handle(method, path, body=None):
    """Returns (status code, JSON-able dict), or None if the path isn't one of these."""
    body = body if isinstance(body, dict) else {}
    if method == "GET":
        if path == "/api/brain":
            return 200, {"current": engines.current(), "brains": [
                {"name": p, "model": engines.model_for(p), "problem": engines.missing_key(p)}
                for p in engines.PROVIDERS]}
        if path == "/api/jobs":
            return 200, {"jobs": [{"id": j["id"], "text": jobs.describe(j), "next_run": j["next_run"],
                                   "last_result": j.get("last_result") or ""} for j in jobs.load()]}
        if path == "/api/notes":
            facts = BRAIN.facts()
            wiki = BRAIN.path("wiki")
            pages = sorted(n[:-3] for n in os.listdir(wiki) if n.endswith(".md")) if os.path.isdir(wiki) else []
            return 200, {"folder": BRAIN.root, "fact_count": len(facts), "pages": pages,
                         "recent": [f"{fact} ({category}, {day})" for category, day, fact in facts[-6:]]}
        if path == "/api/telegram":
            return 200, {"enabled": telegram.enabled(), "owner_set": telegram.owner_id() is not None}
    if method == "POST":
        if path == "/api/chat":
            text = str(body.get("text", "")).strip()
            if not text:
                return 400, {"ok": False, "error": "type something first"}
            return 200, {"ok": True, "reply": chat(text)}
        if path == "/api/brain":
            return 200, {"ok": True, "result": tools.use_brain(body.get("name", ""))}
        if path == "/api/jobs/add":
            try:
                job = jobs.add(body.get("task", ""), every_minutes=body.get("every_minutes") or 0,
                               at=str(body.get("at", "")).strip(), days=body.get("days", ""))
            except (ValueError, TypeError) as e:
                return 400, {"ok": False, "error": str(e)}
            return 200, {"ok": True, "job": jobs.describe(job)}
        if path == "/api/jobs/remove":
            return 200, {"ok": jobs.remove(body.get("id", ""))}
    return None
