#!/usr/bin/env bash
# Monthly Archivist refresh — re-collect, export, sync bounty hunters, refresh model.
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
LOG="$ROOT/logs/human-review-monthly-$(date +%Y%m%d).log"
mkdir -p "$ROOT/logs"

{
  echo "=== monthly human review refresh $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  issue-agent-human-review collect-deep --limit 25
  PYTHONPATH="$ROOT" python3 "$ROOT/scripts/sync-bounty-hunters.py"
  PYTHONPATH="$ROOT" python3 "$ROOT/scripts/train_reviewer_lora.py"
  issue-agent-human-review stats
} 2>&1 | tee -a "$LOG"