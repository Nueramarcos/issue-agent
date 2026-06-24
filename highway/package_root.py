"""Detect Python package root inside a workspace."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def detect_package_root(ws: Path, repo_meta: dict[str, Any] | None = None) -> Path:
    """Return directory for package-scoped files (py.typed, etc.)."""
    meta = repo_meta or {}
    highway = meta.get("highway") if isinstance(meta.get("highway"), dict) else {}
    if root := highway.get("package_root"):
        candidate = ws / str(root)
        if candidate.is_dir():
            return candidate

    for name in ("habitat", "forge", "Orion", "orion", "nexus", "vertex"):
        candidate = ws / name
        if (candidate / "__init__.py").exists():
            return candidate

    pyproject = ws / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            pkg = match.group(1).replace("-", "_")
            for candidate in (ws / pkg, ws / match.group(1)):
                if candidate.is_dir():
                    return candidate

    for init in ws.rglob("__init__.py"):
        if ".git" in init.parts or "tests" in init.parts or ".venv" in init.parts:
            continue
        if init.parent == ws:
            continue
        return init.parent

    return ws