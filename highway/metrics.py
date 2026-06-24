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


def highway_wins_by_repo(hours: int = 48) -> dict[str, int]:
    """Count L0+L1+satisfied highway successes per repo in the recent window."""
    wins: dict[str, int] = {}
    for row in load_recent_entries(hours):
        if row.get("outcome") not in ("highway_l0", "highway_l1", "highway_satisfied"):
            continue
        repo = str(row.get("repo") or "")
        if repo:
            wins[repo] = wins.get(repo, 0) + 1
    return wins


def highway_stats(hours: int = 24) -> dict[str, Any]:
    rows = load_recent_entries(hours)
    outcomes = Counter()
    l0_handlers = Counter()
    l1_handlers = Counter()
    repos_l0 = Counter()
    repos_l1 = Counter()
    skips = Counter()
    aider_runs = 0
    merges = 0

    for row in rows:
        outcome = row.get("outcome", "")
        outcomes[outcome] += 1
        if outcome == "highway_l0":
            l0_handlers[row.get("handler", "?")] += 1
            repos_l0[row.get("repo", "?")] += 1
        if outcome == "highway_l1":
            l1_handlers[row.get("handler", "?")] += 1
            repos_l1[row.get("repo", "?")] += 1
        if outcome == "highway_skip":
            skips[row.get("reason", "?")] += 1
        if outcome in ("fix_retry", "fix_success") and row.get("attempt", 1) == 1:
            aider_runs += 1
        if outcome == "merged_pr":
            merges += 1

    l0_hits = outcomes.get("highway_l0", 0)
    l1_hits = outcomes.get("highway_l1", 0)
    satisfied_hits = outcomes.get("highway_satisfied", 0)
    highway_wins = l0_hits + l1_hits + satisfied_hits
    highway_fixes = highway_wins + aider_runs
    applied_share = l0_hits + l1_hits
    return {
        "hours": hours,
        "l0_hits": l0_hits,
        "l1_hits": l1_hits,
        "satisfied_hits": satisfied_hits,
        "l0_handlers": dict(l0_handlers.most_common(12)),
        "l1_handlers": dict(l1_handlers.most_common(8)),
        "l0_repos": dict(repos_l0.most_common(8)),
        "l1_repos": dict(repos_l1.most_common(8)),
        "highway_skips": dict(skips.most_common(8)),
        "aider_attempts": aider_runs,
        "merged_pr": merges,
        "ollama_calls_saved": l0_hits,
        "highway_share_pct": round(100 * highway_wins / highway_fixes, 1) if highway_fixes else 0.0,
        "highway_applied_share_pct": round(100 * applied_share / highway_fixes, 1) if highway_fixes else 0.0,
        "l0_share_pct": round(100 * l0_hits / highway_fixes, 1) if highway_fixes else 0.0,
        "outcomes": dict(outcomes.most_common(15)),
    }


def format_stats_report(stats: dict[str, Any]) -> str:
    lines = [
        f"Solvability Highway — last {stats['hours']}h",
        "",
        f"  L0 hits (0 Ollama):     {stats['l0_hits']}",
        f"  L1 hits (micro-LLM):    {stats.get('l1_hits', 0)}",
        f"  Satisfied (0 diff):     {stats.get('satisfied_hits', 0)}",
        f"  Aider attempts:          {stats['aider_attempts']}",
        f"  Highway share (all):     {stats.get('highway_share_pct', stats.get('l0_share_pct', 0))}%",
        f"  Highway share (applied): {stats.get('highway_applied_share_pct', stats.get('highway_share_pct', 0))}%",
        f"  Ollama calls saved:      {stats['ollama_calls_saved']}",
        f"  Merged PRs (recorder):   {stats['merged_pr']}",
    ]
    if stats.get("l0_handlers"):
        lines.append("")
        lines.append("  L0 handlers:")
        for k, v in stats["l0_handlers"].items():
            lines.append(f"    {v:4d}  {k}")
    if stats.get("l1_handlers"):
        lines.append("")
        lines.append("  L1 handlers:")
        for k, v in stats["l1_handlers"].items():
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
    if stats.get("l1_repos"):
        lines.append("")
        lines.append("  L1 by repo:")
        for k, v in stats["l1_repos"].items():
            lines.append(f"    {v:4d}  {k}")
    return "\n".join(lines)