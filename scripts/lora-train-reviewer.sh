#!/usr/bin/env bash
# Fine-tune qwen2.5-coder:1.5b on human-reviewer-lora.jsonl (requires unsloth or axolotl).
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
DATA="$ROOT/flight-recorder/human-reviewer-lora.jsonl"
OUT="$ROOT/flight-recorder/reviewer-lora-adapter"

if [[ ! -f "$DATA" ]]; then
  echo "missing $DATA — run: issue-agent-human-review collect && export"
  exit 1
fi
N=$(wc -l < "$DATA")
if [[ "$N" -lt 50 ]]; then
  echo "only $N examples — collect until 200+ for stable fine-tune"
  exit 1
fi

echo "Human Reviewer train — $N examples"
echo "  data: $DATA"
export PYTHONPATH="$ROOT"
python3 "$ROOT/scripts/train_reviewer_lora.py"
echo ""
echo "Human Tower model: customs-reviewer-ft-1.5b"
echo "Optional GPU LoRA later: pip install unsloth torch && python3 $ROOT/scripts/lora_unsloth.py"