#!/usr/bin/env bash
set -euo pipefail

OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

echo "==> Pulling Ollama models (coder + triage)"
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5-coder:1.5b

echo "==> Models ready at $OLLAMA_HOST"
curl -sf "$OLLAMA_HOST/api/tags" | python3 -c "import sys,json; m=[x['name'] for x in json.load(sys.stdin).get('models',[])]; print('  ', '\n   '.join(m[:6]))" 2>/dev/null || true