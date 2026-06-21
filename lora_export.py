"""LoRA dataset export — convert Flight Recorder trajectories to instruction JSONL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _instruction(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map a flight-recorder row to an instruction-tuning example."""
    outcome = str(record.get("outcome", ""))
    repo = str(record.get("repo", ""))
    if not repo:
        return None

    if outcome == "failure":
        kind = record.get("kind", "unknown")
        detail = str(record.get("detail", ""))[:800]
        hint = str(record.get("hint", ""))
        return {
            "task": "triage_failure",
            "instruction": "You are Customs — triage agent for Issue Agent. Given a failure, classify kind and recommend action.",
            "input": f"repo: {repo}\nscope: {record.get('scope')}\nident: {record.get('ident')}\ndetail: {detail}",
            "output": json.dumps({"kind": kind, "action": "skip" if record.get("blocked") else "retry", "hint": hint}),
            "meta": {"repo": repo, "outcome": outcome, "ts": record.get("ts")},
        }

    if outcome == "tower_reject":
        reasons = record.get("reasons") or []
        files = record.get("files") or []
        return {
            "task": "tower_review",
            "instruction": "You are Tower — reject or approve this diff before push.",
            "input": f"repo: {repo}\nfiles: {', '.join(files[:12])}\nreasons: {'; '.join(reasons)}",
            "output": json.dumps({"verdict": "reject", "reasons": reasons[:5]}),
            "meta": {"repo": repo, "outcome": outcome},
        }

    if outcome in ("success", "tower_pass", "merged_pr"):
        title = str(record.get("spec_title") or record.get("title") or record.get("ident", ""))[:200]
        return {
            "task": "merge_success",
            "instruction": "You are Customs — score this issue archetype as merge-worthy for a local 7b coder.",
            "input": f"repo: {repo}\ntitle: {title}",
            "output": json.dumps({"actionable": True, "complexity": "low", "confidence": "high", "verdict": "merge"}),
            "meta": {"repo": repo, "outcome": outcome, "pr_url": record.get("url")},
        }

    if outcome == "failure_ledger":
        kind = record.get("kind", "unknown")
        hint = record.get("hint", "")
        return {
            "task": "failure_ledger",
            "instruction": "Predict whether Issue Agent should retry this repo scope after repeated failures.",
            "input": f"repo: {repo}\nscope: {record.get('scope')}\nkind: {kind}\nattempts: {record.get('attempts')}",
            "output": json.dumps({"retry": not record.get("blocked"), "hint": hint}),
            "meta": {"repo": repo, "key": record.get("key")},
        }

    if outcome == "activity" and record.get("event") in ("tower_reject", "tower_pass"):
        return {
            "task": "tower_activity",
            "instruction": "Tower gate result for local agent diff.",
            "input": f"repo: {repo}\nevent: {record.get('event')}\ndetail: {record.get('detail', '')}",
            "output": json.dumps({"verdict": "pass" if record.get("event") == "tower_pass" else "reject"}),
            "meta": {"repo": repo},
        }

    return None


def build_lora_dataset(
    rows: list[dict[str, Any]],
    *,
    include_tasks: set[str] | None = None,
) -> list[dict[str, Any]]:
    dataset: list[dict[str, Any]] = []
    for row in rows:
        ex = _instruction(row)
        if not ex:
            continue
        if include_tasks and ex["task"] not in include_tasks:
            continue
        dataset.append(ex)
    return dataset


def export_lora_jsonl(
    rows: list[dict[str, Any]],
    out_path: Path,
    *,
    include_tasks: set[str] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dataset = build_lora_dataset(rows, include_tasks=include_tasks)
    with out_path.open("w") as f:
        for i, ex in enumerate(dataset):
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
            if on_progress:
                on_progress(i + 1)
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "examples": len(dataset),
        "tasks": sorted({ex["task"] for ex in dataset}),
        "path": str(out_path),
    }
    out_path.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
    return len(dataset)