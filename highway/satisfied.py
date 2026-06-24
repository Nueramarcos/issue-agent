"""Detect when a highway issue goal is already met — avoid Aider no_commits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import re

from highway.junk import junk_targets
from highway.router import HighwayPlan

_DEFAULT_GITIGNORE = (
    "__pycache__/",
    ".pytest_cache/",
    "*.pyc",
    ".venv/",
    ".issue-agent-venv/",
    "dist/",
    "*.egg-info/",
)


def _gitignore_patterns(text: str, ws: Path) -> list[str]:
    patterns: list[str] = []
    for m in re.finditer(r"[\w.*]+/|[\w.*]+\.[\w]+", text):
        p = m.group(0)
        if "gitignore" not in p:
            patterns.append(p)
    if not patterns:
        patterns = list(_DEFAULT_GITIGNORE)
    if (ws / "Cargo.toml").exists():
        patterns.extend(["/target/", "target/"])
    return patterns


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
    if arch == "gitignore":
        target = ws / ".gitignore"
        if not target.exists():
            return False
        existing = target.read_text(encoding="utf-8", errors="replace")
        return all(p in existing for p in _gitignore_patterns(text, ws))
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
    if arch == "cli_version":
        from highway.golden import _cli_version_paths, _cli_version_satisfied

        for cli_file in _cli_version_paths(ws, text):
            if _cli_version_satisfied(cli_file.read_text(encoding="utf-8", errors="replace")):
                return True
    if arch == "readme_entity":
        readme = ws / "README.md"
        if readme.exists() and "&amp;" not in readme.read_text(encoding="utf-8", errors="replace"):
            return True
    if arch == "version":
        for init in ws.rglob("__init__.py"):
            if "__version__" in init.read_text(encoding="utf-8", errors="replace"):
                return True
    if arch == "junk":
        return not junk_targets(ws, issue)
    if arch == "pyproject_meta":
        proj = ws / "pyproject.toml"
        if proj.exists():
            content = proj.read_text(encoding="utf-8", errors="replace")
            if (
                "name" in content
                and "version" in content
                and "description" in content
                and "requires-python" in content
            ):
                return True
    if arch == "rustfmt" and (ws / "rustfmt.toml").exists():
        return True
    if arch == "cargo_meta":
        cargo = ws / "Cargo.toml"
        if cargo.exists():
            content = cargo.read_text(encoding="utf-8", errors="replace")
            if "version" in content and "description" in content:
                return True
    if arch == "rust_unit_test":
        for lib in (ws / "Vertex" / "lib.rs", *ws.rglob("lib.rs")):
            if "target" in lib.parts or not lib.is_file():
                continue
            body_text = lib.read_text(encoding="utf-8", errors="replace")
            if "SimConfig::default()" in body_text and "#[test]" in body_text:
                return True
    if arch == "docstring":
        for init in ws.rglob("__init__.py"):
            stripped = init.read_text(encoding="utf-8", errors="replace").lstrip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                return True
    return False