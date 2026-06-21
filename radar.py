"""Radar — scout context enrichment via GitHub search + optional web hints."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any
from urllib.parse import quote_plus
from urllib.request import urlopen


def _gh_json(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def _title_keywords(title: str, *, max_words: int = 5) -> str:
    stop = {"fix", "add", "the", "for", "and", "with", "from", "that", "this", "issue"}
    words = [w for w in re.findall(r"[A-Za-z0-9_./+-]{3,}", title) if w.lower() not in stop]
    return " ".join(words[:max_words])


def github_context(repo: str, number: int | None = None) -> dict[str, Any]:
    """Closed-issue precedents + labels from GitHub (no scraping)."""
    ctx: dict[str, Any] = {"repo": repo, "number": number}
    if number:
        issue = _gh_json(["issue", "view", str(number), "-R", repo, "--json", "title,labels,comments,state,url"])
        if isinstance(issue, dict):
            comments = issue.get("comments")
            comment_count = None
            if isinstance(comments, dict):
                comment_count = comments.get("totalCount")
            elif isinstance(comments, list):
                comment_count = len(comments)
            ctx["issue"] = {
                "title": issue.get("title"),
                "labels": [l.get("name") for l in issue.get("labels") or [] if isinstance(l, dict)],
                "comments": comment_count,
                "url": issue.get("url"),
            }
    keywords = _title_keywords(str(ctx.get("issue", {}).get("title") or repo.split("/")[-1]))
    if keywords:
        search = _gh_json(
            [
                "search",
                "issues",
                "--json",
                "title,number,state,url",
                "-L",
                "5",
                "-R",
                repo,
                "--",
                f"is:closed {keywords}",
            ]
        )
        if isinstance(search, list):
            ctx["similar_closed"] = search[:5]
    return ctx


def web_hint(query: str, *, max_chars: int = 400) -> str:
    """Lightweight doc hint from DuckDuckGo instant answers (best-effort)."""
    try:
        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_redirect=1"
        with urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        parts = [
            str(data.get("AbstractText") or ""),
            str(data.get("Answer") or ""),
        ]
        text = " ".join(p for p in parts if p).strip()
        if data.get("AbstractURL"):
            text = f"{text} ({data['AbstractURL']})".strip()
        return text[:max_chars]
    except Exception:
        return ""


def enrich_opportunity(item: dict[str, Any], *, web: bool = False) -> dict[str, Any]:
    """Attach Radar context to a scout opportunity."""
    repo = str(item.get("repo") or "")
    number = item.get("number")
    num = int(number) if number else None
    enriched = dict(item)
    enriched["radar"] = github_context(repo, num)
    if web:
        title = str(item.get("title") or repo)
        hint = web_hint(f"{repo} {title} github issue fix")
        if hint:
            enriched["radar"]["web_hint"] = hint
    return enriched