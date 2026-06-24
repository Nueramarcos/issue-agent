#!/usr/bin/env bash
# Notify-zero — stop GitHub failure emails. Failures → Flight Recorder only.
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
SECRETS="${HOME}/.config/cockpit/secrets.env"
AIRPORT="$ROOT/airport.yaml"
AIRPORT_BACKUP="$ROOT/airport.yaml.bak-notify-zero"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"

log() { printf '\033[38;5;141m[notify-zero]\033[0m %s\n' "$*"; }

mkdir -p "$(dirname "$SECRETS")"
touch "$SECRETS"
if grep -q '^export ISSUE_AGENT_GITHUB_QUIET=' "$SECRETS" 2>/dev/null; then
  sed -i 's/^export ISSUE_AGENT_GITHUB_QUIET=.*/export ISSUE_AGENT_GITHUB_QUIET=1/' "$SECRETS"
else
  echo 'export ISSUE_AGENT_GITHUB_QUIET=1' >>"$SECRETS"
fi
log "ISSUE_AGENT_GITHUB_QUIET=1 → failures stay local (no issue comments → no Gmail)"

if [[ ! -f "$AIRPORT_BACKUP" ]]; then
  cp -a "$AIRPORT" "$AIRPORT_BACKUP"
  log "backed up airport.yaml → airport.yaml.bak-notify-zero"
fi

cat >"$AIRPORT" <<'YAML'
# Airport — notify-zero profile: demo lane only until merge yield > 70%
enabled: true
park_minutes: 5
local_first: true
wait_for_checks: true
factory_interval_secs: 600
ci_heal_interval_secs: 900
factory_max_per_repo: 1
ci_heal_max: 1
failure_skip_hours: 6
max_concurrent_aider: 1

lanes:
  - repo: Nueramarcos/agent-habitat-demo
    kind: github
    interval: 300
    collect_max: 1
    fix_max: 1
YAML

log "airport → demo-only lane (forge/orion/nexus/vertex/roam paused)"

if command -v issue-agent-always-on >/dev/null 2>&1; then
  issue-agent-airport-restart 2>/dev/null || systemctl --user restart issue-agent-airport.service 2>/dev/null || true
  log "airport restarted"
fi

log ""
log "═══ Notify-zero active ═══"
log "  ✓ failure comments suppressed on GitHub"
log "  ✓ fleet narrowed to agent-habitat-demo"
log "  ✓ failures still logged: ~/issue-agent/flight-recorder/"
log ""
log "Restore fleet: cp $AIRPORT_BACKUP $AIRPORT && unset ISSUE_AGENT_GITHUB_QUIET"
log "GitHub UI: Settings → Notifications → Actions → Only notify for failed workflows on repos you participate in (or off)"