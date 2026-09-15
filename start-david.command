#!/bin/bash
# Double-click to start David. The first run sets everything up.
# macOS permissions (Microphone, Screen Recording, Accessibility) go to Terminal.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: installing what David needs. This takes a minute or two."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || {
    echo "Setup failed. Is Python 3.10 or newer installed? (brew install python@3.12)"
    read -r -p "Press Return to close."
    exit 1
  }
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Paste your Gemini key into .env (opening it now), save, then double-click this again."
  open -e .env
  read -r -p "Press Return to close."
  exit 1
fi

.venv/bin/python david_live.py --check || { read -r -p "Press Return to close."; exit 1; }
.venv/bin/python david_live.py --ui
