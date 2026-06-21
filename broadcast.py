"""Broadcast — compose X posts for Issue Agent merges and fleet wins."""

from __future__ import annotations

import json
import os
import subprocess
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus


def compose_merge_post(
    repo: str,
    *,
    issue_num: int | None = None,
    pr_url: str = "",
    title: str = "",
) -> str:
    short = repo.split("/")[-1]
    lines = [
        "🛫 Issue Agent merged while AFK",
        "",
        f"{short}: {title[:120]}" if title else short,
    ]
    if issue_num:
        lines.append(f"Closes #{issue_num}")
    if pr_url:
        lines.append(pr_url)
    lines.extend(
        [
            "",
            "Habitat → Tower → merge · local Ollama",
            "github.com/Nueramarcos/issue-agent",
        ]
    )
    text = "\n".join(lines)
    return text[:280] if len(text) > 280 else text


def compose_fleet_post(*, merges: int, repos: list[str]) -> str:
    repo_line = ", ".join(r.split("/")[-1] for r in repos[:4])
    extra = f" +{len(repos) - 4}" if len(repos) > 4 else ""
    return (
        f"🛫 Airport overnight: {merges} merge(s)\n"
        f"Fleet: {repo_line}{extra}\n"
        f"Local agent stack · issue-agent\n"
        f"github.com/Nueramarcos"
    )[:280]


def save_broadcast(text: str, broadcast_dir: Path) -> Path:
    broadcast_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = broadcast_dir / f"post-{ts}.txt"
    path.write_text(text + "\n")
    latest = broadcast_dir / "latest.txt"
    latest.write_text(text + "\n")
    return path


def copy_to_clipboard(text: str) -> bool:
    for cmd in (
        ["wl-copy", text],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        try:
            r = subprocess.run(cmd, input=text, text=True, capture_output=True, check=False)
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def x_compose_url(text: str) -> str:
    """Browser intent URL — no API keys required."""
    return f"https://x.com/intent/tweet?text={quote_plus(text[:280])}"


def open_x_compose(text: str) -> tuple[bool, str]:
    url = x_compose_url(text)
    try:
        if subprocess.run(["which", "xdg-open"], capture_output=True).returncode == 0:
            subprocess.run(["xdg-open", url], check=False)
            return True, url
        webbrowser.open(url)
        return True, url
    except Exception as exc:
        return False, str(exc)


def _oauth1_ready() -> bool:
    keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")
    return all(os.environ.get(k) for k in keys)


def post_to_x_oauth1(text: str) -> tuple[bool, str]:
    """Post via X API v2 with OAuth 1.0a user context (recommended)."""
    if not _oauth1_ready():
        return False, "OAuth 1.0a keys missing — run: setup-x-post"

    try:
        from requests_oauthlib import OAuth1Session
    except ImportError:
        return False, "pip install requests-oauthlib (or run from aider venv)"

    session = OAuth1Session(
        os.environ["X_API_KEY"],
        client_secret=os.environ["X_API_SECRET"],
        resource_owner_key=os.environ["X_ACCESS_TOKEN"],
        resource_owner_secret=os.environ["X_ACCESS_SECRET"],
    )
    resp = session.post(
        "https://api.x.com/2/tweets",
        json={"text": text[:280]},
        timeout=30,
    )
    try:
        data = resp.json()
    except json.JSONDecodeError:
        data = {"raw": resp.text[:300]}
    if resp.status_code in (200, 201) and data.get("data", {}).get("id"):
        return True, f"posted tweet id {data['data']['id']}"
    err = data.get("detail") or data.get("title") or data.get("raw") or resp.text
    return False, f"X API {resp.status_code}: {str(err)[:300]}"


def post_to_x_bearer(text: str) -> tuple[bool, str]:
    """OAuth 2.0 user access token (less common for personal bots)."""
    token = os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")
    if not token:
        return False, "X_BEARER_TOKEN not set"
    payload = json.dumps({"text": text[:280]})
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            "https://api.x.com/2/tweets",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        data = json.loads(result.stdout or "{}")
        if data.get("data", {}).get("id"):
            return True, f"posted tweet id {data['data']['id']}"
        err = data.get("detail") or data.get("title") or result.stdout
        return False, str(err)[:300]
    except json.JSONDecodeError:
        return False, (result.stdout or result.stderr or "curl failed")[:300]


def post_to_x(text: str) -> tuple[bool, str]:
    """Post to X — prefers OAuth 1.0a, falls back to bearer token."""
    if _oauth1_ready():
        return post_to_x_oauth1(text)
    if os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN"):
        return post_to_x_bearer(text)
    return False, (
        "No X credentials — options:\n"
        "  1. issue-agent broadcast --open   (browser compose, no keys)\n"
        "  2. setup-x-post                   (one-time API key setup)\n"
        "  3. export X_AUTO_POST=1 after setup"
    )


def broadcast_merge(
    repo: str,
    broadcast_dir: Path,
    *,
    issue_num: int | None = None,
    pr_url: str = "",
    title: str = "",
    auto_post: bool = False,
    clipboard: bool = True,
    open_compose: bool = False,
) -> dict[str, Any]:
    text = compose_merge_post(repo, issue_num=issue_num, pr_url=pr_url, title=title)
    path = save_broadcast(text, broadcast_dir)
    result: dict[str, Any] = {"text": text, "path": str(path), "posted": False}
    if clipboard:
        result["clipboard"] = copy_to_clipboard(text)
    if open_compose:
        ok, detail = open_x_compose(text)
        result["compose_opened"] = ok
        result["compose_url"] = detail
    if auto_post or os.environ.get("X_AUTO_POST", "").lower() in ("1", "true", "yes"):
        ok, detail = post_to_x(text)
        result["posted"] = ok
        result["post_detail"] = detail
        if not ok and os.environ.get("X_FALLBACK_OPEN", "1").lower() not in ("0", "false"):
            open_x_compose(text)
            result["compose_fallback"] = True
    return result