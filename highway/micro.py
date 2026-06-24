"""Lane 1 micro-LLM handlers — small Ollama calls, README-only edits."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_MODEL = os.environ.get("ISSUE_AGENT_L1_MODEL", "qwen2.5-coder:1.5b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")


def _has_badges(text: str) -> bool:
    low = text.lower()
    return "shields.io" in low or "![badge" in low or "![license" in low or "badge.svg" in low


def _repo_slug(repo: str) -> str:
    return repo.split("/", 1)[-1] if "/" in repo else repo


def _detect_license(ws: Path) -> str:
    lic = ws / "LICENSE"
    if lic.exists():
        head = lic.read_text(encoding="utf-8", errors="replace")[:400].upper()
        if "APACHE" in head:
            return "Apache-2.0"
        if "MIT" in head:
            return "MIT"
    return "MIT"


def _detect_language(ws: Path) -> str:
    if (ws / "Cargo.toml").exists():
        return "Rust"
    if list(ws.glob("**/*.rs")) and not list(ws.glob("**/*.py")):
        return "Rust"
    return "Python"


def _badge_block(repo: str, ws: Path) -> str:
    slug = _repo_slug(repo)
    owner = repo.split("/", 1)[0] if "/" in repo else "Nueramarcos"
    lic = _detect_license(ws)
    lang = _detect_language(ws)
    if lic == "Apache-2.0":
        lic_badge = "[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)"
    else:
        lic_badge = f"[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)"
    if lang == "Rust":
        lang_badge = "[![Rust](https://img.shields.io/badge/rust-1.60%2B-orange.svg)](https://www.rust-lang.org/)"
    else:
        lang_badge = "[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)"
    ci_badge = (
        f"[![CI](https://github.com/{owner}/{slug}/actions/workflows/ci.yml/badge.svg)]"
        f"(https://github.com/{owner}/{slug}/actions/workflows/ci.yml)"
    )
    return "\n".join([lic_badge, lang_badge, ci_badge])


def _insert_badges(readme_text: str, badges: str) -> str:
    lines = readme_text.splitlines()
    if not lines:
        return badges + "\n"
    out: list[str] = []
    inserted = False
    for i, line in enumerate(lines):
        out.append(line)
        if not inserted and line.startswith("#") and (i + 1 >= len(lines) or lines[i + 1].strip()):
            out.append("")
            out.extend(badges.splitlines())
            inserted = True
    if not inserted:
        out = [lines[0], ""] + badges.splitlines() + lines[1:]
    return "\n".join(out).rstrip() + "\n"


def _ollama_readme_edit(prompt: str, model: str = DEFAULT_MODEL) -> str | None:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"num_predict": 600, "temperature": 0.1},
        }
    )
    r = subprocess.run(
        ["curl", "-s", f"{OLLAMA_HOST}/api/generate", "-d", payload],
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        raw = json.loads(r.stdout).get("response", "").strip()
        data = json.loads(raw)
        content = data.get("readme") or data.get("content") or ""
        return str(content).strip() or None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", r.stdout or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
            content = data.get("readme") or data.get("content") or ""
            return str(content).strip() or None
        except json.JSONDecodeError:
            return None


def apply_micro(handler: str, ws: Path, issue: dict[str, Any], repo_meta: dict[str, Any] | None, *, repo: str = "") -> bool:
    """Apply lane-1 handler. Returns True if README.md changed."""
    if handler != "micro:readme":
        return False
    readme = ws / "README.md"
    if not readme.exists():
        return False

    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    text = readme.read_text(encoding="utf-8")

    if any(k in title or k in body for k in ("badge", "shield")):
        if _has_badges(text):
            return False
        updated = _insert_badges(text, _badge_block(repo or (repo_meta or {}).get("name", ""), ws))
        if updated == text:
            return False
        readme.write_text(updated, encoding="utf-8")
        return True

    issue_title = issue.get("title") or ""
    issue_body = (issue.get("body") or "")[:800]
    prompt = f"""You edit README.md only for a GitHub issue fix. Return JSON with key "readme" (full file).

Repository: {repo or (repo_meta or {}).get('name', '')}
Issue: {issue_title}
Instructions: {issue_body}

Current README.md:
```
{text[:3500]}
```

Rules:
- Output the complete updated README.md in "readme"
- Touch README.md only; preserve existing sections
- Keep under 120 lines
- No HTML entities like &amp;
"""
    edited = _ollama_readme_edit(prompt)
    if not edited or edited == text:
        return False
    if len(edited) > 12000:
        return False
    readme.write_text(edited.rstrip() + "\n", encoding="utf-8")
    return True