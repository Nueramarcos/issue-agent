"""Load and render Habitat Solver prompts with adaptive Flight Recorder feedback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_SOLVER = PROMPTS_DIR / "habitat-solver.md"
DEFAULT_TRIAGE = PROMPTS_DIR / "customs-triage.md"
DEFAULT_VISION = PROMPTS_DIR / "vision.md"


def _read_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8")
    # Strip markdown title line for injection into LLM prompts
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def adaptive_feedback_snippet(
    agent_root: Path,
    *,
    max_items: int = 6,
) -> str:
    """Summarize recent Flight Recorder patterns for prompt injection."""
    lines: list[str] = []
    ledger_path = agent_root / "failure-ledger.json"
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text())
            items = list((ledger.get("items") or {}).values())
            blocked = [i for i in items if i.get("blocked")]
            no_commits = [i for i in items if i.get("kind") == "no_commits"]
            if blocked:
                lines.append(f"- {len(blocked)} scope(s) currently blocked (6h cooldown after 2 failures)")
            for entry in sorted(no_commits, key=lambda x: -int(x.get("attempts", 0)))[:3]:
                repo = str(entry.get("repo", "")).split("/")[-1]
                lines.append(f"- no_commits pattern: {repo} / {entry.get('scope')}/{entry.get('ident')} — prefer simpler issues")
        except json.JSONDecodeError:
            pass

    traj = agent_root / "flight-recorder" / "trajectories.jsonl"
    if traj.exists():
        recent_success = 0
        recent_fail = 0
        for raw in traj.read_text().splitlines()[-40:]:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("outcome") == "success":
                recent_success += 1
            elif row.get("outcome") == "failure":
                recent_fail += 1
        if recent_success or recent_fail:
            lines.append(f"- recent trajectories: {recent_success} success, {recent_fail} failure")

    manifest = agent_root / "flight-recorder" / "lora-dataset.manifest.json"
    if manifest.exists():
        try:
            m = json.loads(manifest.read_text())
            lines.append(f"- LoRA dataset: {m.get('examples', 0)} instruction examples ({', '.join(m.get('tasks') or [])})")
        except json.JSONDecodeError:
            pass

    if not lines:
        return "No Flight Recorder patterns yet — prefer low-complexity docs/CI/smoke-test issues."
    return "\n".join(lines[:max_items])


def render_prompt(
    template_path: Path,
    *,
    variables: dict[str, str] | None = None,
    agent_root: Path | None = None,
    include_adaptive: bool = True,
) -> str:
    text = _read_prompt(template_path)
    vars_all = dict(variables or {})
    if include_adaptive and "{adaptive_feedback}" in text:
        root = agent_root or template_path.resolve().parent.parent
        vars_all.setdefault("adaptive_feedback", adaptive_feedback_snippet(root))
    for key, val in vars_all.items():
        text = text.replace("{" + key + "}", str(val))
    # Remove unfilled optional placeholders
    text = re.sub(r"\{[a-z_]+\}", "", text)
    return text.strip()


def load_solver_prompt(
    repo: str,
    issue_summary: str,
    *,
    max_files: int = 8,
    agent_root: Path | None = None,
    prompt_path: Path | None = None,
) -> str:
    path = prompt_path or DEFAULT_SOLVER
    return render_prompt(
        path,
        variables={
            "repo": repo,
            "issue_summary": issue_summary,
            "max_files": str(max_files),
        },
        agent_root=agent_root,
    )


def load_triage_prompt(
    title: str,
    body: str,
    *,
    agent_root: Path | None = None,
    prompt_path: Path | None = None,
) -> str:
    path = prompt_path or DEFAULT_TRIAGE
    return render_prompt(
        path,
        variables={
            "title": title,
            "body": (body or "")[:4000],
        },
        agent_root=agent_root,
    )


def load_vision(agent_root: Path | None = None) -> str:
    return _read_prompt(DEFAULT_VISION)


def prompt_inventory(agent_root: Path) -> dict[str, Any]:
    """Validate prompt goal — all templates present and renderable."""
    root = agent_root
    checks: dict[str, Any] = {
        "vision": DEFAULT_VISION.exists(),
        "habitat_solver": DEFAULT_SOLVER.exists(),
        "customs_triage": DEFAULT_TRIAGE.exists(),
    }
    try:
        solver = load_solver_prompt("Nueramarcos/issue-agent", "demo issue", max_files=8, agent_root=root)
        triage = load_triage_prompt("Add README badge", "README only", agent_root=root)
        checks["solver_chars"] = len(solver)
        checks["triage_chars"] = len(triage)
        checks["solver_has_tools"] = "rg" in solver and "Web search" in solver
        checks["solver_has_tower"] = "Tower" in solver
        checks["solver_has_adaptive"] = "Flight Recorder" in solver or "no_commits" in solver
        checks["goal_met"] = all(
            [
                checks["vision"],
                checks["habitat_solver"],
                checks["customs_triage"],
                checks["solver_has_tools"],
                checks["solver_has_tower"],
                checks["solver_chars"] >= 1500,
            ]
        )
    except Exception as exc:
        checks["goal_met"] = False
        checks["error"] = str(exc)
    return checks