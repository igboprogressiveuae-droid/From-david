#!/bin/bash
# Double-click to start n8n, the automation server David's run_workflow tool calls.
# Installed once with: mkdir -p ~/Developer/n8n-local && cd ~/Developer/n8n-local && npm install n8n
# Editor: http://127.0.0.1:5678
N8N="$HOME/Developer/n8n-local/node_modules/.bin/n8n"

if [ ! -x "$N8N" ]; then
  echo "n8n is not installed at $N8N — see the install line at the top of this file."
  read -r -p "Press Return to close."
  exit 1
fi

# n8n renames its process to "n8n start", so ask it rather than pgrep for the path
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:5678/healthz; then
  echo "n8n is already running."
  exit 0
fi

# Only this Mac can reach it, and nothing is reported back to n8n's servers.
export N8N_LISTEN_ADDRESS=127.0.0.1 N8N_DIAGNOSTICS_ENABLED=false N8N_PERSONALIZATION_ENABLED=false
# n8n defaults to New York time; use the Mac's own zone instead
export GENERIC_TIMEZONE="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
exec "$N8N" start
