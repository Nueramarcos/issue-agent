"""Human Tower — RAG-backed maintainer-voice reviewer gate."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from human_reviewer.export import INSTRUCTION, load_corpus, row_to_example

AGENT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "customs-reviewer-ft-1.5b"
FALLBACK_MODEL = "customs-reviewer-1.5b"


@dataclass
class HumanTowerVerdict:
    passed: bool
    confidence: str
    review_comment: str
    reasons: list[str]
    similar_prs: list[str]
    model: str


def _ollama_json(prompt: str, model: str) -> dict[str, Any]:
    import os

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"})
    result = subprocess.run(
        ["curl", "-s", f"{host}/api/generate", "-d", payload],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return {"verdict": "reject", "review_comment": "ollama unavailable", "confidence": "low", "reasons": ["model offline"]}
    try:
        text = json.loads(result.stdout).get("response", "").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", result.stdout or "", re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {"verdict": "reject", "review_comment": (result.stdout or "")[:500], "confidence": "low"}


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text)}


def _similar_examples(
    repo: str,
    files: list[str],
    issue_summary: str,
    *,
    k: int = 3,
) -> list[dict[str, Any]]:
    corpus = load_corpus()
    if not corpus:
        return []
    query_tokens = _tokenize(issue_summary + " " + " ".join(files))
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in corpus:
        row_text = " ".join(
            [
                str(row.get("repo", "")),
                str(row.get("title", "")),
                " ".join(row.get("files_changed") or []),
                str(row.get("maintainer_voice", "")),
            ]
        )
        row_tokens = _tokenize(row_text)
        if not row_tokens:
            continue
        overlap = len(query_tokens & row_tokens) / max(len(query_tokens), 1)
        if row.get("repo") == repo:
            overlap += 0.35
        if row.get("verdict") == "merged":
            overlap += 0.15
        if row.get("maintainer_voice"):
            overlap += 0.1
        scored.append((overlap, row))
    scored.sort(key=lambda x: -x[0])
    return [row for _, row in scored[:k]]


def _diff_excerpt(ws: Path, base_branch: str = "main") -> str:
    subprocess.run(["git", "fetch", "origin"], cwd=ws, capture_output=True, check=False)
    result = subprocess.run(
        ["git", "diff", f"origin/{base_branch}...HEAD"],
        cwd=ws,
        text=True,
        capture_output=True,
        check=False,
    )
    return (result.stdout or "")[:4000]


def _changed_files(ws: Path, base_branch: str = "main") -> list[str]:
    subprocess.run(["git", "fetch", "origin"], cwd=ws, capture_output=True, check=False)
    result = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
        cwd=ws,
        text=True,
        capture_output=True,
        check=False,
    )
    return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]


def _resolve_model(model: str) -> str:
    result = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=False)
    names = result.stdout or ""
    if model in names or f"{model}:" in names:
        return model
    if FALLBACK_MODEL in names or f"{FALLBACK_MODEL}:" in names:
        return FALLBACK_MODEL
    return "qwen2.5-coder:1.5b"


_DOC_SUFFIXES = (".md", ".rst", ".txt")
_DOC_NAMES = {"LICENSE", "CONTRIBUTING", "CODEOWNERS", ".gitignore"}


def _is_docs_only(files: list[str]) -> bool:
    if not files:
        return False
    for f in files:
        base = Path(f).name
        if base in _DOC_NAMES:
            continue
        if any(f.endswith(s) for s in _DOC_SUFFIXES):
            continue
        return False
    return True


def human_tower_review(
    ws: Path,
    repo: str,
    *,
    issue_summary: str = "",
    base_branch: str = "main",
    model: str = DEFAULT_MODEL,
    k: int = 3,
) -> HumanTowerVerdict:
    model = _resolve_model(model)
    files = _changed_files(ws, base_branch)
    diff = _diff_excerpt(ws, base_branch)

    if not files or not diff.strip():
        return HumanTowerVerdict(
            passed=False,
            confidence="high",
            review_comment="No diff to review — agent produced no commits.",
            reasons=["empty_diff"],
            similar_prs=[],
            model=model,
        )

    if _is_docs_only(files) and len(diff.splitlines()) <= 80:
        return HumanTowerVerdict(
            passed=True,
            confidence="high",
            review_comment="Docs-only change within scope — auto-approved.",
            reasons=["docs_only_fast_path"],
            similar_prs=[],
            model=model,
        )

    similar = _similar_examples(repo, files, issue_summary, k=k)

    examples_text = []
    similar_ids: list[str] = []
    for row in similar:
        ex = row_to_example(row)
        if not ex:
            continue
        similar_ids.append(str(row.get("id", "")))
        examples_text.append(
            f"--- example {row.get('id')} ({row.get('verdict')}) ---\n"
            f"INPUT:\n{ex['input'][:1200]}\n"
            f"OUTPUT:\n{ex['output']}\n"
        )

    prompt = f"""{INSTRUCTION}

Study these real maintainer decisions from merged/rejected PRs:
{chr(10).join(examples_text) or '(no corpus examples yet — be conservative)'}

Now review this NEW change:
repo: {repo}
issue: {issue_summary[:200]}
files: {', '.join(files[:12])}
diff:
{diff[:3500]}

Respond as JSON only:
{{"verdict":"approve"|"reject","confidence":"low"|"med"|"high","review_comment":"GitHub review style","reasons":["..."]}}
"""
    parsed = _ollama_json(prompt, model)
    verdict = str(parsed.get("verdict", "reject")).lower()
    passed = verdict == "approve"
    comment = str(parsed.get("review_comment", ""))[:1200]
    reasons = parsed.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    confidence = str(parsed.get("confidence", "med"))
    return HumanTowerVerdict(
        passed=passed,
        confidence=confidence,
        review_comment=comment,
        reasons=[str(r) for r in reasons][:8],
        similar_prs=similar_ids,
        model=model,
    )