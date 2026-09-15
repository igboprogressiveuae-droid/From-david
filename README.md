# David
An AI voice assistant that runs your Mac. You talk, Gemini answers out loud, and it can open apps and sites, read your screen and click things, check Gmail, journal to Notion, check the news, and research what to buy.

Originally built for Windows by Ibans. This version runs on macOS.

## Set up
**Easiest:** double-click `start-david.command`. The first time it installs what David needs, opens `.env` for your Gemini key ([get one here](https://aistudio.google.com/apikey)), then checks everything and starts him.

**By hand:**
1. Python 3.10 or newer: `brew install python@3.12`
2. In this folder:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. `cp .env.example .env`, then paste your Gemini key into `.env`
4. `python david_live.py --check` — checks your key, models, microphone, permissions and browsers, and says how to fix anything wrong
5. `python david_live.py --ui`

## Permissions
Turn these on for the app you run David from (Terminal, iTerm or VS Code) in **System Settings → Privacy & Security**, then restart David:

| Permission | What it's for |
|---|---|
| Microphone | hearing you |
| Screen Recording | reading the screen |
| Accessibility | clicking, typing, moving windows |
| Automation → System Events, Finder | asked once, the first time it's needed |

If one is missing, David says which — he won't pretend a click worked.

**Use headphones.** There is no echo cancellation, so on speakers David can hear himself.

## Make it yours
- `prompt.md` — how David behaves, and who he works for
- `config.json` — your name, apps, sites, browsers and folders. No keys here: this file is on GitHub.
- Say "remember that…" and he keeps it in `memory.json`

## Tests
```
python -m unittest discover -s tests
```

`david.py` is the older, Windows-only version with offline speech recognition. It is not used.
