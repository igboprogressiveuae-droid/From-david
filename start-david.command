#!/bin/bash
# Double-click to start David. The first run sets everything up.
# macOS permissions (Microphone, Screen Recording, Accessibility) go to Terminal.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: installing what David needs. This takes a minute or two."
  # OpenJarvis supports Python 3.10 to 3.13, so prefer those over a newer python3
  PY=$(command -v python3.12 || command -v python3.13 || command -v python3)
  "$PY" -m venv .venv && .venv/bin/pip install -q -r requirements.txt || {
    echo "Setup failed. Is Python 3.10 or newer installed? (brew install python@3.12)"
    read -r -p "Press Return to close."
    exit 1
  }
  touch .venv/.requirements-stamp
fi

# An older .venv (from before OpenJarvis) has to be topped up when requirements.txt changes
if [ requirements.txt -nt .venv/.requirements-stamp ]; then
  echo "Updating what David needs..."
  .venv/bin/pip install -q -r requirements.txt || {
    echo "Update failed. If .venv is Python 3.14 or newer, delete the .venv folder and start David again."
    read -r -p "Press Return to close."
    exit 1
  }
  touch .venv/.requirements-stamp
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
