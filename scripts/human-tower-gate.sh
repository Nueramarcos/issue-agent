#!/usr/bin/env bash
# Run Human Tower on a workspace — exits 0 on approve, 1 on reject.
set -euo pipefail
REPO="${1:-}"
WS="${2:-}"
SUMMARY="${3:-}"
MODEL="${HUMAN_TOWER_MODEL:-customs-reviewer-ft-1.5b}"
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

[[ -n "$REPO" && -n "$WS" && -d "$WS" ]] || { echo "usage: human-tower-gate.sh <owner/repo> <workspace> [issue-summary]"; exit 2; }

python3 - "$REPO" "$WS" "$SUMMARY" "$MODEL" <<'PY'
import json, sys
from pathlib import Path
from human_reviewer.gate import human_tower_review
from human_reviewer.record import append_human_tower_record

repo, ws, summary, model = sys.argv[1:5]
v = human_tower_review(Path(ws), repo, issue_summary=summary, model=model)
append_human_tower_record(v, repo=repo, issue_summary=summary)
print(json.dumps({"passed": v.passed, "confidence": v.confidence, "comment": v.review_comment[:500], "model": v.model}))
sys.exit(0 if v.passed else 1)
PY