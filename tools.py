"""
David's tool hub — every action he can take on the Mac, in one place.

The voice session (david_live.py), Telegram (telegram.py), the thinking
engines (engines.py) and background jobs (jobs.py) all call the same tools
from here, so a tool written once works on every channel.
"""
import base64
import difflib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta

import threading
import time

import email as emaillib
import imaplib
from email.header import decode_header

try:
    import oauth_gmail
    _HAVE_OAUTH_GMAIL = True
except ImportError:
    _HAVE_OAUTH_GMAIL = False

try:
    import oauth_notion
    _HAVE_OAUTH_NOTION = True
except ImportError:
    _HAVE_OAUTH_NOTION = False

if sys.platform != "darwin":
    sys.exit("This version of David runs on macOS. The Windows original is in the git history.")

import requests

import macos
from brain import Brain

HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

def _load_env_file():
    """KEY=value lines from .env next to this script, into the environment.
       Anything already set in the shell wins."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        sys.exit("config.json is missing.")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    # Keys come from the environment or .env, never config.json — that file
    # is committed, and the repository is public.
    _load_env_file()
    cfg["api_key"] = os.environ.get("GEMINI_API_KEY", "").strip()
    cfg["groq_key"] = os.environ.get("GROQ_API_KEY", "").strip()
    if os.environ.get("EMAIL_ADDRESS"):
        cfg["email"] = {**cfg.get("email", {}), "address": os.environ["EMAIL_ADDRESS"],
                        "app_password": os.environ.get("EMAIL_APP_PASSWORD", "")}
    if not cfg["api_key"] and "--check" not in sys.argv:
        sys.exit("No Gemini key. Copy .env.example to .env and put your key in it.")
    return cfg


CFG = load_config()


def _update_config(**changes):
    """Persist a few keys to config.json without ever leaving it half-written —
       write the new version to a temp file first, then swap it into place."""
    path = os.path.join(HERE, "config.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.update(changes)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def log(*parts):
    print(f"[{datetime.now():%H:%M:%S}] " + " ".join(str(p) for p in parts), flush=True)


# Fields that hold whatever he dictated or wrote — the log is a plain text
# file served to no one now, but there is no reason a transcript needs to
# carry the full body of every email and note verbatim.
_LOG_REDACT = {"content", "body", "text"}


def _log_args(args):
    if not isinstance(args, dict):
        return args
    return {k: (f"<{len(v)} chars>" if k in _LOG_REDACT and isinstance(v, str) else v)
            for k, v in args.items()}


class Panel:
    """A tool result that has something to show on the HUD, not just say.

       `speech` is exactly what goes back to the model to read aloud — the
       same plain string every other tool already returns. `type` and the
       keyword fields in `data` are the structured half, and never reach
       the model at all; run_one peels them off for the HUD and hands the
       model only `speech`. That split is what keeps ordinary conversation
       from ever touching the panel — only a tool that deliberately builds
       one of these can put something on screen."""

    def __init__(self, speech, type, **data):
        self.speech = speech
        self.type = type
        self.data = data


def _speech(result):
    """The spoken half of a tool result, whether or not it built a Panel."""
    return result.speech if isinstance(result, Panel) else result


# ----------------------------------------------------------------------------
# things he can do
# ----------------------------------------------------------------------------

def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def _open_web(url, new_window=False):
    """Every web address goes through his working browser, never the system default."""
    app = CFG.get("browsers", {}).get(CFG.get("work_browser") or "chrome")
    if app and _app_path(app):
        chromium = app != "Safari"
        macos.open_target(url, app, ["--new-window"] if new_window and chromium else None)
        return True
    macos.open_target(url)
    return False


def find_file(name, limit=8):
    needle = name.lower().strip()
    hits = []
    for folder in CFG.get("search_folders", []):
        root = expand(folder)
        if not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")][:40]
            for f in files:
                if needle in f.lower():
                    hits.append(os.path.join(base, f))
                    if len(hits) >= limit:
                        return hits
    return hits


def open_app(name):
    target = CFG.get("apps", {}).get(name.lower())
    if not target:
        return f"{name} is not in the app list."
    ok = macos.open_target(target) if "://" in target else macos.launch_app(target)
    return f"opened {name}" if ok else f"{name} would not open — is it installed?"


def open_site(name):
    url = CFG.get("sites", {}).get(name.lower())
    if not url:
        return f"{name} is not in the site list."
    _open_web(url)
    return f"opened {name}"


def open_url(url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _open_web(url)
    return "opened"


def _resolve(target):
    """Turn a site name or a rough address into a real URL."""
    known = CFG.get("sites", {}).get(target.lower())
    if known:
        return known
    if target.startswith(("http://", "https://")):
        return target
    if "." in target and " " not in target:
        return "https://" + target
    return "https://www.google.com/search?q=" + urllib.parse.quote(target)


def open_in_browser(browser, target):
    """Open something in a named browser rather than the default one."""
    browsers = CFG.get("browsers", {})
    exe = browsers.get(browser.lower()) or CFG.get("apps", {}).get(browser.lower())
    url = _resolve(target)

    if not exe or not _app_path(exe):
        _open_web(url)
        return f"{browser} is not installed or not in my list, so I used the usual browser"

    macos.open_target(url, exe)
    return f"opened {target} in {browser}"


def close_tab():
    if not macos.can_control():
        return macos.NO_CONTROL
    macos.press("w", "cmd")
    return "closed the tab"


def search_web(query):
    _open_web("https://www.google.com/search?q=" + urllib.parse.quote(query))
    return f"searching for {query}"


def open_file(name):
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    macos.open_target(hits[0])
    return f"opened {os.path.basename(hits[0])}"


def find_files(name):
    hits = find_file(name)
    if not hits:
        return f"nothing matching {name}"
    speech = "found: " + ", ".join(os.path.basename(h) for h in hits[:5])
    items = [{"primary": os.path.basename(h), "secondary": os.path.dirname(h)} for h in hits[:8]]
    return Panel(speech, "list", title=f'Files matching "{name}"', items=items)


def open_folder(name):
    root = expand(name) if os.path.isdir(expand(name)) else None
    if not root:
        for folder in CFG.get("search_folders", []):
            candidate = expand(folder)
            if name.lower() in os.path.basename(candidate).lower():
                root = candidate
                break
    if not root:
        return f"no folder called {name}"
    macos.open_target(root)
    return f"opened {os.path.basename(root)}"


def volume(direction):
    return "done" if macos.change_volume(str(direction).lower()) else "the volume would not change"


def lock():
    return "locking" if macos.lock_screen() else "I could not lock the screen"


def _safe_path(root, name):
    """Keep a name-supplied-by-voice from walking out of the one folder it is
       supposed to land in — an absolute path or a `..` would otherwise let
       write_file put a file anywhere on disk."""
    root = os.path.abspath(expand(root))
    target = os.path.abspath(os.path.join(root, name))
    if os.path.commonpath([root, target]) != root:
        raise ValueError("that name reaches outside the allowed folder")
    return target


def delete_file(name):
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    macos.move_to_trash(hits[0])
    return f"moved {os.path.basename(hits[0])} to the Trash — Put Back in Finder undoes it"


def rename_file(name, new_name):
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    if not new_name.strip() or os.path.basename(new_name) != new_name:
        return "give me a plain file name for that, not a path"
    os.rename(hits[0], os.path.join(os.path.dirname(hits[0]), new_name))
    return f"renamed to {new_name}"


# ---- typing and clipboard ----

def to_clipboard(text):
    return "copied" if macos.set_clipboard(text) else "the clipboard would not take it"


def type_text(text):
    """Put text into whatever window he is in — dictation, essentially."""
    if not macos.can_control():
        return macos.NO_CONTROL
    macos.set_clipboard(text)
    time.sleep(0.15)
    macos.press("v", "cmd")
    return "typed it"


def read_clipboard():
    text = macos.get_clipboard()
    return text[:1200] if text else "the clipboard is empty"


# ---- screen and windows ----

# ---- finding what is actually installed ----

_APP_INDEX = None


def _index_apps():
    """Every app in /Applications and friends, so he can open anything installed."""
    global _APP_INDEX
    if _APP_INDEX is None:
        _APP_INDEX = macos.installed_apps()
        log(f"indexed {len(_APP_INDEX)} installed apps")
    return _APP_INDEX


def _app_path(name):
    """The installed .app for an exact name like "Google Chrome", or None."""
    return _index_apps().get(str(name).lower())


def find_installed(name):
    apps = _index_apps()
    key = name.lower().strip()
    if key in apps:
        return apps[key]
    if len(key) >= 4:          # a short name hides inside long ones: "oca" isn't LocalSend
        matches = [p for n, p in apps.items() if key in n]
        if matches:
            return sorted(matches, key=len)[0]
        # match the other way round: "word" finding "Microsoft Word 2019"
        words = key.split()
        for n, p in apps.items():
            if all(w in n for w in words):
                return p
    # misheard names: speech-to-text hears "Orca" as "Oca" — take only a clearly close spelling
    close = difflib.get_close_matches(key, list(apps), n=1, cutoff=0.85)
    return apps[close[0]] if close else None


def smart_open(name):
    """Open the installed program if he has it, otherwise the website."""
    configured = CFG.get("apps", {}).get(name.lower())
    if configured:
        return open_app(name)

    installed = find_installed(name)
    if installed:
        macos.launch_app(installed)
        return f"opened {macos.app_name(installed)}"

    site = CFG.get("sites", {}).get(name.lower())
    if site:
        _open_web(site)
        return f"you do not have {name} installed, so I opened the web version"

    # likely a misheard name: ask rather than open a web search for the wrong word
    near = [n.title() for w in [name.lower()] + name.lower().split()
            for n in difflib.get_close_matches(w, list(_index_apps()), n=3, cutoff=0.6)]
    if near:
        return (f"nothing installed is called {name}. Closest installed: {', '.join(dict.fromkeys(near))}. "
                "Ask which one they meant — don't guess")

    _open_web("https://www.google.com/search?q=" + urllib.parse.quote(name + " web app"))
    return f"you do not have {name} installed and I have no site for it, so I searched the web"


def list_installed(contains=""):
    apps = _index_apps()
    names = sorted(n for n in apps if contains.lower() in n)
    if not names:
        return "nothing matching that"
    speech = f"{len(names)} found, including " + ", ".join(names[:8])
    items = [{"primary": n.title()} for n in names[:30]]
    return Panel(speech, "list", title="Installed Programs", items=items)


# ---- screenshots on the clipboard ----

def _latest_screenshot():
    desktop = expand("~/Desktop")
    shots = [os.path.join(desktop, f) for f in os.listdir(desktop)
             if f.startswith("screenshot-") and f.endswith(".png")]
    if not shots:
        return None
    return max(shots, key=os.path.getmtime)


def copy_image_to_clipboard(path=None):
    """Puts the image itself on the clipboard, ready to paste anywhere."""
    path = path or _latest_screenshot()
    if not path:
        return "there is no screenshot to copy"
    if not macos.image_to_clipboard(path):
        return "that image would not go on the clipboard"
    return f"{os.path.basename(path)} is on the clipboard"


def screenshot():
    if not macos.can_see_screen():
        return macos.NO_SCREEN
    name = f"screenshot-{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    path = os.path.join(expand("~/Desktop"), name)
    if not macos.capture(path):
        return "the screenshot did not save"
    return f"saved {name} to the desktop"


# ---- the Second Brain: what he has told David, and what David writes down ----

BRAIN = Brain(expand(CFG.get("brain_dir") or os.path.join(HERE, "brain")),
              legacy_json=os.path.join(HERE, "memory.json"))


def remember(fact, category="general"):
    """Keep something he said for every session from now on."""
    if BRAIN.remember(fact, category):
        log("replacing an older version of that memory")
    return "noted, I will remember that"


def recall(about=""):
    """What he has told David, optionally filtered."""
    items = BRAIN.facts()
    if not items:
        return "you have not told me anything to remember yet"
    if about:
        needle = about.lower()
        items = [i for i in items if needle in i[2].lower() or needle in i[0]]
        if not items:
            return f"nothing about {about}"
    recent = sorted(items, key=lambda i: i[1])[-15:]
    speech = "; ".join(i[2] for i in recent)
    rows = [{"primary": fact, "secondary": f"{cat} · {day}"} for cat, day, fact in reversed(recent)]
    return Panel(speech, "list", title="Remembered" + (f" — {about}" if about else ""), items=rows)


def forget(about):
    """Drop anything matching what he describes."""
    dropped = BRAIN.forget(about)
    if not dropped:
        return f"I had nothing about {about}"
    return f"forgotten — dropped {dropped} thing{'s' if dropped != 1 else ''}"


def memory_for_prompt():
    return BRAIN.for_prompt()


def write_page(title, text, append=True):
    """A longer note in the brain's wiki — for knowledge, not one-line facts."""
    where = BRAIN.write_page(title, text, append=append)
    return f"saved to {where} in the brain"


def read_page(name):
    text = BRAIN.read(name)
    if text is None:
        hits = BRAIN.search(name, limit=5)
        if not hits:
            return f"there is no note called {name}"
        return "no note by that name. Closest: " + "; ".join(p for p, _ in hits)
    return text[-6000:]     # shortcut: tail of very long notes only; add paging if pages grow


def search_brain(query):
    hits = BRAIN.search(query)
    if not hits:
        return f"nothing in the brain about {query}"
    return "\n".join(f"{p}: {line}" for p, line in hits)


# ---- email ----

_LAST_MAIL = []



def _decode(raw):
    if not raw:
        return ""
    out = []
    for part, enc in decode_header(raw):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out).strip()


def _mail_connect():
    conf = CFG.get("email", {})
    if not conf.get("address") or not conf.get("app_password"):
        return None, "no email account is set up in the config yet"
    try:
        box = imaplib.IMAP4_SSL(conf.get("imap", "imap.gmail.com"))
        box.login(conf["address"], conf["app_password"])
        box.select("INBOX")
        return box, None
    except Exception as e:
        return None, f"I could not sign in to the mailbox: {e}"


_LAST_OAUTH_MAIL = []


# The oauth path and the plain IMAP path fill in slightly different dicts —
# this is the one place that turns either of them into what the panel wants.
def _email_panel_items(found):
    return [{
        "from": (m["from"].split("<")[0].strip() or m["from"]),
        "subject": m["subject"],
        "time": m.get("date", ""),
        "unread": bool(m.get("unread", False)),
        "body": " ".join((m.get("body") or "").split())[:4000],
    } for m in found]


def check_email(sender="", subject="", hours=24, unread_only=False, limit=6):
    """Look through the inbox. Real Gmail API if connected, otherwise the
       browser with vision as a fallback."""
    global _LAST_OAUTH_MAIL

    if _HAVE_OAUTH_GMAIL and oauth_gmail.is_connected():
        try:
            found, err = oauth_gmail.search(sender, subject, hours, unread_only, limit)
        except Exception as e:
            log("oauth gmail failed, falling back to the browser:", e)
            return check_gmail(sender=sender, subject=subject, hours=hours, unread_only=unread_only)

        if err:
            return err
        _LAST_OAUTH_MAIL = found
        lines = [f"{i + 1}. from {m['from'].split('<')[0].strip()}, subject: {m['subject']}"
                 f"{' (unread)' if m['unread'] else ''}"
                 for i, m in enumerate(found)]
        speech = f"{len(found)} message{'s' if len(found) != 1 else ''}: " + "; ".join(lines)
        return Panel(speech, "email", messages=_email_panel_items(found))

    if CFG.get("email_mode", "browser") == "browser":
        return check_gmail(sender=sender, subject=subject, hours=hours, unread_only=unread_only)

    global _LAST_MAIL
    box, err = _mail_connect()
    if err:
        return err

    try:
        since = (datetime.now() - timedelta(hours=float(hours))).strftime("%d-%b-%Y")
        terms = [f'(SINCE "{since}")']
        if sender:
            terms.append(f'(FROM "{sender}")')
        if subject:
            terms.append(f'(SUBJECT "{subject}")')
        if unread_only:
            terms.append("(UNSEEN)")

        typ, data = box.search(None, *terms)
        ids = (data[0].split() if data and data[0] else [])[-int(limit):]

        if not ids:
            who = f" from {sender}" if sender else ""
            return f"nothing{who} in the last {int(hours)} hours"

        found = []
        for num in reversed(ids):
            typ, raw = box.fetch(num, "(RFC822)")
            msg = emaillib.message_from_bytes(raw[0][1])
            found.append({
                "id": num,
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")) or "(no subject)",
                "date": _decode(msg.get("Date")),
                "body": _body_of(msg),
            })

        _LAST_MAIL = found
        lines = [f"{i + 1}. from {m['from'].split('<')[0].strip()}, subject: {m['subject']}"
                 for i, m in enumerate(found)]
        speech = f"{len(found)} message{'s' if len(found) != 1 else ''}: " + "; ".join(lines)
        return Panel(speech, "email", messages=_email_panel_items(found))
    except Exception as e:
        return f"something went wrong reading the mailbox: {e}"
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _body_of(msg, limit=4000):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                try:
                    return part.get_payload(decode=True).decode(errors="replace")[:limit]
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(errors="replace")[:limit]
    except Exception:
        return ""


def read_email(which=1):
    """Read out one of the messages found by the last check. Also flips the
       HUD panel to that message's full-body view, so what he sees matches
       what he hears."""
    if _LAST_OAUTH_MAIL:
        idx = int(which) - 1
        if idx < 0 or idx >= len(_LAST_OAUTH_MAIL):
            return f"there is no message {which}"
        m = _LAST_OAUTH_MAIL[idx]
        if m["unread"]:
            try:
                oauth_gmail.mark_read(m["id"])
            except Exception:
                pass
        # search() only fetches headers, to keep check_email fast — the body
        # is pulled here, the one time he actually asks to hear a message,
        # and cached on the entry so reading the same one twice is free.
        body = m.get("body") or ""
        if not body:
            try:
                body = oauth_gmail.get_body(m["id"])
                m["body"] = body
            except Exception as e:
                log("could not fetch message body:", e)
        body = " ".join(body.split())[:1800]
        speech = f"From {m['from']}, subject {m['subject']}. {body or 'there is no readable text in it'}"
        return Panel(speech, "email", messages=_email_panel_items(_LAST_OAUTH_MAIL), open_index=idx)

    if not _LAST_MAIL:
        return open_gmail_message(which)
    idx = int(which) - 1
    if idx < 0 or idx >= len(_LAST_MAIL):
        return f"there is no message {which}"
    m = _LAST_MAIL[idx]
    body = " ".join(m["body"].split())[:1800]
    speech = f"From {m['from']}, subject {m['subject']}. {body or 'there is no readable text in it'}"
    return Panel(speech, "email", messages=_email_panel_items(_LAST_MAIL), open_index=idx)


def send_email(to, subject, body):
    """Send a message from his account."""
    if _HAVE_OAUTH_GMAIL and oauth_gmail.is_connected():
        try:
            return oauth_gmail.send(to, subject, body)
        except Exception as e:
            return f"the email would not send: {e}"

    import smtplib
    from email.message import EmailMessage

    conf = CFG.get("email", {})
    if not conf.get("address") or not conf.get("app_password"):
        return "no email account is set up — use connect_gmail, or set an app password in config"

    msg = EmailMessage()
    msg["From"] = conf["address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(conf.get("smtp", "smtp.gmail.com"), 465) as server:
            server.login(conf["address"], conf["app_password"])
            server.send_message(msg)
        return f"sent to {to}"
    except Exception as e:
        return f"it would not send: {e}"


# ---- Gmail through the browser, using the session he is already signed into ----

def _gmail_search_url(sender="", subject="", hours=0, unread_only=False):
    bits = []
    if sender:
        bits.append(f"from:{sender}")
    if subject:
        bits.append(f"subject:{subject}")
    if unread_only:
        bits.append("is:unread")
    if hours:
        days = max(1, int(round(float(hours) / 24)))
        bits.append(f"newer_than:{days}d")
    query = " ".join(bits) or "in:inbox"
    return "https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote(query)


def connect_gmail():
    """Runs the Google sign-in flow. Opens a browser window for him to approve."""
    if not _HAVE_OAUTH_GMAIL:
        return "the Gmail login pieces are not installed yet"
    if not oauth_gmail.is_ready():
        return ("Gmail is not set up yet — a credentials.json file needs to be in the folder first. "
                "That comes from a Google Cloud project.")
    try:
        address = oauth_gmail.connect()
        return f"connected to {address}. I can read and send mail properly now"
    except Exception as e:
        return f"the connection did not go through: {e}"


def disconnect_gmail():
    if not _HAVE_OAUTH_GMAIL:
        return "nothing to disconnect"
    return oauth_gmail.disconnect()


def check_gmail(sender="", subject="", hours=24, unread_only=False, question=""):
    """Open Gmail already signed in, search it, and read the list off the screen.
       Opens one window the first time and then reuses that same tab."""
    url = _gmail_search_url(sender, subject, hours, unread_only)
    reused = _show_web(url, "mail.google.com")

    if not vision_available():
        return ("Gmail is open with that search, but I cannot read the screen right now — "
                "the daily vision quota is used up. Take a look yourself for now.")

    time.sleep(3 if reused else 4)
    if not reused:
        place_active_window()
        time.sleep(1.8)

    ask = question or (
        "This is their Gmail. Read the message list and say exactly what is there: who each "
        "message is from, the subject, and the date shown beside it. Be precise about dates — if "
        "something is from yesterday, say yesterday, do not call it today. If the list is empty, "
        "say plainly that there is nothing" + (f" from {sender}" if sender else "") + "."
    )
    answer, err = _look(ask + " Answer in two or three spoken sentences, no lists.")
    if err:
        log("gmail vision failed:", err)
        return f"I opened Gmail but could not read it: {err}"
    return answer


def open_gmail_message(which=1):
    """Open one of the messages on screen and read it out."""
    ordinal = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}.get(int(which), "first")
    click_on(f"the {ordinal} email in the message list")
    time.sleep(2.5)
    answer, err = _look(
        "This is an open email. Read out what it actually says — who wrote it and the body of "
        "the message. Keep it to what is written, do not summarise it away to nothing."
    )
    return err or f"{answer}"


def reply_in_gmail(text):
    """Click reply, type the message, and stop before sending."""
    click_on("the Reply button at the bottom of the open email")
    time.sleep(2)
    type_text(text)
    return "the reply is typed and waiting — say send it when you are happy"


def send_open_gmail():
    click_on("the blue Send button in the reply box")
    return "sent"


# ---- reading chats through the screen ----

# Store apps have no shortcut path, only a protocol handler
APP_PROTOCOLS = {
    "whatsapp": "whatsapp://",
    "telegram": "tg://",
    "spotify": "spotify://",
    "discord": "discord://",
    "slack": "slack://",
}


def _launch_app_or_web(app):
    """Installed application first, protocol second, website last."""
    key = app.lower()

    configured = CFG.get("apps", {}).get(key)
    if configured and "://" not in configured and _app_path(configured):
        if macos.launch_app(configured):
            return "app"

    installed = find_installed(app)
    if installed and macos.launch_app(installed):
        return "app"

    # `open whatsapp://` fails when nothing is installed to handle it,
    # which is exactly when the website below should take over
    proto = configured if configured and "://" in configured else APP_PROTOCOLS.get(key)
    if proto and macos.open_target(proto):
        return "app"

    site = CFG.get("sites", {}).get(key)
    if site:
        _open_web(site)
        return "web"
    return None


def check_chat(person, question="", app="whatsapp"):
    """Personal messaging has no API, so this opens the chat and reads the
       screen the way he would himself. Uses the installed app if he has it."""
    if not macos.can_control():
        return macos.NO_CONTROL
    if not vision_available():
        _launch_app_or_web(app)
        return (f"{app} is open on your screen, but I cannot read it right now — "
                f"the daily vision quota is used up. Have a look yourself for now.")
    key = "chat:" + app.lower()
    if not _activate(key):
        how = _launch_app_or_web(app)
        if how is None:
            return f"I have no {app} app or website configured"
        time.sleep(4.5)
        _remember_window(key)
        place_active_window()
        time.sleep(1.5)

    # find the person by searching in the app
    click_on("the chat search box or search field")
    time.sleep(0.8)
    macos.set_clipboard(person)
    time.sleep(0.2)
    macos.press("v", "cmd")
    time.sleep(1.6)
    click_on(f"the chat or conversation with {person} in the results list")
    time.sleep(2.2)

    ask = (question or f"Has {person} replied? Read the most recent messages in this "
                       f"conversation and tell me what {person} said, and when. If you cannot "
                       f"find a conversation with {person}, say so plainly.")
    answer, err = _look(ask + " Answer in two or three spoken sentences.")
    if err:
        log("chat vision failed:", err)
        return f"I opened {app} but could not read it: {err}"
    return answer


# ---- window handles, so a window is opened once and then reused ----

_WINDOWS = {}


def _remember_window(key):
    """macOS gives no window handle to hold on to from here, so remember the
       app that came to the front — activating it later brings back its
       frontmost window."""
    time.sleep(0.4)
    app = macos.front_app()
    if app:
        _WINDOWS[key] = app
    return app


def _activate(key):
    """Bring an app we opened earlier back to the front. False if it has quit."""
    app = _WINDOWS.get(key)
    if not app or not macos.app_running(app):
        _WINDOWS.pop(key, None)
        return False
    macos.activate_app(app)
    time.sleep(0.5)
    return True


def _show_web(url, match):
    """Show a page in the working browser, reusing the tab already on that site.
       The Windows version typed the address into whatever tab was in front,
       which took over whatever he was reading. True when a tab was reused."""
    app = CFG.get("browsers", {}).get(CFG.get("work_browser") or "chrome")
    if app and _app_path(app):
        reused = macos.show_in_browser_tab(app, url, match)
        if reused is not None:
            return reused
    _open_web(url, new_window=True)
    return False


# ---- instant acknowledgement ----

_ACKS = ["On it.", "Working on it.", "Give me a second.", "Checking now."]
_ack_lock = threading.Lock()


def ack_out_loud():
    """Say something the moment a job starts, so he is never left in silence.
       Uses the Mac's built-in voice, which is instant and local."""
    if not CFG.get("spoken_acknowledgement", True):
        return
    phrase = _ACKS[int(time.time()) % len(_ACKS)]

    def run():
        if not _ack_lock.acquire(blocking=False):
            return
        try:
            macos.speak(phrase)
        except Exception:
            pass
        finally:
            _ack_lock.release()

    threading.Thread(target=run, daemon=True).start()


# ---- monitors ----

def monitors():
    """Every screen attached, left to right."""
    return macos.displays()


_MONITOR_NAMES = None


def monitor_names():
    """Hardware names of the plugged-in screens, so they can be called what they are."""
    global _MONITOR_NAMES
    if _MONITOR_NAMES is None:
        _MONITOR_NAMES = macos.external_display_names()
    return _MONITOR_NAMES


def _position_word(index, total):
    if total <= 1:
        return ""
    if total == 2:
        return ["left", "right"][index]
    if index == 0:
        return "far left"
    if index == total - 1:
        return "far right"
    return "middle" if total == 3 else f"number {index + 1}"


def screen_label(index):
    """What to call screen `index`: your nickname first, then hardware name,
       with a position added when two screens share the same name."""
    nicknames = CFG.get("monitor_names", {})
    key = str(index + 1)
    if key in nicknames:
        return nicknames[key]

    screens = monitors()
    if index < len(screens) and screens[index].get("builtin"):
        name = "MacBook screen"
        hardware = [name]
    else:
        # System Information lists external screens in its own order; this
        # assumes it matches left to right, which a nickname in config overrides
        hardware = monitor_names()
        externals = [i for i, s in enumerate(screens) if not s.get("builtin")]
        pos = externals.index(index) if index in externals else -1
        name = hardware[pos] if 0 <= pos < len(hardware) else None

    if not name:
        return f"the {_position_word(index, len(screens))} screen".replace("the  ", "the ")

    # two monitors of the same model need telling apart by where they are
    if hardware.count(name) > 1:
        where = _position_word(index, len(screens))
        return f"the {where} {name}" if where else name
    return name


def resolve_screen(target):
    """Turn 'screen 2', 'the right one', 'the ThinkVision' or '1' into an index."""
    screens = monitors()
    if target is None or not screens:
        return None

    text = str(target).strip().lower()

    # a number anywhere in the phrase: "2", "screen 2", "monitor two"
    digits = re.findall(r"\d+", text)
    words = {"one": 1, "two": 2, "three": 3, "first": 1, "second": 2, "third": 3}
    number = int(digits[0]) if digits else next((v for w, v in words.items() if w in text.split()), None)
    if number is not None:
        idx = number - 1
        if 0 <= idx < len(screens):
            return idx

    # match against nicknames and hardware names, ignoring filler words
    def strip_filler(t):
        for junk in ("the ", "my ", "screen", "monitor", "display", "one", "hand", "side"):
            t = t.replace(junk, " ")
        return " ".join(t.split())

    want = strip_filler(text)
    if want:
        for i in range(len(screens)):
            label = strip_filler(screen_label(i).lower())
            if label and (want in label or label in want):
                return i

    # plain direction words, which is what he actually says most of the time
    if "left" in text:
        return 0
    if "right" in text:
        return len(screens) - 1
    if any(w in text for w in ("main", "primary", "laptop", "this")):
        return int(CFG.get("work_monitor", 1)) - 1
    if any(w in text for w in ("other", "another")):
        ui = int(CFG.get("ui_monitor", 2)) - 1
        return 0 if ui != 0 else min(1, len(screens) - 1)
    return None


def list_drives():
    """Every disk mounted, with free space."""
    found = macos.volumes()
    if not found:
        return "no disks found"
    gb = 1024 ** 3
    speech = "; ".join(f"{name}: {free / gb:.1f} GB free of {total / gb:.1f} GB"
                       for name, _path, free, total in found)
    rows = [{"primary": name, "secondary": f"{free / gb:.1f} GB free of {total / gb:.1f} GB"}
            for name, _path, free, total in found]
    return Panel(speech, "list", title="Disks", items=rows)


def copy_file(name, destination):
    """Copy a file somewhere — a disk by name, a folder, or a configured nickname."""
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    source = hits[0]

    places = CFG.get("places", {})
    target = places.get(destination.lower(), destination)
    target = expand(target)

    # a disk by name: "copy it to the USB stick" finds /Volumes/USB STICK
    if not os.path.isdir(target):
        for vol_name, vol_path, _free, _total in macos.volumes():
            if destination.lower() in vol_name.lower():
                target = vol_path
                break
    if not os.path.isdir(target):
        return f"{destination} is not a folder I can reach"

    shutil.copy2(source, os.path.join(target, os.path.basename(source)))
    return f"copied {os.path.basename(source)} to {destination}"


def list_monitors():
    screens = monitors()
    bits = [f"{screen_label(i)} at {r['right'] - r['left']} by {r['bottom'] - r['top']}"
            for i, r in enumerate(screens)]
    return f"{len(screens)} screen{'s' if len(screens) != 1 else ''}: " + ", ".join(bits)


def _work_screen():
    """The screen that is NOT showing the interface."""
    screens = monitors()
    if len(screens) < 2:
        return None
    ui_index = int(CFG.get("ui_monitor", 2)) - 1
    work = CFG.get("work_monitor")
    if work is not None:
        idx = int(work) - 1
    else:
        idx = 0 if ui_index != 0 else 1
    return screens[min(max(idx, 0), len(screens) - 1)]


def _main_screen(screens):
    """The screen with the menu bar — the one whose top-left corner is (0, 0)."""
    return next((s for s in screens if s["left"] == 0 and s["top"] == 0),
                screens[0] if screens else None)


def _fill(screen):
    """Resize the front window to fill a screen, below the menu bar."""
    top = screen["top"] + 25
    return macos.set_front_window(screen["left"], top, screen["right"] - screen["left"],
                                  screen["bottom"] - top)


def place_active_window(maximise=True):
    """Move whatever just opened onto the working screen."""
    screen = _work_screen()
    if not screen:
        return "there is only one screen"
    if not macos.can_control():
        return macos.NO_CONTROL
    return "moved it to your other screen" if _fill(screen) else "that window would not move"


def move_window(screen):
    screens = monitors()
    idx = resolve_screen(screen)
    if idx is None:
        return f"I do not know which screen {screen} is"
    if not macos.can_control():
        return macos.NO_CONTROL
    if not _fill(screens[idx]):
        return "that window would not move"
    return f"moved to {screen_label(idx)}"


# ---- seeing the screen and pointing at things ----

_LAST_VIEW = None   # the rectangle the last look captured, in click coordinates


def _grab_screen_png(only_work_screen=True):
    """Capture one screen, scaled down. A full Retina grab is enormous and
       burns through the daily quota in a handful of looks. JPEG, because for
       a screenshot it is a fraction of PNG's size and uploads that much faster.

       Remembers the rectangle it captured: click_on must map the model's
       point back onto that same rectangle. The Windows version mapped it
       across every screen at once, so on two screens clicks landed wrong."""
    global _LAST_VIEW
    path = os.path.join(tempfile.gettempdir(), "david_view.jpg")

    # by default only look at the screen he actually works on
    screen = _work_screen() if only_work_screen and CFG.get("vision_work_screen_only", True) else None
    screen = screen or _main_screen(monitors())
    if not screen:
        return None
    box = (screen["left"], screen["top"], screen["right"], screen["bottom"])
    if not macos.capture(path, box, CFG.get("vision_max_width", 1000), fmt="jpg"):
        return None
    _LAST_VIEW = box
    return path


# each provider rests on its own; one having a bad day must not silence the other
_VISION_DOWN = {}


def vision_available():
    order = _vision_order()
    return any(_VISION_DOWN.get(p.__name__, 0) < time.time() for p in order)


def _next_vision_return():
    times = [t for t in _VISION_DOWN.values() if t > time.time()]
    return min(times) if times else time.time()


def _look(question, want_point=False):
    """Send the screen to a vision model and ask it something.

       Tries Gemini first, then falls back to Groq when a key is set —
       separate free allowance, and they do not train on your images."""
    if not vision_available():
        secs = int(max(0, _next_vision_return() - time.time()))
        when = f"{secs} seconds" if secs < 90 else f"{secs // 60} minutes"
        return None, (f"I cannot read the screen just yet — rate limited for another {when}")

    if not macos.can_see_screen():
        return None, macos.NO_SCREEN
    path = _grab_screen_png()
    if not path:
        return None, "I could not capture the screen"

    with open(path, "rb") as f:
        img = base64.b64encode(f.read()).decode()

    if not want_point:
        question += " Answer directly with the answer only. Do not explain your reasoning."

    if want_point:
        question += (" Reply with ONLY a JSON object like {\"point\": [y, x], \"found\": true}"
                     " where y and x are normalised 0 to 1000 from the top left of the image."
                     " If it is not visible, reply {\"found\": false}.")

    errors = []
    for provider in _vision_order():
        name = provider.__name__
        if _VISION_DOWN.get(name, 0) > time.time():
            continue                       # this one is resting

        text, err = provider(question, img, want_point)
        if text is not None:
            return text, None

        errors.append(err)
        log("vision:", err)

        # Only a real quota refusal earns a rest, and only for as long as the
        # provider asked for.
        if "quota" in (err or "").lower() or "429" in (err or ""):
            wait = 45.0
            if "|" in (err or ""):
                try:
                    wait = float(err.split("|")[-1])
                except ValueError:
                    pass
            _VISION_DOWN[name] = time.time() + wait
            log(f"{name} rate limited — resting it for {int(wait)}s")

    if errors and all("quota" in (e or "").lower() or "429" in (e or "") for e in errors):
        secs = int(max(0, _next_vision_return() - time.time()))
        when = f"{secs} seconds" if secs < 90 else f"{secs // 60} minutes"
        return None, (f"both vision providers are rate limited at the moment. "
                      f"Try me again in about {when} — everything else still works")

    return None, errors[-1] if errors else "I could not read the screen"


def _vision_order():
    import engines
    order = [_look_gemini]
    if CFG.get("groq_key", "").strip() and not CFG.get("groq_key", "").startswith("PASTE"):
        order.append(_look_groq)
    if engines.missing_key("claude") is None:      # signed in: Claude reads the screen when the rest are down
        order.append(_look_claude)
    return order


def _strip_thinking(text):
    """Drop any reasoning the model leaves in, and keep the answer."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"<think>.*", "", text, flags=re.S | re.I)   # unclosed, cut short
    text = re.sub(r"^\s*\d+\.\s*\*\*.*?\*\*.*$", "", text, flags=re.M)  # drafting notes
    text = text.replace("**", "").strip()
    return " ".join(text.split())


def _retry_after(res, default=60.0):
    """How long the provider actually wants us to wait. Their per-minute limits
       reset in seconds, so guessing twenty minutes throws away the whole day."""
    header = res.headers.get("retry-after") or res.headers.get("Retry-After")
    if header:
        try:
            return max(3.0, float(header))
        except ValueError:
            pass

    body = (res.text or "")[:600]
    m = re.search(r"try again in ([\d.]+)\s*(ms|s|m|second|minute)", body, re.I)
    if m:
        value, unit = float(m.group(1)), m.group(2).lower()
        seconds = value / 1000 if unit == "ms" else value * 60 if unit.startswith("m") and unit != "ms" else value
        return max(3.0, min(seconds + 1, 900))

    # a daily ceiling usually says so
    if re.search(r"per day|daily|RPD|TPD", body, re.I):
        return float(CFG.get("vision_backoff_minutes", 20)) * 60
    return default


def _look_groq(question, img_b64, want_point):
    """Groq's free tier, using its current multimodal model."""
    model = CFG.get("groq_vision_model") or "qwen/qwen3.6-27b"
    body = {
        "model": model,
        "max_tokens": 400,
        # Qwen and friends think out loud by default, which floods the log and
        # ends up being read aloud. Ask for the answer only.
        "reasoning_format": "hidden",
        "reasoning_effort": "none",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ]}],
    }
    if want_point:
        body["response_format"] = {"type": "json_object"}

    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {CFG['groq_key']}",
                                     "Content-Type": "application/json"},
                            json=body, timeout=60)
    except requests.RequestException as e:
        return None, f"groq unreachable: {e}"

    if res.status_code == 429:
        wait = _retry_after(res, default=45.0)
        return None, f"groq quota reached|{wait:.0f}"
    if not res.ok:
        return None, f"groq returned {res.status_code}: {res.text[:160]}"

    try:
        return _strip_thinking(res.json()["choices"][0]["message"]["content"]), None
    except (KeyError, IndexError, ValueError):
        return None, "groq sent something unreadable"


def _thinking(level):
    """Gemini 3 thinks at medium unless told otherwise, and those thinking tokens
       come out of maxOutputTokens — so a short reply could be slow, or cut off
       before it started. Low is plenty for reading a screen or a search."""
    return {"thinkingConfig": {"thinkingLevel": level}} if level else {}


def _gemini_post(body, model=None, timeout=90):
    """One generateContent call. The key goes in a header rather than the URL,
       so it can never turn up in a logged error or a proxy's access log."""
    model = model or CFG.get("model") or "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": CFG["api_key"]}
    res = requests.post(url, headers=headers, json=body, timeout=timeout)

    config = body.get("generationConfig", {})
    if res.status_code == 400 and "thinkingConfig" in config and "thinking" in res.text.lower():
        # a model that doesn't take thinking levels — ask again without one
        log(f"{model} refused a thinking level, asking again without it")
        config = {k: v for k, v in config.items() if k != "thinkingConfig"}
        res = requests.post(url, headers=headers, json={**body, "generationConfig": config},
                            timeout=timeout)
    return res


def _look_gemini(question, img_b64, want_point):
    body = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
            {"text": question},
        ]}],
        "generationConfig": {"maxOutputTokens": 500,
                             **_thinking(CFG.get("vision_thinking", "low")),
                             **({"responseMimeType": "application/json"} if want_point else {})},
    }

    try:
        res = _gemini_post(body, CFG.get("vision_model"), timeout=30)   # busy Gemini can hang; let the backup try
    except requests.RequestException as e:
        return None, f"gemini unreachable: {e}"

    if res.status_code == 429:
        wait = _retry_after(res, default=180.0)
        return None, f"gemini quota reached|{wait:.0f}"
    if not res.ok:
        return None, f"gemini returned {res.status_code}"

    try:
        parts = res.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip(), None
    except (KeyError, IndexError, ValueError):
        return None, "gemini sent something unreadable"


def _look_claude(question, img_b64, want_point):
    """Claude, through the same sign-in as the Claude brain."""
    import anthropic
    import engines
    try:
        res = anthropic.Anthropic().messages.create(
            model=engines.model_for("claude"), max_tokens=1000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": question}]}])
    except anthropic.RateLimitError:
        return None, "claude quota reached|60"
    except anthropic.APIStatusError as e:
        return None, f"claude returned {e.status_code}"
    except anthropic.APIError as e:
        return None, f"claude unreachable: {e}"
    if res.stop_reason == "refusal":
        return None, "claude declined to read that screen"
    text = "".join(b.text for b in res.content if b.type == "text").strip()
    if want_point:   # Claude has no JSON mode; keep just the object
        m = re.search(r"\{.*\}", text, re.S)
        text = m.group(0) if m else text
    return text, None


def describe_screen(question="What is on this screen? Answer in two spoken sentences."):
    text, err = _look(question)
    return err or text


def _point_on_screen(point, box):
    """The model answers [y, x], each 0 to 1000 across the image it was shown.
       Turn that into a click position on the rectangle the image came from."""
    y, x = point[:2]
    left, top, right, bottom = box
    return (int(left + float(x) / 1000 * (right - left)),
            int(top + float(y) / 1000 * (bottom - top)))


def click_on(description, double=False):
    """Look at the screen, find what he described, and click it."""
    if not macos.can_control():
        return macos.NO_CONTROL
    raw, err = _look(f"Find this on the screen: {description}.", want_point=True)
    if err:
        return err

    try:
        found = json.loads(raw.replace("```json", "").replace("```", "").strip())
    except ValueError:
        return f"I could not work out where {description} is"

    if not found.get("found") or not found.get("point"):
        return f"I cannot see {description} on the screen"

    px, py = _point_on_screen(found["point"], _LAST_VIEW)
    macos.click(px, py, double=bool(double))
    return f"clicked {description}"


def scroll(direction="down", amount=5):
    if not macos.can_control():
        return macos.NO_CONTROL
    step = -3 if str(direction).lower() == "down" else 3
    for _ in range(abs(int(amount))):
        macos.scroll_lines(step)
        time.sleep(0.05)
    return f"scrolled {direction}"


def press_key(key):
    key = str(key).lower().strip()
    if not macos.has_key(key):
        return f"I do not know the {key} key"
    if not macos.can_control():
        return macos.NO_CONTROL
    macos.press(key)
    return f"pressed {key}"


def open_dashboard():
    """Open the settings dashboard so he can connect tools or change display mode."""
    _open_web("http://127.0.0.1:8765/dashboard.html", new_window=True)
    return "opened the dashboard"


def show_image(query):
    """Open image results, put them on the working screen, and open the
       clearest one large so he can actually look at it."""
    _open_web("https://www.google.com/search?udm=2&q=" + urllib.parse.quote(query))
    time.sleep(2.5)
    place_active_window()
    time.sleep(1.8)

    result = click_on(f"the clearest, largest single photo of {query} in the image results")
    time.sleep(1.2)
    return f"showing {query} on your other screen — {result}"


def _front_screen():
    """The screen to size windows against — the working screen when there is one."""
    return _work_screen() or _main_screen(monitors())


def maximise_window():
    if not macos.can_control():
        return macos.NO_CONTROL
    return "maximised" if _fill(_front_screen()) else "that window would not resize"


def minimise_window():
    if not macos.can_control():
        return macos.NO_CONTROL
    macos.press("m", "cmd")
    return "minimised"


def snap_window(side):
    if not macos.can_control():
        return macos.NO_CONTROL
    s = _front_screen()
    top = s["top"] + 25
    half = (s["right"] - s["left"]) // 2
    left = s["left"] + (half if str(side).lower() == "right" else 0)
    if not macos.set_front_window(left, top, half, s["bottom"] - top):
        return "that window would not move"
    return f"snapped to the {side}"


def minimise_all():
    return "hid everything" if macos.hide_all_apps() else "I could not hide the windows"


def close_window():
    if not macos.can_control():
        return macos.NO_CONTROL
    macos.press("w", "cmd")
    return "closed it"


def switch_window():
    if not macos.can_control():
        return macos.NO_CONTROL
    macos.press("tab", "cmd")
    return "switched"


def close_app(name):
    path = CFG.get("apps", {}).get(name.lower(), "")
    if not path or "://" in path:
        path = find_installed(name) or name
    app = macos.app_name(path)
    if not macos.app_running(app):
        return f"{app} was not running"
    return f"closed {app}" if macos.quit_app(app) else f"{app} would not quit"


# ---- media ----

def media(command):
    app = macos.media_control(str(command).lower())
    return f"done, in {app}" if app else "neither Spotify nor Music is open"


# ---- system ----

_LAST_CPU = macos.cpu_ticks()


def cpu_percent():
    global _LAST_CPU
    idle, total = macos.cpu_ticks()
    d_idle, d_total = idle - _LAST_CPU[0], total - _LAST_CPU[1]
    _LAST_CPU = (idle, total)
    if d_total <= 0:
        return 0
    return max(0, min(100, (1 - d_idle / d_total) * 100))


def ram_percent():
    return macos.ram_percent()


def disk_percent():
    return macos.disk_percent()


def battery_percent():
    percent, _plugged = macos.battery_status()
    return percent or 0


def battery():
    percent, plugged = macos.battery_status(max_age=0)
    if percent is None:
        return "this Mac has no battery"
    state = "plugged in" if plugged else "on battery"
    return Panel(f"{percent} percent, {state}", "system", value=f"{percent}%", label="Battery",
                 detail=state.capitalize())


def what_time():
    now = datetime.now()
    speech = now.strftime("%I:%M %p on %A the %d of %B")
    return Panel(speech, "system", value=now.strftime("%I:%M %p").lstrip("0"),
                 label="Time", detail=now.strftime("%A, %B %d"))


def sleep_pc():
    return "sleeping" if macos.sleep_now() else "the Mac would not go to sleep"


def shutdown(when_seconds=30):
    macos.schedule_power("shut down", when_seconds)
    return f"shutting down in {when_seconds} seconds — say cancel shutdown to stop it"


def restart(when_seconds=30):
    macos.schedule_power("restart", when_seconds)
    return f"restarting in {when_seconds} seconds — say cancel shutdown to stop it"


def cancel_shutdown():
    return "cancelled" if macos.cancel_power() else "nothing was scheduled"


# ---- notes, files, trading ----

def add_note(text):
    path = os.path.join(expand("~/Desktop"), "notes.md")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n- [{datetime.now():%Y-%m-%d %H:%M}] {text}")
    return "noted"


# Matches what add_note writes: "[2026-08-15 10:30] whatever he said"
_NOTE_RE = re.compile(r"^\[(?P<stamp>[^\]]+)\]\s*(?P<text>.*)$")


def read_notes(count=5):
    path = os.path.join(expand("~/Desktop"), "notes.md")
    if not os.path.exists(path):
        return "there are no notes yet"
    lines = [l.strip("- \n") for l in open(path, encoding="utf-8") if l.strip().startswith("-")]
    if not lines:
        return "there are no notes yet"

    recent = lines[-int(count):]
    speech = " ... ".join(recent)
    rows = []
    for l in reversed(recent):
        m = _NOTE_RE.match(l)
        rows.append({"primary": m.group("text"), "secondary": m.group("stamp")} if m else {"primary": l})
    return Panel(speech, "list", title="Notes", items=rows)


def notion_status():
    """Check right now whether Notion is actually connected — a plain
       filesystem check, no network call, effectively instant. The line
       build_system() writes into the system prompt is only a snapshot from
       when this conversation started; he can connect or disconnect Notion
       from the dashboard at any point without restarting, so that snapshot
       goes stale the moment he does. This is the live answer."""
    if not _HAVE_OAUTH_NOTION:
        return "Notion is not set up on this machine."
    if oauth_notion.is_connected():
        return f"connected, to {oauth_notion.status().get('page_title', 'the journal page')}"
    return "not connected"


def journal(entry):
    """Write a journal entry into his connected Notion page."""
    if not (_HAVE_OAUTH_NOTION and oauth_notion.is_connected()):
        return "Notion is not connected — connect it from the dashboard and I can journal for you."
    try:
        stamp = oauth_notion.journal(entry)
    except Exception as e:
        return f"that did not save to Notion: {e}"
    return f"journaled that under {stamp}"


def notion_table(rows, title=""):
    """Add a real table to his connected Notion page — rows is one line per
       row, cells separated by |, first line is the header."""
    if not (_HAVE_OAUTH_NOTION and oauth_notion.is_connected()):
        return "Notion is not connected — connect it from the dashboard and I can build tables there."
    try:
        summary = oauth_notion.table(rows, title)
    except Exception as e:
        return f"that table did not save to Notion: {e}"
    return f"added a table to Notion — {summary}"


def recent_downloads(count=5):
    root = expand("~/Downloads")
    files = [os.path.join(root, f) for f in os.listdir(root)] if os.path.isdir(root) else []
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=os.path.getmtime, reverse=True)
    if not files:
        return "downloads is empty"

    shown = files[: int(count)]
    speech = "most recent: " + ", ".join(os.path.basename(f) for f in shown)
    rows = [{"primary": os.path.basename(f),
            "secondary": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%b %d, %I:%M %p")}
           for f in shown]
    return Panel(speech, "list", title="Recent Downloads", items=rows)


# ---- weather ----

# WMO weather codes, as returned by Open-Meteo, collapsed down to the handful
# of icons the HUD actually draws.
_WEATHER_CODES = {
    0: ("Clear", "sun"), 1: ("Mostly Clear", "sun"),
    2: ("Partly Cloudy", "cloud-sun"), 3: ("Overcast", "cloud"),
    45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Light Drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy Drizzle", "rain"),
    56: ("Freezing Drizzle", "rain"), 57: ("Freezing Drizzle", "rain"),
    61: ("Light Rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy Rain", "rain"),
    66: ("Freezing Rain", "rain"), 67: ("Freezing Rain", "rain"),
    71: ("Light Snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy Snow", "snow"), 77: ("Snow Grains", "snow"),
    80: ("Rain Showers", "rain"), 81: ("Rain Showers", "rain"), 82: ("Heavy Showers", "rain"),
    85: ("Snow Showers", "snow"), 86: ("Snow Showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm", "storm"), 99: ("Thunderstorm", "storm"),
}


def _weather_look(code):
    return _WEATHER_CODES.get(code, ("Unknown", "cloud"))


_GUESSED_LOCATION = None


def _guess_location():
    """Where in the world this machine's connection actually is — the
       fallback that lets him ask for the weather without ever having to
       say where he is. Cached for the life of the process; only a
       successful lookup is cached, so a transient network hiccup gets
       retried rather than remembered as a permanent failure."""
    global _GUESSED_LOCATION
    if _GUESSED_LOCATION:
        return _GUESSED_LOCATION
    try:
        res = requests.get("http://ip-api.com/json/", timeout=6,
                           params={"fields": "status,city,country"}).json()
        if res.get("status") == "success":
            _GUESSED_LOCATION = ", ".join(x for x in (res.get("city"), res.get("country")) if x)
    except requests.RequestException as e:
        log("could not auto-detect location:", e)
    return _GUESSED_LOCATION or ""


_GEOCODE_CACHE = {}   # place name -> geocoding hit; a city's coordinates never change


def get_weather(location=""):
    """Current conditions and a short forecast for a place. Geocodes the
       name and pulls current + daily data in one round trip — Open-Meteo
       needs no API key, so this works with nothing extra configured.
       No place named and nothing configured? Falls back to wherever this
       machine's connection actually is."""
    place = location.strip() or CFG.get("home_location", "") or _guess_location()
    if not place:
        return "I don't know where to check the weather — tell me a city, or set home_location in config.json"

    cache_key = place.lower()
    hit = _GEOCODE_CACHE.get(cache_key)
    if hit is None:
        try:
            geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                               params={"name": place, "count": 1}, timeout=10).json()
        except requests.RequestException as e:
            return f"I couldn't reach the weather service: {e}"

        hits = geo.get("results") or []
        if not hits:
            return f"I couldn't find a place called {place}"
        hit = hits[0]
        _GEOCODE_CACHE[cache_key] = hit   # he asks about the same handful of places every time

    label = ", ".join(x for x in (hit.get("name"), hit.get("admin1"), hit.get("country")) if x)

    imperial = CFG.get("units", "metric") == "imperial"
    try:
        fc = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": hit["latitude"], "longitude": hit["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                           "weather_code,apparent_temperature",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
                "timezone": "auto", "forecast_days": 5,
            }, timeout=10).json()
    except requests.RequestException as e:
        return f"I found {label} but couldn't get the forecast: {e}"

    cur = fc.get("current") or {}
    daily = fc.get("daily") or {}
    unit = "°F" if imperial else "°C"
    wind_unit = "mph" if imperial else "km/h"

    temp = round(cur.get("temperature_2m", 0))
    feels = round(cur.get("apparent_temperature", temp))
    humidity = round(cur.get("relative_humidity_2m", 0))
    wind = round(cur.get("wind_speed_10m", 0))
    condition, icon = _weather_look(cur.get("weather_code", 0))

    days, hi, lo, codes = (daily.get("time", []), daily.get("temperature_2m_max", []),
                          daily.get("temperature_2m_min", []), daily.get("weather_code", []))
    forecast = []
    for i in range(1, min(5, len(days))):     # skip today, up to 4 days ahead
        _, day_icon = _weather_look(codes[i]) if i < len(codes) else ("", "cloud")
        forecast.append({
            "day": datetime.strptime(days[i], "%Y-%m-%d").strftime("%a"),
            "hi": round(hi[i]) if i < len(hi) else None,
            "lo": round(lo[i]) if i < len(lo) else None,
            "icon": day_icon,
        })

    speech = (f"It's {temp}{unit} and {condition.lower()} in {label} right now, feels like {feels}{unit}. "
              f"Wind {wind} {wind_unit}, humidity {humidity} percent.")

    return Panel(speech, "weather",
                 location=label, temp=temp, unit=unit, feels_like=feels,
                 condition=condition, icon=icon, wind=wind, wind_unit=wind_unit,
                 humidity=humidity, forecast=forecast)


# Short-lived research cache. This prevents David from repeating an expensive
# web-search call when an interrupted turn causes the same topic to be requested again.
_RESEARCH_CACHE = {}
_RESEARCH_CACHE_TTL = 300  # seconds


def _research_cache_key(topic, criteria):
    return re.sub(r"\s+", " ", f"{topic}|{criteria}".strip().lower())


def _get_cached_research(topic, criteria):
    key = _research_cache_key(topic, criteria)
    item = _RESEARCH_CACHE.get(key)
    if not item:
        return None
    saved_at, result = item
    if time.time() - saved_at > _RESEARCH_CACHE_TTL:
        _RESEARCH_CACHE.pop(key, None)
        return None
    log("using cached research for", topic)
    return result


def _cache_research(topic, criteria, result):
    if result:
        _RESEARCH_CACHE[_research_cache_key(topic, criteria)] = (time.time(), result)


def _research_groq(topic, criteria=""):
    """Fallback when Gemini's search quota is spent.
    Uses Groq Compound for live web research. If the full Compound system
    rejects the request with 413, retry with Compound Mini and only web search.
    """
    cached = _get_cached_research(topic, criteria)
    if cached:
        return cached

    if not CFG.get("groq_key", "").strip() or CFG.get("groq_key", "").startswith("PASTE"):
        return None

    ask = (
        f"Research: {topic}. "
        + (f"Criteria: {criteria}. " if criteria else "")
        + "Use live web search. Give one clear recommendation and one runner-up, "
          "with current prices or key facts when available. "
          "Answer in 4-5 spoken sentences, no markdown."
    )
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {CFG['groq_key']}",
        "Content-Type": "application/json",
        "Groq-Model-Version": "latest",
    }

    # First try the configured Compound system.
    model = CFG.get("groq_search_model") or "groq/compound"
    payload = {"model": model, "messages": [{"role": "user", "content": ask}]}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=75)
    except requests.RequestException as e:
        log("groq compound unreachable:", e)
        return None

    # Groq documents 413 as a request-body-too-large error. Since our research
    # prompt is deliberately tiny, retrying with Compound Mini is a useful
    # compatibility fallback rather than treating it as a quota failure.
    if res.status_code == 413:
        log("groq compound returned 413; retrying with compound-mini")
        mini_payload = {
            "model": "groq/compound-mini",
            "messages": [{"role": "user", "content": ask}],
            "compound_custom": {
                "tools": {"enabled_tools": ["web_search"]}
            },
        }
        try:
            res = requests.post(url, headers=headers, json=mini_payload, timeout=60)
        except requests.RequestException as e:
            log("groq compound-mini unreachable:", e)
            return None

    if not res.ok:
        log(f"groq research failed {res.status_code}: {res.text[:1000]}")
        return None

    try:
        content = res.json()["choices"][0]["message"]["content"]
        result = _strip_thinking(content)
        _cache_research(topic, criteria, result)
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _news_groq(topic, focus=""):
    """Fallback when Gemini's search quota is spent — same Groq path as
       research, reusing the same short-lived cache under a namespaced key
       so a news lookup for "gold" never returns a cached shopping answer
       for "gold" and vice versa."""
    cache_topic = f"news:{topic}"
    cached = _get_cached_research(cache_topic, focus)
    if cached:
        return cached

    if not CFG.get("groq_key", "").strip() or CFG.get("groq_key", "").startswith("PASTE"):
        return None

    if focus:
        ask = (f"They want more on this story from the news on {topic}: {focus}. Use live web search "
               "and give them the full story: what happened, who is involved, why it matters. "
               "4-6 spoken sentences, no markdown, no links.")
    else:
        ask = (f"Check the news on: {topic}. Use live web search and come back with the three or four "
               "most important recent things going on, one short spoken sentence each. "
               "No markdown, no links, no source names.")

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {CFG['groq_key']}",
        "Content-Type": "application/json",
        "Groq-Model-Version": "latest",
    }
    model = CFG.get("groq_search_model") or "groq/compound"
    payload = {"model": model, "messages": [{"role": "user", "content": ask}]}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=75)
    except requests.RequestException as e:
        log("groq compound unreachable:", e)
        return None

    if res.status_code == 413:
        log("groq compound returned 413; retrying with compound-mini")
        mini_payload = {
            "model": "groq/compound-mini",
            "messages": [{"role": "user", "content": ask}],
            "compound_custom": {"tools": {"enabled_tools": ["web_search"]}},
        }
        try:
            res = requests.post(url, headers=headers, json=mini_payload, timeout=60)
        except requests.RequestException as e:
            log("groq compound-mini unreachable:", e)
            return None

    if not res.ok:
        log(f"groq news failed {res.status_code}: {res.text[:1000]}")
        return None

    try:
        content = res.json()["choices"][0]["message"]["content"]
        result = _strip_thinking(content)
        _cache_research(cache_topic, focus, result)
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _open_sources(candidate, limit=4):
    """Open the actual pages a grounded search looked at, then bring that
       window onto the work screen. Split out so this only ever runs when he
       has explicitly asked to see something — research and news both report
       by voice and stay off his screen otherwise."""
    chunks = (candidate.get("groundingMetadata") or {}).get("groundingChunks") or []
    seen, opened = set(), 0
    for c in chunks:
        uri = (c.get("web") or {}).get("uri")
        if not uri or uri in seen or not uri.startswith(("https://", "http://")):
            continue          # only web pages — never a file:// or app link
        seen.add(uri)
        _open_web(uri)
        opened += 1
        time.sleep(0.3)
        if opened >= limit:
            break
    if opened and CFG.get("auto_place", True):
        time.sleep(1.4)
        place_active_window()
    return opened


# The model writes this one machine-readable line ahead of its spoken answer
# so the panel doesn't have to guess a winner and runner-up out of prose —
# it gets pulled out here and never reaches his ears.
_RESEARCH_META_RE = re.compile(
    r"^\s*WINNER:\s*(?P<winner>.+?)\s*\|\|\s*RUNNERUP:\s*(?P<runner_up>.+?)\s*\|\|\s*WHY:\s*(?P<why>.+?)\s*$",
    re.MULTILINE)


def _split_research_meta(text):
    m = _RESEARCH_META_RE.search(text)
    if not m:
        return None, text.strip()
    meta = {"winner": m.group("winner").strip(), "runner_up": m.group("runner_up").strip(),
            "reasoning": m.group("why").strip()}
    return meta, (text[:m.start()] + text[m.end():]).strip()


def _research_fallback_panel(fallback, default):
    """Groq's fallback path never writes the WINNER line, so there is
       nothing to build the card from — but it is still a real result,
       so it gets the plain-text panel rather than none at all."""
    return Panel(fallback, "text", title="Research") if fallback else default


def _owner():
    """Who David works for, and where, for prompts that search on their behalf."""
    name = CFG.get("owner_name") or "the user"
    where = CFG.get("home_location") or _guess_location()
    return f"{name}, who is in {where}" if where else name


def research(topic, criteria="", open_links=False):
    """Search the web, compare what is out there, and come back with one
       recommendation — not a list of links to read himself. Stays off his
       screen unless he explicitly asks to see the sources."""
    ask = (
        f"Research this for {_owner()}: {topic}."
        + (f" What matters to them: {criteria}." if criteria else "")
        + " Search the web, compare the real options available now, and come back with ONE"
          " clear recommendation and a runner-up. Say what each costs where you can find it,"
          " and why the winner beats the rest on what they actually asked for."
          " This will be read aloud, so write four or five spoken sentences, no lists,"
          " no markdown, no links in the text. Lead with the recommendation."
          " Before that spoken answer, on its own line, write exactly:"
          " WINNER: <name> || RUNNERUP: <name> || WHY: <one short clause, under 12 words>"
          " — then a blank line, then the spoken answer. Never speak that line or mention it."
    )

    body = {
        "contents": [{"role": "user", "parts": [{"text": ask}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 900, **_thinking(CFG.get("search_thinking", "low"))},
    }

    try:
        res = _gemini_post(body, timeout=90)
    except requests.RequestException as e:
        return _research_fallback_panel(_research_groq(topic, criteria),
                                        f"I could not get online to research that: {e}")

    if res.status_code == 429:
        log("research quota reached — falling back to Groq")
        return _research_fallback_panel(
            _research_groq(topic, criteria),
            "my main research quota is used up and the backup search could not "
            "come back with anything either. Give it a few minutes and try again")

    if not res.ok:
        log("research failed", res.status_code, res.text[:200])
        return _research_fallback_panel(_research_groq(topic, criteria),
                                        "neither search worked just now — try again shortly")

    data = res.json()
    try:
        candidate = data["candidates"][0]
        raw = "".join(p.get("text", "") for p in candidate["content"]["parts"]).strip()
    except (KeyError, IndexError):
        return _research_fallback_panel(_research_groq(topic, criteria), "the search came back empty")

    meta, text = _split_research_meta(raw)

    # only when he explicitly asked to see it — otherwise this is voice-only
    if open_links:
        opened = _open_sources(candidate)
        if opened:
            text += f" I have opened {opened} of the sources on your screen."

    if not meta:
        return Panel(text, "text", title="Research")

    sources = len((candidate.get("groundingMetadata") or {}).get("groundingChunks") or [])
    return Panel(text, "research", topic=topic, winner=meta["winner"],
                 runner_up=meta["runner_up"], reasoning=meta["reasoning"], sources=sources)


def news(topic, focus="", open_links=False):
    """Check the news on a topic and read back what is actually happening —
       headlines only, voice-only, never opens a browser unless he explicitly
       asks to see it. Call again with `focus` set to the story he picked
       once he has heard the headlines, for a fuller spoken summary of just
       that one."""
    if focus:
        ask = (
            f"They just heard some news headlines about {topic} and want to hear more about this one: "
            f"{focus}. Search the web for the full story and give them a real spoken summary — what "
            "happened, who is involved, and why it matters. Five or six spoken sentences, no lists, "
            "no markdown, no links, and nothing that sounds like you are reading a webpage aloud."
        )
    else:
        ask = (
            f"Check what is actually happening right now for {_owner()}, on: {topic}. "
            "Search the web and come back with the three or four most important, most recent things "
            "going on. For each, say what happened and who it involves in one short spoken sentence — "
            "do not just read a headline verbatim, and do not read out source names, dates or links as "
            "citations. No lists, no markdown. End by asking, in your own words, whether they want to "
            "hear more on any of them."
        )

    body = {
        "contents": [{"role": "user", "parts": [{"text": ask}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 700, **_thinking(CFG.get("search_thinking", "low"))},
    }

    try:
        res = _gemini_post(body, timeout=90)
    except requests.RequestException as e:
        fallback = _news_groq(topic, focus)
        return fallback or f"I could not get online to check the news: {e}"

    if res.status_code == 429:
        log("news quota reached — falling back to Groq")
        fallback = _news_groq(topic, focus)
        return fallback or ("my main news search is used up and the backup could not come back with "
                            "anything either. Give it a few minutes and try again")

    if not res.ok:
        log("news check failed", res.status_code, res.text[:200])
        fallback = _news_groq(topic, focus)
        return fallback or "that news check did not come back — try again shortly"

    data = res.json()
    try:
        candidate = data["candidates"][0]
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"]).strip()
    except (KeyError, IndexError):
        fallback = _news_groq(topic, focus)
        return fallback or "the news search came back empty"

    if open_links:
        opened = _open_sources(candidate)
        if opened:
            text += f" I have opened {opened} of the sources on your screen."

    return text or "nothing came back for that"


def open_chart(symbol):
    _open_web("https://www.tradingview.com/chart/?symbol=" + urllib.parse.quote(symbol))
    return f"opening the {symbol} chart"


def empty_bin():
    return "Trash emptied" if macos.empty_trash() else "the Trash would not empty"


def set_timer(minutes, label="timer"):
    seconds = float(minutes) * 60
    TIMERS.append((label, time.time() + seconds))
    timer = threading.Timer(seconds, lambda: NOTIFY(f"The {label} you set for {minutes} minutes is up."))
    timer.daemon = True     # a pending timer must not keep David alive after Ctrl+C
    timer.start()
    return f"timer set for {minutes} minutes"


TIMERS = []
NOTIFY = lambda text: None      # replaced once the session is up

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_clock(text):
    """'9', '9am', '9:30 p.m.', '21:30' -> (hour, minute), or None."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?)?\s*", str(text).lower())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    half = re.sub(r"[.\s]", "", m.group(3) or "")
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _resolve_when(at, day="", now=None):
    """A clock time plus an optional day -> (datetime, None), or (None, what went wrong).
       No day means the next time that clock time comes round."""
    now = now or datetime.now()
    clock = _parse_clock(at)
    if not clock:
        return None, f"I could not tell what time {at} is"
    day = (day or "").strip().lower()
    if day in ("", "today"):
        base = now.date()
    elif day == "tomorrow":
        base = now.date() + timedelta(days=1)
    elif day in _WEEKDAYS:
        base = now.date() + timedelta(days=(_WEEKDAYS.index(day) - now.weekday()) % 7)
    else:
        try:
            base = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return None, f"I could not tell which day {day} is"

    when = datetime(base.year, base.month, base.day, clock[0], clock[1])
    if when <= now:
        if day == "":
            when += timedelta(days=1)
        elif day in _WEEKDAYS:
            when += timedelta(days=7)
        else:
            return None, "that time has already passed"
    return when, None


def set_reminder(title, at, day=""):
    """A reminder in Apple Reminders that alerts at a clock time — on the Mac,
       and on his iPhone through iCloud, whether or not David is running."""
    when, problem = _resolve_when(at, day)
    if problem:
        return problem
    if not macos.add_reminder(title, when):
        return ("Reminders would not take it — macOS may be blocking me. Allow it under System "
                "Settings, Privacy and Security, Automation, then ask again")
    today = datetime.now().date()
    spoken_day = ("today" if when.date() == today else
                  "tomorrow" if when.date() == today + timedelta(days=1) else
                  when.strftime("%A %d %B"))
    return f"reminder set for {when.strftime('%I:%M %p').lstrip('0')} {spoken_day}: {title}"


def write_file(name, content):
    path = _safe_path("~/Desktop", name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return f"saved {os.path.basename(path)} to the desktop"


# ---- the swappable brain (engines.py) ----

def ask_brain(question):
    """Voice hands a hard question to whichever brain is chosen in config.json."""
    import engines
    return engines.ask_once(question)


def use_brain(name):
    import engines
    name = str(name).strip().lower()
    if name not in engines.PROVIDERS:
        return f"I only know these brains: {', '.join(engines.PROVIDERS)}"
    problem = engines.missing_key(name)
    if problem:
        return f"can't switch to {name} yet: {problem}"
    CFG["brain"] = name
    _update_config(brain=name)
    return f"switched to the {name} brain, running {engines.model_for(name)}"


SLEEP_NOW = threading.Event()     # the voice session watches this


def go_to_sleep():
    phrase = CFG.get("wake_phrase")
    if not phrase:
        return "there's no wake phrase set, so I'd have no way to wake up — I'll keep listening"
    SLEEP_NOW.set()
    return f"going to sleep once you've said goodnight — they wake you by saying '{phrase}'"


# ---- background jobs (jobs.py) ----

def add_job(task, every_minutes=0, at="", days=""):
    import jobs
    try:
        job = jobs.add(task, every_minutes=every_minutes, at=at, days=days)
    except ValueError as e:
        return str(e)
    return f"scheduled {jobs.describe(job)}. First run {job['next_run'].replace('T', ' at ')}"


def list_jobs():
    import jobs
    items = jobs.load()
    if not items:
        return "no jobs scheduled"
    rows = [{"primary": j["task"], "secondary": jobs.describe(j).split(":")[0]} for j in items]
    return Panel("; ".join(jobs.describe(j) for j in items), "list", title="Jobs", items=rows)


def remove_job(job_id):
    import jobs
    return f"removed job {job_id}" if jobs.remove(job_id) else f"there is no job {job_id}"



ACTIONS = {
    "open_app": open_app, "open_site": open_site, "open_url": open_url,
    "search_web": search_web, "open_file": open_file, "find_files": find_files,
    "open_folder": open_folder, "volume": volume, "lock": lock,
    "delete_file": delete_file, "rename_file": rename_file, "write_file": write_file,
    "type_text": type_text, "to_clipboard": to_clipboard, "read_clipboard": read_clipboard,
    "screenshot": screenshot, "minimise_all": minimise_all, "close_window": close_window,
    "switch_window": switch_window, "close_app": close_app, "media": media,
    "battery": battery, "what_time": what_time, "sleep_pc": sleep_pc,
    "shutdown": shutdown, "restart": restart, "cancel_shutdown": cancel_shutdown,
    "add_note": add_note, "read_notes": read_notes, "recent_downloads": recent_downloads,
    "open_chart": open_chart, "empty_bin": empty_bin, "set_timer": set_timer, "set_reminder": set_reminder,
    "open_in_browser": open_in_browser, "close_tab": close_tab,
    "smart_open": smart_open, "list_installed": list_installed,
    "maximise_window": maximise_window, "minimise_window": minimise_window,
    "snap_window": snap_window, "research": research, "news": news,
    "get_weather": get_weather,
    "describe_screen": describe_screen, "click_on": click_on,
    "list_monitors": list_monitors, "place_active_window": place_active_window,
    "move_window": move_window, "show_image": show_image,
    "list_drives": list_drives, "copy_file": copy_file,
    "remember": remember, "recall": recall, "forget": forget,
    "write_page": write_page, "read_page": read_page, "search_brain": search_brain,
    "ask_brain": ask_brain, "use_brain": use_brain,
    "add_job": add_job, "list_jobs": list_jobs, "remove_job": remove_job,
    "go_to_sleep": go_to_sleep,
    "check_email": check_email, "read_email": read_email, "send_email": send_email,
    "connect_gmail": connect_gmail, "disconnect_gmail": disconnect_gmail,
    "open_dashboard": open_dashboard,
    "check_chat": check_chat, "check_gmail": check_gmail,
    "open_gmail_message": open_gmail_message,
    "reply_in_gmail": reply_in_gmail, "send_open_gmail": send_open_gmail,
    "scroll": scroll, "press_key": press_key,
    "copy_image_to_clipboard": copy_image_to_clipboard,
    "journal": journal, "notion_table": notion_table, "notion_status": notion_status,
}

# these put something new on screen, so it gets moved off the interface screen.
# research and news are deliberately not here any more — they only open a
# window on the rare call where he explicitly asked to see the sources, and
# they place that window themselves (see _open_sources) rather than firing
# blindly on every call the way this static, name-only set would.
OPENS_A_WINDOW = {
    "open_app", "open_site", "open_url", "search_web", "open_file", "open_folder",
    "smart_open", "open_in_browser", "open_chart",
}

# these take over the mouse, keyboard or foreground, so they cannot overlap
# jobs long enough that silence would feel like nothing is happening
SLOW_JOBS = {
    "ask_brain", "check_email", "check_gmail", "read_email", "open_gmail_message",
    "check_chat", "research", "news", "show_image", "click_on", "describe_screen",
    "journal", "notion_table",
}

NEEDS_THE_SCREEN = {
    "click_on", "type_text", "press_key", "scroll", "check_chat", "show_image",
    "check_email", "check_gmail", "read_email", "open_gmail_message",
    "reply_in_gmail", "send_open_gmail",
    "maximise_window", "minimise_window", "snap_window", "move_window",
    "place_active_window", "close_window", "switch_window", "minimise_all",
}

# small input actions that are meant to be repeated — "scroll down" twice is two scrolls
REPEATABLE = {
    "scroll", "press_key", "type_text", "click_on", "close_tab", "close_window",
    "switch_window", "minimise_window", "maximise_window", "snap_window", "move_window",
    "volume", "media",
}

DESTRUCTIVE = {"add_job", "remove_job", "delete_file", "rename_file", "write_file", "lock", "copy_file", "forget",
               "send_email", "send_open_gmail",
               "close_app", "shutdown", "restart", "sleep_pc", "empty_bin"}

_YES_WORDS = ("yes", "yeah", "yep", "yup", "confirm", "go ahead", "do it",
              "sure", "correct", "affirmative", "proceed", "please do")
_NO_WORDS = ("no", "nope", "don't", "dont", "cancel", "stop", "negative",
             "never mind", "nevermind", "wait", "hold on")


def _is_yes(text):
    """Same word list david.py has always used to confirm out loud — checked
       against what he actually said, not against whatever the model decided
       to call the tool with."""
    # Whole words only: "go ahead now" is not a "no", and "yesterday" is not a "yes".
    t = (text or "").lower().replace("’", "'")
    padded = " " + " ".join(re.sub(r"[^a-z' ]", " ", t).split()) + " "
    if any(f" {w} " in padded for w in _NO_WORDS):
        return False
    return any(f" {w} " in padded for w in _YES_WORDS)


class Gate:
    """The real confirmation check for delete/send/shutdown/etc. — code, not a
       prompt instruction the model can skip under pressure. One per channel:
       the voice session feeds it what he said, Telegram what he typed."""

    def __init__(self):
        self.pending = {}        # action -> when we asked
        self.last_heard = ""     # what he actually said or typed, last completed turn
        self.heard_at = 0.0

    def heard(self, text, at=None):
        self.last_heard, self.heard_at = text, at if at is not None else time.time()

    def check(self, name, args):
        """The real confirmation check for delete/send/shutdown/etc. — code,
           not a prompt instruction the model can skip under pressure.

           First call for a given action: nothing runs. We remember it is
           pending and hand back a line telling the model to ask him
           and call again. Second call: only proceeds if a turn has actually
           finished since we asked, and what he said in it reads as a yes.
           A misheard word can trigger the first call; it cannot by itself
           trigger the second, because that requires a completed user turn
           in between that we independently classify as an answer.

           Returns a string to hand back instead of running the action, or
           None once it is genuinely cleared to proceed."""
        key = name + json.dumps(args, sort_keys=True)
        pending_at = self.pending.get(key)
        now = time.time()

        if pending_at is None or now - pending_at > 45:
            self.pending[key] = now
            desc = _describe_destructive(name, args)
            log("confirmation required:", name, _log_args(args))
            return (f"CONFIRMATION_REQUIRED. Do not perform this action. Ask them clearly: "
                    f"'Are you sure you want me to {desc}?' Wait for them to answer, then call "
                    f"{name} again with the exact same details only if they say yes.")

        if self.heard_at <= pending_at:
            return "They have not answered yet — ask again and wait for a real yes or no before calling this again."

        del self.pending[key]
        if _is_yes(self.last_heard):
            log("confirmed:", name, _log_args(args))
            return None
        log("declined:", name, _log_args(args), "— heard:", repr(self.last_heard))
        return "They did not confirm that — left it alone."


def _describe_destructive(name, args):
    phrasing = {
        "delete_file": lambda a: f"delete {a.get('name', 'that file')}",
        "rename_file": lambda a: f"rename {a.get('name', 'that file')} to {a.get('new_name', '')}",
        "write_file": lambda a: f"save a file called {a.get('name', 'that')} to the desktop",
        "lock": lambda a: "lock the computer",
        "copy_file": lambda a: f"copy {a.get('name', 'that file')} to {a.get('destination', '')}",
        "forget": lambda a: f"forget {a.get('about', 'that')}",
        "send_email": lambda a: f"send that email to {a.get('to', '')}",
        "send_open_gmail": lambda a: "send that email",
        "close_app": lambda a: f"close {a.get('name', 'that app')}",
        "shutdown": lambda a: "shut down the computer",
        "restart": lambda a: "restart the computer",
        "sleep_pc": lambda a: "put the computer to sleep",
        "empty_bin": lambda a: "empty the Trash",
        "add_job": lambda a: (f"run '{a.get('task', 'that')}' " +
                              (f"every {a['every_minutes']} minutes" if a.get("every_minutes")
                               else f"at {a.get('at', '')} {a.get('days') or 'every day'}") + " from now on"),
        "remove_job": lambda a: f"remove job {a.get('job_id', '')}",
    }
    try:
        return phrasing.get(name, lambda a: name)(args)
    except Exception:
        return name


# ---- running a tool from any channel ----

SCREEN = threading.Lock()   # one job at a time drives the mouse and keyboard, whichever channel asked


def with_screen(fn, args):
    with SCREEN:
        return fn(**args)


def run(name, args, gate=None):
    """Run one tool for a text channel (Telegram, a background job, a thinking
       engine) and return what it says. The voice session has its own richer
       runner in david_live.py — this is the same rules, without the voice."""
    args = args if isinstance(args, dict) else {}
    fn = ACTIONS.get(name)
    if not fn:
        return f"there is no tool called {name}"
    if name in DESTRUCTIVE:
        if gate is None:
            return (f"{name} needs them to say yes first, and nobody can answer here. "
                    "Tell them to ask for it by voice or on Telegram.")
        held = gate.check(name, args)
        if held is not None:
            return held
    log("action:", name, _log_args(args))
    try:
        if name in NEEDS_THE_SCREEN:
            if not SCREEN.acquire(blocking=False):
                return "the screen is busy with another job — try again in a moment"
            try:
                result = fn(**args)
            finally:
                SCREEN.release()
        else:
            result = fn(**args)
    except Exception as e:
        log("action failed:", e)
        result = f"that failed: {e}"
    if isinstance(result, Panel):
        result = result.speech
    return str(result)


def declarations():
    s = lambda d: {"type": "STRING", "description": d}
    return [
        {"name": "open_app", "description": "Open an app from the configured list.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("app name")}, "required": ["name"]}},
        {"name": "open_site", "description": "Open a site from the configured list.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("site name")}, "required": ["name"]}},
        {"name": "open_url", "description": "Open any web address.",
         "parameters": {"type": "OBJECT", "properties": {"url": s("the address")}, "required": ["url"]}},
        {"name": "search_web", "description": "Open a Google search results page in the browser, on screen, for them to read themselves. Only use this when they explicitly want a browser pulled up — for checking the news, current events, or anything they want read back to them, use news or research instead, which report by voice and never open a browser on their own.",
         "parameters": {"type": "OBJECT", "properties": {"query": s("what to search")}, "required": ["query"]}},
        {"name": "open_file", "description": "Open the closest matching file.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("part of the filename")}, "required": ["name"]}},
        {"name": "find_files", "description": "List matching files without opening them.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("part of the filename")}, "required": ["name"]}},
        {"name": "open_folder", "description": "Open a folder.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("folder name")}, "required": ["name"]}},
        {"name": "volume", "description": "Change system volume.",
         "parameters": {"type": "OBJECT", "properties": {"direction": s("up, down or mute")}, "required": ["direction"]}},
        {"name": "lock", "description": "Lock the workstation.", "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "delete_file", "description": "Move a file to a recovery folder. Confirm with them first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("part of the filename")}, "required": ["name"]}},
        {"name": "rename_file", "description": "Rename a file. Confirm with them first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("current name"), "new_name": s("new name")},
                        "required": ["name", "new_name"]}},
        {"name": "write_file", "description": "Write a text file to the desktop. Confirm with them first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("filename"), "content": s("contents")},
                        "required": ["name"]}},

        {"name": "type_text", "description": "Type text into whatever window they are currently in. Use this when they dictate a message, an email or a paragraph.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("exactly what to type")}, "required": ["text"]}},
        {"name": "to_clipboard", "description": "Put text on the clipboard without typing it.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("what to copy")}, "required": ["text"]}},
        {"name": "read_clipboard", "description": "Read back whatever is on the clipboard.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "screenshot", "description": "Capture the screen and save it to the desktop.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "get_weather", "description": "Current conditions and a short forecast for a place. Use this for any weather question — do not use research or news for weather. If they don't name a place, leave location blank — it falls back to their configured home location, or detects it automatically.",
         "parameters": {"type": "OBJECT", "properties": {"location": s("city or place they asked about, blank if they did not say one")}}},
        {"name": "research", "description": "Search the web, compare the real options, and come back with a recommendation. Use this whenever they ask what to buy, which is best, or want options compared. It takes twenty seconds or so, so tell them you are on it before calling it. Reports by voice and never opens a browser on its own.",
         "parameters": {"type": "OBJECT", "properties": {"topic": s("what they want researched"),
                                                        "criteria": s("what matters to them: budget, use, preferences"),
                                                        "open_links": {"type": "BOOLEAN",
                                                            "description": "true only if they explicitly asked to see, pull up, or open the sources — otherwise leave it out"}},
                        "required": ["topic"]}},
        {"name": "news", "description": "Check the news on a topic — finance, politics, sports, anything current — and read back what is actually happening. Use whenever they ask to check the news, catch up, or what is going on with something; do not use search_web or research for this. Reports a short list of headlines by voice and never opens a browser on its own. Once they have heard the headlines and pick one, call this again with `focus` set to that story for a fuller spoken summary of just it.",
         "parameters": {"type": "OBJECT", "properties": {
             "topic": s("what news to check on"),
             "focus": s("optional — the specific headline or story they want to hear more about, once they have picked one from a list you already read them"),
             "open_links": {"type": "BOOLEAN",
                 "description": "true only if they explicitly asked to see, pull up, or open the source — otherwise leave it out"}},
                        "required": ["topic"]}},

        {"name": "show_image", "description": "Show them a picture of something. Opens image results on the other screen and opens the best photo large.",
         "parameters": {"type": "OBJECT", "properties": {"query": s("what to show a picture of")}, "required": ["query"]}},
        {"name": "list_monitors", "description": "How many screens are attached and how big they are.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "move_window", "description": "Move the current window to a named or numbered screen.",
         "parameters": {"type": "OBJECT", "properties": {"screen": s("a screen name, nickname, or number")},
                        "required": ["screen"]}},
        {"name": "remember", "description": "Save something they have told you, so you still know it in future sessions. Use this whenever they say remember this, from now on, always, never, or tell you how they want things done.",
         "parameters": {"type": "OBJECT", "properties": {
             "fact": s("the thing to remember, written as a plain statement"),
             "category": s("a rough grouping: preferences, trading, work, people, screens")},
                        "required": ["fact"]}},
        {"name": "recall", "description": "Say what they have told you to remember, optionally about one thing.",
         "parameters": {"type": "OBJECT", "properties": {"about": s("topic to filter by, optional")}}},
        {"name": "forget", "description": "Drop something from memory. Confirm with them first.",
         "parameters": {"type": "OBJECT", "properties": {"about": s("what to forget")}, "required": ["about"]}},
        {"name": "ask_brain", "description": "Hand a hard question — one needing careful reasoning, several steps, or writing — to your thinking brain (Gemini, Claude or another model, whichever they chose), which can use every tool you have. Tell them you are thinking it over first; it can take a minute. Read its answer back in your own words, briefly.",
         "parameters": {"type": "OBJECT", "properties": {"question": s("the full question, with everything it needs to know")}, "required": ["question"]}},
        {"name": "use_brain", "description": "Switch which model does the thinking for typed chats, background jobs and ask_brain: gemini, claude or openrouter. Your voice stays the same.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("gemini, claude or openrouter")}, "required": ["name"]}},
        {"name": "add_job", "description": "Schedule something to do on its own, again and again: at a clock time (optionally on certain days) or every so many minutes. The task is plain words, done with all your tools, and the result is spoken and sent to their Telegram. Use for 'every morning', 'every hour', 'each Friday', 'keep an eye on'. For a one-off at a time, use set_reminder instead.",
         "parameters": {"type": "OBJECT", "properties": {
             "task": s("what to do each time, written as an instruction to yourself"),
             "at": s("a clock time like 7am or 18:30 — leave out if using every_minutes"),
             "days": s("which days for a clock time: weekdays, weekends, or names like monday, friday — blank for every day"),
             "every_minutes": {"type": "NUMBER", "description": "repeat every this many minutes (5 or more) — leave out if using a clock time"}},
                        "required": ["task"]}},
        {"name": "go_to_sleep", "description": "Stop listening until they say the wake phrase again. Use when they say go to sleep, goodnight, that's all for now, or stop listening. Say a short goodbye first.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "list_jobs", "description": "Say which background jobs are scheduled.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "remove_job", "description": "Stop a background job by its number. Confirm with them first.",
         "parameters": {"type": "OBJECT", "properties": {"job_id": s("the job number")}, "required": ["job_id"]}},
        {"name": "write_page", "description": "Write a longer note into your Second Brain wiki — research results, how something works, a person's details, a plan. Use remember for one-line facts. Link related pages with [[Page Title]].",
         "parameters": {"type": "OBJECT", "properties": {"title": s("page title"), "text": s("what to write, in Markdown"),
                                                        "append": {"type": "BOOLEAN", "description": "false to replace the page; default adds a dated section"}},
                        "required": ["title", "text"]}},
        {"name": "read_page", "description": "Read a page from your Second Brain: a wiki title, or a path such as conversations/2026-11-18 for that day's conversations.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("page title or path")}, "required": ["name"]}},
        {"name": "search_brain", "description": "Search every note in your Second Brain for words.",
         "parameters": {"type": "OBJECT", "properties": {"query": s("words to look for")}, "required": ["query"]}},

        {"name": "open_dashboard", "description": "Open David's dashboard: a chat box to type to him, the thinking-brain switch, their background jobs, their latest notes, Telegram status, and settings for Gmail, Notion and the display. Use when they ask to open or show the dashboard, the chat box, their jobs or their notes.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "connect_gmail", "description": "Sign in to Gmail properly, so email works without reading the screen. Use when they ask to connect, set up, or link their email.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "disconnect_gmail", "description": "Sign out of Gmail.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "check_email", "description": "Look through their inbox. Can filter by sender, subject, how far back, and unread only. Reports who sent what without reading bodies aloud.",
         "parameters": {"type": "OBJECT", "properties": {
             "sender": s("name or address to filter by, optional"),
             "subject": s("words in the subject, optional"),
             "hours": {"type": "NUMBER", "description": "how far back, default 24"},
             "unread_only": {"type": "BOOLEAN", "description": "only unread"}}}},
        {"name": "read_email", "description": "Read out the full text of one message from the last check, by its number. Offer before doing this rather than reading unprompted.",
         "parameters": {"type": "OBJECT", "properties": {"which": {"type": "NUMBER", "description": "1 is the newest"}}}},
        {"name": "send_email", "description": "Send an email from their account. Always read the whole thing back and get a yes before sending.",
         "parameters": {"type": "OBJECT", "properties": {"to": s("address"), "subject": s("subject"), "body": s("the message")},
                        "required": ["to", "subject", "body"]}},
        {"name": "check_chat", "description": "Open a messaging app and read the conversation with someone off the screen. Use for WhatsApp, Telegram and anything without an API.",
         "parameters": {"type": "OBJECT", "properties": {
             "person": s("who the conversation is with"),
             "question": s("what they want to know, optional"),
             "app": s("whatsapp, telegram or similar, default whatsapp")},
                        "required": ["person"]}},

        {"name": "check_gmail", "description": "Open Gmail in the browser and read the results off the screen. check_email already does this; use that unless they ask for Gmail by name.",
         "parameters": {"type": "OBJECT", "properties": {
             "sender": s("who from, optional"), "subject": s("subject words, optional"),
             "hours": {"type": "NUMBER", "description": "how far back, default 24"},
             "unread_only": {"type": "BOOLEAN", "description": "unread only"},
             "question": s("what they want to know, optional")}}},
        {"name": "open_gmail_message", "description": "Open one of the emails showing on screen and read it out.",
         "parameters": {"type": "OBJECT", "properties": {"which": {"type": "NUMBER", "description": "1 is the top one"}}}},

        {"name": "reply_in_gmail", "description": "Reply to the email currently open on screen. Types the reply but does not send it.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("the reply, in full")}, "required": ["text"]}},
        {"name": "send_open_gmail", "description": "Send the reply that is typed and waiting. Only after they say to.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "list_drives", "description": "Every disk mounted, with free space.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "copy_file", "description": "Copy a file to a disk, folder or saved place. Confirm with them first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("part of the filename"),
                                                        "destination": s("disk name, folder path or saved place name")},
                        "required": ["name", "destination"]}},
        {"name": "place_active_window", "description": "Move whatever is in front onto the working screen, away from the interface.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "describe_screen", "description": "Look at what is currently on the screen and describe it, or answer a question about it. Use this whenever they refer to something they can see.",
         "parameters": {"type": "OBJECT", "properties": {"question": s("what to look for, optional")}}},
        {"name": "click_on", "description": "Look at the screen and click on something described in plain words, like the third image, the search box, or the download button.",
         "parameters": {"type": "OBJECT", "properties": {"description": s("what to click"),
                                                        "double": {"type": "BOOLEAN", "description": "double click"}},
                        "required": ["description"]}},
        {"name": "scroll", "description": "Scroll the page up or down.",
         "parameters": {"type": "OBJECT", "properties": {"direction": s("up or down"),
                                                        "amount": {"type": "NUMBER", "description": "how far, default 5"}}}},
        {"name": "press_key", "description": "Press a single key such as enter, escape, f11 or tab.",
         "parameters": {"type": "OBJECT", "properties": {"key": s("the key")}, "required": ["key"]}},

        {"name": "maximise_window", "description": "Make the current window full screen.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "minimise_window", "description": "Minimise the current window.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "snap_window", "description": "Snap the current window to one half of the screen.",
         "parameters": {"type": "OBJECT", "properties": {"side": s("left or right")}, "required": ["side"]}},

        {"name": "minimise_all", "description": "Hide every app and show the desktop.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "close_window", "description": "Close the window they are looking at.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "switch_window", "description": "Switch to the previous window.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "close_app", "description": "Quit an app by name, like chrome or notes.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("app name")}, "required": ["name"]}},

        {"name": "media", "description": "Control whatever is playing.",
         "parameters": {"type": "OBJECT", "properties": {"command": s("play, pause, next or previous")},
                        "required": ["command"]}},

        {"name": "battery", "description": "Battery percentage and whether it is charging.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "what_time", "description": "The current time and date.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "sleep_pc", "description": "Put the Mac to sleep. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "shutdown", "description": "Shut down after a delay. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {"when_seconds": {"type": "NUMBER", "description": "delay, default 30"}}}},
        {"name": "restart", "description": "Restart after a delay. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {"when_seconds": {"type": "NUMBER", "description": "delay, default 30"}}}},
        {"name": "cancel_shutdown", "description": "Cancel a pending shutdown or restart.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "add_note", "description": "Append a line to their notes file on the desktop.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("the note")}, "required": ["text"]}},
        {"name": "read_notes", "description": "Read back their most recent notes.",
         "parameters": {"type": "OBJECT", "properties": {"count": {"type": "NUMBER", "description": "how many, default 5"}}}},
        {"name": "recent_downloads", "description": "Name the most recently downloaded files.",
         "parameters": {"type": "OBJECT", "properties": {"count": {"type": "NUMBER", "description": "how many, default 5"}}}},
        {"name": "notion_status", "description": "Check right now, for real, whether Notion is connected. Instant, no network call. Call this whenever journaling or a Notion table comes up in conversation, before doing or saying anything else about it — the connection state in your instructions is only a snapshot from when this conversation started and goes stale the moment they connect or disconnect it from the dashboard.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "journal", "description": "Write a journal entry to their connected Notion page. Use whenever they ask you to journal something, write it in their journal, or log a thought or reflection there. If Notion is not connected it tells you so — say that to them plainly and suggest they connect it from the dashboard.",
         "parameters": {"type": "OBJECT", "properties": {"entry": s("what to write, tidied into a clear sentence or two")}, "required": ["entry"]}},
        {"name": "notion_table", "description": "Add a real table to their connected Notion page. Use whenever they ask for a table, a tracker, a log, or a grid of rows and columns in Notion. Build the rows yourself from what they tell you — do not ask them to dictate pipe characters or formatting out loud. If Notion is not connected it tells you so — say that plainly and suggest they connect it from the dashboard.",
         "parameters": {"type": "OBJECT", "properties": {
             "rows": s("the table's content as plain text: one row per line, cells separated by a single | character, "
                       "the first line being the column headers. Example: 'Date | Pair | Result\\n14 Aug | EURUSD | Win'"),
             "title": s("a short heading for the table, optional — defaults to the date and time if left out")},
                        "required": ["rows"]}},

        {"name": "open_chart", "description": "Open a TradingView chart for a specific symbol, like US30, EURUSD or XAUUSD.",
         "parameters": {"type": "OBJECT", "properties": {"symbol": s("the ticker")}, "required": ["symbol"]}},
        {"name": "empty_bin", "description": "Empty the Trash. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "smart_open", "description": "Open something by name. Checks what is installed on the Mac first and falls back to the website. Prefer this over open_app whenever they just say open something.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("what to open")}, "required": ["name"]}},
        {"name": "list_installed", "description": "List apps installed on the Mac, optionally filtered.",
         "parameters": {"type": "OBJECT", "properties": {"contains": s("filter word, optional")}}},
        {"name": "copy_image_to_clipboard", "description": "Put the latest screenshot on the clipboard so they can paste it.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "open_in_browser", "description": "Open a site or address in a specific browser rather than the default one. Use when they name a browser.",
         "parameters": {"type": "OBJECT", "properties": {"browser": s("chrome, edge, brave or safari"),
                                                        "target": s("site name or address")},
                        "required": ["browser", "target"]}},
        {"name": "close_tab", "description": "Close the browser tab they are looking at. Use this when they ask to close a website, since sites are tabs and not apps.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "set_reminder", "description": "Set a reminder that alerts at a clock time. Use this whenever they ask for an alarm, a wake-up, or a reminder at a particular time or day. It goes into Apple Reminders, so it alerts on the Mac and their iPhone even when you are not running. For a countdown like 'in 20 minutes', use set_timer instead.",
         "parameters": {"type": "OBJECT", "properties": {
             "title": s("what the reminder says, like 'Wake up' or 'Call the bank'"),
             "at": s("the clock time, like 9am, 9:30 pm or 21:30"),
             "day": s("today, tomorrow, a weekday like friday, or YYYY-MM-DD; blank for the next time that clock time comes round")},
                        "required": ["title", "at"]}},
        {"name": "set_timer", "description": "Set a timer. They will be told out loud when it finishes.",
         "parameters": {"type": "OBJECT", "properties": {"minutes": {"type": "NUMBER", "description": "how long"},
                                                        "label": s("what it is for")},
                        "required": ["minutes"]}},
    ]


def build_system():
    """Written at connect time so it includes what is actually plugged in."""
    screens = monitors()
    screen_lines = [f"  screen {i + 1}: {screen_label(i)}"
                    f"{'  <- your interface lives here' if i == int(CFG.get('ui_monitor', 2)) - 1 else ''}"
                    for i in range(len(screens))]

    gmail_state = ("connected via the real Gmail login — reading and sending mail costs nothing "
                  "and needs no screen" if (_HAVE_OAUTH_GMAIL and oauth_gmail.is_connected())
                  else "not connected — email currently goes through the browser and costs a screen read")

    notion_state = ("connected — journal saves straight to their Notion page"
                    if (_HAVE_OAUTH_NOTION and oauth_notion.is_connected())
                    else "not connected — journal will tell them to connect it from the dashboard first")

    hardware = (
        f"\n\nEmail: {gmail_state}"
        f"\n\nNotion journal: {notion_state}"
        f"\n\nTheir screens right now:\n" + "\n".join(screen_lines) +
        f"\n\nDisks attached: {_speech(list_drives())}"
        f"\nSaved places they can copy things to: {', '.join(CFG.get('places', {})) or 'none set'}"
        "\n\nCall screens by their names, not their numbers, when you talk to them."
    )
    return SYSTEM + hardware + memory_for_prompt()


def _load_system():
    """The persona lives in prompt.md, so it can be changed without touching code."""
    with open(os.path.join(HERE, "prompt.md"), encoding="utf-8") as f:
        text = f.read()
    text = text.replace("{name}", CFG.get("owner_name") or "the user")
    text = text.replace("{about}", CFG.get("owner_about") or "")
    return (text.strip()
            + f"\n\nApps available: {', '.join(CFG.get('apps', {}))}"
            + "\nInstalled on this Mac (speech-to-text often mishears app names — if what you heard "
              f"sounds like one of these, it is that one): {', '.join(sorted(_index_apps()))}"
            + f"\nSites available: {', '.join(CFG.get('sites', {}))}")


SYSTEM = _load_system()
