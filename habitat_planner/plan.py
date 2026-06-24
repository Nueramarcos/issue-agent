"""Generate structured fix plans from issue + repo context (local only)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
PLANS_LOG = AGENT_ROOT / "flight-recorder" / "plans.jsonl"


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    return (r.stdout or "") + (r.stderr or "")


def _rg_symbols(ws: Path, query: str, limit: int = 12) -> list[str]:
    out = _run(["rg", "-l", query, str(ws), "--glob", "!{.git,.venv,.issue-agent-venv}"], cwd=ws)
    return [ln.strip() for ln in out.splitlines() if ln.strip()][:limit]


def _list_py_modules(ws: Path) -> list[str]:
    mods: list[str] = []
    for p in sorted(ws.rglob("*.py")):
        if any(x in p.parts for x in (".venv", ".git", "__pycache__", ".issue-agent-venv")):
            continue
        rel = p.relative_to(ws)
        if len(mods) < 20:
            mods.append(str(rel))
    return mods


def _ollama_plan(prompt: str, model: str = "qwen2.5-coder:1.5b") -> dict[str, Any]:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"})
    r = subprocess.run(
        ["curl", "-s", f"{host}/api/generate", "-d", payload],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        text = json.loads(r.stdout or "{}").get("response", "").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "repo_summary": "Local scan only — model parse failed",
            "root_cause": "",
            "solution_plan": prompt[-800:],
            "files_to_touch": [],
            "tests_to_run": "pytest",
            "confidence": "low",
        }


def plan_path_for(ws: Path) -> Path:
    return ws / ".habitat-plan.json"


def generate_plan(
    ws: Path,
    repo: str,
    *,
    issue_num: int,
    issue_title: str,
    issue_body: str = "",
    model: str = "qwen2.5-coder:1.5b",
) -> dict[str, Any]:
    ws = Path(ws)
    keywords = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{3,}", f"{issue_title} {issue_body}")[:8]
    hits: list[str] = []
    for kw in keywords:
        hits.extend(_rg_symbols(ws, kw, limit=4))
    hits = list(dict.fromkeys(hits))[:15]
    modules = _list_py_modules(ws)[:15]

    prompt = f"""You are Habitat Planner. Produce a fix plan BEFORE any code changes.
Repository: {repo}
Issue #{issue_num}: {issue_title}
Issue body:
{(issue_body or '')[:1200]}

Relevant files (rg): {', '.join(hits) or 'none'}
Python modules: {', '.join(modules) or 'unknown'}

Respond JSON only:
{{
  "repo_summary": "2-3 sentences on relevant code areas",
  "root_cause": "likely bug cause",
  "solution_plan": "step-by-step minimal fix",
  "files_to_touch": ["path1", "path2"],
  "tests_to_run": "command",
  "confidence": "low|med|high"
}}"""

    parsed = _ollama_plan(prompt, model=model)
    plan: dict[str, Any] = {
        "repo": repo,
        "issue_num": issue_num,
        "issue_title": issue_title,
        "repo_summary": str(parsed.get("repo_summary", ""))[:800],
        "root_cause": str(parsed.get("root_cause", ""))[:500],
        "solution_plan": str(parsed.get("solution_plan", ""))[:1200],
        "files_to_touch": list(parsed.get("files_to_touch") or hits[:6]),
        "tests_to_run": str(parsed.get("tests_to_run", "pytest")),
        "confidence": str(parsed.get("confidence", "med")),
        "context_files": hits,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    plan_path_for(ws).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    append_plan_record(plan)
    return plan


def append_plan_record(plan: dict[str, Any]) -> None:
    PLANS_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {**plan, "outcome": "plan_generated", "source": "habitat_planner"}
    with PLANS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_plan(ws: Path) -> dict[str, Any] | None:
    p = plan_path_for(ws)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def plan_prompt_block(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    return (
        "\n\n## Habitat Plan (execute this — do not deviate)\n"
        f"Repo context: {plan.get('repo_summary', '')}\n"
        f"Root cause: {plan.get('root_cause', '')}\n"
        f"Solution: {plan.get('solution_plan', '')}\n"
        f"Files: {', '.join(plan.get('files_to_touch') or [])}\n"
        f"Tests: {plan.get('tests_to_run', '')}\n"
    )