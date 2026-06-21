#!/usr/bin/env bash
# Weekend LoRA prep — export dataset + print next steps for qwen2.5-coder:1.5b
set -euo pipefail
ROOT="${ISSUE_AGENT_ROOT:-$HOME/issue-agent}"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"

echo "=== Flight Recorder → LoRA dataset ==="
issue-agent-lora export --with-gh -o "$ROOT/flight-recorder/lora-dataset.jsonl"
issue-agent-lora stats --with-gh

MANIFEST="$ROOT/flight-recorder/lora-dataset.manifest.json"
EXAMPLES=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['examples'])" 2>/dev/null || echo 0)

echo ""
echo "=== Dataset ready: $EXAMPLES examples ==="
echo ""
echo "Next steps (pick one):"
echo "  1. Ollama Modelfile — create customs specialist from base 1.5b + system prompt:"
echo "     issue-agent prompt triage > /tmp/customs-system.txt"
echo "     ollama create customs-1.5b -f $ROOT/examples/Modelfile.customs"
echo ""
echo "  2. unsloth (GPU/CPU) — fine-tune on lora-dataset.jsonl:"
echo "     pip install unsloth && python3 $ROOT/scripts/lora_unsloth.py"
echo ""
echo "  3. Re-run weekly:"
echo "     issue-agent-lora export --with-gh && issue-agent prompt goal"