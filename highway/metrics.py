"""Highway metrics from Flight Recorder trajectories."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
TRAJECTORIES = AGENT_ROOT / "flight-recorder" / "trajectories.jsonl"


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_recent_entries(hours: int = 24) -> list[dict[str, Any]]:
    if not TRAJECTORIES.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows: list[dict[str, Any]] = []
    for line in TRAJECTORIES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(str(row.get("ts", "")))
        if ts and ts >= cutoff:
            rows.append(row)
    return rows


def highway_stats(hours: int = 24) -> dict[str, Any]:
    rows = load_recent_entries(hours)
    outcomes = Counter()
    handlers = Counter()
    repos_l0 = Counter()
    skips = Counter()
    aider_runs = 0
    merges = 0

    for row in rows:
        outcome = row.get("outcome", "")
        outcomes[outcome] += 1
        if outcome == "highway_l0":
            handlers[row.get("handler", "?")] += 1
            repos_l0[row.get("repo", "?")] += 1
        if outcome == "highway_skip":
            skips[row.get("reason", "?")] += 1
        if outcome in ("fix_retry", "fix_success") and row.get("attempt", 1) == 1:
            aider_runs += 1
        if outcome == "merged_pr":
            merges += 1

    l0_hits = outcomes.get("highway_l0", 0)
    total_fixes = l0_hits + aider_runs
    return {
        "hours": hours,
        "l0_hits": l0_hits,
        "l0_handlers": dict(handlers.most_common(12)),
        "l0_repos": dict(repos_l0.most_common(8)),
        "highway_skips": dict(skips.most_common(8)),
        "aider_attempts": aider_runs,
        "merged_pr": merges,
        "ollama_calls_saved": l0_hits,
        "l0_share_pct": round(100 * l0_hits / total_fixes, 1) if total_fixes else 0.0,
        "outcomes": dict(outcomes.most_common(15)),
    }


def format_stats_report(stats: dict[str, Any]) -> str:
    lines = [
        f"Solvability Highway — last {stats['hours']}h",
        "",
        f"  L0 hits (0 Ollama):     {stats['l0_hits']}",
        f"  Aider attempts:          {stats['aider_attempts']}",
        f"  L0 share of fixes:       {stats['l0_share_pct']}%",
        f"  Ollama calls saved:      {stats['ollama_calls_saved']}",
        f"  Merged PRs (recorder):   {stats['merged_pr']}",
    ]
    if stats.get("l0_handlers"):
        lines.append("")
        lines.append("  L0 handlers:")
        for k, v in stats["l0_handlers"].items():
            lines.append(f"    {v:4d}  {k}")
    if stats.get("highway_skips"):
        lines.append("")
        lines.append("  Admission skips:")
        for k, v in stats["highway_skips"].items():
            lines.append(f"    {v:4d}  {k[:60]}")
    if stats.get("l0_repos"):
        lines.append("")
        lines.append("  L0 by repo:")
        for k, v in stats["l0_repos"].items():
            lines.append(f"    {v:4d}  {k}")
    return "\n".join(lines)