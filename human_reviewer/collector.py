"""Harvest human-reviewed PR discourse from GitHub for Human Tower training."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = AGENT_ROOT / "human-reviewer"
CORPUS_PATH = AGENT_ROOT / "flight-recorder" / "human-reviews.jsonl"
ISSUE_REF = re.compile(r"(?:close[sd]?|fix(?:e[sd])?)\s+#(\d+)", re.I)
DIFF_MAX = 12000
REVIEW_MAX = 1200
COLLECT_SLEEP_SECS = 0.8


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
    owner, name = repo.split("/", 1)
    result = _run(["gh", "api", f"repos/{owner}/{name}/pulls/{pr_number}", "-H", "Accept: application/vnd.github.diff"], check=False)
    if result.returncode == 0 and result.stdout:
        return result.stdout[:DIFF_MAX]
    result = _run(["gh", "pr", "diff", str(pr_number), "-R", repo], check=False)
    return (result.stdout or "")[:DIFF_MAX]


def _fetch_pr_files(repo: str, pr_number: int) -> list[str]:
    owner, name = repo.split("/", 1)
    data = gh_json(["api", f"repos/{owner}/{name}/pulls/{pr_number}/files?per_page=100"])
    if not isinstance(data, list):
        return []
    return [str(f.get("filename", "")) for f in data if f.get("filename")]


def _fetch_issue_context(repo: str, issue_num: int) -> dict[str, str]:
    owner, name = repo.split("/", 1)
    data = gh_json(["api", f"repos/{owner}/{name}/issues/{issue_num}"])
    if not isinstance(data, dict):
        return {}
    labels = ", ".join(l.get("name", "") for l in (data.get("labels") or []) if isinstance(l, dict))
    return {
        "title": str(data.get("title", ""))[:300],
        "body": str(data.get("body") or "")[:1500],
        "labels": labels[:200],
        "state": str(data.get("state", "")),
    }


def _language_tags(files: list[str]) -> list[str]:
    ext_map = {
        ".py": "python",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".h": "c_header",
        ".cu": "cuda",
        ".mlir": "mlir",
        ".td": "tablegen",
        ".yml": "ci",
        ".yaml": "ci",
        ".md": "docs",
        ".toml": "config",
    }
    tags: set[str] = set()
    for f in files:
        for ext, tag in ext_map.items():
            if f.endswith(ext):
                tags.add(tag)
    return sorted(tags)


def _complexity_score(record: dict[str, Any]) -> tuple[int, list[str]]:
    """Score PR complexity for versatile training coverage."""
    score = 0
    tags: list[str] = []
    files = record.get("files_changed") or []
    n_files = len(files)
    delta = int(record.get("additions") or 0) + int(record.get("deletions") or 0)
    reviews = record.get("reviews") or []
    conv = record.get("conversation") or []
    line_comments = record.get("review_comments") or []

    if n_files >= 8:
        score += 25
        tags.append("multi_file")
    elif n_files >= 3:
        score += 12
        tags.append("multi_file")

    if delta >= 2000:
        score += 25
        tags.append("large_diff")
    elif delta >= 400:
        score += 12
        tags.append("medium_diff")

    if any(r.get("state", "").upper() == "CHANGES_REQUESTED" for r in reviews):
        score += 20
        tags.append("iteration")
    if len(reviews) >= 2:
        score += 10
        tags.append("reviewed")
    if len(conv) >= 4:
        score += 15
        tags.append("thread_heavy")
    if len(line_comments) >= 3:
        score += 12
        tags.append("inline_feedback")

    if record.get("verdict") != "merged":
        score += 18
        tags.append("rejection")

    langs = _language_tags(files)
    if len(langs) >= 2:
        score += 10
        tags.append("multi_language")
    tags.extend(langs[:6])

    if record.get("issue_context"):
        score += 8
        tags.append("issue_linked")

    if record.get("bounty_hunter"):
        score += 5
        tags.append("bounty_hunter")

    return score, tags


def _has_training_signal(record: dict[str, Any], min_cfg: dict[str, Any] | None = None) -> bool:
    """Keep PRs with substantive human discourse (skip empty agent-only merges)."""
    if min_cfg is None:
        min_cfg = {}
    if record.get("maintainer_voice"):
        return True
    if len(record.get("review_comments") or []) >= int(min_cfg.get("or_review_comments", 1)):
        return True
    if len(record.get("conversation") or []) >= int(min_cfg.get("or_conversation", 2)):
        return True
    if any(r.get("state", "").upper() == "CHANGES_REQUESTED" for r in (record.get("reviews") or [])):
        return True
    if min_cfg.get("maintainer_voice") and not record.get("maintainer_voice"):
        return False
    # Merged with zero discourse — low value unless complexity is high
    score, _ = _complexity_score(record)
    return score >= 35


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
    if not files:
        files = _fetch_pr_files(repo, pr_number)
    body = pr.get("body") or ""
    issue_nums = _parse_issue_numbers(pr.get("title", ""), body)
    issue_context: dict[str, Any] = {}
    if issue_nums:
        issue_context = _fetch_issue_context(repo, issue_nums[0])

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
        "issue_numbers": issue_nums,
        "issue_context": issue_context,
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
    score, tags = _complexity_score(record)
    record["complexity_score"] = score
    record["complexity_tags"] = tags
    return record


def list_prs_rest(
    repo: str,
    *,
    want_merged: bool = True,
    limit: int = 50,
    pages: int = 3,
) -> list[dict[str, Any]]:
    """Paginated REST pull list — avoids GraphQL rate limits."""
    owner, name = repo.split("/", 1)
    out: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        data = gh_json(
            [
                "api",
                f"repos/{owner}/{name}/pulls?state=closed&page={page}&per_page=100&sort=updated&direction=desc",
            ]
        )
        if not isinstance(data, list) or not data:
            break
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
                return out
    return out


def search_discourse_prs(repo: str, *, limit: int = 15) -> list[int]:
    """Find PRs with substantive comment threads via GitHub search."""
    owner, name = repo.split("/", 1)
    nums: list[int] = []
    queries = [
        f"repo:{owner}/{name} is:pr is:merged comments:>2",
        f"repo:{owner}/{name} is:pr is:closed comments:>3",
        f"repo:{owner}/{name} is:pr review:changes_requested",
    ]
    for q in queries:
        result = _run(
            ["gh", "search", "prs", q, "--json", "number", "--limit", str(limit)],
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            rows = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for row in rows:
            n = int(row.get("number", 0))
            if n and n not in nums:
                nums.append(n)
        if len(nums) >= limit:
            break
    return nums[:limit]


def load_curated_prs() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "curated-prs.yaml"
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("prs") or [])


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


def _repo_limits(cfg: dict[str, Any], repo: str, default: int) -> tuple[int, int]:
    overrides = cfg.get("repo_limits") or {}
    entry = overrides.get(repo) or {}
    return int(entry.get("merged", default)), int(entry.get("rejected", default // 2))


def _try_add_record(
    record: dict[str, Any] | None,
    *,
    seen: set[str],
    out: Path,
    min_cfg: dict[str, Any],
    require_signal: bool,
) -> bool:
    if not record:
        return False
    rid = str(record.get("id", ""))
    if not rid or rid in seen:
        return False
    if require_signal and not _has_training_signal(record, min_cfg):
        return False
    append_corpus(record, out)
    seen.add(rid)
    return True


def collect_repo(
    repo: str,
    *,
    limit: int = 30,
    include_closed: bool = True,
    bounty_hunters: set[str] | None = None,
    corpus_path: Path | None = None,
    deep: bool = False,
    cfg: dict[str, Any] | None = None,
) -> int:
    out = corpus_path or CORPUS_PATH
    seen = _load_existing_ids(out)
    added = 0
    cfg = cfg or load_sources()
    min_cfg = cfg.get("min_signals") or {}
    merged_lim, rejected_lim = _repo_limits(cfg, repo, limit)
    if deep:
        merged_lim = max(merged_lim, limit)
        rejected_lim = max(rejected_lim, limit // 2)

    def ingest(pr_num: int, *, require_signal: bool = True) -> None:
        nonlocal added
        time.sleep(COLLECT_SLEEP_SECS)
        record = collect_pr(repo, pr_num, bounty_hunters=bounty_hunters)
        if _try_add_record(record, seen=seen, out=out, min_cfg=min_cfg, require_signal=require_signal):
            added += 1

    if deep:
        for pr in list_prs_rest(repo, want_merged=True, limit=merged_lim, pages=4):
            ingest(int(pr["number"]))
        for pr_num in search_discourse_prs(repo, limit=max(merged_lim // 2, 10)):
            ingest(pr_num)
        if include_closed:
            for pr in list_prs_rest(repo, want_merged=False, limit=rejected_lim, pages=4):
                ingest(int(pr["number"]), require_signal=True)
    else:
        for pr in list_prs(repo, state="merged", limit=merged_lim):
            ingest(int(pr["number"]))
        if include_closed:
            for pr in list_prs(repo, state="closed", limit=rejected_lim):
                if pr.get("mergedAt"):
                    continue
                ingest(int(pr["number"]), require_signal=True)

    return added


def collect_curated(*, bounty_hunters: set[str] | None = None, corpus_path: Path | None = None) -> int:
    out = corpus_path or CORPUS_PATH
    seen = _load_existing_ids(out)
    cfg = load_sources()
    min_cfg = cfg.get("min_signals") or {}
    added = 0
    for entry in load_curated_prs():
        repo = str(entry.get("repo", ""))
        pr_num = int(entry.get("number", 0))
        if not repo or not pr_num:
            continue
        time.sleep(COLLECT_SLEEP_SECS)
        record = collect_pr(repo, pr_num, bounty_hunters=bounty_hunters)
        if not record:
            continue
        extra_tags = entry.get("tags") or []
        if extra_tags:
            record["complexity_tags"] = sorted(set((record.get("complexity_tags") or []) + list(extra_tags)))
            record["curated"] = True
        if _try_add_record(record, seen=seen, out=out, min_cfg=min_cfg, require_signal=False):
            added += 1
    return added


def collect_all_deep(*, limit: int = 40, sleep_repos: float = 2.0) -> dict[str, int]:
    """Full Archivist pass — all sources, curated seeds, complexity filter."""
    cfg = load_sources()
    hunters = set(str(h) for h in (cfg.get("bounty_hunters") or []))
    results: dict[str, int] = {}
    results["curated"] = collect_curated(bounty_hunters=hunters)
    for repo in cfg.get("repos") or []:
        time.sleep(sleep_repos)
        n = collect_repo(repo, limit=limit, deep=True, bounty_hunters=hunters, cfg=cfg)
        results[repo] = n
    return results