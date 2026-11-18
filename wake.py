"""
The wake phrase. David sleeps until he hears "wake_phrase" from config.json
(say it as one sentence: "wake up David"). Listening for it happens on this Mac,
offline, with Vosk — nothing reaches Google until the phrase is heard.

The English model (about 40 MB) downloads once into vosk-model/ (not on GitHub).
"""

import importlib.util
import json
import os
import re
import zipfile

import requests

from tools import CFG, HERE, log

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_DIR = os.path.join(HERE, "vosk-model")


def normalise(text):
    return " ".join(re.sub(r"[^a-z' ]", " ", str(text).lower()).split())


def ensure_model():
    """The model folder, downloading it the first time."""
    if os.path.isdir(MODEL_DIR) and os.listdir(MODEL_DIR):
        return MODEL_DIR
    log("downloading the wake-phrase model (about 40 MB, once)…")
    tmp = MODEL_DIR + ".zip"
    with requests.get(MODEL_URL, stream=True, timeout=60) as res:
        res.raise_for_status()
        with open(tmp, "wb") as f:
            for block in res.iter_content(1 << 16):
                f.write(block)
    with zipfile.ZipFile(tmp) as z:
        top = z.namelist()[0].split("/")[0]
        z.extractall(HERE)            # extractall refuses paths that climb out of HERE
    os.replace(os.path.join(HERE, top), MODEL_DIR)
    os.remove(tmp)
    return MODEL_DIR


class Listener:
    """Feed it 16-bit mono microphone audio; heard() says when the phrase was spoken."""

    def __init__(self, phrase, rate=16000, make=None):
        self.phrase = normalise(phrase)
        if make is None:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            model = Model(ensure_model())     # the slow part — loaded once
            # Open dictation, not a list of allowed phrases: limited to "the phrase or
            # anything else", Vosk rounds "wake up Daniel" and "hey wake up" up to the phrase.
            make = lambda: KaldiRecognizer(model, rate)  # noqa: E731
        self.make = make
        self.rec = make()

    def heard(self, pcm):
        # Finished sentences only: a half-heard "wake up dav…" can still become "Daniel".
        if not self.rec.AcceptWaveform(pcm):
            return False
        text = normalise(json.loads(self.rec.Result()).get("text", ""))
        if self.phrase and f" {self.phrase} " in f" {text} ":
            self.reset()
            return True
        if text and CFG.get("wake_debug"):     # off by default: it prints what the room says
            log(f'(asleep) heard "{text}"')
        return False

    def reset(self):
        """Start fresh: Vosk's own Reset() keeps tuning to the last voice it heard,
           which turned a second speaker's "wake up David" into "we got David"."""
        self.rec = self.make()


def available():
    """None if the wake phrase can work, else what's missing, in words."""
    if importlib.util.find_spec("vosk") is None:
        return "the vosk package isn't installed: .venv/bin/pip install vosk"
    return None
