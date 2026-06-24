#!/usr/bin/env python3
"""Distill human-reviewer corpus into customs-reviewer-ft-1.5b (Modelfile few-shots).

True weight LoRA needs torch/unsloth — this ships immediately on CPU by embedding
high-value maintainer examples from the 650+ episode corpus.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from human_reviewer.export import INSTRUCTION, load_corpus, row_to_example  # noqa: E402

DATA = ROOT / "flight-recorder" / "human-reviewer-lora.jsonl"
MODelfile = ROOT / "examples" / "Modelfile.reviewer-ft.live"
MODEL = "customs-reviewer-ft-1.5b"
MAX_SHOTS = 10


def _pick_few_shots() -> list[dict]:
    rows = load_corpus()
    if not rows:
        if DATA.exists():
            rows = [json.loads(ln) for ln in DATA.read_text().splitlines() if ln.strip()]
    scored: list[tuple[int, dict]] = []
    for row in rows:
        ex = row_to_example(row)
        if not ex:
            continue
        if not row.get("maintainer_voice") and int(row.get("complexity_score") or 0) < 25:
            continue
        score = int(row.get("complexity_score") or 0)
        if row.get("verdict") != "merged":
            score += 25
        if row.get("curated"):
            score += 15
        scored.append((score, ex))
    scored.sort(key=lambda x: -x[0])
    seen_verdict: set[str] = set()
    picks: list[dict] = []
    for _, ex in scored:
        out = json.loads(ex["output"])
        v = out.get("verdict", "")
        key = f"{v}:{ex['input'][:80]}"
        if key in seen_verdict:
            continue
        picks.append(ex)
        seen_verdict.add(key)
        if len(picks) >= MAX_SHOTS:
            break
    return picks


def build_modelfile(shots: list[dict]) -> str:
    lines = [
        "FROM qwen2.5-coder:1.5b",
        'SYSTEM """',
        INSTRUCTION,
        "",
        "When uncertain, reject with specific actionable feedback — like tinygrad maintainers.",
        'Never approve drive-by refactors, missing tests, or unrefined AI slop.',
        "",
        "Few-shot maintainer decisions from real merged/rejected PRs:",
        "",
    ]
    for i, ex in enumerate(shots, 1):
        lines.append(f"--- Example {i} ---")
        lines.append("INPUT:")
        lines.append(ex["input"][:900])
        lines.append("OUTPUT:")
        lines.append(ex["output"][:600])
        lines.append("")
    lines.append('"""')
    lines.extend(["PARAMETER temperature 0.12", "PARAMETER num_ctx 16384"])
    return "\n".join(lines)


def main() -> int:
    shots = _pick_few_shots()
    if len(shots) < 3:
        print(f"need 3+ few-shot examples, got {len(shots)} — run collect-deep + export")
        return 1
    MODelfile.write_text(build_modelfile(shots), encoding="utf-8")
    print(f"Modelfile: {MODelfile} ({len(shots)} few-shots)")
    result = subprocess.run(
        ["ollama", "create", MODEL, "-f", str(MODelfile)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        subprocess.run(["ollama", "create", MODEL, "-f", str(MODelfile), "--force"], check=False)
    print(f"✓ {MODEL} ready — Human Tower default model")
    manifest = {
        "model": MODEL,
        "few_shots": len(shots),
        "corpus_rows": len(load_corpus()),
        "modelfile": str(MODelfile),
    }
    out = ROOT / "flight-recorder" / "reviewer-model.manifest.json"
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"  manifest: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())