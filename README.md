# David
An AI voice assistant that runs your Mac. You talk, Gemini answers out loud, and it can open apps and sites, read your screen and click things, check Gmail, journal to Notion, check the news, and research what to buy.

He also keeps a **Second Brain** of Markdown notes, can think with **Gemini, Claude or any OpenRouter model**, answers you on **Telegram**, and runs **background jobs** on a schedule. The design borrows from [Javis OS](https://github.com/blogminhquy/javis-os).

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

## How he's built

```
           voice (Gemini Live)      Telegram         background jobs
           david_live.py            telegram.py      jobs.py
                 │                       │                │
                 │  ask_brain            └──── engines.py ┘   the thinking brain:
                 └──────────────────────────► gemini | claude | openrouter
                 │                                 │
                 └────────────► tools.py ◄─────────┘   every action, one place,
                                   │                   one yes-first rule
                                brain.py               Markdown notes in brain/
```

- **Voice** is always Gemini Live — the ears and the mouth. Say "think it over" or ask something hard and he hands it to the thinking brain with `ask_brain`.
- **The thinking brain** is whatever `"brain"` says in `config.json`. Say "switch to Claude", or send `/brain claude` on Telegram. Claude needs `ANTHROPIC_API_KEY` and `pip install anthropic`; OpenRouter needs `OPENROUTER_API_KEY`.
- **The Second Brain** is the `brain/` folder (not on GitHub): `memory/` for facts, `wiki/` for longer pages he writes, `conversations/` for every day's chat, and `archive.md` for anything replaced. Open it in Obsidian. Point `"brain_dir"` in `config.json` somewhere else to keep it in your own vault.
- **Telegram:** make a bot with @BotFather, put `TELEGRAM_BOT_TOKEN` in `.env`, start David, message the bot, and it tells you the `TELEGRAM_OWNER_ID` to add. He answers no one else.
- **Jobs:** "every morning at 7, give me the gold news" or "every 30 minutes, tell me if Bitcoin drops under 90,000". He asks before scheduling, speaks the result and sends it on Telegram, and stays quiet when there's nothing to say. A job can only look things up (news, research, weather, his notes) and write to his notes — it never touches the screen, sends anything or deletes anything.
- `python david_live.py --no-voice` runs only Telegram and jobs — no microphone needed.

## Make it yours
- `prompt.md` — how David behaves, and who he works for
- `config.json` — your name, apps, sites, browsers and folders. No keys here: this file is on GitHub.
- Say "remember that…" and he keeps it in `brain/memory/`

## Tests
```
python -m unittest discover -s tests
```

`david.py` is the older, Windows-only version with offline speech recognition. It is not used.
