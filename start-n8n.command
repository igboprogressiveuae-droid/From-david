#!/bin/bash
# Double-click to start n8n, the automation server David's run_workflow tool calls.
# Runs through npx, so nothing is installed: the first start downloads n8n (a few
# minutes), later starts reuse npm's cache. Editor: http://127.0.0.1:5678
# Node 24, not the Mac's own Node: n8n 2.x and its isolated-vm need Node 24 or newer.
# n8n is pinned to the version that created ~/.n8n, so its database never migrates by surprise.

# n8n renames its process to "n8n start", so ask it rather than pgrep
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:5678/healthz; then
  echo "n8n is already running."
  exit 0
fi

# Only this Mac can reach it, and nothing is reported back to n8n's servers.
export N8N_LISTEN_ADDRESS=127.0.0.1 N8N_DIAGNOSTICS_ENABLED=false N8N_PERSONALIZATION_ENABLED=false
# n8n defaults to New York time; use the Mac's own zone instead
export GENERIC_TIMEZONE="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
# n8n loads a .env from the folder it starts in; start in its own so it never reads David's keys.
cd "$HOME/.n8n" || exit 1
exec npx -y -p node@24 -p n8n@2.48.7 n8n start
