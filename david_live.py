"""
David, live — on macOS.
Created by Ibans; ported to the Mac.

One websocket to Google. Your microphone streams up, his voice streams back,
and he can act on your machine mid-sentence. No local speech recognition, no
local text to speech, no waiting for a round trip after you finish talking.

    python david_live.py --ui           voice, with the interface
    python david_live.py --no-voice     only Telegram and background jobs
    python david_live.py --check        is everything set up?

Needs:  pip install -r requirements.txt, and GEMINI_API_KEY in .env
Everything that touches the operating system itself lives in macos.py.
"""

import array
import asyncio
import base64
import json
import os
import queue
import re
import sys
import urllib.parse
from datetime import datetime

import threading
import time


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
import sounddevice as sd
import websockets

import macos

from tools import *  # noqa: F401,F403 — the tool hub
import dashboard_api
import wake
import tools
from tools import (_app_path, _log_args,  # noqa: F401
                   _open_web, _update_config)

UI = "--ui" in sys.argv or "--hud" in sys.argv

MIC_RATE = 16000        # what the API expects going up
SPEAKER_RATE = 24000    # what it sends back
CHUNK = 1024

MODEL = "gemini-3.1-flash-live-preview"
DEBUG = "--debug" in sys.argv
SAVE_AUDIO = "--save-audio" in sys.argv   # each reply also goes to last_reply.wav, to judge the sound itself
WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")



# ----------------------------------------------------------------------------
# the session
# ----------------------------------------------------------------------------

def host_ok(headers):
    """A rebound domain name pointing at 127.0.0.1 still carries its own Host —
       without this, any website could read your notes and jobs from here."""
    return headers.get("Host", "") in ("127.0.0.1:8765", "localhost:8765")


def dashboard_post_ok(headers):
    """The chat box can run any tool, so its endpoints take only our own page's requests.
       Another site can't send JSON here without a preflight we never answer."""
    return (headers.get("Content-Type", "").startswith("application/json") and host_ok(headers)
            and headers.get("Origin") in (None, "http://127.0.0.1:8765", "http://localhost:8765"))


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
            "turns": [],     # the spoken conversation, for the interface's chat column
        }
        # micLevel changes on basically every audio callback, so serve()
        # would otherwise re-encode and re-broadcast the whole state —
        # panel data and all — twenty times a second forever. This flag
        # means a send only happens when something actually changed.
        self._dirty = True

    def set(self, **kw):
        self.state.update(kw)
        self._dirty = True

    def note_turn(self, who, text):
        self.state["turns"] = (self.state["turns"] + [
            {"who": who, "text": text.strip()[:4000], "at": datetime.now().strftime("%I:%M %p")}])[-30:]
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
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

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
                if not host_ok(self.headers):
                    return self._json({"ok": False, "error": "forbidden"}, 403)
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
                answer = dashboard_api.handle("GET", self.path)
                if answer:
                    return self._json(answer[1], answer[0])
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
                        tools.NOTIFY(f"Notion just connected, to the page called {title}.")
                    except Exception:
                        pass
                    return self._json({"ok": True, "page_title": title})
                if self.path == "/api/notion/disconnect":
                    if not _HAVE_OAUTH_NOTION:
                        return self._json({"ok": False, "error": "oauth_notion.py is missing"}, 500)
                    result = oauth_notion.disconnect()
                    try:
                        tools.NOTIFY("Notion was just disconnected.")
                    except Exception:
                        pass
                    return self._json({"ok": True, "result": result})
                if not dashboard_post_ok(self.headers):
                    return self._json({"ok": False, "error": "forbidden"}, 403)
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    return self._json({"ok": False, "error": "bad request"}, 400)
                answer = dashboard_api.handle("POST", self.path, body)
                if answer:
                    return self._json(answer[1], answer[0])
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

        app = CFG.get("browsers", {}).get(CFG.get("ui_browser", "edge"))
        if app and app != "Safari" and _app_path(app):
            macos.open_app_window(app, page)
        else:
            _open_web(page)

        # give it a moment to appear, then park it on the interface screen —
        # only when there is a spare one; on a single screen it would cover his work
        await asyncio.sleep(2.5)
        screens = monitors()
        idx = int(CFG.get("ui_monitor", 2)) - 1
        if len(screens) > 1 and 0 <= idx < len(screens):
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


def _resample(pcm, from_rate, to_rate):
    """Raw 16-bit mono sound from one sample rate to another, by linear
       interpolation.

       Google sends 24000 Hz; Mac speakers run at 48000. Leaving that gap to
       PortAudio's own conversion came out distorted — like a broken speaker —
       while the same sound converted here, or by afplay, is clean.

       Each chunk is converted on its own. At an exact ratio (48000 from 24000)
       chunk edges line up perfectly; at an odd ratio there may be a faint tick
       between chunks, still far better than what PortAudio was producing."""
    if to_rate == from_rate:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    if not src:
        return b""
    # whole-number arithmetic for the count: dividing in decimals lost a
    # sample on every chunk, which over a long reply is a drifting tick
    count = len(src) * to_rate // from_rate
    step = from_rate / to_rate
    out = array.array("h")
    for i in range(count):
        at = i * step
        j = int(at)
        here = src[j]
        following = src[j + 1] if j + 1 < len(src) else here
        out.append(int(here + (following - here) * (at - j)))
    return out.tobytes()


def _write_wav(path, pcm, rate=SPEAKER_RATE):
    """Raw 16-bit mono sound, exactly as Google sent it, as a playable .wav."""
    import wave
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm)


def _output_device_name():
    try:
        return sd.query_devices(kind="output")["name"]
    except Exception:
        return ""


def _interruption_allowed(setting=None, output_name=None):
    """Talking over David means passing the mic through while he speaks. On
       built-in speakers, right beside the built-in mic and with no echo
       cancellation, his own voice counted as an interruption and cut him off
       mid-word. "auto" allows it only when the sound goes somewhere else —
       headphones, AirPods, a headset. true or false in config forces it."""
    setting = CFG.get("allow_interruption", "auto") if setting is None else setting
    if setting != "auto":
        return bool(setting)
    name = (_output_device_name() if output_name is None else output_name).lower()
    return bool(name) and "speakers" not in name


class GoToSleep(Exception):
    """Ends a session on purpose: back to listening for the wake phrase."""


class SilenceWatch:
    """Notices a microphone sending nothing but exact digital zeros. That is
       what macOS hands over when Microphone permission is off — no error, just
       silence, and David would sit there looking like he's ignoring you."""

    def __init__(self, seconds=4.0):
        self.seconds = seconds
        self.started = None
        self.heard = False
        self.warned = False

    def feed(self, level, now):
        """True exactly once, when the mic has been dead silent long enough."""
        if self.heard or self.warned:
            return False
        if level > 0:
            self.heard = True
            return False
        if self.started is None:
            self.started = now
        if now - self.started >= self.seconds:
            self.warned = True
            return True
        return False


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
        self.gate = Gate()               # destructive actions wait here for a real yes
        self.silence = SilenceWatch()
        self.can_interrupt = True        # decided per session, from where the sound goes
        self.audio_mime = ""             # the format Google says his voice arrives in
        self.saved_reply = b""           # --save-audio: this reply's sound so far
        self.underflows = 0              # times the speaker ran dry mid-sound
        self.last_active = time.time()   # last time anyone spoke or a tool ran — for falling asleep
        self.just_woke = False           # say "I'm here" when the next session starts

    def _note_audio_format(self, mime):
        """Log the voice's audio format once. If Google sends a different sample
           rate from the one the speaker is opened at, the voice plays warped."""
        if not mime or mime == self.audio_mime:
            return
        self.audio_mime = mime
        log("David's voice arrives as", mime)
        rate = re.search(r"rate=(\d+)", mime)
        if rate and int(rate.group(1)) != SPEAKER_RATE:
            log(f"  but the speaker plays at {SPEAKER_RATE} Hz — his voice will sound wrong")

    def mic_callback(self, indata, frames, t, status):
        import array as _a, math as _m

        raw = bytes(indata)
        sm = _a.array("h")
        sm.frombytes(raw[: len(raw) // 2 * 2])
        level = int(_m.sqrt(sum(x * x for x in sm) / len(sm))) if sm else 0

        if self.silence.feed(level, time.time()):
            log(macos.NO_MIC)
            tools.NOTIFY(macos.NO_MIC)          # he can still hear David, so David says it

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
            if not self.can_interrupt:
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
                    self.last_active = time.time()

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
                    if SAVE_AUDIO and self.saved_reply:
                        _write_wav(os.path.join(HERE, "last_reply.wav"), self.saved_reply)
                        self.saved_reply = b""
                    HUD.set(status="idle", outLevel=0,
                            lastLine=(self.said or self.you_said).strip()[:4000])
                    if self.you_said.strip():
                        print(f"    you: {self.you_said.strip()}", flush=True)
                        HUD.note_turn("you", self.you_said)
                        BRAIN.log_turn("voice", CFG.get("owner_name") or "you", self.you_said)
                        # Kept separately from the buffer below so a destructive
                        # action can check what he actually just said, rather
                        # than trusting the model's word for it.
                        self.gate.heard(self.you_said.strip())
                    if self.said.strip():
                        print(f"  David: {self.said.strip()}", flush=True)
                        HUD.note_turn("david", self.said)
                        BRAIN.log_turn("voice", "David", self.said)
                    self.you_said = self.said = ""
                    self.spoke_this_turn = False
                for part in (server.get("modelTurn") or {}).get("parts", []):
                    blob = part.get("inlineData")
                    if blob and blob.get("data"):
                        audio = base64.b64decode(blob["data"])
                        self._note_audio_format(blob.get("mimeType", ""))
                        self.out.put(audio)
                        if SAVE_AUDIO:
                            self.saved_reply += audio
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

    async def run_one(self, call):
        """Run a single tool and hand back its answer."""
        name, args = call.get("name"), call.get("args") or {}

        if name in DESTRUCTIVE:
            gate = self.gate.check(name, args)
            if gate is not None:
                HUD.note_action(f"confirm? {name}")
                return {"id": call.get("id"), "name": name, "response": {"result": gate}}

        # The model sometimes asks for the same thing several times over
        # while it waits. That is how you end up with six Gmail windows.
        # Small input actions only count as a repeat with identical details
        # inside two seconds — otherwise a second "scroll down" was swallowed.
        if name in REPEATABLE:
            fingerprint, window = name + json.dumps(args, sort_keys=True), 2.0
        else:
            fingerprint = name if name in NEEDS_THE_SCREEN else name + json.dumps(args, sort_keys=True)
            window = float(CFG.get("duplicate_window", 25))
        now = time.time()
        if now - self.recent_calls.get(fingerprint, 0) < window:
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
            if self.screen_lock.locked() or tools.SCREEN.locked():
                log(f"skipping {name} — already busy with {self.busy_with}")
                HUD.set(status="thinking")
                return {"id": call.get("id"), "name": name, "response": {
                    "result": f"still working on {self.busy_with} — wait for that to come back first"}}

            if name in SLOW_JOBS and not self.spoke_this_turn:
                ack_out_loud()

            async with self.screen_lock:
                self.busy_with = name
                try:
                    result = await asyncio.to_thread(lambda: tools.with_screen(fn, args))
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

        self.last_active = time.time()
        responses = []
        if quick:
            responses += list(await asyncio.gather(*[self.run_one(c) for c in quick]))
        for c in screen:
            responses.append(await self.run_one(c))

        self.last_active = time.time()
        await ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))

    def play_forever(self):
        """Runs on its own thread, feeding whatever comes back to the speakers.
           Holds the microphone shut while anything is playing."""
        # Play at the speaker's own rate and convert on the way in — see
        # _resample. Asking PortAudio for 24000 Hz on a 48000 Hz speaker
        # sounded broken, while the same sound converted here is clean.
        try:
            rate = int(sd.query_devices(kind="output")["default_samplerate"])
        except Exception:
            rate = SPEAKER_RATE
        if rate != SPEAKER_RATE:
            log(f"playing at the speaker's own {rate} Hz, converting up from {SPEAKER_RATE} Hz")

        # "high" latency is still only ~20ms on a Mac, but it gives the speaker a
        # real buffer — at the default, uneven network chunks came out as crackle.
        with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                latency="high") as speaker:
            while True:
                chunk = self.out.get()
                starting = not self.speaking.is_set()
                self.speaking.set()
                if starting:
                    # a new answer: let a little more of it arrive before playing,
                    # so the gaps between network chunks don't become stutters
                    time.sleep(float(CFG.get("playback_prebuffer_ms", 150)) / 1000)

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
                if speaker.write(_resample(chunk, SPEAKER_RATE, rate)):
                    # the speaker ran dry before this chunk arrived — heard as a crackle
                    self.underflows += 1
                    if self.underflows in (1, 10, 100) or self.underflows % 500 == 0:
                        log(f"speaker ran out of sound {self.underflows} time(s) so far")

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
                    "turns": [{"role": "user", "parts": [{"text": f"[system] {text} Tell them now, briefly."}]}],
                    "turnComplete": True,
                }
            }))

    async def watch_for_sleep(self):
        """With a wake phrase set: end the session after a quiet spell, or when asked
           to sleep — never while David is talking or a tool is still running."""
        minutes = float(CFG.get("sleep_after_minutes") or 0)
        while True:
            await asyncio.sleep(2)
            if self.speaking.is_set() or not self.out.empty():
                self.last_active = time.time()
                continue
            if tools.SLEEP_NOW.is_set() or (minutes and time.time() - self.last_active > minutes * 60):
                tools.SLEEP_NOW.clear()
                raise GoToSleep()

    async def run(self):
        self.loop = asyncio.get_running_loop()

        tools.NOTIFY = lambda text: self.loop.call_soon_threadsafe(self.notices.put_nowait, text)
        url = f"{WS_URL}?key={urllib.parse.quote(CFG['api_key'])}"

        # build_system() reads the screens and disks, which takes a moment,
        # and used to run synchronously right here, blocking the
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

            self.last_active = time.time()
            if self.just_woke and self.resume_handle:
                self.just_woke = False
                log("awake — listening")
                await ws.send(json.dumps({
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text":
                            "[system] He just woke you with the wake phrase. Say only: 'I'm here.' "
                            "Do not call any tool."}]}],
                        "turnComplete": True,
                    }
                }))
            elif self.resume_handle:
                log("reconnected — carry on")
            else:
                self.just_woke = False
                log("connected — just talk, and say David when you want him")
                await ws.send(json.dumps({
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text":
                            "[system startup] Greet him out loud with just the words "
                            "'David online.' Speak it, do not call any tool."}]}],
                        "turnComplete": True,
                    }
                }))

            self.can_interrupt = _interruption_allowed()
            if not self.can_interrupt and not self.resume_handle:
                log("sound is going to the laptop speakers, so David stops listening while he "
                    "talks — his own voice was cutting him off. Use headphones to interrupt him.")
            device = CFG.get("input_device")
            with sd.RawInputStream(samplerate=MIC_RATE, blocksize=CHUNK, dtype="int16",
                                   channels=1, callback=self.mic_callback,
                                   device=device if device is not None else None):
                jobs = [self.send_audio(ws), self.receive(ws), self.announce(ws)]
                if CFG.get("wake_phrase"):
                    jobs.append(self.watch_for_sleep())
                tasks = [asyncio.create_task(j) for j in jobs]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    # otherwise a leftover announce() would swallow the next reminder
                    for t in tasks:
                        t.cancel()


async def wait_for_wake(live, listener):
    """Asleep: the microphone is heard only by this Mac, until the wake phrase —
       or until a reminder, timer or job has something to say."""
    live.loop = asyncio.get_running_loop()
    tools.NOTIFY = lambda text: live.loop.call_soon_threadsafe(live.notices.put_nowait, text)
    HUD.set(status="asleep", connected=False, micArmed=False, wakePhrase=CFG.get("wake_phrase", ""))
    log(f"asleep — say \"{CFG.get('wake_phrase')}\" to wake me")
    chunks, silence = queue.Queue(), SilenceWatch()
    listener.reset()

    def listen():
        while True:
            try:
                raw = chunks.get(timeout=0.5)
            except queue.Empty:
                raw = b""
            if raw and silence.feed(1 if any(raw) else 0, time.time()):
                log(macos.NO_MIC)
                tools.NOTIFY(macos.NO_MIC)
            if raw and listener.heard(raw):
                return "phrase"
            if not live.notices.empty():
                return "notice"

    device = CFG.get("input_device")
    with sd.RawInputStream(samplerate=MIC_RATE, blocksize=CHUNK, dtype="int16", channels=1,
                           callback=lambda indata, *_: chunks.put(bytes(indata)),
                           device=device if device is not None else None):
        why = await asyncio.to_thread(listen)
    live.just_woke = why == "phrase"
    HUD.set(status="idle")
    log("woke up" if why == "phrase" else "woke up to say something")


def _refusal_kind(text):
    """How to treat a refusal from Google, since several arrive as the same
       kind of websocket error and only some are worth giving up over.

       'key'   — the key is wrong, and no amount of retrying fixes that
       'model' — the live model in config.json does not exist
       'stale' — the saved resumption handle points at a session Google has
                 already dropped. It says "not found" like a bad model does,
                 but the fix is to forget the handle and start a fresh
                 session, not to stop.
       'retry' — anything else: the network, or a passing refusal
    """
    t = (text or "").lower()
    if "api key" in t:
        return "key"
    if "session not found" in t:
        return "stale"
    if "not found" in t or "1007" in t:
        return "model"
    return "retry"


def _wake_listener():
    """The wake-phrase listener, or None to listen all the time (no phrase set, or Vosk missing)."""
    phrase = CFG.get("wake_phrase")
    if not phrase:
        return None
    problem = wake.available()
    if problem:
        log(f"no wake phrase — {problem}. Listening all the time instead.")
        return None
    try:
        return wake.Listener(phrase, MIC_RATE)
    except Exception as e:
        log(f"the wake-phrase listener failed ({e}) — listening all the time instead")
        return None


async def stay_connected(live):
    """Sessions have a time limit and connections drop. Keep coming back.
       With a wake phrase, he sleeps between conversations."""
    listener = await asyncio.to_thread(_wake_listener)
    if listener is None:
        CFG["wake_phrase"] = ""        # watch_for_sleep and go_to_sleep look at this
    asleep = listener is not None
    wait = 2
    while True:
        if asleep:
            await wait_for_wake(live, listener)
            asleep = False
        try:
            await live.run()
            HUD.set(connected=False)
            wait = 2
        except GoToSleep:
            HUD.set(connected=False)
            asleep = True
        except Reconnect:
            wait = 2
            await asyncio.sleep(0.3)
        except (TimeoutError, asyncio.TimeoutError):
            log(f"the connection timed out — trying again in {wait}s")
            await asyncio.sleep(wait)
            wait = min(30, wait * 2)
        except websockets.exceptions.WebSocketException as e:
            text = str(e)
            kind = _refusal_kind(text)
            if kind == "key":
                print(f"\nGoogle refused the key: {text[:160]}")
                print("Check GEMINI_API_KEY in .env.")
                return
            if kind == "model":
                print(f"\nConnection refused: {e}")
                print("Try a different live_model in config.json.")
                return
            if kind == "stale":
                # Carrying the dead handle into the next attempt would be
                # refused exactly the same way, forever.
                live.resume_handle = None
                log(f"the saved session had already expired — starting a fresh one in {wait}s")
            else:
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


def _mic_peak(seconds):
    """The loudest sample the microphone delivers over a few seconds."""
    import array
    peak = 0
    with sd.RawInputStream(samplerate=MIC_RATE, blocksize=CHUNK, dtype="int16", channels=1,
                           device=CFG.get("input_device")) as stream:
        end = time.time() + seconds
        while time.time() < end:
            data, _overflowed = stream.read(CHUNK)
            samples = array.array("h")
            samples.frombytes(bytes(data))
            if samples:
                peak = max(peak, max(abs(s) for s in samples))
    return peak


def _check_gemini_model(key, model):
    """(ok, fix) for one model name against the Gemini API."""
    try:
        res = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                           headers={"x-goog-api-key": key}, timeout=15)
    except requests.RequestException as e:
        return False, f"Could not reach Google: {e}"
    if res.ok:
        return True, ""
    if res.status_code == 404:
        return False, f"Google has no model called {model} — change it in config.json"
    if res.status_code in (400, 401, 403):
        return False, "Google refused the key — check GEMINI_API_KEY in .env"
    return False, f"Google answered {res.status_code}"


def doctor():
    """python david_live.py --check — everything David needs, in plain words."""
    problems = 0

    def report(ok, what, fix="", required=True):
        nonlocal problems
        print(f"  {'ok  ' if ok else ('FIX ' if required else 'note')} {what}")
        if not ok and fix:
            print(f"       {fix}")
        if not ok and required:
            problems += 1

    print("\nChecking David's setup\n")
    report(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}",
           "Install a newer Python: brew install python@3.12")

    key = CFG.get("api_key")
    if not key:
        report(False, "Gemini key",
               "Copy .env.example to .env and paste a key from https://aistudio.google.com/apikey")
    else:
        for label, model in (("Gemini voice model", CFG.get("live_model") or MODEL),
                             ("Gemini text and vision model", CFG.get("model") or "gemini-3.6-flash")):
            ok, fix = _check_gemini_model(key, model)
            report(ok, f"{label}: {model}", fix)

    groq = CFG.get("groq_key")
    if not groq:
        report(False, "Groq backup key (optional)",
               "Without it, screen reading and search stop when Gemini's free quota runs out", False)
    else:
        try:
            res = requests.get("https://api.groq.com/openai/v1/models",
                               headers={"Authorization": f"Bearer {groq}"}, timeout=15)
            ids = {m.get("id") for m in res.json().get("data", [])} if res.ok else set()
        except (requests.RequestException, ValueError):
            res, ids = None, set()
        report(bool(res is not None and res.ok), "Groq backup key",
               "Groq refused it — check GROQ_API_KEY in .env", False)
        vision = CFG.get("groq_vision_model") or "qwen/qwen3.6-27b"
        if ids:
            report(vision in ids, f"Groq vision model: {vision}",
                   "Groq no longer offers it — change groq_vision_model in config.json", False)

    print("\n  Say something for a couple of seconds...")
    try:
        peak = _mic_peak(2.5)
        if peak == 0:
            report(False, "Microphone", macos.NO_MIC[0].upper() + macos.NO_MIC[1:])
        elif peak < 300:
            report(False, f"Microphone works but is very quiet (peak {peak})",
                   "Raise the input volume in System Settings → Sound → Input", False)
        else:
            report(True, f"Microphone (peak {peak})")
    except Exception as e:
        report(False, "Microphone", f"Could not open it: {e}")

    where = "System Settings → Privacy & Security → {} → turn on the app you run David from, then restart it"
    report(macos.can_see_screen(), "Screen Recording (reading the screen)",
           where.format("Screen Recording"), False)
    report(macos.can_control(), "Accessibility (clicking, typing, moving windows)",
           where.format("Accessibility"), False)

    for role in ("work_browser", "ui_browser"):
        app = CFG.get("browsers", {}).get(CFG.get(role, ""), CFG.get(role, ""))
        report(bool(app and _app_path(app)), f"{role.replace('_', ' ')}: {app}",
               "Not installed — change it in config.json", False)

    report(_HAVE_OAUTH_GMAIL and oauth_gmail.is_connected(), "Gmail connected (optional)",
           "Without it, David reads Gmail off the screen", False)
    report(_HAVE_OAUTH_NOTION and oauth_notion.is_connected(), "Notion connected (optional)",
           "Connect it from the dashboard to journal by voice", False)

    phrase = CFG.get("wake_phrase")
    if phrase:
        problem = wake.available()
        report(problem is None, f'Wake phrase: "{phrase}" — listened for on this Mac, offline',
               f"{problem} — until then David listens all the time", False)
        if problem is None and not os.path.isdir(wake.MODEL_DIR):
            print("       The speech model (about 130 MB) downloads the first time David starts.")
    else:
        report(True, "No wake phrase — David listens all the time")

    import engines
    import telegram
    brain = engines.current()
    problem = engines.missing_key(brain)
    report(problem is None, f"Thinking brain: {brain} ({engines.model_for(brain)})",
           f"{problem} — or set \"brain\" back to gemini in config.json")
    report(True, f"Second Brain notes: {BRAIN.root} ({len(BRAIN.facts())} facts)")
    if telegram.enabled():
        try:
            name = telegram._call("getMe")["username"]
            report(bool(telegram.owner_id()), f"Telegram bot @{name}",
                   "Message the bot once — it replies with the TELEGRAM_OWNER_ID to put in .env", False)
        except ConnectionError as e:
            report(False, "Telegram bot", f"Telegram refused the token: {e}", False)
    else:
        report(False, "Telegram (optional)",
               "Set TELEGRAM_BOT_TOKEN in .env to talk to David from your phone", False)

    if problems:
        print(f"\n{problems} thing{'s' if problems != 1 else ''} to fix before David can run.\n")
        return 1
    print("\nReady. Start David with: python david_live.py --ui\n")
    return 0


def main():
    if "--check" in sys.argv:
        sys.exit(doctor())

    import jobs
    import telegram
    telegram.start()
    jobs.start()
    if "--no-voice" in sys.argv:
        # No microphone and no interface: just Telegram and jobs, e.g. while away.
        tools.NOTIFY = telegram.send_owner
        log("David is running without voice" +
            (" — talk to him on Telegram" if telegram.enabled() else " — only jobs will run") +
            ". Ctrl+C to stop.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    live = Live()
    threading.Thread(target=live.play_forever, daemon=True).start()
    try:
        asyncio.run(everything(live))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
