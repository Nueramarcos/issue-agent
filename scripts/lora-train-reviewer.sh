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

echo "Human Reviewer LoRA train — $N examples"
echo "  data: $DATA"
echo "  out:  $OUT"
echo ""
echo "Plug your trainer here (unsloth recommended on this workstation):"
echo "  python3 $ROOT/scripts/train_reviewer_lora.py  # TODO: add when GPU schedule allows"
echo ""
echo "Until fine-tune ships, Human Tower uses RAG + customs-reviewer-1.5b base prompt."