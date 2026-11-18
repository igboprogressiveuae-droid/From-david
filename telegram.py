"""
Talk to David from your phone, through a Telegram bot.

    TELEGRAM_BOT_TOKEN   from @BotFather (/newbot)
    TELEGRAM_OWNER_ID    your own numeric Telegram id — David answers no one else.
                         Leave it empty, message the bot once, and it tells you yours.

Typed messages go to the thinking brain (engines.py) with every tool. Anything
that needs a yes — deleting, sending, shutting down — asks first, and only a
reply from you that reads as a yes lets it run.

Commands:  /brain gemini|claude|openrouter   /new  (start a fresh conversation)
"""

import os
import threading
import time

import requests

import engines
import tools
from tools import BRAIN, CFG, log

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 4000          # Telegram refuses messages over 4096 characters


def token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def owner_id():
    raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def enabled():
    return bool(token())


def _call(method, http_timeout=30, **params):
    """One Bot API call. The token sits in the URL, so no error from here is
       ever logged as it came: requests puts the URL in its messages."""
    t = token()
    try:
        res = requests.post(API.format(token=t, method=method), json=params, timeout=http_timeout)
        data = res.json()
    except (requests.RequestException, ValueError) as e:
        raise ConnectionError(str(e).replace(t, "<token>")) from None
    if not data.get("ok"):
        raise ConnectionError(f"Telegram refused {method}: {data.get('description', '')}")
    return data.get("result")


def send(chat_id, text):
    text = str(text).strip() or "…"
    for i in range(0, len(text), LIMIT):
        _call("sendMessage", chat_id=chat_id, text=text[i:i + LIMIT])


def send_owner(text):
    """For jobs and reminders. Quietly does nothing if Telegram isn't set up."""
    owner = owner_id()
    if not (enabled() and owner):
        return False
    try:
        send(owner, text)
        return True
    except ConnectionError as e:
        log("could not reach Telegram:", e)
        return False


class Bot:
    def __init__(self):
        self.gate = tools.Gate()
        self.convo = engines.Conversation("Telegram", gate=self.gate)
        self.offset = None

    def handle(self, message):
        """One incoming message. Returns the reply sent, or None if it was ignored."""
        chat = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        owner = owner_id()
        if chat is None:
            return None
        if owner is None:
            reply = (f"This is David. Your Telegram id is {chat}. Put TELEGRAM_OWNER_ID={chat} "
                     "in David's .env and restart him, and I'll answer you from then on.")
            log(f"Telegram: message from {chat}, but TELEGRAM_OWNER_ID is not set")
            send(chat, reply)
            return reply
        if chat != owner:
            log(f"Telegram: ignored a message from {chat} — not the owner")
            return None
        if not text:
            reply = "I can only read text for now."
            send(chat, reply)
            return reply

        if text.startswith("/new"):
            self.convo = engines.Conversation("Telegram", gate=self.gate)
            reply = "Fresh start. My memory is still here."
        elif text.startswith("/brain"):
            choice = text[len("/brain"):].strip()
            reply = (tools.use_brain(choice) if choice else
                     f"Thinking with {engines.current()} ({engines.model_for(engines.current())}). "
                     f"Switch with /brain {' | '.join(engines.PROVIDERS)}")
        elif text.startswith("/start"):
            reply = f"Hi {CFG.get('owner_name') or ''}. Ask me anything — I have the same tools as on the Mac."
        else:
            self.gate.heard(text)
            BRAIN.log_turn("telegram", CFG.get("owner_name") or "you", text)
            try:
                _call("sendChatAction", chat_id=chat, action="typing")
            except ConnectionError:
                pass
            reply = self.convo.ask(text)
            BRAIN.log_turn("telegram", "David", reply)
        send(chat, reply)
        return reply

    def poll_forever(self):
        log("Telegram is on" + ("" if owner_id() else " — waiting to learn your id"))
        wait = 2
        while True:
            try:
                # Telegram holds the request open up to 50s waiting for a message
                updates = _call("getUpdates", http_timeout=60, offset=self.offset, timeout=50,
                                allowed_updates=["message"]) or []
                wait = 2
            except ConnectionError as e:
                log(f"Telegram unreachable, trying again in {wait}s:", e)
                time.sleep(wait)
                wait = min(60, wait * 2)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                if u.get("message"):
                    try:
                        self.handle(u["message"])
                    except Exception as e:
                        log("Telegram message failed:", e)


def start():
    """Run the bot on its own thread, if a token is set. Returns the thread or None."""
    if not enabled():
        return None
    t = threading.Thread(target=Bot().poll_forever, daemon=True, name="telegram")
    t.start()
    return t
