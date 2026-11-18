"""
Background jobs: things David does on his own, on a schedule.

    "every morning at 7, give me the gold and oil news"
    "every 30 minutes, tell me if Bitcoin falls under 90,000"
    "every night at 11, read today's conversations and remember anything new about me"

Each job is a plain-language task run through the thinking brain with every
tool. The answer is spoken (if voice is running) and sent on Telegram (if set
up). A job that finds nothing worth saying stays quiet. Jobs cannot do
anything that needs a yes — nobody is there to give one.

Kept in jobs.json next to this file (gitignored). David checks every 30 seconds;
a job missed while he was off runs once when he starts.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

import tools
from tools import BRAIN, HERE, _WEEKDAYS, _parse_clock, log

FILE = os.path.join(HERE, "jobs.json")
MIN_INTERVAL = 5          # minutes — a tighter loop would burn the API quota for little gain
QUIET = "NOTHING"         # what a job replies when there's nothing to report
_LOCK = threading.Lock()


def _days(text):
    """'' -> every day; 'weekdays'; 'weekends'; 'monday, friday' -> [0, 4]."""
    t = str(text or "").lower().strip()
    if t in ("", "daily", "every day", "everyday"):
        return []
    if t == "weekdays":
        return [0, 1, 2, 3, 4]
    if t == "weekends":
        return [5, 6]
    picked = sorted({_WEEKDAYS.index(w) for w in _WEEKDAYS if w in t or w[:3] in t.replace(",", " ").split()})
    if not picked:
        raise ValueError(f"I could not tell which days '{text}' means")
    return picked


def next_run(job, after):
    """When this job should next run, strictly after `after`."""
    if job.get("every_minutes"):
        return after + timedelta(minutes=job["every_minutes"])
    hour, minute = _parse_clock(job["at"])
    day = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day <= after:
        day += timedelta(days=1)
    while job.get("days") and day.weekday() not in job["days"]:
        day += timedelta(days=1)
    return day


def load():
    if not os.path.exists(FILE):
        return []
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        log("jobs.json is unreadable — leaving it alone and running no jobs")
        return []


def save(items):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1, ensure_ascii=False)
    os.replace(tmp, FILE)


def add(task, every_minutes=0, at="", days="", now=None):
    """Returns the new job, or raises ValueError saying what's wrong."""
    task = " ".join(str(task).split())
    if not task:
        raise ValueError("a job needs something to do")
    every = int(float(every_minutes or 0))
    if every and at:
        raise ValueError("give either an interval or a clock time, not both")
    if every:
        if every < MIN_INTERVAL:
            raise ValueError(f"the shortest interval is {MIN_INTERVAL} minutes")
    elif not at or not _parse_clock(at):
        raise ValueError("a job needs a clock time like 7am, or an interval in minutes")
    now = now or datetime.now()
    with _LOCK:
        items = load()
        job = {"id": max([j["id"] for j in items] + [0]) + 1, "task": task,
               "every_minutes": every or None, "at": None if every else at,
               "days": [] if every else _days(days), "last_run": None, "last_result": ""}
        job["next_run"] = next_run(job, now).isoformat(timespec="minutes")
        items.append(job)
        save(items)
    return job


def remove(job_id):
    with _LOCK:
        items = load()
        keep = [j for j in items if str(j["id"]) != str(job_id).strip().lstrip("#")]
        if len(keep) == len(items):
            return False
        save(keep)
    return True


def describe(job):
    if job.get("every_minutes"):
        when = f"every {job['every_minutes']} minutes"
    else:
        days = ", ".join(_WEEKDAYS[d].title() for d in job.get("days") or []) or "every day"
        when = f"at {job['at']}, {days}"
    return f"#{job['id']} {when}: {job['task']}"


def run_job(job, ask=None):
    """Run one job now and report it. Returns what it said, or '' if it stayed quiet."""
    import engines
    import telegram
    ask = ask or (lambda prompt: engines.Conversation("a background job", gate=None).ask(prompt))
    prompt = (f"{job['task']}\n\n(This is a scheduled job — nobody is waiting on the other end. "
              f"Do the task with your tools, then write what they should hear, briefly. If there is "
              f"nothing worth telling them, reply with exactly {QUIET}.)")
    log("job:", describe(job))
    result = (ask(prompt) or "").strip()
    BRAIN.log_turn("job", f"job #{job['id']}", f"{job['task']} -> {result}")
    if not result or result.strip(" .").upper() == QUIET:
        return ""
    tools.NOTIFY(f"A scheduled job finished — {job['task']}. It says: {result}")
    telegram.send_owner(f"⏰ {job['task']}\n\n{result}")
    return result


def tick(now=None, run=run_job):
    """Run every job that is due. Returns how many ran."""
    now = now or datetime.now()
    with _LOCK:
        due = [j for j in load() if datetime.fromisoformat(j["next_run"]) <= now]
    for job in due:
        try:
            result = run(job)
        except Exception as e:
            log("job failed:", e)
            result = f"failed: {e}"
        with _LOCK:
            items = load()
            for j in items:
                if j["id"] == job["id"]:
                    j["last_run"] = now.isoformat(timespec="minutes")
                    j["last_result"] = result[:500]
                    j["next_run"] = next_run(j, max(now, datetime.now())).isoformat(timespec="minutes")
            save(items)
    return len(due)


def run_forever():
    log(f"jobs are on — {len(load())} scheduled")
    while True:
        try:
            tick()
        except Exception as e:
            log("job scheduler hiccup:", e)
        time.sleep(30)


def start():
    t = threading.Thread(target=run_forever, daemon=True, name="jobs")
    t.start()
    return t
