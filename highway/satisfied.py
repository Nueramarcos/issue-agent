"""Detect when a highway issue goal is already met — avoid Aider no_commits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from highway.junk import junk_targets
from highway.router import HighwayPlan


def issue_already_satisfied(ws: Path, issue: dict[str, Any], plan: HighwayPlan) -> bool:
    """True when the repo already contains what the issue asks for."""
    if plan.lane not in (0, 1):
        return False
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    text = f"{title} {body}"
    arch = plan.archetype

    if arch == "security" and (ws / "SECURITY.md").exists():
        return True
    if arch == "changelog" and (ws / "CHANGELOG.md").exists():
        return True
    if arch == "license" and (ws / "LICENSE").exists():
        return True
    if arch == "contributing" and (ws / "CONTRIBUTING.md").exists():
        return True
    if arch == "codeowners" and (ws / "CODEOWNERS").exists():
        return True
    if arch == "ci_workflow" and list((ws / ".github" / "workflows").glob("*.yml")):
        return True
    if arch == "requirements_dev" and (ws / "requirements-dev.txt").exists():
        return True
    if arch == "smoke_tests":
        tests = ws / "tests"
        if tests.is_dir() and list(tests.glob("test_*.py")):
            if "value" in text:
                return (tests / "test_value.py").exists()
            return True
    if arch == "templates" and (ws / ".github" / "ISSUE_TEMPLATE").is_dir():
        return True
    if arch == "readme" and ("badge" in text or "shield" in text):
        readme = ws / "README.md"
        if readme.exists():
            low = readme.read_text(encoding="utf-8", errors="replace").lower()
            if "shields.io" in low or "badge.svg" in low:
                return True
    if arch == "version":
        for init in ws.rglob("__init__.py"):
            if "__version__" in init.read_text(encoding="utf-8", errors="replace"):
                return True
    if arch == "junk":
        return not junk_targets(ws, issue)
    return False