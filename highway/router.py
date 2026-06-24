"""Highway router — pick execution lane before touching Ollama."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from highway.archetype import detect_archetype, is_lane0_candidate
from highway.micro import apply_micro
from highway.templates import apply_template

REGISTRY_PATH = Path(__file__).resolve().parent / "registry.yaml"


@dataclass
class HighwayPlan:
    lane: int
    archetype: str
    handler: str
    package_root: str = ""
    skip_reason: str = ""
    ollama_budget: int = 0


def _load_registry() -> dict[str, Any]:
    if yaml is None or not REGISTRY_PATH.exists():
        return {}
    data = yaml.safe_load(REGISTRY_PATH.read_text()) or {}
    return data.get("archetypes") or {}


def route_issue(
    repo: str,
    issue: dict[str, Any],
    repo_meta: dict[str, Any] | None = None,
) -> HighwayPlan:
    """Return execution plan for an issue. Lane 0 avoids Ollama entirely."""
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    archetype = detect_archetype(title, body)
    registry = _load_registry()
    spec = registry.get(archetype, {})
    lane = int(spec.get("lane", 2))
    handler = str(spec.get("handler", "aider"))

    highway = (repo_meta or {}).get("highway") if isinstance((repo_meta or {}).get("highway"), dict) else {}
    if highway:
        if lane == 0 and highway.get("l0_enabled") is False:
            lane = 2
            handler = "aider"
            skip_reason = "L0 disabled for repo"
        elif lane == 2 and highway.get("l2_enabled") is False:
            if is_lane0_candidate(title, body):
                lane = 0
                handler = str(spec.get("handler", f"template:{archetype}"))
            else:
                return HighwayPlan(
                    lane=-1,
                    archetype=archetype,
                    handler="skip",
                    skip_reason="L2 disabled — no L0 handler",
                    ollama_budget=0,
                )

    if lane == 0:
        return HighwayPlan(
            lane=0,
            archetype=archetype,
            handler=handler,
            package_root=str((highway or {}).get("package_root", "")),
            ollama_budget=0,
        )

    if lane == 1:
        if highway and highway.get("l1_enabled") is False:
            if highway.get("l2_enabled") is False:
                return HighwayPlan(
                    lane=-1,
                    archetype=archetype,
                    handler="skip",
                    skip_reason="L1 disabled — no L2 fallback",
                    ollama_budget=0,
                )
            lane = 2
            handler = "aider"
        else:
            return HighwayPlan(
                lane=1,
                archetype=archetype,
                handler=handler,
                ollama_budget=500,
            )

    return HighwayPlan(
        lane=2,
        archetype=archetype,
        handler=handler,
        ollama_budget=50_000,
    )


def is_highway_lane0(issue: dict[str, Any]) -> bool:
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    return is_lane0_candidate(title, body)


def apply_lane0(ws: Path, issue: dict[str, Any], plan: HighwayPlan, repo_meta: dict[str, Any] | None) -> bool:
    """Run lane-0 template handler. Returns True if files changed."""
    if plan.lane != 0:
        return False
    return apply_template(plan.handler, ws, issue, repo_meta)


def apply_lane1(
    ws: Path,
    issue: dict[str, Any],
    plan: HighwayPlan,
    repo_meta: dict[str, Any] | None,
    *,
    repo: str = "",
) -> bool:
    """Run lane-1 micro-LLM handler. Returns True if files changed."""
    if plan.lane != 1:
        return False
    return apply_micro(plan.handler, ws, issue, repo_meta, repo=repo)