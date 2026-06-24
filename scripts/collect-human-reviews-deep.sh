#!/usr/bin/env bash
# Overnight Archivist — deep collect across all corpus sources.
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
LOG="$ROOT/logs/human-review-collect-$(date +%Y%m%d-%H%M%S).log"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
mkdir -p "$ROOT/logs"

{
  echo "=== Human Reviewer deep collect $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  issue-agent-human-review stats
  echo ""
  issue-agent-human-review collect-deep --limit 35 2>&1
  echo ""
  issue-agent-human-review stats
} | tee "$LOG"

echo "log: $LOG"