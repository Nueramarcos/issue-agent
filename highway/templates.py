"""Lane 0 deterministic templates — zero Ollama tokens."""

from __future__ import annotations

import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from highway.package_root import detect_package_root


def _owner(repo_meta: dict[str, Any] | None) -> str:
    highway = (repo_meta or {}).get("highway") or {}
    if isinstance(highway, dict) and highway.get("github_owner"):
        return str(highway["github_owner"])
    return __import__("os").environ.get("HABITAT_GITHUB_OWNER", "Nueramarcos")


def _test_hint(repo_meta: dict[str, Any] | None) -> str:
    cmd = (repo_meta or {}).get("test_command") or "python3 -m pytest -q"
    if "pytest" in cmd:
        return "python3 -m pytest -q"
    return cmd.split("&&")[-1].strip() if "&&" in cmd else cmd


def apply_template(handler: str, ws: Path, issue: dict[str, Any], repo_meta: dict[str, Any] | None) -> bool:
    """Apply a lane-0 handler. Returns True if workspace was modified."""
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    year = datetime.now(timezone.utc).year
    owner = _owner(repo_meta)
    pkg = detect_package_root(ws, repo_meta)

    if handler == "template:license":
        if "license" not in title and "license" not in body:
            return False
        target = ws / "LICENSE"
        if target.exists():
            return False
        target.write_text(
            textwrap.dedent(
                f"""\
                MIT License

                Copyright (c) {year} {owner}

                Permission is hereby granted, free of charge, to any person obtaining a copy
                of this software and associated documentation files (the "Software"), to deal
                in the Software without restriction, including without limitation the rights
                to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
                copies of the Software, and to permit persons to whom the Software is
                furnished to do so, subject to the following conditions:

                The above copyright notice and this permission notice shall be included in all
                copies or substantial portions of the Software.

                THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
                IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
                FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
                AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
                LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
                OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
                SOFTWARE.
                """
            ),
            encoding="utf-8",
        )
        return True

    if handler == "template:contributing":
        if "contributing" not in title and "contributing" not in body:
            return False
        target = ws / "CONTRIBUTING.md"
        if target.exists():
            return False
        test = _test_hint(repo_meta)
        target.write_text(
            textwrap.dedent(
                f"""\
                # Contributing

                ## Setup

                ```bash
                git clone <repo-url>
                cd $(basename <repo-url> .git)
                python3 -m venv .venv
                source .venv/bin/activate
                pip install -e .
                ```

                ## Tests

                ```bash
                {test}
                ```

                ## Pull requests

                Open a PR against `main`. Keep changes focused; run tests locally first.
                """
            ),
            encoding="utf-8",
        )
        return True

    if handler == "template:security":
        if not any(k in title or k in body for k in ("security", "vulnerability")):
            return False
        target = ws / "SECURITY.md"
        if target.exists():
            return False
        target.write_text(
            textwrap.dedent(
                f"""\
                # Security Policy

                ## Reporting a Vulnerability

                Open a private GitHub security advisory or email **@{owner}**.
                Include reproduction steps and impact. We aim to respond within 7 days.
                """
            ),
            encoding="utf-8",
        )
        return True

    if handler == "template:changelog":
        if "changelog" not in title and "changelog" not in body:
            return False
        target = ws / "CHANGELOG.md"
        if target.exists():
            return False
        target.write_text(
            textwrap.dedent(
                f"""\
                # Changelog

                ## [0.1.0] - {year}-01-01

                - Initial release
                """
            ),
            encoding="utf-8",
        )
        return True

    if handler == "template:codeowners":
        if "codeowners" not in title and "code owners" not in title:
            return False
        target = ws / "CODEOWNERS"
        if target.exists():
            return False
        pkg_name = pkg.name if pkg != ws else "."
        target.write_text(f"{pkg_name}/ @{owner}\n", encoding="utf-8")
        return True

    if handler == "template:gitignore":
        if "gitignore" not in title and "gitignore" not in body:
            return False
        target = ws / ".gitignore"
        patterns = [
            "__pycache__/",
            ".pytest_cache/",
            "*.pyc",
            ".venv/",
            ".issue-agent-venv/",
            "dist/",
            "*.egg-info/",
        ]
        if (ws / "Cargo.toml").exists():
            patterns.extend(["/target/", "target/", "Cargo.lock"])
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        added = []
        for pat in patterns:
            if pat not in existing:
                added.append(pat)
        if not added and target.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = existing.rstrip("\n")
        if lines and not lines.endswith("\n"):
            lines += "\n"
        if not lines:
            lines = ""
        for pat in added:
            lines += f"{pat}\n"
        target.write_text(lines, encoding="utf-8")
        return bool(added)

    if handler == "template:py_typed":
        if "py.typed" not in title and "py.typed" not in body:
            return False
        target = pkg / "py.typed"
        if target.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return True

    if handler == "template:junk_delete":
        if "junk" not in title and "accidental" not in title:
            return False
        removed = False
        for line in (issue.get("body") or "").splitlines():
            line = line.strip().lstrip("-").strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("Only ") or line.startswith("Delete "):
                continue
            candidate = ws / line
            if candidate.exists() and candidate.is_file():
                candidate.unlink()
                removed = True
        return removed

    return False