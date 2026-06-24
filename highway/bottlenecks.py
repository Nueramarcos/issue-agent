"""Fleet bottleneck analysis — failure ledger + highway stats."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
FAILURE_LEDGER = AGENT_ROOT / "failure-ledger.json"
TRAJECTORIES = AGENT_ROOT / "flight-recorder" / "trajectories.jsonl"

from highway.metrics import highway_stats, highway_wins_by_repo  # noqa: E402


def load_failure_items() -> dict[str, Any]:
    if not FAILURE_LEDGER.exists():
        return {}
    try:
        return json.loads(FAILURE_LEDGER.read_text()).get("items", {})
    except json.JSONDecodeError:
        return {}


def analyze_bottlenecks(hours: int = 168) -> dict[str, Any]:
    items = load_failure_items()
    kinds = Counter()
    scopes = Counter()
    repos = Counter()
    blocked = 0
    no_commits = 0
    for entry in items.values():
        kinds[entry.get("kind", "unknown")] += 1
        scopes[entry.get("scope", "?")] += 1
        repos[entry.get("repo", "?")] += 1
        if entry.get("blocked"):
            blocked += 1
        if entry.get("kind") == "no_commits":
            no_commits += 1

    traj_kinds = Counter()
    for line in TRAJECTORIES.read_text().splitlines() if TRAJECTORIES.exists() else []:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("outcome") == "failure" and row.get("kind"):
            traj_kinds[row["kind"]] += 1

    hw = highway_stats(hours)
    wins = highway_wins_by_repo(hours=48)
    total_fixes = hw["l0_hits"] + hw.get("l1_hits", 0) + hw["aider_attempts"]
    aider_fail_rate = round(100 * traj_kinds.get("no_commits", 0) / max(1, hw["aider_attempts"]), 1)

    return {
        "hours": hours,
        "ledger_total": len(items),
        "ledger_blocked": blocked,
        "ledger_no_commits": no_commits,
        "ledger_kinds": dict(kinds.most_common(8)),
        "ledger_scopes": dict(scopes.most_common(6)),
        "ledger_repos": dict(repos.most_common(10)),
        "trajectory_no_commits": traj_kinds.get("no_commits", 0),
        "highway_share_pct": hw.get("highway_share_pct", 0),
        "aider_attempts": hw["aider_attempts"],
        "aider_no_commits_est_pct": aider_fail_rate,
        "highway_wins_48h": wins,
        "top_actions": _recommendations(kinds, hw, blocked),
    }


def _recommendations(kinds: Counter, hw: dict[str, Any], blocked: int) -> list[str]:
    actions: list[str] = []
    if kinds.get("no_commits", 0) >= 5:
        actions.append("Route smoke_tests/ci_workflow/templates to L0 golden handlers (Phase 6)")
    if hw.get("highway_share_pct", 0) < 55:
        actions.append("Expand highway: seed only L0/L1 until share > 55%")
    if blocked >= 10:
        actions.append("Run: issue-agent highway heal — decay stale failure blocks")
    if kinds.get("test_fail", 0) >= 3:
        actions.append("Fix test_command / doc-only skip for markdown-only diffs")
    if not actions:
        actions.append("Fleet healthy — enable L2 selectively on warm repos")
    return actions


def format_bottleneck_report(data: dict[str, Any]) -> str:
    lines = [
        "Solvability Highway — Bottleneck Report",
        "",
        f"  Failure ledger entries:  {data['ledger_total']} ({data['ledger_blocked']} blocked)",
        f"  no_commits in ledger:    {data['ledger_no_commits']}",
        f"  Highway share ({data['hours']}h):   {data['highway_share_pct']}%",
        f"  Aider attempts:          {data['aider_attempts']}",
        f"  Est. Aider no_commits:   {data['aider_no_commits_est_pct']}%",
        "",
        "  Ledger failure kinds:",
    ]
    for k, v in data.get("ledger_kinds", {}).items():
        lines.append(f"    {v:4d}  {k}")
    lines.append("")
    lines.append("  Hot repos (failures):")
    for k, v in data.get("ledger_repos", {}).items():
        wins = data.get("highway_wins_48h", {}).get(k, 0)
        lines.append(f"    {v:4d}  {k}  (hw_wins={wins})")
    lines.append("")
    lines.append("  Recommended actions:")
    for a in data.get("top_actions", []):
        lines.append(f"    • {a}")
    return "\n".join(lines)


def heal_stale_blocks(*, min_highway_wins: int = 2) -> dict[str, int]:
    """Unblock failure-ledger entries for repos with recent highway success."""
    wins = highway_wins_by_repo(hours=72)
    state = json.loads(FAILURE_LEDGER.read_text()) if FAILURE_LEDGER.exists() else {"items": {}}
    items = state.setdefault("items", {})
    cleared = 0
    now = datetime.now(timezone.utc)
    for key, entry in list(items.items()):
        repo = entry.get("repo", "")
        if wins.get(repo, 0) < min_highway_wins:
            continue
        if entry.get("kind") not in ("no_commits", "unknown"):
            continue
        if not entry.get("blocked") and entry.get("attempts", 0) < 2:
            continue
        entry["skip_until"] = None
        entry["blocked"] = False
        entry["attempts"] = 0
        entry["healed_ts"] = now.isoformat()
        items[key] = entry
        cleared += 1
    if cleared:
        state["ts"] = now.isoformat()
        FAILURE_LEDGER.write_text(json.dumps(state, indent=2))
    return {"cleared": cleared, "repos_with_wins": len([r for r, w in wins.items() if w >= min_highway_wins])}