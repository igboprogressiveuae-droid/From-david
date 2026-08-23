"""
Real Notion access for David — journal straight into a page in your workspace.

Notion's public OAuth needs a hosted https redirect, which a local desktop
tool does not have. The way a personal script like this is meant to talk to
your own workspace is an "internal integration": you create one, share a
page with it, and paste the secret here once. No browser flow, nothing to
host.

Needs:  pip install requests   (already a dependency)

Setup, from the dashboard's Notion card:
  1. https://www.notion.so/my-integrations -> New integration -> copy the secret
  2. Open the page you want journaled to -> ... menu -> Connections -> add that integration
  3. Paste the secret and that page's link into the dashboard and hit Connect
"""

import json
import os
import re
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "notion_token.json")
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def is_connected():
    return os.path.exists(TOKEN_FILE)


def _load():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return json.load(f)


def _extract_page_id(raw):
    """Accepts a bare id or a full Notion page URL and returns the id,
       hyphenated the way the API expects.

       A titled URL looks like .../My-Journal-1a2b3c4d5e6f7080901122334455aabb —
       the id is one clean 32-hex block glued to the end of the last path
       segment. Stripping every non-hex character from the whole URL first
       (an earlier version of this did) is wrong: title words like
       "workspace" contain hex letters too, and they get spliced onto the
       real id instead of discarded. Anchoring the match to the end of the
       last segment avoids that."""
    raw = (raw or "").strip().split("?", 1)[0]
    tail = raw.rstrip("/").rsplit("/", 1)[-1]

    m = re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", tail)
    if m:
        return tail.lower()

    m = re.search(r"[0-9a-fA-F]{32}$", tail)
    if not m:
        raise ValueError("that does not look like a Notion page link or id")
    h = m.group(0).lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _title_of(page_obj):
    for prop in page_obj.get("properties", {}).values():
        if prop.get("type") == "title":
            text = "".join(t.get("plain_text", "") for t in prop.get("title", []))
            return text or "Untitled"
    return "Untitled"


def connect(secret, page):
    """Verify the secret can actually reach that page, then save both."""
    secret = (secret or "").strip()
    if not secret:
        raise ValueError("that secret is empty")
    page_id = _extract_page_id(page)

    headers = {"Authorization": f"Bearer {secret}", "Notion-Version": VERSION}
    try:
        res = requests.get(f"{API}/pages/{page_id}", headers=headers, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach Notion: {e}")

    if res.status_code == 401:
        raise RuntimeError("that integration secret was not accepted")
    if res.status_code == 404:
        raise RuntimeError("page not found — share it with your integration first, "
                            "from the page's ... menu -> Connections")
    if not res.ok:
        raise RuntimeError(f"Notion returned {res.status_code}")

    title = _title_of(res.json())
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": secret, "page_id": page_id, "page_title": title}, f, indent=2)
    return title


def disconnect():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return "disconnected"


def status():
    if not is_connected():
        return {"connected": False, "page_title": ""}
    try:
        return {"connected": True, "page_title": _load().get("page_title", "connected")}
    except Exception:
        return {"connected": False, "page_title": ""}


def _heading(text):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def _append_blocks(children):
    """Shared by journal() and table() — everything he adds to the page goes
       through here so the two behave the same way when Notion pushes back."""
    data = _load()
    headers = {"Authorization": f"Bearer {data['token']}", "Notion-Version": VERSION,
               "Content-Type": "application/json"}
    try:
        res = requests.patch(f"{API}/blocks/{data['page_id']}/children",
                              headers=headers, json={"children": children}, timeout=20)
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach Notion: {e}")

    if res.status_code == 401:
        raise RuntimeError("Notion no longer accepts that integration secret — reconnect it from the dashboard")
    if not res.ok:
        raise RuntimeError(f"Notion returned {res.status_code}: {res.text[:200]}")


def journal(text):
    """Append a dated entry to the connected page. Returns the heading it
       was filed under."""
    stamp = datetime.now().strftime("%A, %d %B — %I:%M %p")
    _append_blocks([_heading(stamp),
                    {"object": "block", "type": "paragraph",
                     "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}])
    return stamp


def table(rows_text, title=""):
    """Add a real Notion table block to the connected page.

       rows_text is plain text: one row per line, cells separated by a pipe.
       The first line is the header row. Rows with fewer cells than the
       widest one are padded — Notion requires every row in a table to carry
       the same number of cells as the table's declared width."""
    lines = [l.strip() for l in (rows_text or "").splitlines() if l.strip()]
    if not lines:
        raise ValueError("no rows given for that table")

    grid = [[c.strip() for c in row.split("|")] for row in lines]
    width = max(len(r) for r in grid)
    if width < 1:
        raise ValueError("that table has no columns")
    grid = [r + [""] * (width - len(r)) for r in grid]

    def cell(text):
        return [{"type": "text", "text": {"content": text}}] if text else []

    table_rows = [{"object": "block", "type": "table_row",
                   "table_row": {"cells": [cell(c) for c in row]}} for row in grid]

    heading = title or datetime.now().strftime("%A, %d %B — %I:%M %p")
    children = [_heading(heading), {
        "object": "block", "type": "table",
        "table": {"table_width": width, "has_column_header": True,
                  "has_row_header": False, "children": table_rows},
    }]
    _append_blocks(children)
    return f"{width} column{'s' if width != 1 else ''}, {len(grid)} row{'s' if len(grid) != 1 else ''}, under {heading}"
