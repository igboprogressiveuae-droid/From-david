"""
David — a voice assistant that lives on the whole machine, not just Chrome.
Created by Ibans.

    python david.py

Say "David", wait for the tone of the reply, then say what you want.
Anything that changes or destroys a file is read back to you and needs a
spoken yes before it happens.

Needs:  pip install vosk sounddevice requests
        plus a Vosk model folder (see the README notes at the bottom).
"""

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime

import requests
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

HERE = os.path.dirname(os.path.abspath(__file__))
SetLogLevel(-1)

# Raised while David is talking, so the microphone ignores his own voice
# coming back through the speakers.
SPEAKING = threading.Event()

# --verbose prints every partial guess, so you can see what it thinks you said
VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv


# ----------------------------------------------------------------------------
# config + logging
# ----------------------------------------------------------------------------

def close_enough(word, target, allowed=1):
    """True when one word is within `allowed` single-character edits of another."""
    if abs(len(word) - len(target)) > allowed:
        return False
    prev = list(range(len(target) + 1))
    for i, a in enumerate(word, 1):
        cur = [i]
        for j, b in enumerate(target, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[-1] <= allowed


def matches_wake(text, words):
    t = text.lower()
    if any(w in t for w in words):
        return True
    # catch near-misses the recogniser produces with accented speech
    return any(close_enough(tok, "david") for tok in t.split())


def load_config():
    path = os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        sys.exit("config.json is missing. It should sit next to this script.")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("api_key", "").startswith("PASTE"):
        sys.exit("Put your API key in config.json first.")
    return cfg


CFG = load_config()


def log(*parts):
    line = f"[{datetime.now():%H:%M:%S}] " + " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        with open(os.path.join(HERE, CFG.get("log_file", "david.log")), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# speech out — Piper if it is running, otherwise the built-in Windows voice
# ----------------------------------------------------------------------------

def say(text):
    text = (text or "").strip()
    if not text:
        return
    log("David:", text)

    SPEAKING.set()
    try:
        _speak(text)
    finally:
        time.sleep(0.35)      # let the room go quiet
        SPEAKING.clear()
        if EARS:
            EARS.drain()      # throw away anything heard while talking


def _speak(text):
    url = CFG.get("piper_url")
    if url:
        try:
            res = requests.post(url + "/speak", json={"text": text}, timeout=90)
            if res.ok:
                path = os.path.join(HERE, "_say.wav")
                with open(path, "wb") as f:
                    f.write(res.content)
                play_wav(path)
                return
        except requests.RequestException:
            pass  # server not running; fall through

    windows_say(text)


def play_wav(path):
    ctypes.windll.winmm.mciSendStringW(f'open "{path}" type waveaudio alias dv', None, 0, None)
    ctypes.windll.winmm.mciSendStringW("play dv wait", None, 0, None)
    ctypes.windll.winmm.mciSendStringW("close dv", None, 0, None)


def windows_say(text):
    safe = text.replace("'", "''")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Speech; "
         "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         f"$s.Speak('{safe}')"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ----------------------------------------------------------------------------
# speech in — Vosk, entirely offline
# ----------------------------------------------------------------------------

EARS = None


def pick_microphone():
    """Prefer a real microphone over loopback devices like Stereo Mix."""
    devices = sd.query_devices()
    default = sd.default.device[0]

    name = (devices[default]["name"] if default is not None else "").lower()
    if default is not None and "stereo mix" not in name and "loopback" not in name:
        return default

    for i, d in enumerate(devices):
        n = d["name"].lower()
        if d["max_input_channels"] > 0 and "microphone" in n and "stereo mix" not in n:
            return i
    return default


class Ears:
    def __init__(self):
        model_dir = os.path.join(HERE, CFG.get("vosk_model", "model"))
        if not os.path.isdir(model_dir):
            sys.exit(f"Vosk model folder not found at {model_dir}")
        log("loading the speech model, this takes a moment")
        self.model = Model(model_dir)
        self.q = queue.Queue()
        device = CFG.get("input_device")
        if device is None:
            device = pick_microphone()
        log("using input device:", sd.query_devices(device)["name"] if device is not None else "system default")
        self.stream = sd.RawInputStream(
            samplerate=16000, blocksize=4000, dtype="int16",
            channels=1, callback=self._feed,
            device=device if device is not None else None
        )
        self.stream.start()
        log("microphone open")

    def _feed(self, data, frames, t, status):
        if SPEAKING.is_set():
            return                      # deaf while he is speaking
        self.q.put(bytes(data))

    def command_grammar(self):
        """A vocabulary of the things he actually says to it — much easier to
           recognise than the whole language."""
        words = set()
        for key in list(CFG.get("apps", {})) + list(CFG.get("sites", {})):
            for part in key.replace("-", " ").split():
                words.add(part.lower())
                # split run-together names like tradingview
                for guess in ("trading view", "you tube", "whats app", "funded next"):
                    if part.lower() == guess.replace(" ", ""):
                        words.update(guess.split())
        words.update("""
            open close launch start run show go to search find look up
            volume up down mute louder quieter lock sleep stop listening
            file folder document delete remove rename save write
            my the a on in for me please david never mind that is all
            website site page app chart charts email mail music video
        """.split())
        return json.dumps(sorted(w for w in words if w.isalpha()) + ["[unk]"])

    def _command_recogniser(self):
        try:
            return KaldiRecognizer(self.model, 16000, self.command_grammar())
        except Exception as e:
            log("no grammar support, using open vocabulary:", e)
            return None

    def listen(self, seconds=12, settle=1.2):
        """Return what was said, stopping shortly after you stop talking.

           Two recognisers run together: one that only knows your commands,
           one that knows everything. The narrow one wins when it hears
           something it recognises, so "open tradingview" survives even when
           the open recogniser mangles it."""
        rec = KaldiRecognizer(self.model, 16000)
        narrow = self._command_recogniser()
        started = time.time()
        heard = []
        last_voice = None

        narrow_heard = []

        while time.time() - started < seconds:
            chunk = self.q.get()

            if narrow is not None and narrow.AcceptWaveform(chunk):
                piece = json.loads(narrow.Result()).get("text", "").strip()
                if piece:
                    narrow_heard.append(piece)

            if rec.AcceptWaveform(chunk):
                piece = json.loads(rec.Result()).get("text", "").strip()
                if piece:
                    heard.append(piece)
                    last_voice = time.time()
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "").strip()
                if partial:
                    last_voice = time.time()
            if last_voice and time.time() - last_voice > settle and heard:
                break

        tail = json.loads(rec.FinalResult()).get("text", "").strip()
        if tail:
            heard.append(tail)

        if narrow is not None:
            tail = json.loads(narrow.FinalResult()).get("text", "").strip()
            if tail:
                narrow_heard.append(tail)

        open_text = " ".join(heard).strip()
        narrow_text = " ".join(w for w in " ".join(narrow_heard).split() if w != "[unk]").strip()

        if VERBOSE and narrow_text:
            print(f"    [commands heard: {narrow_text}]", flush=True)

        # the narrow pass only produces real words when it genuinely matched
        if len(narrow_text.split()) >= 2:
            return f"{open_text} (heard as commands: {narrow_text})" if open_text else narrow_text
        return open_text

    def drain(self):
        while not self.q.empty():
            self.q.get_nowait()

    def _wake_recogniser(self, words):
        """A recogniser that only knows the wake word — far more accurate
           than asking it to pick the name out of all of English."""
        try:
            grammar = json.dumps(list(dict.fromkeys(list(words) + ["[unk]"])))
            return KaldiRecognizer(self.model, 16000, grammar)
        except Exception:
            return KaldiRecognizer(self.model, 16000)   # model without grammar support

    def wait_for_wake(self, words):
        """Block until the wake word turns up, checking as the words arrive."""
        rec = self._wake_recogniser(words)
        started = time.time()
        shown = ""

        while True:
            chunk = self.q.get()

            if rec.AcceptWaveform(chunk):
                text = json.loads(rec.Result()).get("text", "").strip()
                shown = ""
                if text and text != "[unk]":
                    log("heard:", repr(text))
                    if matches_wake(text, words):
                        return text
                rec = self._wake_recogniser(words)
                started = time.time()
                continue

            partial = json.loads(rec.PartialResult()).get("partial", "").strip()
            if partial and partial != shown:
                shown = partial
                if VERBOSE:
                    print(f"    ...{partial}", flush=True)
                if matches_wake(partial, words):
                    log("heard:", repr(partial))
                    return partial

            # keep the recogniser fresh so it does not drift over long silences
            if time.time() - started > 20:
                rec = self._wake_recogniser(words)
                started = time.time()

    def level_test(self, seconds=6):
        """Show how loud the microphone actually is."""
        import array
        import math

        def rms_of(chunk):
            samples = array.array("h")
            samples.frombytes(chunk[: len(chunk) // 2 * 2])
            if not samples:
                return 0
            return int(math.sqrt(sum(s * s for s in samples) / len(samples)))

        print("\nSpeak normally for a few seconds...\n")
        end = time.time() + seconds
        peak = 0
        while time.time() < end:
            chunk = self.q.get()
            rms = rms_of(chunk)
            peak = max(peak, rms)
            bars = min(50, rms // 60)
            print("\r  " + "#" * bars + " " * (50 - bars) + f"  {rms:5d}", end="")
        print(f"\n\n  peak: {peak}")
        if peak < 500:
            print("  Too quiet. Raise the microphone level in Windows sound settings.")
        elif peak < 1500:
            print("  Quiet but usable. Louder would be better.")
        else:
            print("  Good level.")
        print()


# ----------------------------------------------------------------------------
# what David is allowed to do
# ----------------------------------------------------------------------------

def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def find_file(name, limit=8):
    """Look for a file by loose name match across the configured folders."""
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


def act_open_app(name):
    target = CFG.get("apps", {}).get(name.lower())
    if not target:
        return f"I do not have {name} in my list of apps."
    try:
        os.startfile(target) if os.path.exists(target) else subprocess.Popen(target, shell=False)
    except Exception:
        subprocess.Popen(f'start "" "{target}"', shell=True)
    return f"Opening {name}."


def act_open_site(name):
    url = CFG.get("sites", {}).get(name.lower())
    if not url:
        return f"I do not have a site called {name}."
    webbrowser.open(url)
    return f"Opening {name}."


def act_open_url(url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return "Opening it now."


def act_search_web(query):
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(query))
    return f"Searching for {query}."


def act_open_file(name):
    hits = find_file(name)
    if not hits:
        return f"I could not find anything called {name}."
    os.startfile(hits[0])
    return f"Opening {os.path.basename(hits[0])}."


def act_find_files(name):
    hits = find_file(name)
    if not hits:
        return f"Nothing matching {name}."
    names = [os.path.basename(h) for h in hits[:4]]
    return "I found " + ", ".join(names) + "."


def act_open_folder(name):
    root = expand(name) if os.path.isdir(expand(name)) else None
    if not root:
        for folder in CFG.get("search_folders", []):
            candidate = expand(folder)
            if name.lower() in os.path.basename(candidate).lower():
                root = candidate
                break
    if not root:
        return f"I could not find a folder called {name}."
    os.startfile(root)
    return f"Opening {os.path.basename(root)}."


def act_volume(direction):
    key = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}.get(direction, 0xAF)
    for _ in range(1 if direction == "mute" else 5):
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)
    return "Done."


def act_lock():
    ctypes.windll.user32.LockWorkStation()
    return "Locking up."


def act_delete_file(name):
    hits = find_file(name)
    if not hits:
        return f"I could not find {name}."
    target = hits[0]
    bin_dir = expand("~/.david_trash")
    os.makedirs(bin_dir, exist_ok=True)
    shutil.move(target, os.path.join(bin_dir, os.path.basename(target)))
    return f"Moved {os.path.basename(target)} to a recovery folder in your home directory."


def act_rename_file(name, new_name):
    hits = find_file(name)
    if not hits:
        return f"I could not find {name}."
    target = hits[0]
    dest = os.path.join(os.path.dirname(target), new_name)
    os.rename(target, dest)
    return f"Renamed to {new_name}."


def act_write_file(name, content):
    path = os.path.join(expand("~/Desktop"), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return f"Saved {name} to your desktop."


# name -> (function, needs spoken confirmation, how to describe it first)
ACTIONS = {
    "open_app":     (act_open_app,     False, None),
    "open_site":    (act_open_site,    False, None),
    "open_url":     (act_open_url,     False, None),
    "search_web":   (act_search_web,   False, None),
    "open_file":    (act_open_file,    False, None),
    "find_files":   (act_find_files,   False, None),
    "open_folder":  (act_open_folder,  False, None),
    "volume":       (act_volume,       False, None),
    "lock":         (act_lock,         True,  "lock the computer"),
    "delete_file":  (act_delete_file,  True,  "delete {name}"),
    "rename_file":  (act_rename_file,  True,  "rename {name} to {new_name}"),
    "write_file":   (act_write_file,   True,  "save a new file called {name} to your desktop"),
}

TOOL_SPEC = """
open_app(name)                  one of the app names you are given
open_site(name)                 one of the site names you are given
open_url(url)                   any web address
search_web(query)               a Google search
open_file(name)                 opens the closest matching file
find_files(name)                lists matching files without opening them
open_folder(name)               opens a folder
volume(direction)               "up", "down" or "mute"
lock()                          locks the workstation
delete_file(name)               moves a file to a recovery folder
rename_file(name, new_name)     renames a file
write_file(name, content)       writes a new text file to the desktop
"""


# ----------------------------------------------------------------------------
# the brain
# ----------------------------------------------------------------------------

def system_prompt():
    return f"""You are David, a voice assistant on Ibans's Windows laptop. He is a trader and a freelance web developer in Nigeria.

You are spoken to and you speak back, so keep replies to one or two short sentences. No markdown, no lists, no emoji.

What you receive is imperfect speech-to-text, so it arrives garbled. Read through the noise to the intent. "the way of finding the people open tradingview" is him asking you to open TradingView. "the deadbeat our you doing" is him asking how you are. Latch onto the words that match an app, a site or an action you know, and ignore the rest. Only ask him to repeat himself when you genuinely cannot find any intent in it.

You can do things on the machine. Available actions:
{TOOL_SPEC}

Apps he has configured: {", ".join(CFG.get("apps", {}))}
Sites he has configured: {", ".join(CFG.get("sites", {}))}

Reply with a JSON object and nothing else:
{{"say": "what you say out loud", "action": {{"name": "open_site", "args": {{"name": "tradingview"}}}}}}

Use "action": null when nothing needs doing — small talk, questions, or when you need him to clarify first.
Never invent an action name that is not in the list. If he asks for something you cannot do, say so plainly in "say" and set action to null.
If a request is ambiguous about which file or app he means, ask rather than guessing.
"""


def think(history):
    key = CFG["api_key"]
    model = CFG.get("model") or "gemini-3.6-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
           f":generateContent?key={urllib.parse.quote(key)}")

    body = {
        "system_instruction": {"parts": [{"text": system_prompt()}]},
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in history[-12:]
        ],
        "generationConfig": {
            "maxOutputTokens": 600,
            "responseMimeType": "application/json",
        },
    }

    res = None
    for attempt in (1, 2):
        try:
            res = requests.post(url, json=body, timeout=75)
            break
        except requests.RequestException as e:
            log(f"network attempt {attempt} failed:", e)
            if attempt == 2:
                return {"say": "I cannot reach the model right now.", "action": None}
            time.sleep(1.5)

    if not res.ok:
        log("model error", res.status_code, res.text[:200])
        if res.status_code == 429:
            note = "My daily quota is used up."
        elif res.status_code == 404:
            note = "That model name is wrong. Check the model line in config."
        else:
            note = "The model refused that."
        return {"say": note, "action": None}

    try:
        parts = res.json()["candidates"][0]["content"]["parts"]
        raw = "".join(p.get("text", "") for p in parts)
        return json.loads(raw.replace("```json", "").replace("```", "").strip())
    except (KeyError, IndexError, ValueError) as e:
        log("could not read the reply:", e)
        return {"say": "Something came back garbled.", "action": None}


# ----------------------------------------------------------------------------
# the loop
# ----------------------------------------------------------------------------

YES = ("yes", "yeah", "yep", "go ahead", "do it", "confirm", "sure", "correct", "please do")


def describe(spec, args):
    try:
        return spec.format(**args)
    except (KeyError, IndexError):
        return spec


def run_action(action, ears):
    name = (action or {}).get("name")
    args = (action or {}).get("args") or {}
    entry = ACTIONS.get(name)
    if not entry:
        log("unknown action:", name)
        return None

    fn, destructive, phrasing = entry

    if destructive and CFG.get("confirm_destructive", True):
        what = describe(phrasing or name, args)
        say(f"Are you sure you want me to {what}?")
        ears.drain()
        answer = ears.listen(seconds=8, settle=1.0).lower()
        log("confirmation heard:", repr(answer))
        if not any(y in answer for y in YES):
            return "Left it alone."

    try:
        return fn(**args)
    except TypeError as e:
        log("bad arguments:", e)
        return "I did not understand what to do that to."
    except Exception as e:
        log("action failed:", e)
        return "That did not work."


def conversation(ears):
    """One exchange, starting after the wake word, continuing while he talks."""
    history = []
    say("I'm here.")

    idle = 0
    while idle < 2:
        ears.drain()
        heard = ears.listen(seconds=14)
        if not heard:
            idle += 1
            continue
        idle = 0

        log("Ibans:", heard)
        if any(s in heard.lower() for s in CFG.get("sleep_words", [])):
            say("Going quiet.")
            return

        history.append({"role": "user", "content": heard})
        reply = think(history)
        history.append({"role": "assistant", "content": json.dumps(reply)})

        spoken = reply.get("say", "")
        action = reply.get("action")

        if action:
            say(spoken)
            outcome = run_action(action, ears)
            if outcome:
                say(outcome)
        else:
            say(spoken)


def list_inputs():
    print("\nInput devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            mark = "  <- default" if i == sd.default.device[0] else ""
            print(f"  {i}: {d['name']}{mark}")
    print()


def main():
    global EARS
    say_hello = "David is awake. Say my name when you need me."
    list_inputs()
    ears = Ears()
    EARS = ears

    if "--mic" in sys.argv:
        ears.level_test()
        return
    log("listening for the wake word")
    say(say_hello)

    wake_words = [w.lower() for w in CFG.get("wake_words", ["david"])]

    while True:
        heard = ears.wait_for_wake(wake_words)
        log("woken by:", repr(heard))
        conversation(ears)
        log("back to waiting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
