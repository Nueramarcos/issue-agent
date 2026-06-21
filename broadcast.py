"""Broadcast — compose X posts for Issue Agent merges and fleet wins."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
            "x.com/Nueramarcos",
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


def post_to_x(text: str) -> tuple[bool, str]:
    """Post via X API v2 if X_BEARER_TOKEN is set in environment."""
    token = os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN")
    if not token:
        return False, "X_BEARER_TOKEN not set — saved to broadcasts/latest.txt only"
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
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "curl failed")[:300]
    try:
        data = json.loads(result.stdout or "{}")
        if data.get("data", {}).get("id"):
            return True, f"posted tweet id {data['data']['id']}"
    except json.JSONDecodeError:
        pass
    return False, (result.stdout or result.stderr or "unknown X API error")[:300]


def broadcast_merge(
    repo: str,
    broadcast_dir: Path,
    *,
    issue_num: int | None = None,
    pr_url: str = "",
    title: str = "",
    auto_post: bool = False,
    clipboard: bool = True,
) -> dict[str, Any]:
    text = compose_merge_post(repo, issue_num=issue_num, pr_url=pr_url, title=title)
    path = save_broadcast(text, broadcast_dir)
    result: dict[str, Any] = {"text": text, "path": str(path), "posted": False}
    if clipboard:
        result["clipboard"] = copy_to_clipboard(text)
    if auto_post or os.environ.get("X_AUTO_POST", "").lower() in ("1", "true", "yes"):
        ok, detail = post_to_x(text)
        result["posted"] = ok
        result["post_detail"] = detail
    return result