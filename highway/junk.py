"""Junk file detection — agent artifact cleanup (lane 0)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_JUNK_PREFIXES = ("python ", "cargo ", "npm ", "path/")
_JUNK_EXACT = frozenset(
    {
        "path",
        "path/to",
        "path/to/filename.js",
        "Output format",
        "output format",
    }
)


def _strip_bullet(line: str) -> str:
    line = line.strip()
    if line.startswith("- "):
        line = line[2:].strip()
    return line.strip("`").strip()


def junk_paths_from_text(text: str) -> list[str]:
    """Extract candidate junk relative paths from issue title/body."""
    paths: list[str] = []
    for raw in text.splitlines():
        line = _strip_bullet(raw)
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("only delete") or low.startswith("do not modify"):
            continue
        if low.startswith("delete accidental") or low.startswith("delete these"):
            if ":" in line:
                _, rest = line.split(":", 1)
                for part in rest.split(","):
                    p = part.strip().strip("`").strip()
                    if p and "do not modify" not in p.lower():
                        paths.append(p)
            continue
        if line.startswith("Delete ") and ":" not in line:
            continue
        paths.append(line)
    # Comma-separated single-line bodies (nexus style)
    if not paths and "," in text and ("accidental" in text or "junk" in text):
        chunk = text.split(":", 1)[-1] if ":" in text else text
        for part in chunk.split(","):
            p = part.strip().strip("`").strip()
            if p and "do not modify" not in p.lower() and len(p) < 120:
                paths.append(p)
    return paths


def junk_targets(ws: Path, issue: dict[str, Any]) -> list[Path]:
    """Existing junk files in workspace matching issue spec or known patterns."""
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    text = f"{title}\n{body}".lower()
    if "junk" not in text and "accidental" not in text:
        return []

    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key not in seen and path.is_file():
            seen.add(key)
            found.append(path)

    for rel in junk_paths_from_text(f"{title}\n{body}"):
        add(ws / rel)

    if ws.is_dir():
        for child in ws.iterdir():
            if not child.is_file():
                continue
            name = child.name
            if name in _JUNK_EXACT or name.lower() in _JUNK_EXACT:
                add(child)
            if any(name.startswith(p) for p in _JUNK_PREFIXES):
                add(child)
            if re.match(r"^path(/|$)", name):
                add(child)

    return found