"""Harvest human-reviewed PR discourse from GitHub for Human Tower training."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = AGENT_ROOT / "human-reviewer"
CORPUS_PATH = AGENT_ROOT / "flight-recorder" / "human-reviews.jsonl"
ISSUE_REF = re.compile(r"(?:close[sd]?|fix(?:e[sd])?)\s+#(\d+)", re.I)
DIFF_MAX = 8000
REVIEW_MAX = 1200


def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def gh_json(args: list[str]) -> Any:
    result = _run(["gh", *args], check=False)
    if result.returncode == 0 and (result.stdout or "").strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            pass
    stderr = (result.stderr or "").lower()
    if "rate limit" in stderr or "graphql" in stderr:
        return _gh_rest_fallback(args)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def _gh_rest_fallback(args: list[str]) -> Any:
    """REST fallback when GraphQL rate-limits gh pr/issue list."""
    if len(args) >= 4 and args[0] == "pr" and args[1] == "list" and "-R" in args:
        repo = args[args.index("-R") + 1]
        gh_state = "closed"
        want_merged = True
        if "--state" in args:
            gh_state = args[args.index("--state") + 1]
            want_merged = gh_state == "merged"
            if gh_state == "merged":
                gh_state = "closed"
        limit = 30
        if "--limit" in args:
            limit = int(args[args.index("--limit") + 1])
        owner, name = repo.split("/", 1)
        data = gh_json(
            ["api", f"repos/{owner}/{name}/pulls?state={gh_state}&per_page={min(limit, 100)}&sort=updated&direction=desc"]
        )
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for p in data:
            merged_at = p.get("merged_at")
            if want_merged and not merged_at:
                continue
            if not want_merged and merged_at:
                continue
            out.append(
                {
                    "number": p.get("number"),
                    "title": p.get("title"),
                    "author": {"login": (p.get("user") or {}).get("login", "")},
                    "mergedAt": merged_at,
                }
            )
            if len(out) >= limit:
                break
        return out
    if len(args) >= 4 and args[0] == "pr" and args[1] == "view" and "-R" in args:
        pr_num = args[2]
        repo = args[args.index("-R") + 1]
        owner, name = repo.split("/", 1)
        p = gh_json(["api", f"repos/{owner}/{name}/pulls/{pr_num}"])
        if not isinstance(p, dict):
            return None
        return {
            "number": p.get("number"),
            "title": p.get("title"),
            "body": p.get("body") or "",
            "url": p.get("html_url"),
            "author": {"login": (p.get("user") or {}).get("login", "")},
            "mergedAt": p.get("merged_at"),
            "closedAt": p.get("closed_at"),
            "state": "MERGED" if p.get("merged_at") else str(p.get("state", "")).upper(),
            "additions": p.get("additions", 0),
            "deletions": p.get("deletions", 0),
            "files": [],
        }
    return None


def load_sources(path: Path | None = None) -> dict[str, Any]:
    cfg = path or CONFIG_DIR / "sources.yaml"
    if not cfg.exists():
        starter = AGENT_ROOT.parent / "agent-habitat-os" / "human-reviewer" / "sources.starter.yaml"
        if starter.exists():
            cfg = starter
        else:
            return {"repos": ["Nueramarcos/agent-habitat-demo"], "bounty_hunters": []}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {"repos": ["Nueramarcos/agent-habitat-demo"], "bounty_hunters": []}
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    hunters_path = CONFIG_DIR / "bounty-hunters.yaml"
    if hunters_path.exists():
        hunters = yaml.safe_load(hunters_path.read_text(encoding="utf-8")) or {}
        data.setdefault("bounty_hunters", [])
        for login in hunters.get("authors") or []:
            if isinstance(login, dict):
                login = login.get("login", "")
            if login and login not in data["bounty_hunters"]:
                data["bounty_hunters"].append(str(login))
    return data


def _parse_issue_numbers(title: str, body: str) -> list[int]:
    nums: list[int] = []
    for text in (title, body or ""):
        for m in ISSUE_REF.finditer(text):
            n = int(m.group(1))
            if n not in nums:
                nums.append(n)
    return nums


MAINTAINER_HINTS = (
    "lgtm",
    "looks good",
    "merge",
    "do not use ai",
    "don't use ai",
    "wasting",
    "closing",
    "needs",
    "fix",
    "reject",
    "approved",
    "ship it",
    "nit",
    "please",
)


def _pick_maintainer_voice(
    reviews: list[dict[str, Any]],
    pr_author: str,
    conversation: list[dict[str, Any]] | None = None,
) -> str:
    """Prefer APPROVED maintainer comments; fall back to PR thread discourse."""
    candidates: list[tuple[int, str]] = []
    for rev in reviews:
        author = str(rev.get("author", {}).get("login", rev.get("user", {}).get("login", "")))
        body = (rev.get("body") or "").strip()
        if not body or author == pr_author:
            continue
        state = str(rev.get("state", "")).upper()
        score = 0
        if state == "APPROVED":
            score += 10
        elif state == "CHANGES_REQUESTED":
            score += 8
        elif state == "COMMENTED":
            score += 5
        elif state == "DISMISSED":
            score += 2
        low = body.lower()
        if any(h in low for h in MAINTAINER_HINTS):
            score += 4
        score += min(len(body) // 40, 5)
        candidates.append((score, body[:REVIEW_MAX]))
    for msg in conversation or []:
        author = str(msg.get("user", {}).get("login", ""))
        body = (msg.get("body") or "").strip()
        if not body or author == pr_author:
            continue
        low = body.lower()
        score = 3 + min(len(body) // 50, 6)
        if any(h in low for h in MAINTAINER_HINTS):
            score += 6
        if author in ("geohot", "dmlc", "syb0rg"):
            score += 2
        candidates.append((score, body[:REVIEW_MAX]))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def _fetch_pr_reviews(repo: str, pr_number: int) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    data = gh_json(["api", f"repos/{owner}/{name}/pulls/{pr_number}/reviews", "--paginate"])
    return data if isinstance(data, list) else []


def _fetch_review_comments(repo: str, pr_number: int) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    data = gh_json(["api", f"repos/{owner}/{name}/pulls/{pr_number}/comments", "--paginate"])
    return data if isinstance(data, list) else []


def _fetch_pr_conversation(repo: str, pr_number: int) -> list[dict[str, Any]]:
    """PR thread comments (tinygrad-style — often no formal reviews)."""
    owner, name = repo.split("/", 1)
    data = gh_json(["api", f"repos/{owner}/{name}/issues/{pr_number}/comments", "--paginate"])
    return data if isinstance(data, list) else []


def _fetch_diff(repo: str, pr_number: int) -> str:
    result = _run(["gh", "pr", "diff", str(pr_number), "-R", repo], check=False)
    text = (result.stdout or "")[:DIFF_MAX]
    return text


def _normalize_review(rev: dict[str, Any]) -> dict[str, str]:
    return {
        "author": str(rev.get("user", {}).get("login", "")),
        "state": str(rev.get("state", "")),
        "body": (rev.get("body") or "").strip()[:REVIEW_MAX],
        "submitted_at": str(rev.get("submitted_at", "")),
    }


def _normalize_comment(c: dict[str, Any]) -> dict[str, str]:
    return {
        "author": str(c.get("user", {}).get("login", "")),
        "path": str(c.get("path", "")),
        "body": (c.get("body") or "").strip()[:600],
    }


def collect_pr(
    repo: str,
    pr_number: int,
    *,
    bounty_hunters: set[str] | None = None,
) -> dict[str, Any] | None:
    pr = gh_json(
        [
            "pr",
            "view",
            str(pr_number),
            "-R",
            repo,
            "--json",
            "number,title,body,url,author,mergedAt,closedAt,state,additions,deletions,files",
        ]
    )
    if not pr:
        return None

    author = str(pr.get("author", {}).get("login", ""))
    state = str(pr.get("state", "")).upper()
    if state == "MERGED":
        verdict = "merged"
    elif state == "CLOSED":
        verdict = "closed_without_merge"
    else:
        verdict = "rejected"

    reviews_raw = _fetch_pr_reviews(repo, pr_number)
    comments_raw = _fetch_review_comments(repo, pr_number)
    conversation_raw = _fetch_pr_conversation(repo, pr_number)
    reviews = [_normalize_review(r) for r in reviews_raw if (r.get("body") or "").strip()]
    review_comments = [_normalize_comment(c) for c in comments_raw if (c.get("body") or "").strip()]
    conversation = [
        {
            "author": str(c.get("user", {}).get("login", "")),
            "body": (c.get("body") or "").strip()[:REVIEW_MAX],
            "created_at": str(c.get("created_at", "")),
        }
        for c in conversation_raw
        if (c.get("body") or "").strip()
    ]

    maintainer_voice = _pick_maintainer_voice(reviews_raw, author, conversation_raw)
    if not maintainer_voice and review_comments:
        maintainer_voice = review_comments[0]["body"]

    files = [f.get("path", "") for f in (pr.get("files") or []) if f.get("path")]
    body = pr.get("body") or ""

    record: dict[str, Any] = {
        "id": f"{repo}#{pr_number}",
        "repo": repo,
        "pr_number": pr_number,
        "pr_url": pr.get("url", ""),
        "title": pr.get("title", ""),
        "body": body[:2000],
        "author": author,
        "merged_at": pr.get("mergedAt") or "",
        "closed_at": pr.get("closedAt") or "",
        "verdict": verdict,
        "issue_numbers": _parse_issue_numbers(pr.get("title", ""), body),
        "files_changed": files[:30],
        "additions": int(pr.get("additions") or 0),
        "deletions": int(pr.get("deletions") or 0),
        "diff_excerpt": _fetch_diff(repo, pr_number),
        "reviews": reviews[:12],
        "review_comments": review_comments[:20],
        "conversation": conversation[:20],
        "maintainer_voice": maintainer_voice,
        "bounty_hunter": author in (bounty_hunters or set()),
        "source": "gh_collect",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return record


def list_prs(
    repo: str,
    *,
    state: str = "merged",
    author: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    args = [
        "pr",
        "list",
        "-R",
        repo,
        "--state",
        state,
        "--json",
        "number,title,author,mergedAt",
        "--limit",
        str(limit),
    ]
    if author:
        args.extend(["--author", author])
    data = gh_json(args)
    return data if isinstance(data, list) else []


def append_corpus(record: dict[str, Any], path: Path | None = None) -> None:
    out = path or CORPUS_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("id"):
                ids.add(str(row["id"]))
        except json.JSONDecodeError:
            continue
    return ids


def collect_repo(
    repo: str,
    *,
    limit: int = 30,
    include_closed: bool = True,
    bounty_hunters: set[str] | None = None,
    corpus_path: Path | None = None,
) -> int:
    out = corpus_path or CORPUS_PATH
    seen = _load_existing_ids(out)
    added = 0

    for state in ("merged",):
        for pr in list_prs(repo, state=state, limit=limit):
            pr_num = int(pr["number"])
            rid = f"{repo}#{pr_num}"
            if rid in seen:
                continue
            record = collect_pr(repo, pr_num, bounty_hunters=bounty_hunters)
            if record:
                append_corpus(record, out)
                seen.add(rid)
                added += 1

    if include_closed:
        for pr in list_prs(repo, state="closed", limit=min(limit, 15)):
            if pr.get("mergedAt"):
                continue
            pr_num = int(pr["number"])
            rid = f"{repo}#{pr_num}"
            if rid in seen:
                continue
            record = collect_pr(repo, pr_num, bounty_hunters=bounty_hunters)
            if record and record.get("maintainer_voice"):
                append_corpus(record, out)
                seen.add(rid)
                added += 1

    return added