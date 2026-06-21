#!/usr/bin/env bash
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
MODelfile="$ROOT/examples/Modelfile.customs.live"

{
  echo "FROM qwen2.5-coder:1.5b"
  echo 'SYSTEM """'
  python3 "$ROOT/issue_agent.py" prompt triage \
    --title "Classify GitHub issues for local agent" \
    --body "Use Flight Recorder adaptive feedback." 2>/dev/null | head -80
  echo '"""'
  echo "PARAMETER temperature 0.1"
  echo "PARAMETER num_ctx 8192"
} > "$MODelfile"

echo "Creating customs-1.5b ..."
ollama create customs-1.5b -f "$MODelfile" 2>&1 || ollama create customs-1.5b -f "$MODelfile" --force 2>&1 || true
if ollama list | grep -q customs-1.5b; then
  echo "✓ customs-1.5b ready"
else
  echo "fallback: alias via Modelfile.customs"
  ollama create customs-1.5b -f "$ROOT/examples/Modelfile.customs"
fi