#!/usr/bin/env bash
# Run every 6h via cron: 0 */6 * * * ~/issue-agent/scripts/vision-loop-cron.sh
set -euo pipefail
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
export ISSUE_AGENT_ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
source "$HOME/.config/cockpit/secrets.env" 2>/dev/null || true

issue-agent-loop --rounds 1 --triage --limit 50
issue-agent-lora export --with-gh
issue-agent prompt goal >/dev/null && echo "prompt goal OK"
# Monthly Human Reviewer refresh (1st of month)
if [[ "$(date +%d)" == "01" ]]; then
  bash "$ISSUE_AGENT_ROOT/scripts/human-review-monthly.sh" || true
fi