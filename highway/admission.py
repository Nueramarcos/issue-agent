"""Admission control — gate Ollama spend before Aider runs."""

from __future__ import annotations

from typing import Any

from highway.archetype import detect_archetype
from highway.router import HighwayPlan

BLOCKED_L2_ARCHETYPES = frozenset({"ci_workflow", "smoke_tests", "templates"})


def spec_highway_lane(spec: dict[str, Any]) -> int:
    """Lane from backlog spec (explicit tag or inferred from title/body)."""
    if "highway_lane" in spec:
        return int(spec["highway_lane"])
    title = spec.get("title") or ""
    body = spec.get("body") or ""
    from highway.router import route_issue

    plan = route_issue("", {"title": title, "body": body}, None)
    return plan.lane if plan.lane >= 0 else 2


def _highway_meta(repo_meta: dict[str, Any] | None) -> dict[str, Any]:
    hw = (repo_meta or {}).get("highway")
    return hw if isinstance(hw, dict) else {}


def admit_seed(repo: str, spec: dict[str, Any], repo_meta: dict[str, Any] | None) -> tuple[bool, str]:
    """Factory/collect may only auto-seed L0 issues (Phase 2 policy)."""
    lane = spec_highway_lane(spec)
    archetype = detect_archetype(spec.get("title", ""), spec.get("body", ""))
    if lane != 0:
        return False, f"auto-seed L0 only (lane={lane}, archetype={archetype})"
    hw = _highway_meta(repo_meta)
    if not hw.get("l0_enabled", True):
        return False, "L0 disabled for repo"
    return True, ""


def admit_to_aider(
    repo: str,
    issue: dict[str, Any],
    plan: HighwayPlan,
    repo_meta: dict[str, Any] | None,
    solv: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Hard gate before 7b/1.5b Aider — protects the Ollama bus."""
    if plan.lane < 2:
        return False, f"lane {plan.lane} must not use Aider"

    hw = _highway_meta(repo_meta)
    if not hw.get("l2_enabled", True):
        return False, "L2 disabled for repo — use highway L0/L1 only"

    if plan.archetype in BLOCKED_L2_ARCHETYPES:
        return False, f"archetype {plan.archetype} blocked for Aider"

    if solv:
        score = int(solv.get("score", 0))
        tier = solv.get("tier", "cold")
        fails_6h = int((solv.get("factors") or {}).get("no_commits_6h", 0))
        if tier == "cold" and score < 45:
            return False, f"repo cold (score={score}) — L0 only"
        if fails_6h >= 5:
            return False, f"no_commits burn ({fails_6h}/6h) — cooldown"

    return True, ""