"""Flight Recorder hooks for Human Tower outcomes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from human_reviewer.gate import HumanTowerVerdict

AGENT_ROOT = Path(__file__).resolve().parent.parent
TRAJECTORIES = AGENT_ROOT / "flight-recorder" / "trajectories.jsonl"
HUMAN_REVIEWS_LOG = AGENT_ROOT / "flight-recorder" / "human-tower.jsonl"


def append_human_tower_record(
    verdict: HumanTowerVerdict,
    *,
    repo: str,
    issue_num: int | None = None,
    issue_summary: str = "",
) -> None:
    row: dict[str, Any] = {
        "outcome": "human_tower_pass" if verdict.passed else "human_tower_reject",
        "repo": repo,
        "issue_num": issue_num,
        "issue_summary": issue_summary[:200],
        "confidence": verdict.confidence,
        "review_comment": verdict.review_comment[:1200],
        "reasons": verdict.reasons[:8],
        "similar_prs": verdict.similar_prs,
        "model": verdict.model,
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "human_tower",
    }
    for path in (HUMAN_REVIEWS_LOG, TRAJECTORIES):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def human_tower_block_comment(verdict: HumanTowerVerdict) -> str:
    lines = ["🤖 **Human Tower** — maintainer-voice review", ""]
    if verdict.review_comment:
        lines.append(verdict.review_comment)
        lines.append("")
    for r in verdict.reasons:
        lines.append(f"- {r}")
    if verdict.similar_prs:
        lines.append("")
        lines.append("**Similar corpus PRs:** " + ", ".join(verdict.similar_prs[:3]))
    lines.append("")
    lines.append(f"*Model: {verdict.model} · confidence: {verdict.confidence}*")
    return "\n".join(lines)