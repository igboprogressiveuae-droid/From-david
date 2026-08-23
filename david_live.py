"""
David, live.
Created by Ibans.

One websocket to Google. Your microphone streams up, his voice streams back,
and he can act on your machine mid-sentence. No local speech recognition, no
local text to speech, no waiting for a round trip after you finish talking.

    python david_live.py

Needs:  pip install websockets sounddevice requests
Uses the same config.json as david.py.
"""

import asyncio
import base64
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import urllib.parse
import webbrowser
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

import requests
import sounddevice as sd
import websockets
from PIL import Image, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
UI = "--ui" in sys.argv or "--hud" in sys.argv

MIC_RATE = 16000        # what the API expects going up
SPEAKER_RATE = 24000    # what it sends back
CHUNK = 1024

MODEL = "gemini-3.1-flash-live-preview"
DEBUG = "--debug" in sys.argv
WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        sys.exit("config.json is missing.")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("api_key", "").startswith("PASTE"):
        sys.exit("Put your API key in config.json first.")
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


# ----------------------------------------------------------------------------
# things he can do
# ----------------------------------------------------------------------------

def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def _open_web(url, new_window=False):
    """Every web address goes through his working browser, never the system default."""
    browser = CFG.get("work_browser") or "chrome"
    exe = CFG.get("browsers", {}).get(browser)
    if exe and os.path.exists(exe):
        args = [exe, "--new-window", url] if new_window else [exe, url]
        subprocess.Popen(args)
        return True
    webbrowser.open(url)
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
    try:
        os.startfile(target) if os.path.exists(target) else subprocess.Popen(target, shell=False)
    except Exception:
        subprocess.Popen(f'start "" "{target}"', shell=True)
    return f"opened {name}"


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

    if not exe or not os.path.exists(exe):
        _open_web(url)
        return f"{browser} is not in my list, so I used the default browser"

    subprocess.Popen([exe, url])
    return f"opened {target} in {browser}"


def close_tab():
    _press(0x11, 0x57)          # ctrl + w
    return "closed the tab"


def search_web(query):
    _open_web("https://www.google.com/search?q=" + urllib.parse.quote(query))
    return f"searching for {query}"


def open_file(name):
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    os.startfile(hits[0])
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
    os.startfile(root)
    return f"opened {os.path.basename(root)}"


def volume(direction):
    key = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}.get(direction, 0xAF)
    for _ in range(1 if direction == "mute" else 5):
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)
    return "done"


def lock():
    ctypes.windll.user32.LockWorkStation()
    return "locking"


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
    target = hits[0]
    bin_dir = expand("~/.david_trash")
    os.makedirs(bin_dir, exist_ok=True)
    shutil.move(target, os.path.join(bin_dir, os.path.basename(target)))
    return f"moved {os.path.basename(target)} to the recovery folder"


def rename_file(name, new_name):
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    if not new_name.strip() or os.path.basename(new_name) != new_name:
        return "give me a plain file name for that, not a path"
    os.rename(hits[0], os.path.join(os.path.dirname(hits[0]), new_name))
    return f"renamed to {new_name}"


# ---- typing and clipboard ----

def _press(*codes):
    for c in codes:
        ctypes.windll.user32.keybd_event(c, 0, 0, 0)
    for c in reversed(codes):
        ctypes.windll.user32.keybd_event(c, 0, 2, 0)


def to_clipboard(text):
    subprocess.run("clip", input=text.encode("utf-16-le"), shell=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "copied"


def type_text(text):
    """Put text into whatever window he is in — dictation, essentially."""
    to_clipboard(text)
    time.sleep(0.15)
    _press(0x11, 0x56)          # ctrl + v
    return "typed it"


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetClipboardData.restype = ctypes.c_void_p
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_CF_UNICODETEXT = 13


def read_clipboard():
    # Native clipboard read instead of shelling out to PowerShell for
    # Get-Clipboard — same PowerShell-startup tax as everything else here
    # for what is, in-process, a sub-millisecond call.
    if not _user32.OpenClipboard(0):
        return "the clipboard is empty"
    try:
        if not _user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return "the clipboard is empty"
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return "the clipboard is empty"
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return "the clipboard is empty"
        try:
            text = (ctypes.wstring_at(ptr) or "").strip()
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()
    return text[:1200] if text else "the clipboard is empty"


# ---- screen and windows ----

# ---- finding what is actually installed ----

_APP_INDEX = None


def _index_apps():
    """Every shortcut in the Start Menu, so he can open anything installed."""
    global _APP_INDEX
    if _APP_INDEX is not None:
        return _APP_INDEX

    roots = [
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".lnk", ".url")):
                    found.setdefault(os.path.splitext(f)[0].lower(), os.path.join(base, f))
    _APP_INDEX = found
    log(f"indexed {len(found)} installed programs")
    return found


def find_installed(name):
    apps = _index_apps()
    key = name.lower().strip()
    if key in apps:
        return apps[key]
    matches = [p for n, p in apps.items() if key in n]
    if matches:
        return sorted(matches, key=len)[0]
    # match the other way round: "word" finding "Microsoft Word 2019"
    words = key.split()
    for n, p in apps.items():
        if all(w in n for w in words):
            return p
    return None


def smart_open(name):
    """Open the installed program if he has it, otherwise the website."""
    configured = CFG.get("apps", {}).get(name.lower())
    if configured:
        return open_app(name)

    installed = find_installed(name)
    if installed:
        os.startfile(installed)
        return f"opened {os.path.basename(os.path.splitext(installed)[0])}"

    site = CFG.get("sites", {}).get(name.lower())
    if site:
        _open_web(site)
        return f"you do not have {name} installed, so I opened the web version"

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
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
          f"$img = [Drawing.Image]::FromFile('{path}'); "
          "[Windows.Forms.Clipboard]::SetImage($img)")
    subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"{os.path.basename(path)} is on the clipboard"


def screenshot():
    name = f"screenshot-{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
    path = os.path.join(expand("~/Desktop"), name)
    ImageGrab.grab(all_screens=True).save(path)
    return f"saved {name} to the desktop"


# ---- things he has told David to remember ----

MEMORY_FILE = os.path.join(HERE, "memory.json")


def _load_memory():
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _save_memory(items):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items[-300:], f, indent=1, ensure_ascii=False)


def remember(fact, category="general"):
    """Keep something he said for every session from now on."""
    items = _load_memory()

    # replace anything that is clearly the same fact rather than stacking
    def key_words(text):
        cleaned = re.sub(r"[^a-z0-9 ]", " ", text.lower())
        return set(w for w in cleaned.split() if len(w) > 3)

    words = key_words(fact)
    for existing in list(items):
        old = key_words(existing["fact"])
        if not words or not old:
            continue
        overlap = len(words & old) / min(len(words), len(old))
        if overlap >= 0.7:
            log("replacing an older version of that memory")
            items.remove(existing)

    items.append({"fact": fact.strip(), "category": category,
                  "learned": datetime.now().strftime("%Y-%m-%d")})
    _save_memory(items)
    return f"noted, I will remember that"


def recall(about=""):
    """What he has told David, optionally filtered."""
    items = _load_memory()
    if not items:
        return "you have not told me anything to remember yet"
    if about:
        needle = about.lower()
        items = [i for i in items if needle in i["fact"].lower() or needle in i["category"].lower()]
        if not items:
            return f"nothing about {about}"
    recent = items[-15:]
    speech = "; ".join(i["fact"] for i in recent)
    rows = [{"primary": i["fact"], "secondary": f'{i["category"]} · {i["learned"]}'}
           for i in reversed(recent)]
    return Panel(speech, "list", title="Remembered" + (f" — {about}" if about else ""), items=rows)


def forget(about):
    """Drop anything matching what he describes."""
    items = _load_memory()
    needle = about.lower()
    keep = [i for i in items if needle not in i["fact"].lower() and needle not in i["category"].lower()]
    dropped = len(items) - len(keep)
    if not dropped:
        return f"I had nothing about {about}"
    _save_memory(keep)
    return f"forgotten — dropped {dropped} thing{'s' if dropped != 1 else ''}"


def memory_for_prompt():
    items = _load_memory()
    if not items:
        return ""
    # Every category caps its own tail below, but with enough categories that
    # still grows the prompt — and the cost — without bound as he uses David
    # for longer. Keep only the most recent 80 facts overall, newest last.
    items = items[-80:]
    by_cat = {}
    for i in items:
        by_cat.setdefault(i["category"], []).append(i["fact"])
    lines = []
    for cat, facts in by_cat.items():
        lines.append(f"  {cat}:")
        lines += [f"    - {f}" for f in facts[-25:]]
    return ("\n\nThings he has told you to remember. Treat these as true and act on them "
            "without being reminded:\n" + "\n".join(lines))


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

    if not vision_available():
        if _activate("gmail"):
            _navigate_current_tab(url)
        else:
            _open_web(url, new_window=True)
            _remember_window("gmail")
        return ("Gmail is open with that search, but I cannot read the screen right now — "
                "the daily vision quota is used up. Take a look yourself for now.")

    if _activate("gmail"):
        _navigate_current_tab(url)
        time.sleep(4)
    else:
        browser = CFG.get("work_browser") or "chrome"
        exe = CFG.get("browsers", {}).get(browser)
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, "--new-window", url])
        else:
            _open_web(url)
        time.sleep(4)
        _remember_window("gmail")
        place_active_window()
        time.sleep(1.8)

    ask = question or (
        "This is his Gmail. Read the message list and tell him exactly what is there: who each "
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
    result = click_on(f"the {ordinal} email in the message list")
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
    if configured:
        try:
            os.startfile(configured) if os.path.exists(configured) else subprocess.Popen(configured, shell=False)
            return "app"
        except Exception:
            pass

    installed = find_installed(app)
    if installed:
        os.startfile(installed)
        return "app"

    proto = APP_PROTOCOLS.get(key)
    if proto:
        try:
            os.startfile(proto)
            return "app"
        except Exception:
            pass

    site = CFG.get("sites", {}).get(key)
    if site:
        _open_web(site)
        return "web"
    return None


def check_chat(person, question="", app="whatsapp"):
    """Personal messaging has no API, so this opens the chat and reads the
       screen the way he would himself. Uses the installed app if he has it."""
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
    to_clipboard(person)
    time.sleep(0.2)
    _press(0x11, 0x56)
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
    time.sleep(0.4)
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if hwnd:
        _WINDOWS[key] = hwnd
    return hwnd


def _activate(key):
    """Bring a window we opened earlier back to the front. False if it is gone."""
    hwnd = _WINDOWS.get(key)
    if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
        _WINDOWS.pop(key, None)
        return False
    ctypes.windll.user32.ShowWindow(hwnd, 9)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return True


def _navigate_current_tab(url):
    """Ctrl+L, type the address, Enter — reuses the tab instead of adding one."""
    _press(0x11, 0x4C)
    time.sleep(0.35)
    to_clipboard(url)
    time.sleep(0.15)
    _press(0x11, 0x56)
    time.sleep(0.2)
    _press(0x0D)


# ---- instant acknowledgement ----

_ACKS = ["On it.", "Working on it.", "Give me a second.", "Checking now."]
_ack_lock = threading.Lock()


def ack_out_loud():
    """Say something the moment a job starts, so he is never left in silence.
       Uses the built-in Windows voice, which is instant and local."""
    if not CFG.get("spoken_acknowledgement", True):
        return
    phrase = _ACKS[int(time.time()) % len(_ACKS)]

    def run():
        if not _ack_lock.acquire(blocking=False):
            return
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Speech; "
                 "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                 f"$s.Rate = 1; $s.Speak('{phrase}')"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=8)
        except Exception:
            pass
        finally:
            _ack_lock.release()

    threading.Thread(target=run, daemon=True).start()


# ---- monitors ----

def monitors():
    """Every screen attached, left to right."""
    rects = []

    MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_long), ctypes.c_double)

    def collect(hmon, hdc, lprect, data):
        r = ctypes.cast(lprect, ctypes.POINTER(ctypes.c_long * 4)).contents
        rects.append({"left": r[0], "top": r[1], "right": r[2], "bottom": r[3]})
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(collect), 0)
    rects.sort(key=lambda r: r["left"])
    return rects


_MONITOR_NAMES = None


def monitor_names():
    """Real display names from Windows, so screens can be called what they are."""
    global _MONITOR_NAMES
    if _MONITOR_NAMES is not None:
        return _MONITOR_NAMES

    ps = ("Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorID | "
          "ForEach-Object { ($_.UserFriendlyName | Where-Object {$_ -ne 0} | "
          "ForEach-Object {[char]$_}) -join '' }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    except Exception:
        names = []

    _MONITOR_NAMES = names
    return names


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
    hardware = monitor_names()
    name = hardware[index] if index < len(hardware) else None

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


# Matches the PowerShell one-liner below: "C: Windows 123.4GB free of 456.7GB"
_DRIVE_RE = re.compile(r"^(?P<device>\S+)\s+(?:(?P<label>.*?)\s+)?"
                       r"(?P<free>[\d.]+)GB free of (?P<total>[\d.]+)GB$")


def list_drives():
    """Every drive attached, with space and label.

       This used to shell out to PowerShell for a WMI query — Get-CimInstance
       plus the PowerShell process itself together cost the better part of a
       second, called fresh on every reconnect since build_system() reports
       this in the system prompt. The same numbers are sitting right there in
       the Win32 API, in-process, effectively free."""
    kernel32 = ctypes.windll.kernel32
    bitmask = kernel32.GetLogicalDrives()
    lines = []
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if not (bitmask & (1 << i)):
            continue
        root = f"{letter}:\\"
        # 2 = removable, 3 = fixed, 4 = network — skips empty optical drives
        # and other drive types that would just add noise here.
        if kernel32.GetDriveTypeW(root) not in (2, 3, 4):
            continue
        vol_name = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(root, vol_name, 260, None, None, None, None, 0)
        if not ok:
            continue
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        if not kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free_bytes),
                                            ctypes.byref(total_bytes), None):
            continue
        free_gb = free_bytes.value / (1024 ** 3)
        total_gb = total_bytes.value / (1024 ** 3)
        lines.append(f"{letter}: {vol_name.value} {free_gb:.1f}GB free of {total_gb:.1f}GB")
    if not lines:
        return "no drives found"

    speech = "; ".join(lines)
    rows = []
    for line in lines:
        m = _DRIVE_RE.match(line)
        if m:
            rows.append({"primary": f'{m.group("device")} {m.group("label") or "Local Disk"}'.strip(),
                        "secondary": f'{m.group("free")} GB free of {m.group("total")} GB'})
        else:
            rows.append({"primary": line})
    return Panel(speech, "list", title="Drives", items=rows)


def copy_file(name, destination):
    """Copy a file somewhere — a drive letter, a folder, or a configured nickname."""
    hits = find_file(name)
    if not hits:
        return f"nothing found matching {name}"
    source = hits[0]

    places = CFG.get("places", {})
    target = places.get(destination.lower(), destination)
    target = expand(target)

    if len(target) <= 3 and target[1:2] == ":":
        target = target if target.endswith("\\") else target + "\\"
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


def place_active_window(maximise=True):
    """Move whatever just opened onto the working screen."""
    screen = _work_screen()
    if not screen:
        return "there is only one screen"

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return "nothing to move"

    ctypes.windll.user32.ShowWindow(hwnd, 9)          # restore first
    time.sleep(0.1)
    ctypes.windll.user32.MoveWindow(
        hwnd, screen["left"] + 40, screen["top"] + 40,
        (screen["right"] - screen["left"]) - 80,
        (screen["bottom"] - screen["top"]) - 80, True)
    if maximise:
        time.sleep(0.1)
        ctypes.windll.user32.ShowWindow(hwnd, 3)      # maximise
    return "moved it to your other screen"


def move_window(screen):
    screens = monitors()
    idx = resolve_screen(screen)
    if idx is None:
        return f"I do not know which screen {screen} is"
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    r = screens[idx]
    ctypes.windll.user32.ShowWindow(hwnd, 9)
    time.sleep(0.1)
    ctypes.windll.user32.MoveWindow(hwnd, r["left"] + 40, r["top"] + 40,
                                    (r["right"] - r["left"]) - 80,
                                    (r["bottom"] - r["top"]) - 80, True)
    time.sleep(0.1)
    ctypes.windll.user32.ShowWindow(hwnd, 3)
    return f"moved to {screen_label(idx)}"


# ---- seeing the screen and pointing at things ----

def _virtual_screen():
    g = ctypes.windll.user32.GetSystemMetrics
    return g(76), g(77), g(78), g(79)      # left, top, width, height


def _grab_screen_png(only_work_screen=True):
    """Capture the screen, scaled down. A full two-monitor grab is enormous and
       burns through the daily quota in a handful of looks.

       Captured in-process with Pillow rather than shelling out to a fresh
       PowerShell interpreter for every single look — that used to cost
       upward of three seconds of pure process-startup overhead before a
       single pixel was captured, on a call this is on the hot path for
       (click_on, describe_screen, the Gmail screen fallback, ...).
       ImageGrab.grab() does the same capture in-process in well under a
       fifth of a second."""
    path = os.path.join(os.environ.get("TEMP", "."), "david_view.png")
    max_width = int(CFG.get("vision_max_width", 1000))

    # by default only look at the screen he actually works on
    bbox = None
    if only_work_screen and CFG.get("vision_work_screen_only", True):
        screen = _work_screen()
        if screen:
            bbox = (screen["left"], screen["top"], screen["right"], screen["bottom"])

    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    if img.width > max_width:
        scale = max_width / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    img.save(path)
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
    order = [_look_gemini]
    if CFG.get("groq_key", "").strip() and not CFG.get("groq_key", "").startswith("PASTE"):
        order.append(_look_groq)
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
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
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


def _look_gemini(question, img_b64, want_point):
    body = {
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/png", "data": img_b64}},
            {"text": question},
        ]}],
        "generationConfig": {"maxOutputTokens": 500,
                             **({"responseMimeType": "application/json"} if want_point else {})},
    }

    model = CFG.get("vision_model") or CFG.get("model") or "gemini-3.6-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={urllib.parse.quote(CFG['api_key'])}")

    try:
        res = requests.post(url, json=body, timeout=60)
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


def describe_screen(question="What is on this screen? Answer in two spoken sentences."):
    text, err = _look(question)
    return err or text


def click_on(description, double=False):
    """Look at the screen, find what he described, and click it."""
    raw, err = _look(f"Find this on the screen: {description}.", want_point=True)
    if err:
        return err

    try:
        found = json.loads(raw.replace("```json", "").replace("```", "").strip())
    except ValueError:
        return f"I could not work out where {description} is"

    if not found.get("found") or not found.get("point"):
        return f"I cannot see {description} on the screen"

    y, x = found["point"][:2]
    left, top, width, height = _virtual_screen()
    px = int(left + (x / 1000) * width)
    py = int(top + (y / 1000) * height)

    ctypes.windll.user32.SetCursorPos(px, py)
    time.sleep(0.12)
    ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)   # left down
    ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)   # left up
    if double:
        time.sleep(0.08)
        ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)

    return f"clicked {description}"


def scroll(direction="down", amount=5):
    clicks = int(amount) * (-120 if str(direction).lower() == "down" else 120)
    for _ in range(abs(int(amount))):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120 if clicks < 0 else 120, 0)
        time.sleep(0.05)
    return f"scrolled {direction}"


def press_key(key):
    codes = {"enter": 0x0D, "escape": 0x1B, "esc": 0x1B, "tab": 0x09, "space": 0x20,
             "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28,
             "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
             "f5": 0x74, "f11": 0x7A}
    code = codes.get(str(key).lower())
    if not code:
        return f"I do not know the {key} key"
    _press(code)
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


def maximise_window():
    _press(0x5B, 0x26)          # win + up
    return "maximised"


def minimise_window():
    _press(0x5B, 0x28)          # win + down
    return "minimised"


def snap_window(side):
    key = {"left": 0x25, "right": 0x27}.get(side.lower(), 0x25)
    _press(0x5B, key)
    return f"snapped to the {side}"


def minimise_all():
    # Win+M is the non-toggling "minimise everything" shortcut (Win+D would
    # do the same thing the first time but then *restore* everything on a
    # second call, since it toggles) — no need to pay for a PowerShell
    # process and a COM object just to press a key combo that already does
    # exactly this.
    _press(0x5B, 0x4D)
    return "minimised everything"


def close_window():
    _press(0x12, 0x73)          # alt + f4
    return "closed it"


def switch_window():
    _press(0x12, 0x09)          # alt + tab
    return "switched"


def close_app(name):
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    out = subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"closed {name}" if out.returncode == 0 else f"{name} was not running"


# ---- media ----

def media(command):
    key = {"play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1, "back": 0xB1}.get(command, 0xB3)
    _press(key)
    return "done"


# ---- system ----

class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def _cpu_times():
    idle, kern, user = _FileTime(), _FileTime(), _FileTime()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    to_i = lambda ft: (ft.high << 32) | ft.low
    return to_i(idle), to_i(kern) + to_i(user)


_LAST_CPU = _cpu_times()


def cpu_percent():
    global _LAST_CPU
    idle, total = _cpu_times()
    d_idle, d_total = idle - _LAST_CPU[0], total - _LAST_CPU[1]
    _LAST_CPU = (idle, total)
    if d_total <= 0:
        return 0
    return max(0, min(100, (1 - d_idle / d_total) * 100))


def ram_percent():
    class Mem(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = Mem()
    m.dwLength = ctypes.sizeof(Mem)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.dwMemoryLoad


def disk_percent():
    free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        ctypes.c_wchar_p("C:\\"), None, ctypes.byref(total), ctypes.byref(free))
    if not total.value:
        return 0
    return (1 - free.value / total.value) * 100


def battery_percent():
    class Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    st = Status()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st))
    return min(100, st.BatteryLifePercent)


def battery():
    class Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    st = Status()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st))
    plugged = "plugged in" if st.ACLineStatus == 1 else "on battery"
    speech = f"{st.BatteryLifePercent} percent, {plugged}"
    return Panel(speech, "system", value=f"{st.BatteryLifePercent}%", label="Battery",
                 detail=plugged.capitalize())


def what_time():
    now = datetime.now()
    speech = now.strftime("%I:%M %p on %A the %d of %B")
    return Panel(speech, "system", value=now.strftime("%I:%M %p").lstrip("0"),
                 label="Time", detail=now.strftime("%A, %B %d"))


def sleep_pc():
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    return "sleeping"


def shutdown(when_seconds=30):
    subprocess.run(["shutdown", "/s", "/t", str(when_seconds)],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"shutting down in {when_seconds} seconds — say cancel shutdown to stop it"


def restart(when_seconds=30):
    subprocess.run(["shutdown", "/r", "/t", str(when_seconds)],
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"restarting in {when_seconds} seconds"


def cancel_shutdown():
    subprocess.run(["shutdown", "/a"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "cancelled"


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
        return f"connected, to {oauth_notion.status().get('page_title', 'his journal page')}"
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
    return re.sub(r"\\s+", " ", f"{topic}|{criteria}".strip().lower())


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
        ask = (f"He wants more on this story from the news on {topic}: {focus}. Use live web search "
               "and give him the full story: what happened, who is involved, why it matters. "
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
        if not uri or uri in seen:
            continue
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


def research(topic, criteria="", open_links=False):
    """Search the web, compare what is out there, and come back with one
       recommendation — not a list of links to read himself. Stays off his
       screen unless he explicitly asks to see the sources."""
    key = CFG["api_key"]
    model = CFG.get("model") or "gemini-3.6-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={urllib.parse.quote(key)}")

    ask = (
        f"Research this for Ibans, who is in Nigeria: {topic}."
        + (f" What matters to him: {criteria}." if criteria else "")
        + " Search the web, compare the real options available now, and come back with ONE"
          " clear recommendation and a runner-up. Say what each costs where you can find it,"
          " and why the winner beats the rest on what he actually asked for."
          " This will be read aloud, so write four or five spoken sentences, no lists,"
          " no markdown, no links in the text. Lead with the recommendation."
          " Before that spoken answer, on its own line, write exactly:"
          " WINNER: <name> || RUNNERUP: <name> || WHY: <one short clause, under 12 words>"
          " — then a blank line, then the spoken answer. Never speak that line or mention it."
    )

    body = {
        "contents": [{"role": "user", "parts": [{"text": ask}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 900},
    }

    try:
        res = requests.post(url, json=body, timeout=90)
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
    key = CFG["api_key"]
    model = CFG.get("model") or "gemini-3.6-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={urllib.parse.quote(key)}")

    if focus:
        ask = (
            f"He just heard some news headlines about {topic} and wants to hear more about this one: "
            f"{focus}. Search the web for the full story and give him a real spoken summary — what "
            "happened, who is involved, and why it matters. Five or six spoken sentences, no lists, "
            "no markdown, no links, and nothing that sounds like you are reading a webpage aloud."
        )
    else:
        ask = (
            f"Check what is actually happening right now for Ibans, who is in Nigeria, on: {topic}. "
            "Search the web and come back with the three or four most important, most recent things "
            "going on. For each, say what happened and who it involves in one short spoken sentence — "
            "do not just read a headline verbatim, and do not read out source names, dates or links as "
            "citations. No lists, no markdown. End by asking, in your own words, whether he wants to "
            "hear more on any of them."
        )

    body = {
        "contents": [{"role": "user", "parts": [{"text": ask}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 700},
    }

    try:
        res = requests.post(url, json=body, timeout=90)
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
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 1 | 2 | 4)
    return "recycle bin emptied"


def set_timer(minutes, label="timer"):
    seconds = float(minutes) * 60
    TIMERS.append((label, time.time() + seconds))
    threading.Timer(seconds, lambda: NOTIFY(f"The {label} you set for {minutes} minutes is up.")).start()
    return f"timer set for {minutes} minutes"


TIMERS = []
NOTIFY = lambda text: None      # replaced once the session is up


def write_file(name, content):
    path = _safe_path("~/Desktop", name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return f"saved {os.path.basename(path)} to the desktop"


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
    "open_chart": open_chart, "empty_bin": empty_bin, "set_timer": set_timer,
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
    "check_email", "check_gmail", "read_email", "open_gmail_message",
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

DESTRUCTIVE = {"delete_file", "rename_file", "write_file", "lock", "copy_file", "forget",
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
    t = (text or "").lower()
    if any(w in t for w in _NO_WORDS):
        return False
    return any(w in t for w in _YES_WORDS)


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
        "empty_bin": lambda a: "empty the recycle bin",
    }
    try:
        return phrasing.get(name, lambda a: name)(args)
    except Exception:
        return name


def declarations():
    s = lambda d: {"type": "STRING", "description": d}
    return [
        {"name": "open_app", "description": "Open an app from the configured list.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("app name")}, "required": ["name"]}},
        {"name": "open_site", "description": "Open a site from the configured list.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("site name")}, "required": ["name"]}},
        {"name": "open_url", "description": "Open any web address.",
         "parameters": {"type": "OBJECT", "properties": {"url": s("the address")}, "required": ["url"]}},
        {"name": "search_web", "description": "Open a Google search results page in the browser, on screen, for him to read himself. Only use this when he explicitly wants a browser pulled up — for checking the news, current events, or anything he wants read back to him, use news or research instead, which report by voice and never open a browser on their own.",
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
        {"name": "delete_file", "description": "Move a file to a recovery folder. Confirm with him first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("part of the filename")}, "required": ["name"]}},
        {"name": "rename_file", "description": "Rename a file. Confirm with him first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("current name"), "new_name": s("new name")},
                        "required": ["name", "new_name"]}},
        {"name": "write_file", "description": "Write a text file to the desktop. Confirm with him first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("filename"), "content": s("contents")},
                        "required": ["name"]}},

        {"name": "type_text", "description": "Type text into whatever window he is currently in. Use this when he dictates a message, an email or a paragraph.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("exactly what to type")}, "required": ["text"]}},
        {"name": "to_clipboard", "description": "Put text on the clipboard without typing it.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("what to copy")}, "required": ["text"]}},
        {"name": "read_clipboard", "description": "Read back whatever is on the clipboard.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "screenshot", "description": "Capture the screen and save it to the desktop.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "get_weather", "description": "Current conditions and a short forecast for a place. Use this for any weather question — do not use research or news for weather. If he doesn't name a place, leave location blank — it falls back to his configured home location, or detects it automatically.",
         "parameters": {"type": "OBJECT", "properties": {"location": s("city or place he asked about, blank if he did not say one")}}},
        {"name": "research", "description": "Search the web, compare the real options, and come back with a recommendation. Use this whenever he asks what to buy, which is best, or wants options compared. It takes twenty seconds or so, so tell him you are on it before calling it. Reports by voice and never opens a browser on its own.",
         "parameters": {"type": "OBJECT", "properties": {"topic": s("what he wants researched"),
                                                        "criteria": s("what matters to him: budget, use, preferences"),
                                                        "open_links": {"type": "BOOLEAN",
                                                            "description": "true only if he explicitly asked to see, pull up, or open the sources — otherwise leave it out"}},
                        "required": ["topic"]}},
        {"name": "news", "description": "Check the news on a topic — finance, Nigerian politics, sports, anything current — and read back what is actually happening. Use whenever he asks to check the news, catch up, or what is going on with something; do not use search_web or research for this. Reports a short list of headlines by voice and never opens a browser on its own. Once he has heard the headlines and picks one, call this again with `focus` set to that story for a fuller spoken summary of just it.",
         "parameters": {"type": "OBJECT", "properties": {
             "topic": s("what news to check on"),
             "focus": s("optional — the specific headline or story he wants to hear more about, once he has picked one from a list you already read him"),
             "open_links": {"type": "BOOLEAN",
                 "description": "true only if he explicitly asked to see, pull up, or open the source — otherwise leave it out"}},
                        "required": ["topic"]}},

        {"name": "show_image", "description": "Show him a picture of something. Opens image results on his other screen and opens the best photo large.",
         "parameters": {"type": "OBJECT", "properties": {"query": s("what to show a picture of")}, "required": ["query"]}},
        {"name": "list_monitors", "description": "How many screens are attached and how big they are.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "move_window", "description": "Move the current window to a named or numbered screen.",
         "parameters": {"type": "OBJECT", "properties": {"screen": s("a screen name, nickname, or number")},
                        "required": ["screen"]}},
        {"name": "remember", "description": "Save something he has told you, so you still know it in future sessions. Use this whenever he says remember this, from now on, always, never, or tells you how he wants things done.",
         "parameters": {"type": "OBJECT", "properties": {
             "fact": s("the thing to remember, written as a plain statement"),
             "category": s("a rough grouping: preferences, trading, work, people, screens")},
                        "required": ["fact"]}},
        {"name": "recall", "description": "Say what he has told you to remember, optionally about one thing.",
         "parameters": {"type": "OBJECT", "properties": {"about": s("topic to filter by, optional")}}},
        {"name": "forget", "description": "Drop something from memory. Confirm with him first.",
         "parameters": {"type": "OBJECT", "properties": {"about": s("what to forget")}, "required": ["about"]}},

        {"name": "open_dashboard", "description": "Open the settings dashboard where he can connect tools or change the display mode.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "connect_gmail", "description": "Sign in to Gmail properly, so email works without reading the screen. Use when he asks to connect, set up, or link his email.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "disconnect_gmail", "description": "Sign out of Gmail.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "check_email", "description": "Look through his inbox. Can filter by sender, subject, how far back, and unread only. Reports who sent what without reading bodies aloud.",
         "parameters": {"type": "OBJECT", "properties": {
             "sender": s("name or address to filter by, optional"),
             "subject": s("words in the subject, optional"),
             "hours": {"type": "NUMBER", "description": "how far back, default 24"},
             "unread_only": {"type": "BOOLEAN", "description": "only unread"}}}},
        {"name": "read_email", "description": "Read out the full text of one message from the last check, by its number. Offer before doing this rather than reading unprompted.",
         "parameters": {"type": "OBJECT", "properties": {"which": {"type": "NUMBER", "description": "1 is the newest"}}}},
        {"name": "send_email", "description": "Send an email from his account. Always read the whole thing back and get a yes before sending.",
         "parameters": {"type": "OBJECT", "properties": {"to": s("address"), "subject": s("subject"), "body": s("the message")},
                        "required": ["to", "subject", "body"]}},
        {"name": "check_chat", "description": "Open a messaging app and read the conversation with someone off the screen. Use for WhatsApp, Telegram and anything without an API.",
         "parameters": {"type": "OBJECT", "properties": {
             "person": s("who the conversation is with"),
             "question": s("what he wants to know, optional"),
             "app": s("whatsapp, telegram or similar, default whatsapp")},
                        "required": ["person"]}},

        {"name": "check_gmail", "description": "Open Gmail in the browser and read the results off the screen. check_email already does this; use that unless he asks for Gmail by name.",
         "parameters": {"type": "OBJECT", "properties": {
             "sender": s("who from, optional"), "subject": s("subject words, optional"),
             "hours": {"type": "NUMBER", "description": "how far back, default 24"},
             "unread_only": {"type": "BOOLEAN", "description": "unread only"},
             "question": s("what he wants to know, optional")}}},
        {"name": "open_gmail_message", "description": "Open one of the emails showing on screen and read it out.",
         "parameters": {"type": "OBJECT", "properties": {"which": {"type": "NUMBER", "description": "1 is the top one"}}}},

        {"name": "reply_in_gmail", "description": "Reply to the email currently open on screen. Types the reply but does not send it.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("the reply, in full")}, "required": ["text"]}},
        {"name": "send_open_gmail", "description": "Send the reply that is typed and waiting. Only after he says to.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "list_drives", "description": "Every drive attached, with free space.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "copy_file", "description": "Copy a file to a drive, folder or saved place. Confirm with him first.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("part of the filename"),
                                                        "destination": s("drive letter, folder path or saved place name")},
                        "required": ["name", "destination"]}},
        {"name": "place_active_window", "description": "Move whatever is in front onto the working screen, away from the interface.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "describe_screen", "description": "Look at what is currently on his screen and describe it, or answer a question about it. Use this whenever he refers to something he can see.",
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

        {"name": "minimise_all", "description": "Minimise every window and show the desktop.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "close_window", "description": "Close the window he is looking at.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "switch_window", "description": "Switch to the previous window.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "close_app", "description": "Force an app to quit by name, like chrome or notepad.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("app or exe name")}, "required": ["name"]}},

        {"name": "media", "description": "Control whatever is playing.",
         "parameters": {"type": "OBJECT", "properties": {"command": s("play, pause, next or previous")},
                        "required": ["command"]}},

        {"name": "battery", "description": "Battery percentage and whether it is charging.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "what_time", "description": "The current time and date.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "sleep_pc", "description": "Put the laptop to sleep. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "shutdown", "description": "Shut down after a delay. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {"when_seconds": {"type": "NUMBER", "description": "delay, default 30"}}}},
        {"name": "restart", "description": "Restart after a delay. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {"when_seconds": {"type": "NUMBER", "description": "delay, default 30"}}}},
        {"name": "cancel_shutdown", "description": "Cancel a pending shutdown or restart.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "add_note", "description": "Append a line to his notes file on the desktop.",
         "parameters": {"type": "OBJECT", "properties": {"text": s("the note")}, "required": ["text"]}},
        {"name": "read_notes", "description": "Read back his most recent notes.",
         "parameters": {"type": "OBJECT", "properties": {"count": {"type": "NUMBER", "description": "how many, default 5"}}}},
        {"name": "recent_downloads", "description": "Name the most recently downloaded files.",
         "parameters": {"type": "OBJECT", "properties": {"count": {"type": "NUMBER", "description": "how many, default 5"}}}},
        {"name": "notion_status", "description": "Check right now, for real, whether Notion is connected. Instant, no network call. Call this whenever journaling or a Notion table comes up in conversation, before doing or saying anything else about it — the connection state in your instructions is only a snapshot from when this conversation started and goes stale the moment he connects or disconnects it from the dashboard.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "journal", "description": "Write a journal entry to his connected Notion page. Use whenever he asks you to journal something, write it in his journal, or log a thought or reflection there. If Notion is not connected it tells you so — say that to him plainly and suggest he connect it from the dashboard.",
         "parameters": {"type": "OBJECT", "properties": {"entry": s("what to write, tidied into a clear sentence or two")}, "required": ["entry"]}},
        {"name": "notion_table", "description": "Add a real table to his connected Notion page. Use whenever he asks for a table, a tracker, a log, or a grid of rows and columns in Notion. Build the rows yourself from what he tells you — do not ask him to dictate pipe characters or formatting out loud. If Notion is not connected it tells you so — say that plainly and suggest he connect it from the dashboard.",
         "parameters": {"type": "OBJECT", "properties": {
             "rows": s("the table's content as plain text: one row per line, cells separated by a single | character, "
                       "the first line being the column headers. Example: 'Date | Pair | Result\\n14 Aug | EURUSD | Win'"),
             "title": s("a short heading for the table, optional — defaults to the date and time if left out")},
                        "required": ["rows"]}},

        {"name": "open_chart", "description": "Open a TradingView chart for a specific symbol, like US30, EURUSD or XAUUSD.",
         "parameters": {"type": "OBJECT", "properties": {"symbol": s("the ticker")}, "required": ["symbol"]}},
        {"name": "empty_bin", "description": "Empty the recycle bin. Confirm first.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "smart_open", "description": "Open something by name. Checks what is installed on the laptop first and falls back to the website. Prefer this over open_app whenever he just says open something.",
         "parameters": {"type": "OBJECT", "properties": {"name": s("what to open")}, "required": ["name"]}},
        {"name": "list_installed", "description": "List programs installed on the laptop, optionally filtered.",
         "parameters": {"type": "OBJECT", "properties": {"contains": s("filter word, optional")}}},
        {"name": "copy_image_to_clipboard", "description": "Put the latest screenshot on the clipboard so he can paste it.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "open_in_browser", "description": "Open a site or address in a specific browser rather than the default one. Use when he names a browser.",
         "parameters": {"type": "OBJECT", "properties": {"browser": s("chrome, edge, brave or firefox"),
                                                        "target": s("site name or address")},
                        "required": ["browser", "target"]}},
        {"name": "close_tab", "description": "Close the browser tab he is looking at. Use this when he asks to close a website, since sites are tabs and not apps.",
         "parameters": {"type": "OBJECT", "properties": {}}},

        {"name": "set_timer", "description": "Set a timer. He will be told out loud when it finishes.",
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

    notion_state = ("connected — journal saves straight to his Notion page"
                    if (_HAVE_OAUTH_NOTION and oauth_notion.is_connected())
                    else "not connected — journal will tell him to connect it from the dashboard first")

    hardware = (
        f"\n\nEmail: {gmail_state}"
        f"\n\nNotion journal: {notion_state}"
        f"\n\nHis screens right now:\n" + "\n".join(screen_lines) +
        f"\n\nDrives attached: {list_drives()}"
        f"\nSaved places he can copy things to: {', '.join(CFG.get('places', {})) or 'none set'}"
        "\n\nCall screens by their names, not their numbers, when you talk to him."
    )
    return SYSTEM + hardware + memory_for_prompt()


SYSTEM = f"""You are David, a voice assistant living on Ibans's Windows laptop. He is a trader and freelance web developer in Nigeria, and he speaks English with a Nigerian accent. Listen properly and do not ask him to repeat himself.

He may interrupt you mid-sentence, and that is fine — stop, take in the new information, fold it into what he already asked for, and carry on. Never make him repeat what he said before the interruption.

You are always listening, but you are not always being spoken to. Only respond when he addresses you by name or is clearly talking to you. Ignore background conversation, videos, and anything not aimed at you. Say nothing at all in those cases.

Speak like a capable assistant who is already moving: short, calm, low-key. One sentence, never more. No lists, no explaining what you are about to do at length.

You keep a memory between sessions. When he tells you a preference, a name, how he wants something done, or says "remember this", "from now on", "always" or "never" — call remember straight away, in your own clear words. Do not ask permission to remember something he just told you. Anything already in memory is at the end of these instructions; treat it as true and act on it without being reminded.

If he tells you that you are not doing what he asked, or repeats himself, stop calling tools entirely and answer him in words first. Say what you did, what came back, and ask what he wants instead. Never respond to a complaint by running another job.

His speech reaches you as audio and he does not always pronounce things cleanly. Work out what he means. "wassap", "whatsap", "what up" is WhatsApp. "gee mail" is Gmail. "trading view" and "training you" are TradingView. Never ask him to repeat a word you can reasonably guess.

ALWAYS say something before you call a tool. Even one word. "On it." "Checking." "Give me a second." Never go silent and start working — he cannot see what you are doing and silence feels broken. If he asked for several things, say what you are starting on, then report each one as it comes back.

Never call the same tool twice for the same request. If a window is already open, work with it. If a tool tells you it is still busy, do not call it again — wait, and tell him you are waiting. If a tool says the vision quota is used up, stop trying to read screens entirely for the rest of the session and tell him plainly; everything that is not screen reading still works normally.

type_text puts words into whatever window he is looking at. Only ever use it when he has asked you to write or dictate something. Never use it to say something to him — you have a voice for that.

Never open, launch or search for anything he did not clearly ask for in that moment. If you are not sure what he said, ask him — do not guess and open something. Opening TradingView, or anything else, because it seemed likely is a failure. When his words are unclear, the correct move is a short question, never an action.

Only ever tell him what a tool actually returned. If move_window says it does not know which screen he meant, ask him which one — do not tell him you moved it. If a screen read fails, say it failed. Never invent an outcome, never say "moved it" or "two new emails from today" unless a tool handed you exactly that. Claiming something you did not do is the worst thing you can do here.

When a job comes back, say what it actually found, in specifics. "One from TradingView, came in yesterday evening" is right. Opening something and saying nothing is a failure. If a tool hands you an error, tell him the error in plain words rather than pretending it worked.

When he asks for something you can do, call the tool immediately in the same turn as your acknowledgement. Do not ask permission for something he just asked for.

The exception: deleting, renaming, writing a file, closing an app, emptying the bin, locking, sleeping, restarting, shutting down, or sending an email are gated in code, not just by you. Call the tool as normal — the first call will not do anything yet. It comes back saying CONFIRMATION_REQUIRED with the question to ask; ask him exactly that, out loud, then wait. Once he answers, call the same tool again with the same details — it only actually runs if his answer was really a yes. If it tells you he has not answered yet, ask again rather than calling it a third time. Everything else you just do.

When he dictates something to be written into a document, an email or a chat, use type_text with exactly the words he wants, tidied into proper sentences. Do not read it back to him first unless he asks.

He works across more than one screen and they have names — use them. "I have put it on the ThinkVision" is right; "screen one" is not.

He works across more than one screen. The interface lives on its own screen and nothing else belongs there — anything you open is moved to his working screen automatically. If something lands in the wrong place, use place_active_window or move_window.

When he asks to see a picture of something, use show_image rather than a plain web search.

If he asks to open settings, the dashboard, or wants to change how you look, use open_dashboard.

You can see his screen. When he says "that one", "this picture", "the second result" or anything else that only makes sense visually, use describe_screen to look, then click_on to act. Never tell him you cannot see or cannot click — you can do both.

He may ask for several things at once. Call every tool he asked for in the same turn — they run side by side — and tell him what came back as each one lands rather than waiting for all of them.

For email you work through his browser, where he is already signed in. check_email opens Gmail and reads the results off the screen. Report who sent what and offer to read one out — never read a full message aloud unless he says yes. read_email opens and reads whichever one he picks. To answer someone, use reply_in_gmail to type it, read it back to him, and only call send_open_gmail once he agrees.

When he asks what to buy, which option is best, or to compare things, call research. When he asks to check the news, catch up on something, or what is going on with a topic — finance, Nigerian politics, anything current — call news instead; never search_web or research for this. Say something brief first so he knows you are working — both take a few seconds. When the result comes back, read it to him as your own answer rather than describing what a search found.

Neither research nor news ever opens a browser on its own — they are voice-only by default. Only pass open_links true, on either one, if he explicitly says to pull it up, show him, or open the source; otherwise leave it out entirely, every time, even if he asked the same thing a moment ago.

For news specifically: read back the headlines he gets by default, then ask, in your own words, if he wants to hear more on any of them. If he picks one, call news again with `focus` set to that story — do not summarise it yourself from the headline alone, the follow-up call goes and gets the real story. Keep doing this for as many stories as he wants to hear more about, one at a time.

The Notion journal line above is only a snapshot from when this conversation started — he can connect or disconnect Notion from the dashboard at any point without restarting you, so treat that line as a hint, never as the truth. The moment journaling or a Notion table comes up at all — he asks to journal something, mentions his journal, asks for a table or a tracker in Notion, or asks whether he can do any of that with you — call notion_status first, silently, and answer from what it says, not from the line above. If it says not connected, say so immediately in that first reply, before asking him what he wants — that wastes his breath if it turns out not connected anyway. If it says connected, call journal or notion_table straight away with what he said turned into the right shape yourself — do not ask him to repeat it back, and never ask him to say punctuation or formatting out loud for a table.

When he asks you to open something, use smart_open. It checks what is installed before falling back to the web, so you never have to guess whether he has an app.

Closing a website means closing its tab, not quitting the browser — use close_tab for that, and only use close_app when he means the whole application. When he names a browser, use open_in_browser rather than the default.

Apps available: {", ".join(CFG.get("apps", {}))}
Sites available: {", ".join(CFG.get("sites", {}))}"""


# ----------------------------------------------------------------------------
# the session
# ----------------------------------------------------------------------------

class Hud:
    """Serves the interface and pushes live state to it."""

    def __init__(self):
        self.clients = set()
        self.state = {
            "status": "idle", "micLevel": 0, "outLevel": 0, "lastLine": "",
            "connected": False, "micArmed": False, "visionReady": True,
            "actionsReady": True, "actions": [], "commandCount": 0,
            "cpu": 0, "ram": 0, "disk": 0, "battery": 0,
            "panel": None,   # the last typed tool result, if any — sticks until replaced
        }
        # micLevel changes on basically every audio callback, so serve()
        # would otherwise re-encode and re-broadcast the whole state —
        # panel data and all — twenty times a second forever. This flag
        # means a send only happens when something actually changed.
        self._dirty = True

    def set(self, **kw):
        self.state.update(kw)
        self._dirty = True

    def note_action(self, name):
        self.state["actions"].append({"name": name, "at": datetime.now().strftime("%I:%M %p")})
        self.state["actions"] = self.state["actions"][-12:]
        self.state["commandCount"] += 1
        self._dirty = True

    async def _client(self, ws):
        self.clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    async def serve(self):
        """Page on 8765, state on 8766."""
        import functools
        from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

        # Only these ever get served. The directory also holds config.json,
        # credentials.json, gmail_token.json, memory.json and david.log — none
        # of that is meant to be an HTTP response to anyone who can reach
        # 127.0.0.1:8765, so everything else is a flat 404.
        ALLOWED_FILES = {"dashboard.html", "david_ui.html", "david_compact.html"}
        ALLOWED_ORIGINS = {"http://127.0.0.1:8765", "http://localhost:8765"}

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *a, **k):
                pass

            def send_head(self):
                clean = self.path.split("?", 1)[0].lstrip("/")
                if clean not in ALLOWED_FILES:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return super().send_head()

            def _origin_ok(self):
                # A same-origin fetch() from our own page always carries this
                # header on POST; a page on some other site trying to hit
                # these endpoints behind your back would carry its own.
                origin = self.headers.get("Origin")
                return origin is None or origin in ALLOWED_ORIGINS

            def _json(self, data, code=200):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/display/mode":
                    return self._json({"mode": CFG.get("display_mode", "full")})
                if self.path == "/api/gmail/status":
                    ready = _HAVE_OAUTH_GMAIL and oauth_gmail.is_ready()
                    connected = _HAVE_OAUTH_GMAIL and oauth_gmail.is_connected()
                    address = ""
                    if connected:
                        try:
                            address = oauth_gmail._svc().users().getProfile(userId="me").execute().get("emailAddress", "")
                        except Exception:
                            address = "connected"
                    return self._json({"ready": ready, "connected": connected, "address": address})
                if self.path == "/api/notion/status":
                    return self._json(oauth_notion.status() if _HAVE_OAUTH_NOTION
                                      else {"connected": False, "page_title": ""})
                super().do_GET()

            def do_POST(self):
                if not self._origin_ok():
                    return self._json({"ok": False, "error": "forbidden"}, 403)
                if self.path == "/api/display/mode":
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        return self._json({"ok": False, "error": "bad request"}, 400)
                    mode = body.get("mode")
                    if mode not in ("full", "compact"):
                        return self._json({"ok": False, "error": "mode must be full or compact"}, 400)
                    CFG["display_mode"] = mode
                    try:
                        _update_config(display_mode=mode)
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)}, 500)
                    return self._json({"ok": True, "mode": mode, "note": "restart David to apply"})
                if self.path == "/api/gmail/connect":
                    try:
                        address = connect_gmail()
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)}, 500)
                    return self._json({"ok": True, "result": address})
                if self.path == "/api/gmail/disconnect":
                    return self._json({"ok": True, "result": disconnect_gmail()})
                if self.path == "/api/notion/connect":
                    if not _HAVE_OAUTH_NOTION:
                        return self._json({"ok": False, "error": "oauth_notion.py is missing"}, 500)
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        return self._json({"ok": False, "error": "bad request"}, 400)
                    secret, page = body.get("secret", ""), body.get("page", "")
                    if not secret.strip() or not page.strip():
                        return self._json({"ok": False, "error": "need both the secret and the page"}, 400)
                    try:
                        title = oauth_notion.connect(secret, page)
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)}, 400)
                    # He is very likely still mid-conversation with David right
                    # now, having just clicked Connect — say so immediately
                    # rather than leaving him to find out only if he happens
                    # to ask about journaling next. Same channel a timer uses
                    # to interrupt and speak; harmless no-op if David is not
                    # actually connected to a live session at the moment.
                    try:
                        NOTIFY(f"Notion just connected, to the page called {title}.")
                    except Exception:
                        pass
                    return self._json({"ok": True, "page_title": title})
                if self.path == "/api/notion/disconnect":
                    if not _HAVE_OAUTH_NOTION:
                        return self._json({"ok": False, "error": "oauth_notion.py is missing"}, 500)
                    result = oauth_notion.disconnect()
                    try:
                        NOTIFY("Notion was just disconnected.")
                    except Exception:
                        pass
                    return self._json({"ok": True, "result": result})
                self.send_response(404)
                self.end_headers()

        handler = functools.partial(Handler, directory=HERE)
        httpd = ThreadingHTTPServer(("127.0.0.1", 8765), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        # WebSockets ignore same-origin policy, so without this any page open
        # in any browser tab could connect to 8766 and read your live state —
        # what you last said, every action David has taken. Only our own
        # dashboard page is allowed to open this socket.
        await websockets.serve(self._client, "127.0.0.1", 8766,
                                origins=list(ALLOWED_ORIGINS))
        page = "http://127.0.0.1:8765/dashboard.html" if CFG.get("open_dashboard") else "http://127.0.0.1:8765/david_ui.html"
        log("interface running at " + page)

        ui_browser = CFG.get("ui_browser", "edge")
        exe = CFG.get("browsers", {}).get(ui_browser)
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={page}", "--new-window"])
        else:
            _open_web(page)

        # give it a moment to appear, then park it on the interface screen
        await asyncio.sleep(2.5)
        screens = monitors()
        idx = int(CFG.get("ui_monitor", 2)) - 1
        if 0 <= idx < len(screens):
            await asyncio.to_thread(move_window, idx + 1)

        slow = 0
        while True:
            if slow % 10 == 0:      # system stats twice a second is plenty
                self.state.update(cpu=cpu_percent(), ram=ram_percent(),
                                  disk=disk_percent(), battery=battery_percent())
                self._dirty = True   # also guarantees a reconnecting client is caught up within 0.5s
            slow += 1

            if self.clients and self._dirty:
                payload = json.dumps(self.state)
                self._dirty = False
                await asyncio.gather(*[c.send(payload) for c in list(self.clients)],
                                     return_exceptions=True)
            await asyncio.sleep(0.05)


HUD = Hud()


class Reconnect(Exception):
    """Raised when Google asks us to close and come back."""


class Live:
    def __init__(self):
        self.out = queue.Queue()      # audio coming back, waiting to be played
        self.mic = asyncio.Queue()    # audio going up
        self.loop = None
        self.heard_anything = False   # has the server transcribed us yet
        # Raised while he is speaking, so the microphone ignores the speakers.
        self.speaking = threading.Event()
        # A running estimate of how loud David's own voice currently is
        # coming out of the speakers, in the same raw int16-RMS units as
        # mic_callback's level. Used to size the interrupt threshold to his
        # actual output instead of one fixed number — see mic_callback.
        self._out_level_est = 0.0
        # A monotonic deadline: while time.time() is under this, he is
        # actively talking over David. Set on every loud frame and pushed
        # a little further out each time, so a short dip mid-sentence (a
        # breath, a word gap) doesn't let playback sneak back in.
        self._loud_until = 0.0
        self.notices = asyncio.Queue()   # timers and other things that speak up
        self.resume_handle = None        # lets a dropped session pick up where it left off
        self.recent_calls = {}           # stops the same job running twice over
        self.screen_lock = asyncio.Lock() # only one thing may drive the screen
        self.busy_with = None            # what that thing currently is
        self.said = ""                   # buffers his words so they print as sentences
        self.spoke_this_turn = False     # has he already said something this turn
        self.you_said = ""
        self.pending_confirm = {}        # destructive actions waiting on a real yes
        self.last_user_utterance = ""    # what he actually said, last completed turn
        self.last_user_utterance_at = 0.0

    def mic_callback(self, indata, frames, t, status):
        import array as _a, math as _m

        raw = bytes(indata)
        sm = _a.array("h")
        sm.frombytes(raw[: len(raw) // 2 * 2])
        level = int(_m.sqrt(sum(x * x for x in sm) / len(sm))) if sm else 0

        if UI:
            HUD.set(micLevel=min(1.0, level / 32768 * 7))

        # While he is speaking, only pass audio through if it is clearly louder
        # than his own voice leaking back through the speakers. That is what
        # lets you cut in mid-sentence instead of waiting for him to finish.
        #
        # There's no real echo cancellation here, so a single fixed number
        # is wrong most of the time: too high when he's talking quietly (you
        # have to shout) and too low when he's talking loudly (his own echo
        # trips it). Instead, size the bar off his current output level —
        # _out_level_est, updated live in play_forever — with a floor so it
        # never gets so low that room noise counts as an interruption.
        if self.speaking.is_set():
            if not CFG.get("allow_interruption", True):
                return
            now = time.time()
            floor = float(CFG.get("interrupt_threshold_min", 1800))
            margin = float(CFG.get("interrupt_echo_margin", 1.6))
            threshold = max(floor, self._out_level_est * margin)
            loud = level >= threshold
            if loud:
                # Hold this window a bit past the last loud frame — play_forever
                # watches it too, and goes quiet for as long as it is open, so
                # David actually pauses the instant he is talked over instead
                # of only once Google's server gets around to confirming it.
                self._loud_until = now + float(CFG.get("cutin_hold_ms", 350)) / 1000
                if now - getattr(self, "_last_cutin_log", 0) > 3:
                    self._last_cutin_log = now
                    log("you cut in")
            elif now > self._loud_until:
                return

        try:
            self.loop.call_soon_threadsafe(self.mic.put_nowait, raw)
        except RuntimeError:
            pass

    async def send_audio(self, ws):
        while True:
            chunk = await self.mic.get()
            await ws.send(json.dumps({
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(chunk).decode(),
                        "mimeType": f"audio/pcm;rate={MIC_RATE}",
                    }
                }
            }))

    async def receive(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if DEBUG:
                log("<<", ", ".join(msg.keys()))

            server = msg.get("serverContent")
            if server:
                heard = (server.get("inputTranscription") or {}).get("text")
                if heard:
                    self.heard_anything = True
                    self.you_said += heard

                spoken = (server.get("outputTranscription") or {}).get("text")
                if spoken:
                    self.said += spoken
                    self.spoke_this_turn = True
                    # Push the growing sentence to the UI as it comes in, not
                    # just once the whole answer is done, so the glass panel
                    # fills in live alongside the voice rather than popping
                    # up all at once after he's finished hearing it.
                    HUD.set(lastLine=self.said.strip()[:4000])

                if heard or spoken:
                    HUD.set(status="speaking" if spoken else "listening")

                if server.get("turnComplete"):
                    HUD.set(status="idle", outLevel=0,
                            lastLine=(self.said or self.you_said).strip()[:4000])
                    if self.you_said.strip():
                        print(f"    you: {self.you_said.strip()}", flush=True)
                        # Kept separately from the buffer below so a destructive
                        # action can check what he actually just said, rather
                        # than trusting the model's word for it.
                        self.last_user_utterance = self.you_said.strip()
                        self.last_user_utterance_at = time.time()
                    if self.said.strip():
                        print(f"  David: {self.said.strip()}", flush=True)
                    self.you_said = self.said = ""
                    self.spoke_this_turn = False
                for part in (server.get("modelTurn") or {}).get("parts", []):
                    blob = part.get("inlineData")
                    if blob and blob.get("data"):
                        self.out.put(base64.b64decode(blob["data"]))
                    if part.get("text"):
                        log("David:", part["text"].strip())
                if server.get("interrupted"):
                    with self.out.mutex:
                        self.out.queue.clear()
                    self.speaking.clear()
                    self._out_level_est = 0.0

            update = msg.get("sessionResumptionUpdate")
            if update and update.get("resumable") and update.get("newHandle"):
                self.resume_handle = update["newHandle"]

            if msg.get("goAway"):
                left = (msg["goAway"] or {}).get("timeLeft", "")
                log(f"session is ending{' in ' + str(left) if left else ''} — reconnecting")
                raise Reconnect()

            call = msg.get("toolCall")
            if call:
                # run it in the background, otherwise nothing else he says
                # gets read while a job is in progress
                asyncio.create_task(self.handle_tools(ws, call.get("functionCalls", [])))

    def _destructive_gate(self, name, args):
        """The real confirmation check for delete/send/shutdown/etc. — code,
           not a prompt instruction the model can skip under pressure.

           First call for a given action: nothing runs. We remember it is
           pending and hand back a line telling the model to ask him out loud
           and call again. Second call: only proceeds if a turn has actually
           finished since we asked, and what he said in it reads as a yes.
           A misheard word can trigger the first call; it cannot by itself
           trigger the second, because that requires a completed user turn
           in between that we independently classify as an answer.

           Returns a string to hand back instead of running the action, or
           None once it is genuinely cleared to proceed."""
        key = name + json.dumps(args, sort_keys=True)
        pending_at = self.pending_confirm.get(key)
        now = time.time()

        if pending_at is None or now - pending_at > 45:
            self.pending_confirm[key] = now
            desc = _describe_destructive(name, args)
            log("confirmation required:", name, _log_args(args))
            return (f"CONFIRMATION_REQUIRED. Do not perform this action. Ask him clearly: "
                    f"'Are you sure you want me to {desc}?' Wait for him to answer, then call "
                    f"{name} again with the exact same details only if he says yes.")

        if self.last_user_utterance_at <= pending_at:
            return "He has not answered yet — ask him again and wait for a real yes or no before calling this again."

        del self.pending_confirm[key]
        if _is_yes(self.last_user_utterance):
            log("confirmed:", name, _log_args(args))
            return None
        log("declined:", name, _log_args(args), "— heard:", repr(self.last_user_utterance))
        return "He did not confirm that — left it alone."

    async def run_one(self, call):
        """Run a single tool and hand back its answer."""
        name, args = call.get("name"), call.get("args") or {}

        if name in DESTRUCTIVE:
            gate = self._destructive_gate(name, args)
            if gate is not None:
                HUD.note_action(f"confirm? {name}")
                return {"id": call.get("id"), "name": name, "response": {"result": gate}}

        # The model sometimes asks for the same thing several times over
        # while it waits. That is how you end up with six Gmail windows.
        fingerprint = name if name in NEEDS_THE_SCREEN else name + json.dumps(args, sort_keys=True)
        now = time.time()
        if now - self.recent_calls.get(fingerprint, 0) < float(CFG.get("duplicate_window", 25)):
            log("ignoring a repeat of", name)
            return {"id": call.get("id"), "name": name,
                    "response": {"result": "already doing that — no need to ask again"}}
        self.recent_calls[fingerprint] = now

        log("action:", name, _log_args(args))
        HUD.note_action(name)
        HUD.set(status="thinking")

        fn = ACTIONS.get(name)
        if not fn:
            result = "unknown action"

        elif name in NEEDS_THE_SCREEN:
            # These drive the mouse and keyboard, so they must never overlap.
            # If one is already running, refuse rather than queue.
            if self.screen_lock.locked():
                log(f"skipping {name} — already busy with {self.busy_with}")
                HUD.set(status="thinking")
                return {"id": call.get("id"), "name": name, "response": {
                    "result": f"still working on {self.busy_with} — wait for that to come back first"}}

            if name in SLOW_JOBS and not self.spoke_this_turn:
                ack_out_loud()

            async with self.screen_lock:
                self.busy_with = name
                try:
                    result = await asyncio.to_thread(lambda: fn(**args))
                except Exception as e:
                    log("action failed:", e)
                    result = f"that failed: {e}"
                finally:
                    self.busy_with = None

        else:
            if name in SLOW_JOBS and not self.spoke_this_turn:
                ack_out_loud()
            try:
                result = await asyncio.to_thread(lambda: fn(**args))
            except Exception as e:
                log("action failed:", e)
                result = f"that failed: {e}"

        if isinstance(result, Panel):
            # The structured half is for the HUD only — the model, and the
            # log, only ever see the words David is actually going to say.
            HUD.set(panel={"type": result.type, "data": result.data, "at": time.time()})
            result = result.speech

        log("  ->", result)
        HUD.set(status="idle")

        if name in OPENS_A_WINDOW and CFG.get("auto_place", True):
            await asyncio.sleep(1.4)
            await asyncio.to_thread(place_active_window)

        return {"id": call.get("id"), "name": name, "response": {"result": result}}

    async def handle_tools(self, ws, calls):
        """Everything he asked for at once runs at once, except the jobs that
           need the screen — those take turns."""
        quick = [c for c in calls if c.get("name") not in NEEDS_THE_SCREEN]
        screen = [c for c in calls if c.get("name") in NEEDS_THE_SCREEN]

        responses = []
        if quick:
            responses += list(await asyncio.gather(*[self.run_one(c) for c in quick]))
        for c in screen:
            responses.append(await self.run_one(c))

        await ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))

    def play_forever(self):
        """Runs on its own thread, feeding whatever comes back to the speakers.
           Holds the microphone shut while anything is playing."""
        with sd.RawOutputStream(samplerate=SPEAKER_RATE, channels=1, dtype="int16") as speaker:
            while True:
                chunk = self.out.get()
                self.speaking.set()

                # He's talking over David right now — go quiet immediately
                # rather than finishing the sentence while the interruption
                # is still only in flight to Google and back. Nothing is
                # thrown away here; this chunk just waits. If it turns out
                # to be a real interruption, receive() clears self.speaking
                # and the queue underneath us and we drop it below. If he
                # just paused mid-word, playback picks back up right here.
                while time.time() < self._loud_until:
                    if UI:
                        HUD.set(outLevel=0, status="listening")
                    time.sleep(0.05)
                    if not self.speaking.is_set():
                        break
                if not self.speaking.is_set():
                    continue

                # Needed whether or not the UI is up: mic_callback sizes its
                # interrupt bar off this, so it has to track his real output
                # even headless. Smoothed so one loud syllable doesn't spike
                # the bar for the rest of the sentence.
                import array as _a, math as _m
                sm = _a.array("h")
                sm.frombytes(chunk[: len(chunk) // 2 * 2])
                raw_rms = _m.sqrt(sum(x * x for x in sm) / len(sm)) if sm else 0.0
                self._out_level_est = 0.7 * self._out_level_est + 0.3 * raw_rms
                if UI:
                    HUD.set(outLevel=min(1.0, raw_rms / 32768 * 6), status="speaking")
                speaker.write(chunk)

                if self.out.empty():
                    time.sleep(0.45)          # let the room settle
                    if self.out.empty():
                        self.speaking.clear()
                        self._out_level_est = 0.0
                        HUD.set(outLevel=0)

    async def announce(self, ws):
        """Lets a finished timer interrupt and speak."""
        while True:
            text = await self.notices.get()
            await ws.send(json.dumps({
                "clientContent": {
                    "turns": [{"role": "user", "parts": [{"text": f"[system] {text} Tell him now, briefly."}]}],
                    "turnComplete": True,
                }
            }))

    async def run(self):
        self.loop = asyncio.get_running_loop()

        global NOTIFY
        NOTIFY = lambda text: self.loop.call_soon_threadsafe(self.notices.put_nowait, text)
        url = f"{WS_URL}?key={urllib.parse.quote(CFG['api_key'])}"

        # build_system() shells out (list_drives() runs a PowerShell/WMI
        # query) and used to run synchronously right here, blocking the
        # whole event loop for it — on every single reconnect, not just at
        # startup, since Live sessions have a time limit and this runs
        # again each time one comes back. Starting it now, off-thread, lets
        # it overlap with the websocket handshake below instead of adding
        # to it.
        system_task = asyncio.create_task(asyncio.to_thread(build_system))

        async with websockets.connect(url, max_size=None,
                                      open_timeout=45, ping_interval=20,
                                      ping_timeout=60, close_timeout=5) as ws:
            await ws.send(json.dumps({
                "setup": {
                    "model": f"models/{CFG.get('live_model') or MODEL}",
                    "sessionResumption": ({"handle": self.resume_handle}
                                          if self.resume_handle else {}),
                    "contextWindowCompression": {"slidingWindow": {}},
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": CFG.get("live_voice", "Charon")}
                            }
                        },
                    },
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {},
                    "systemInstruction": {"parts": [{"text": await system_task}]},
                    "tools": [{"functionDeclarations": declarations()}],
                }
            }))

            # wait for the handshake before streaming anything
            while True:
                msg = json.loads(await ws.recv())
                if "setupComplete" in msg:
                    break
                if "error" in msg:
                    sys.exit(f"setup refused: {msg['error']}")

            HUD.set(connected=True, micArmed=True)

            if not self.resume_handle:
                for i, r in enumerate(monitors()):
                    log(f"  screen {i + 1}: {screen_label(i)} "
                        f"({r['right'] - r['left']}x{r['bottom'] - r['top']} at x={r['left']})")

            if self.resume_handle:
                log("reconnected — carry on")
            else:
                log("connected — just talk, and say David when you want him")
                await ws.send(json.dumps({
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text":
                            "[system startup] Greet him out loud with just the words "
                            "'David online.' Speak it, do not call any tool."}]}],
                        "turnComplete": True,
                    }
                }))

            device = CFG.get("input_device")
            with sd.RawInputStream(samplerate=MIC_RATE, blocksize=CHUNK, dtype="int16",
                                   channels=1, callback=self.mic_callback,
                                   device=device if device is not None else None):
                await asyncio.gather(self.send_audio(ws), self.receive(ws), self.announce(ws))


async def stay_connected(live):
    """Sessions have a time limit and connections drop. Keep coming back."""
    wait = 2
    while True:
        try:
            await live.run()
            HUD.set(connected=False)
            wait = 2
        except Reconnect:
            wait = 2
            await asyncio.sleep(0.3)
        except (TimeoutError, asyncio.TimeoutError):
            log(f"the connection timed out — trying again in {wait}s")
            await asyncio.sleep(wait)
            wait = min(30, wait * 2)
        except websockets.exceptions.WebSocketException as e:
            text = str(e)
            if "not found" in text.lower() or "1007" in text:
                print(f"\nConnection refused: {e}")
                print("Try a different live_model in config.json.")
                return
            log(f"connection dropped, back in {wait}s:", text[:100])
            await asyncio.sleep(wait)
            wait = min(30, wait * 2)
        except OSError as e:
            log(f"network unreachable, back in {wait}s:", str(e)[:100])
            await asyncio.sleep(wait)
            wait = min(30, wait * 2)


async def everything(live):
    tasks = [stay_connected(live)]
    if UI:
        tasks.append(HUD.serve())
    await asyncio.gather(*tasks)


def main():
    live = Live()
    threading.Thread(target=live.play_forever, daemon=True).start()
    try:
        asyncio.run(everything(live))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
