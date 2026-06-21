"""Tower reviewer — shared quality gate for issue-agent and build-composer."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token|private[_-]?key)\s*[=:]\s*['\"]?[a-zA-Z0-9_\-./]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
]
JUNK_FILENAME_PREFIXES = ("python ", "cargo ", "npm ", "pip ", "make ")


@dataclass
class TowerVerdict:
    passed: bool
    confidence: str
    reasons: list[str]
    checks: dict[str, bool]
    files_changed: list[str]


def _run(cmd: list[str] | str, *, cwd: Path | None = None, shell: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, shell=shell, text=True, capture_output=True, check=False)


def changed_files(ws: Path, base_branch: str) -> list[str]:
    _run(["git", "fetch", "origin"], cwd=ws)
    result = _run(["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"], cwd=ws)
    if result.returncode != 0:
        result = _run(["git", "diff", "--name-only", "HEAD"], cwd=ws)
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def diff_text(ws: Path, base_branch: str) -> str:
    _run(["git", "fetch", "origin"], cwd=ws)
    result = _run(["git", "diff", f"origin/{base_branch}...HEAD"], cwd=ws)
    if result.returncode != 0:
        result = _run(["git", "diff", "HEAD"], cwd=ws)
    return (result.stdout or "") + (result.stderr or "")


def _load_orion():
    """Import Orion AST parser from clone or agent-workspaces."""
    candidates = [
        Path(__file__).resolve().parent.parent / "orion-ai-agent",
        Path.home() / "orion-ai-agent",
        Path.home() / "agent-workspaces" / "Nueramarcos_orion-ai-agent",
    ]
    for root in candidates:
        orion_dir = root / "Orion"
        if (orion_dir / "ast_parser.py").exists():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from Orion.ast_parser import ProjectAnalyser, parse_file  # type: ignore

            return ProjectAnalyser, parse_file
    return None, None


def orion_ast_check(ws: Path, changed_py: list[str]) -> tuple[bool, list[str]]:
    """Reject if changed Python files have syntax errors or deleted exported symbols with callers."""
    ProjectAnalyser, parse_file = _load_orion()
    if parse_file is None:
        return True, []

    reasons: list[str] = []
    for rel in changed_py:
        path = ws / rel
        if not path.exists():
            continue
        result = parse_file(path)
        if result.errors:
            reasons.append(f"Orion syntax error in {rel}: {result.errors[0][:120]}")
            continue
        if not path.is_file():
            continue

    deleted_py = []
    for rel in changed_py:
        if not (ws / rel).exists():
            deleted_py.append(rel)

    if deleted_py and ProjectAnalyser is not None:
        try:
            analyser = ProjectAnalyser(str(ws)).scan()
            for rel in deleted_py:
                stem = Path(rel).stem
                if stem == "__init__":
                    continue
                callers = analyser.find_callers(stem)
                if callers:
                    sample = ", ".join(Path(p).name for p in list(callers)[:3])
                    reasons.append(f"Orion: deleted {rel} but symbol still called from {sample}")
        except Exception:
            pass

    return not reasons, reasons


def tower_review_files(
    ws: Path,
    files: list[str],
    *,
    max_files: int = 8,
    issue_summary: str = "",
    on_log: Callable[[str], None] | None = None,
) -> TowerVerdict:
    """Tower gate on explicit file list (build-composer / non-git)."""
    log = on_log or (lambda _msg: None)
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    rel_files = [f for f in files if f.strip()]
    diff_parts: list[str] = []

    checks["has_changes"] = bool(rel_files)
    if not rel_files:
        reasons.append("No files to review")
        return TowerVerdict(False, "high", reasons, checks, rel_files)

    checks["file_count"] = len(rel_files) <= max_files
    if len(rel_files) > max_files:
        reasons.append(f"Too many files ({len(rel_files)} > {max_files})")

    forbidden = [f for f in rel_files if f.endswith(".env") or Path(f).name.startswith(".env")]
    checks["no_env_files"] = not forbidden
    if forbidden:
        reasons.append(f"Forbidden env files: {', '.join(forbidden)}")

    junk = [f for f in rel_files if any(Path(f).name.startswith(p) for p in JUNK_FILENAME_PREFIXES)]
    checks["no_junk_filenames"] = not junk
    if junk:
        reasons.append(f"Junk filenames: {', '.join(junk)}")

    for rel in rel_files:
        path = ws / rel
        if path.is_file():
            try:
                diff_parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    diff = "\n".join(diff_parts)
    secret_hits = [pat.pattern[:40] for pat in SECRET_PATTERNS if pat.search(diff)]
    checks["no_secrets"] = not secret_hits
    if secret_hits:
        reasons.append("Possible secrets/credentials in file content")

    checks["diff_size"] = len(diff) <= 12000
    if not checks["diff_size"]:
        reasons.append(f"Content too large ({len(diff)} chars)")

    py_files = [f for f in rel_files if f.endswith(".py") and (ws / f).exists()]
    if py_files:
        ruff = _run(["ruff", "check", "--select=E9,F821", *py_files], cwd=ws)
        ruff_out = (ruff.stdout or "") + (ruff.stderr or "")
        critical = [line for line in ruff_out.splitlines() if any(code in line for code in ("E9", "F821", "SyntaxError"))]
        checks["ruff_critical"] = ruff.returncode == 0 or not critical
        if critical and ruff.returncode != 0:
            reasons.append(f"Ruff critical issues in {len(critical)} line(s)")
    else:
        checks["ruff_critical"] = True

    orion_ok, orion_reasons = orion_ast_check(ws, rel_files)
    checks["orion_ast"] = orion_ok
    reasons.extend(orion_reasons)

    passed = all(checks.values())
    confidence = "med" if passed else "high"
    log(f"Tower {'PASS' if passed else 'REJECT'}: {len(rel_files)} files" + (f" — {issue_summary[:60]}" if issue_summary else ""))
    return TowerVerdict(passed, confidence, reasons, checks, rel_files)


def tower_review_workspace(
    ws: Path,
    *,
    base_branch: str = "main",
    max_files: int = 8,
    issue_summary: str = "",
    on_log: Callable[[str], None] | None = None,
) -> TowerVerdict:
    """Deterministic Tower gate on a git workspace."""
    log = on_log or (lambda _msg: None)
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    files = changed_files(ws, base_branch)
    diff = diff_text(ws, base_branch)

    checks["has_changes"] = bool(files)
    if not files:
        reasons.append("No file changes to review")
        return TowerVerdict(False, "high", reasons, checks, files)

    checks["file_count"] = len(files) <= max_files
    if len(files) > max_files:
        reasons.append(f"Diff touches {len(files)} files (max {max_files}): {', '.join(files[:12])}")

    forbidden = [f for f in files if f.endswith(".env") or f.split("/")[-1].startswith(".env")]
    checks["no_env_files"] = not forbidden
    if forbidden:
        reasons.append(f"Forbidden env files in diff: {', '.join(forbidden)}")

    junk = [f for f in files if any(f.startswith(p) for p in JUNK_FILENAME_PREFIXES)]
    checks["no_junk_filenames"] = not junk
    if junk:
        reasons.append(f"Junk shell-command filenames: {', '.join(junk)}")

    secret_hits = [pat.pattern[:40] for pat in SECRET_PATTERNS if pat.search(diff)]
    checks["no_secrets"] = not secret_hits
    if secret_hits:
        reasons.append("Possible secrets/credentials detected in diff")

    checks["diff_size"] = len(diff) <= 12000
    if not checks["diff_size"]:
        reasons.append(f"Diff too large ({len(diff)} chars) — likely drive-by refactor")

    py_files = [f for f in files if f.endswith(".py") and (ws / f).exists()]
    if py_files:
        ruff = _run(["ruff", "check", "--select=E9,F821", *py_files], cwd=ws)
        ruff_out = (ruff.stdout or "") + (ruff.stderr or "")
        critical = [line for line in ruff_out.splitlines() if any(code in line for code in ("E9", "F821", "SyntaxError"))]
        checks["ruff_critical"] = ruff.returncode == 0 or not critical
        if critical and ruff.returncode != 0:
            reasons.append(f"Ruff critical issues in {len(critical)} line(s)")
    else:
        checks["ruff_critical"] = True

    orion_ok, orion_reasons = orion_ast_check(ws, files)
    checks["orion_ast"] = orion_ok
    reasons.extend(orion_reasons)

    passed = all(checks.values())
    confidence = "high" if passed and len(files) <= 2 else ("med" if passed else "high")
    detail = "PASS" if passed else "REJECT"
    log(f"Tower {detail}: {len(files)} files, confidence={confidence}" + (f" — {issue_summary[:60]}" if issue_summary else ""))
    return TowerVerdict(passed, confidence, reasons, checks, files)


def tower_block_comment(verdict: TowerVerdict) -> str:
    lines = ["🤖 **Issue Agent — Tower rejected this diff**", ""]
    lines.extend(f"- {r}" for r in verdict.reasons)
    if verdict.files_changed:
        lines.append("")
        lines.append("**Files changed:**")
        lines.extend(f"- `{f}`" for f in verdict.files_changed[:15])
    lines.append("")
    lines.append(f"*Confidence: {verdict.confidence} · re-queue with agent-triage after adjusting scope*")
    return "\n".join(lines)