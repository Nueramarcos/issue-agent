"""Export human review corpus to LoRA instruction JSONL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = AGENT_ROOT / "flight-recorder" / "human-reviews.jsonl"
DEFAULT_OUT = AGENT_ROOT / "flight-recorder" / "human-reviewer-lora.jsonl"

INSTRUCTION = (
    "You are Human Tower — a senior maintainer reviewing a pull request. "
    "Mimic real human bounty hunters and maintainers: scrutinize scope, tests, "
    "and engineering judgment. AI-assisted PRs are acceptable only when a human "
    "refined and validated them. Approve or reject with a GitHub-style review comment."
)


def load_corpus(path: Path | None = None) -> list[dict[str, Any]]:
    src = path or CORPUS_PATH
    if not src.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _input_block(row: dict[str, Any]) -> str:
    parts = [
        f"repo: {row.get('repo', '')}",
        f"pr: #{row.get('pr_number', '')} — {row.get('title', '')[:200]}",
        f"author: {row.get('author', '')}",
        f"files: {', '.join((row.get('files_changed') or [])[:12])}",
        f"delta: +{row.get('additions', 0)}/-{row.get('deletions', 0)}",
    ]
    if row.get("issue_numbers"):
        parts.append(f"issues: {', '.join('#' + str(n) for n in row['issue_numbers'])}")
    body = (row.get("body") or "").strip()
    if body:
        parts.append(f"pr_body:\n{body[:800]}")
    diff = (row.get("diff_excerpt") or "").strip()
    if diff:
        parts.append(f"diff_excerpt:\n{diff[:4000]}")
    prior = row.get("reviews") or []
    conv = row.get("conversation") or []
    if prior or conv:
        snippets = []
        for r in prior[:3]:
            snippets.append(f"@{r.get('author')}[{r.get('state')}]: {r.get('body', '')[:300]}")
        for c in conv[:3]:
            snippets.append(f"@{c.get('author')}[thread]: {c.get('body', '')[:300]}")
        parts.append("prior_discourse:\n" + "\n".join(snippets))
    return "\n".join(parts)


def _output_block(row: dict[str, Any]) -> str:
    verdict = row.get("verdict", "merged")
    approve = verdict == "merged"
    voice = (row.get("maintainer_voice") or "").strip()
    if not voice and approve:
        voice = "LGTM — scoped fix, tests green, merge."
    if not voice and not approve:
        voice = "Closing — needs human refinement before resubmit."
    return json.dumps(
        {
            "verdict": "approve" if approve else "reject",
            "confidence": "high" if voice else "med",
            "review_comment": voice[:REVIEW_CAP],
            "merge": approve,
        },
        ensure_ascii=False,
    )


REVIEW_CAP = 1200


def row_to_example(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("repo") or not row.get("pr_number"):
        return None
    verdict = row.get("verdict", "")
    if verdict not in ("merged", "closed_without_merge", "rejected"):
        return None
    if verdict != "merged" and not row.get("maintainer_voice"):
        return None
    return {
        "task": "human_review",
        "instruction": INSTRUCTION,
        "input": _input_block(row),
        "output": _output_block(row),
        "meta": {
            "repo": row.get("repo"),
            "pr_number": row.get("pr_number"),
            "author": row.get("author"),
            "bounty_hunter": bool(row.get("bounty_hunter")),
            "verdict": verdict,
        },
    }


def export_lora_dataset(
    rows: list[dict[str, Any]] | None = None,
    out_path: Path | None = None,
) -> int:
    corpus = rows if rows is not None else load_corpus()
    out = out_path or DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    examples: list[dict[str, Any]] = []
    for row in corpus:
        ex = row_to_example(row)
        if ex:
            examples.append(ex)
    with out.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "examples": len(examples),
        "corpus_rows": len(corpus),
        "merged": sum(1 for r in corpus if r.get("verdict") == "merged"),
        "rejected": sum(1 for r in corpus if r.get("verdict") != "merged"),
        "with_maintainer_voice": sum(1 for r in corpus if r.get("maintainer_voice")),
        "path": str(out),
    }
    out.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return len(examples)


def stats(path: Path | None = None) -> dict[str, Any]:
    rows = load_corpus(path)
    by_repo: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    hunters = 0
    with_voice = 0
    for r in rows:
        repo = str(r.get("repo", "unknown"))
        by_repo[repo] = by_repo.get(repo, 0) + 1
        v = str(r.get("verdict", "unknown"))
        by_verdict[v] = by_verdict.get(v, 0) + 1
        if r.get("bounty_hunter"):
            hunters += 1
        if r.get("maintainer_voice"):
            with_voice += 1
    lora_n = len([ex for ex in (row_to_example(r) for r in rows) if ex])
    return {
        "corpus_rows": len(rows),
        "lora_examples": lora_n,
        "with_maintainer_voice": with_voice,
        "bounty_hunter_prs": hunters,
        "by_repo": by_repo,
        "by_verdict": by_verdict,
    }