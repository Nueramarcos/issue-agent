#!/usr/bin/env bash
# Create customs-reviewer-1.5b — Human Tower base model (prompt + optional LoRA weights).
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
MODelfile="$ROOT/examples/Modelfile.reviewer.live"
LORA="$ROOT/flight-recorder/human-reviewer-lora.jsonl"

{
  echo "FROM qwen2.5-coder:1.5b"
  echo 'SYSTEM """'
  PYTHONPATH="$ROOT" python3 -c "
from human_reviewer.export import INSTRUCTION
print(INSTRUCTION)
print()
print('When uncertain, reject with specific actionable feedback — like tinygrad maintainers.')
print('Never approve drive-by refactors, missing tests, or unrefined AI slop.')
" 2>/dev/null
  echo '"""'
  echo "PARAMETER temperature 0.15"
  echo "PARAMETER num_ctx 16384"
} > "$MODelfile"

echo "Creating customs-reviewer-1.5b ..."
ollama create customs-reviewer-1.5b -f "$MODelfile" 2>&1 || ollama create customs-reviewer-1.5b -f "$MODelfile" --force 2>&1 || true

if [[ -f "$LORA" ]] && [[ "$(wc -l < "$LORA")" -ge 50 ]]; then
  echo "LoRA corpus ready ($(wc -l < "$LORA") examples) — run: bash $ROOT/scripts/lora-train-reviewer.sh"
else
  echo "Collect more data: issue-agent-human-review collect && issue-agent-human-review export"
  echo "Target: 200+ examples before fine-tune"
fi

if ollama list | grep -q customs-reviewer-1.5b; then
  echo "✓ customs-reviewer-1.5b ready (base prompt; fine-tune optional)"
fi