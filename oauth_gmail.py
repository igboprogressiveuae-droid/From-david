"""
Real Gmail access for David — OAuth login once, then the Gmail API directly.

No screen reading, no vision quota, no app passwords. This is what "Connect
your email" should mean, and it is the first piece of the settings dashboard.

Needs:  pip install google-auth-oauthlib google-auth google-api-python-client

First run opens a browser for you to sign in and approve access. After that,
a refresh token is saved locally and it never asks again.
"""

import base64
import json
import os
import socket
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, "credentials.json")
TOKEN_FILE = os.path.join(HERE, "gmail_token.json")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_service = None


@contextmanager
def _bounded(seconds=20):
    """Google's client libraries don't reliably expose a request timeout of
       their own — on a stalled connection they can hang forever, and since
       every Gmail call runs inside David's screen lock, that hang used to
       take every other screen action down with it. This caps every
       blocking socket call made while it's open, then restores whatever
       the default was before. Not used around the interactive sign-in
       flow — that one waits on him, not the network."""
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def is_ready():
    """True once both the app credentials and a granted token exist."""
    return os.path.exists(CREDENTIALS_FILE)


def is_connected():
    return os.path.exists(TOKEN_FILE)


def _get_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        with _bounded():
            creds.refresh(Request())

    if not creds or not creds.valid:
        if not os.path.exists(CREDENTIALS_FILE):
            raise RuntimeError(
                "credentials.json is missing. Set up a Google Cloud OAuth client "
                "and place the downloaded file next to this script."
            )
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True,
                                       success_message="Connected. You can close this tab.")

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    return creds


def connect():
    """Run the login flow now, on purpose — this is what the dashboard's
       'Connect Gmail' button calls."""
    global _service
    from googleapiclient.discovery import build

    creds = _get_creds()
    with _bounded():
        _service = build("gmail", "v1", credentials=creds)
        profile = _service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "connected")


def disconnect():
    global _service
    _service = None
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return "disconnected"


def _svc():
    global _service
    if _service is None:
        from googleapiclient.discovery import build
        with _bounded():
            _service = build("gmail", "v1", credentials=_get_creds())
    return _service


# A small pool of worker threads that live for the whole process, used to
# fetch several messages' headers at once instead of one after another.
# Each worker builds its own keep-alive HTTP connection the first time it
# runs and reuses it after that — a fresh httplib2.Http() per call pays a
# full TLS handshake every single time, which is slow enough that it can
# eat the entire benefit of doing this in parallel. Reusing one warm
# connection per thread is what actually makes it fast.
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="gmail")
_thread_ctx = threading.local()


def _thread_transport(creds):
    """creds must come from the caller, already fetched on the calling
       thread — _get_creds() reads and rewrites gmail_token.json on disk,
       and letting every worker thread call it independently the first
       time it runs means their reads and writes can interleave and hand
       one of them back a half-written, unparsable file."""
    http = getattr(_thread_ctx, "http", None)
    if http is None:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=20))
        _thread_ctx.http = http
    return http


# ---- reading ----

def _header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _body_text(payload, limit=4000):
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        raw = base64.urlsafe_b64decode(payload["body"]["data"])
        return raw.decode("utf-8", errors="replace")[:limit]
    for part in payload.get("parts", []) or []:
        text = _body_text(part, limit)
        if text:
            return text
    return ""


def search(sender="", subject="", hours=24, unread_only=False, limit=6):
    """Real Gmail search — same query syntax you would type in the search box.

       Only pulls headers here (From/Subject/Date), not full bodies — that
       used to mean one full-message fetch per result, one after another,
       which is most of why "check my email" felt slow. get_body() below
       fetches a body on demand, the one time he actually asks to hear a
       message read out. The header fetches this does still need also run
       in parallel rather than sequentially, for the same reason."""
    terms = []
    if sender:
        terms.append(f"from:{sender}")
    if subject:
        terms.append(f"subject:{subject}")
    if unread_only:
        terms.append("is:unread")
    if hours:
        days = max(1, round(float(hours) / 24))
        terms.append(f"newer_than:{days}d")
    query = " ".join(terms) or "in:inbox"

    svc = _svc()
    creds = _get_creds()   # fetched once, here, on this thread — see _thread_transport
    with _bounded():
        res = svc.users().messages().list(userId="me", q=query, maxResults=int(limit)).execute()

    ids = [m["id"] for m in res.get("messages", [])]
    if not ids:
        who = f" from {sender}" if sender else ""
        return [], f"nothing{who} in the last {int(hours)} hours"

    def _fetch(mid):
        msg = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute(http=_thread_transport(creds))
        headers = msg["payload"].get("headers", [])
        return {
            "id": msg["id"],
            "from": _header(headers, "From"),
            "subject": _header(headers, "Subject") or "(no subject)",
            "date": _header(headers, "Date"),
            "body": "",
            "unread": "UNREAD" in msg.get("labelIds", []),
        }

    found = list(_POOL.map(_fetch, ids))
    return found, None


def get_body(message_id):
    """Pulls one message's full text on demand — the only place a full
       fetch happens now, so it's paid for once, for the message he
       actually wants read, instead of for every result in a list."""
    with _bounded():
        msg = _svc().users().messages().get(userId="me", id=message_id, format="full").execute()
    return _body_text(msg["payload"])


def send(to, subject, body, reply_to_id=None):
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    payload = {"raw": raw}
    if reply_to_id:
        payload["threadId"] = reply_to_id

    with _bounded():
        _svc().users().messages().send(userId="me", body=payload).execute()
    return f"sent to {to}"


def mark_read(message_id):
    with _bounded():
        _svc().users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
