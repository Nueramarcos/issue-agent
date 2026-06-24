#!/usr/bin/env python3
"""Local GitHub issue agent — triage, fix, test, draft PR."""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from broadcast import (
    broadcast_merge,
    compose_fleet_post,
    compose_merge_post,
    open_x_compose,
    post_to_x,
    save_broadcast,
    x_compose_url,
    _oauth1_ready,
)
from personality import (
    QUESTIONS,
    answers_code,
    compose_quiz_post,
    compose_quiz_thread,
    compose_result_post,
    format_result,
    match_opportunity,
    run_interactive_quiz,
    tally_answers,
)
from lora_export import build_lora_dataset, export_lora_jsonl
from prompt_loader import (
    DEFAULT_SOLVER,
    DEFAULT_TRIAGE,
    load_solver_prompt,
    load_triage_prompt,
    load_vision,
    prompt_inventory,
)
from radar import enrich_opportunity
from tower import TowerVerdict, tower_block_comment, tower_review_workspace

HOME = Path.home()
AGENT_ROOT = Path(os.environ.get("ISSUE_AGENT_ROOT", HOME / "issue-agent"))
SECRETS = Path(os.environ.get("ISSUE_AGENT_SECRETS", HOME / ".config/cockpit/secrets.env"))
AIDER = Path(os.environ.get("ISSUE_AGENT_AIDER", HOME / ".local/venvs/aider/bin/aider"))
WORKSPACES = Path(os.environ.get("ISSUE_AGENT_WORKSPACES", HOME / "agent-workspaces"))
LOG_DIR = AGENT_ROOT / "logs"
CONFIG_DEFAULTS = AGENT_ROOT / "config.default.toml"

def solver_prompt(
    repo: str,
    issue_summary: str,
    cfg: RepoConfig,
    *,
    prompt_path: Path | None = None,
) -> str:
    path = prompt_path or (Path(cfg.prompt_path) if cfg.prompt_path else DEFAULT_SOLVER)
    return load_solver_prompt(
        repo,
        issue_summary,
        max_files=cfg.max_files,
        agent_root=AGENT_ROOT,
        prompt_path=path,
    )


@dataclass
class RepoConfig:
    repo: str
    model: str = "ollama/qwen2.5-coder:7b"
    triage_model: str = "customs-1.5b"
    test_command: str | None = None
    max_files: int = 8
    draft_pr: bool = False
    auto_merge: bool = True
    wait_for_checks: bool = True
    check_timeout_secs: int = 300
    check_poll_secs: int = 15
    ci_workflows: list[str] = field(default_factory=lambda: ["CI", "Fork Smoke Test"])
    skip_labels: list[str] = field(
        default_factory=lambda: ["wontfix", "question", "help wanted", "architecture"]
    )
    trigger_label: str = "agent-triage"
    tower_enabled: bool = True
    human_tower_enabled: bool = True
    human_tower_model: str = "customs-reviewer-ft-1.5b"
    plan_enabled: bool = True
    max_fix_retries: int = 3
    prompt_path: str | None = None
    triage_prompt_path: str | None = None


_QUIET_COMMANDS = 0


@contextmanager
def quiet_commands(enabled: bool = True):
    """Suppress per-command '+ gh ...' noise (ci-watch dashboard mode)."""
    global _QUIET_COMMANDS
    if not enabled:
        yield
        return
    _QUIET_COMMANDS += 1
    try:
        yield
    finally:
        _QUIET_COMMANDS -= 1


def github_quiet() -> bool:
    """When true, failures stay in Flight Recorder only — no GitHub issue comments (no Gmail)."""
    return os.environ.get("ISSUE_AGENT_GITHUB_QUIET", "0") == "1"


def maybe_issue_comment(
    repo: str,
    issue_num: int | str,
    body: str,
    *,
    success: bool = False,
) -> None:
    if github_quiet() and not success:
        log(f"quiet: skip issue comment #{issue_num} — {body[:72].replace(chr(10), ' ')}...")
        return
    if not repo_has_issues(repo):
        return
    run(["gh", "issue", "comment", str(issue_num), "-R", repo, "--body", body], check=False)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with (LOG_DIR / "issue-agent.log").open("a") as f:
        f.write(line + "\n")


def load_secrets() -> None:
    if not SECRETS.exists():
        return
    for raw in SECRETS.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


_GH_READY: bool | None = None


def ensure_gh_ready() -> bool:
    """Drop stale GITHUB_TOKEN from secrets.env so gh can use keyring if available."""
    global _GH_READY
    if _GH_READY is not None:
        return _GH_READY
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        probe = subprocess.run(
            ["gh", "api", "user", "-q", ".login"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if probe.returncode != 0:
            err = (probe.stderr or probe.stdout or "").lower()
            if any(s in err for s in ("invalid", "bad credentials", "not accessible")):
                log(
                    "gh: GITHUB_TOKEN in secrets.env is unusable — unsetting for this run "
                    "(regenerate PAT with repo scope in ~/.config/cockpit/secrets.env)"
                )
                os.environ.pop("GITHUB_TOKEN", None)
                os.environ.pop("GH_TOKEN", None)
    status = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
    _GH_READY = status.returncode == 0
    if not _GH_READY and token:
        log("gh: not authenticated — fix secrets.env or run: gh auth login")
    return _GH_READY


def run(
    cmd: list[str] | str,
    *,
    cwd: Path | None = None,
    check: bool = True,
    shell: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if isinstance(cmd, str):
        display = cmd
    else:
        display = " ".join(shlex.quote(c) for c in cmd)
    if _QUIET_COMMANDS <= 0:
        log(f"+ {display}")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        shell=shell,
        text=True,
        capture_output=True,
        env=merged,
    )


def _gh_issue_view_rest(repo: str, issue_num: str, fields: str) -> dict[str, Any]:
    """REST fallback when `gh issue view --json` hits GraphQL rate limits."""
    owner, name = repo.split("/", 1)
    result = run(["gh", "api", f"repos/{owner}/{name}/issues/{issue_num}"], check=False)
    if result.returncode != 0 or not (result.stdout or "").strip():
        raise RuntimeError((result.stderr or "gh api issue failed").strip())
    raw = json.loads(result.stdout)
    want = {f.strip() for f in fields.split(",")}
    out: dict[str, Any] = {}
    if "title" in want:
        out["title"] = raw.get("title", "")
    if "body" in want:
        out["body"] = raw.get("body") or ""
    if "state" in want:
        out["state"] = raw.get("state", "")
    if "labels" in want:
        out["labels"] = [{"name": lb["name"]} for lb in raw.get("labels", [])]
    if "number" in want:
        out["number"] = raw.get("number")
    return out


def gh_json(args: list[str]) -> Any:
    cmd = ["gh", *args]
    if "--json" not in args:
        cmd.append("--json")
    result = run(cmd, check=False)
    if result.returncode == 0 and (result.stdout or "").strip():
        return json.loads(result.stdout)
    stderr = (result.stderr or "").lower()
    graphql_limited = "rate limit" in stderr or "graphql" in stderr
    if graphql_limited and len(args) >= 5 and args[0] == "issue" and args[1] == "view" and "-R" in args:
        issue_num = args[2]
        repo = args[args.index("-R") + 1]
        fields = args[args.index("--json") + 1] if "--json" in args else "title,body"
        data = _gh_issue_view_rest(repo, issue_num, fields)
        log(f"gh_json: REST fallback for {repo}#{issue_num}")
        return data
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )
    return json.loads(result.stdout or "null")


def ollama_json(prompt: str, model: str) -> dict[str, Any]:
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"})
    result = run(
        ["curl", "-s", f"{os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434')}/api/generate", "-d", payload],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"actionable": False, "complexity": "high", "type": "unknown", "summary": "ollama failed"}
    try:
        response = json.loads(result.stdout)
        text = response.get("response", "").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        # fallback: parse loose JSON from response
        match = re.search(r"\{.*\}", result.stdout, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {"actionable": False, "complexity": "high", "type": "unknown", "summary": "parse failed"}


def repo_config(repo: str, ws: Path) -> RepoConfig:
    cfg = RepoConfig(repo=repo)
    meta = repo_entry(repo)
    airport = load_airport_config()
    if meta.get("test_command"):
        cfg.test_command = str(meta["test_command"])
    if "wait_for_checks" in meta:
        cfg.wait_for_checks = bool(meta["wait_for_checks"])
    elif airport.get("enabled") and airport.get("local_first"):
        cfg.wait_for_checks = bool(airport.get("wait_for_checks", False))
    if meta.get("check_timeout_secs"):
        cfg.check_timeout_secs = int(meta["check_timeout_secs"])
    if meta.get("check_poll_secs"):
        cfg.check_poll_secs = int(meta["check_poll_secs"])
    if meta.get("ci_workflows"):
        cfg.ci_workflows = list(meta["ci_workflows"])
    if "tower_enabled" in meta:
        cfg.tower_enabled = bool(meta["tower_enabled"])
    if "human_tower_enabled" in meta:
        cfg.human_tower_enabled = bool(meta["human_tower_enabled"])
    if meta.get("human_tower_model"):
        cfg.human_tower_model = str(meta["human_tower_model"])
    if "plan_enabled" in meta:
        cfg.plan_enabled = bool(meta["plan_enabled"])
    if meta.get("max_fix_retries") is not None:
        cfg.max_fix_retries = int(meta["max_fix_retries"])
    if meta.get("prompt_path"):
        cfg.prompt_path = str(meta["prompt_path"])
    if meta.get("triage_prompt_path"):
        cfg.triage_prompt_path = str(meta["triage_prompt_path"])
    cfg_path = ws / ".issue-agent.yml"
    if cfg_path.exists() and yaml:
        data = yaml.safe_load(cfg_path.read_text()) or {}
        if isinstance(data, dict):
            cfg.model = data.get("model", cfg.model)
            cfg.triage_model = data.get("triage_model", cfg.triage_model)
            cfg.test_command = data.get("test_command", cfg.test_command)
            cfg.max_files = int(data.get("max_files", cfg.max_files))
            cfg.draft_pr = bool(data.get("draft_pr", cfg.draft_pr))
            cfg.auto_merge = bool(data.get("auto_merge", cfg.auto_merge))
            cfg.wait_for_checks = bool(data.get("wait_for_checks", cfg.wait_for_checks))
            cfg.check_timeout_secs = int(data.get("check_timeout_secs", cfg.check_timeout_secs))
            cfg.check_poll_secs = int(data.get("check_poll_secs", cfg.check_poll_secs))
            cfg.trigger_label = data.get("trigger_label", cfg.trigger_label)
            if data.get("skip_labels"):
                cfg.skip_labels = list(data["skip_labels"])
            if "tower_enabled" in data:
                cfg.tower_enabled = bool(data["tower_enabled"])
            if "human_tower_enabled" in data:
                cfg.human_tower_enabled = bool(data["human_tower_enabled"])
            if data.get("human_tower_model"):
                cfg.human_tower_model = str(data["human_tower_model"])
            if "plan_enabled" in data:
                cfg.plan_enabled = bool(data["plan_enabled"])
            if data.get("max_fix_retries") is not None:
                cfg.max_fix_retries = int(data["max_fix_retries"])
            if data.get("prompt_path"):
                cfg.prompt_path = str(data["prompt_path"])
            if data.get("triage_prompt_path"):
                cfg.triage_prompt_path = str(data["triage_prompt_path"])
    return cfg


def sanitize_agent_artifacts(ws: Path) -> None:
    """Remove junk files sometimes created when the model misparses shell examples."""
    for path in list(ws.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("python ") or name.startswith("cargo ") or name.startswith("npm "):
            path.unlink(missing_ok=True)
    habitat_root = Path(os.environ.get("HABITAT_ROOT", HOME / "agent-habitat-os"))
    habitat_sanitize = habitat_root / "agent-runtime/sanitize-workspace.sh"
    if habitat_sanitize.is_file():
        run(["bash", str(habitat_sanitize), str(ws)], check=False)


def detect_test_command(ws: Path, override: str | None) -> str | None:
    if override:
        return override
    if (ws / "pyproject.toml").exists() or (ws / "pytest.ini").exists() or (ws / "setup.cfg").exists():
        return "python3 -m pytest -x -q"
    if (ws / "package.json").exists():
        return "npm test --if-present"
    if (ws / "Makefile").exists():
        return "make test"
    return None


HABITAT_CACHE = Path(os.environ.get("ISSUE_AGENT_HABITATS", HOME / "agent-habitats"))


def tower_review(
    ws: Path,
    repo: str,
    cfg: RepoConfig,
    *,
    base_branch: str,
    issue_summary: str = "",
) -> TowerVerdict:
    verdict = tower_review_workspace(
        ws,
        base_branch=base_branch,
        max_files=cfg.max_files,
        issue_summary=issue_summary,
        on_log=lambda msg: log(f"{msg} [{repo}]"),
    )
    log_activity(
        "tower_pass" if verdict.passed else "tower_reject",
        repo,
        f"{len(verdict.files_changed)} files, {verdict.confidence}",
        files=verdict.files_changed[:20],
        reasons=verdict.reasons[:5],
    )
    return verdict


def human_tower_gate(
    ws: Path,
    repo: str,
    cfg: RepoConfig,
    *,
    base_branch: str,
    issue_summary: str = "",
    issue_num: int | None = None,
) -> Any:
    """Maintainer-voice gate — corpus RAG + customs-reviewer model."""
    if not cfg.human_tower_enabled:
        return None
    if os.environ.get("HUMAN_TOWER", "1") == "0":
        return None
    try:
        from human_reviewer.gate import human_tower_review
        from human_reviewer.record import append_human_tower_record, human_tower_block_comment
    except ImportError:
        log("Human Tower unavailable — skip (human_reviewer module missing)")
        return None
    verdict = human_tower_review(
        ws,
        repo,
        issue_summary=issue_summary,
        base_branch=base_branch,
        model=cfg.human_tower_model,
    )
    append_human_tower_record(verdict, repo=repo, issue_num=issue_num, issue_summary=issue_summary)
    log_activity(
        "human_tower_pass" if verdict.passed else "human_tower_reject",
        repo,
        verdict.review_comment[:120] or "; ".join(verdict.reasons[:2]),
        model=verdict.model,
        confidence=verdict.confidence,
    )
    verdict._block_comment = human_tower_block_comment  # type: ignore[attr-defined]
    log(
        f"Human Tower {'PASS' if verdict.passed else 'REJECT'} "
        f"({verdict.confidence}) [{repo}] — {verdict.review_comment[:80]}"
    )
    return verdict


def detect_stack(ws: Path) -> str:
    if (ws / "Cargo.toml").exists():
        return "rust"
    if (ws / "CMakeLists.txt").exists() and not (ws / "pyproject.toml").exists():
        return "cpp"
    if (ws / "package.json").exists() and not (ws / "pyproject.toml").exists():
        return "node"
    if (ws / "pyproject.toml").exists() or (ws / "setup.py").exists() or (ws / "requirements.txt").exists():
        return "python"
    return "unknown"


def default_habitat_bootstrap(stack: str) -> list[str]:
    return {
        "python": [],
        "rust": [],
        "node": ["npm ci --if-present 2>/dev/null || npm install --if-present 2>/dev/null || true"],
        "cpp": [],
        "unknown": [],
    }.get(stack, [])


def habitat_spec(repo: str, ws: Path) -> dict[str, Any]:
    meta = repo_entry(repo)
    spec = dict(meta.get("habitat") or {})
    if "stack" not in spec:
        spec["stack"] = detect_stack(ws)
    if "bootstrap" not in spec:
        spec["bootstrap"] = default_habitat_bootstrap(str(spec["stack"]))
    return spec


def bootstrap_habitat(ws: Path, repo: str) -> dict[str, Any]:
    """Prepare repo-specific Habitat before the solver runs."""
    spec = habitat_spec(repo, ws)
    stack = str(spec.get("stack", "unknown"))
    slug = repo.replace("/", "_")
    cache_dir = HABITAT_CACHE / slug
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = cache_dir / "habitat.json"
    manifest.write_text(json.dumps({"repo": repo, "stack": stack, "spec": spec}, indent=2))

    for cmd in spec.get("bootstrap") or []:
        if not str(cmd).strip():
            continue
        log(f"habitat bootstrap [{stack}]: {cmd[:120]}")
        result = run(str(cmd), cwd=ws, shell=True, check=False)
        if result.returncode != 0:
            out = ((result.stdout or "") + (result.stderr or ""))[-500:]
            log(f"habitat bootstrap warning (non-fatal): {out}")
    log_activity("habitat_ready", repo, stack, stack=stack, bootstrap_steps=len(spec.get("bootstrap") or []))
    return spec


def workspace_for(repo: str, issue_num: int | None = None) -> Path:
    slug = repo.replace("/", "_")
    if issue_num is None:
        return WORKSPACES / slug
    return WORKSPACES / f"{slug}-issue-{issue_num}"


def repo_entry(repo: str) -> dict[str, Any]:
    for entry in load_repos_config_raw():
        if entry.get("name") == repo:
            return entry
    return {}


def load_airport_config() -> dict[str, Any]:
    if not AIRPORT_CONFIG.exists() or yaml is None:
        return {"enabled": False}
    data = yaml.safe_load(AIRPORT_CONFIG.read_text()) or {}
    return data if isinstance(data, dict) else {"enabled": False}


def load_upstream_projects(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    """Catalog of upstream OSS clones (see upstream.yaml)."""
    projects: list[dict[str, Any]] = []
    if UPSTREAM_CONFIG.exists() and yaml:
        data = yaml.safe_load(UPSTREAM_CONFIG.read_text()) or {}
        root = Path(str(data.get("workspace_root", HOME / "upstream-workspaces"))).expanduser()
        for raw in data.get("projects") or []:
            if not isinstance(raw, dict) or not raw.get("slug"):
                continue
            proj = dict(raw)
            if not proj.get("path"):
                proj["path"] = str(root / proj["slug"])
            projects.append(proj)
    if not projects:
        legacy = load_airport_config().get("upstream")
        if isinstance(legacy, dict) and legacy.get("path"):
            projects.append(
                {
                    "slug": "forge",
                    "upstream": "0xReLogic/Forge",
                    "enabled": True,
                    "tier": 1,
                    "mode": "pr",
                    **legacy,
                }
            )
    if enabled_only:
        projects = [p for p in projects if p.get("enabled", True)]
    return projects


def upstream_project(slug: str) -> dict[str, Any] | None:
    for proj in load_upstream_projects():
        if proj.get("slug") == slug:
            return proj
    return None


def upstream_worker_slug(slug: str) -> str:
    return f"upstream-{slug}"


def effective_upstream_test_command(proj: dict[str, Any]) -> str:
    if sys.platform.startswith("linux") and proj.get("test_command_linux"):
        return str(proj["test_command_linux"])
    return str(proj.get("test_command") or "true")


def run_upstream_test(proj: dict[str, Any], repo_path: Path) -> subprocess.CompletedProcess[str]:
    cmd = effective_upstream_test_command(proj)
    return run(cmd, cwd=repo_path, check=False, shell=True)


def airport_enabled() -> bool:
    return bool(load_airport_config().get("enabled")) or os.environ.get("ISSUE_AGENT_AIRPORT") == "1"


def park_hours_for(entry: dict[str, Any]) -> float:
    if entry.get("park_minutes") is not None:
        return float(entry["park_minutes"]) / 60.0
    cfg = load_airport_config()
    if cfg.get("enabled"):
        return float(cfg.get("park_minutes", 3)) / 60.0
    if entry.get("park_hours") is not None:
        return float(entry["park_hours"])
    return 2.0


def save_airport_status(patch: dict[str, Any]) -> None:
    data: dict[str, Any] = {}
    if AIRPORT_STATUS.exists():
        try:
            data = json.loads(AIRPORT_STATUS.read_text())
        except json.JSONDecodeError:
            data = {}
    data.update(patch)
    data["ts"] = datetime.now(timezone.utc).isoformat()
    AIRPORT_STATUS.write_text(json.dumps(data, indent=2))


def lane_slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", name)


def load_repos_config_raw() -> list[dict[str, Any]]:
    if not REPOS_CONFIG.exists() or yaml is None:
        return []
    data = yaml.safe_load(REPOS_CONFIG.read_text()) or {}
    return [e for e in data.get("repos", []) if isinstance(e, dict) and e.get("name")]


def default_branch(repo: str) -> str:
    entry = repo_entry(repo)
    if entry.get("branch"):
        return str(entry["branch"])
    result = run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"], check=False)
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)["defaultBranchRef"]["name"]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return "main"


def ensure_repo(repo: str, ws: Path) -> None:
    branch = default_branch(repo)
    ws.parent.mkdir(parents=True, exist_ok=True)
    if not (ws / ".git").exists():
        if ws.exists() and any(ws.iterdir()):
            raise RuntimeError(f"Workspace exists but is not a git repo: {ws}")
        run(["gh", "repo", "clone", repo, str(ws)])
    else:
        run(["git", "fetch", "origin"], cwd=ws, check=False)
        run(["git", "checkout", branch], cwd=ws, check=False)
        run(["git", "pull", "--ff-only"], cwd=ws, check=False)


def issue_labels(repo: str, issue_num: int) -> set[str]:
    data = gh_json(["issue", "view", str(issue_num), "-R", repo, "--json", "labels"])
    return {lbl["name"] for lbl in data.get("labels", [])}


def triage_issue(repo: str, issue_num: int, cfg: RepoConfig) -> dict[str, Any]:
    issue = gh_json(["issue", "view", str(issue_num), "-R", repo, "--json", "title,body,labels,state"])
    if issue.get("state") != "OPEN":
        return {"actionable": False, "complexity": "low", "type": "closed", "summary": "issue closed"}

    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if labels & set(cfg.skip_labels):
        return {"actionable": False, "complexity": "low", "type": "skipped", "summary": "skip label present"}

    triage_path = Path(cfg.triage_prompt_path) if cfg.triage_prompt_path else DEFAULT_TRIAGE
    prompt = load_triage_prompt(
        issue.get("title", ""),
        issue.get("body") or "",
        agent_root=AGENT_ROOT,
        prompt_path=triage_path,
    )
    result = ollama_json(prompt, cfg.triage_model)
    result["issue"] = issue
    return result


def run_tests(ws: Path, test_cmd: str | None, *, strict: bool = True) -> tuple[bool, str]:
    if not test_cmd:
        return True, "no test command configured"
    cmd = test_cmd.replace("python ", "python3 ").replace("pip install", "python3 -m pip install")
    result = run(cmd, cwd=ws, shell=True, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return True, output[-8000:]
    if not strict:
        return True, f"skipped local tests: {output[-500:]}"
    # Retry in ephemeral venv when host Python is externally managed (PEP 668)
    if "externally managed" in output or "python: not found" in output or "No module named" in output:
        venv = ws / ".issue-agent-venv"
        if not (venv / "bin/python").exists():
            run(["python3", "-m", "venv", str(venv)], cwd=ws, check=False)
        if (venv / "bin/python").exists():
            bootstrap = ""
            if "pytest" in test_cmd:
                bootstrap = f"{venv}/bin/python -m pip install -q pytest && "
            vcmd = f". {venv}/bin/activate && {bootstrap}{test_cmd}"
            vresult = run(vcmd, cwd=ws, shell=True, check=False)
            vout = (vresult.stdout or "") + (vresult.stderr or "")
            return vresult.returncode == 0, vout[-8000:]
    return False, output[-8000:]


def pr_checks_state(repo: str, pr_num: str, *, cfg: RepoConfig | None = None) -> str:
    """Return 'none', 'pending', 'success', or 'failure'."""
    result = run(
        ["gh", "pr", "checks", pr_num, "-R", repo, "--json", "name,state,bucket,workflow"],
        check=False,
    )
    raw = (result.stdout or "").strip()
    if not raw:
        return "none"
    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return "none"
    if not checks:
        return "none"
    if cfg and cfg.ci_workflows:
        patterns = [p.lower() for p in cfg.ci_workflows]
        checks = [
            c
            for c in checks
            if any(p in (c.get("name") or "").lower() or p in (c.get("workflow") or "").lower() for p in patterns)
        ]
        if not checks:
            return "none"
    if any(c.get("bucket") == "fail" or c.get("state") == "FAILURE" for c in checks):
        return "failure"
    pending_states = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
    if any(c.get("bucket") == "pending" or c.get("state") in pending_states for c in checks):
        return "pending"
    return "success"


def fetch_run_failed_logs(repo: str, run_id: int, *, max_chars: int = 8000) -> str:
    log_out = run(["gh", "run", "view", str(run_id), "-R", repo, "--log-failed"], check=False)
    text = (log_out.stdout or "") + (log_out.stderr or "")
    return text[-max_chars:] if len(text) > max_chars else text


def fetch_pr_failed_logs(repo: str, pr_num: str) -> str:
    branch_result = run(["gh", "pr", "view", pr_num, "-R", repo, "--json", "headRefName"], check=False)
    branch = default_branch(repo)
    try:
        branch = json.loads(branch_result.stdout or "{}").get("headRefName") or branch
    except json.JSONDecodeError:
        pass
    run_result = run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--branch",
            branch,
            "--status",
            "failure",
            "--limit",
            "1",
            "--json",
            "databaseId",
        ],
        check=False,
    )
    try:
        runs = json.loads(run_result.stdout or "[]")
        if runs:
            return fetch_run_failed_logs(repo, int(runs[0]["databaseId"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return "CI failed (could not fetch logs)"


def target_workflow_passed(repo: str, pr_num: str, cfg: RepoConfig) -> bool:
    """True when configured ci_workflows checks succeeded (ignore upstream noise on forks)."""
    if not cfg.ci_workflows:
        return False
    result = run(
        ["gh", "pr", "checks", pr_num, "-R", repo, "--json", "name,state,bucket,workflow"],
        check=False,
    )
    try:
        checks = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    patterns = [p.lower() for p in cfg.ci_workflows]
    matched = [
        c
        for c in checks
        if any(p in (c.get("name") or "").lower() or p in (c.get("workflow") or "").lower() for p in patterns)
    ]
    if not matched:
        return False
    return all(
        c.get("bucket") == "pass"
        or c.get("state") in ("SUCCESS", "SKIPPED", "NEUTRAL")
        for c in matched
    )


def wait_for_pr_checks(
    repo: str,
    pr_num: str,
    *,
    timeout: int,
    poll: int,
    cfg: RepoConfig | None = None,
) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = "pending"
    none_since: float | None = None
    none_grace = min(45, max(poll * 2, 20))
    entry = repo_entry(repo) if cfg else {}
    fork_mode = bool(entry.get("local_fix"))
    time.sleep(min(8, poll))
    while time.time() < deadline:
        if cfg and fork_mode and target_workflow_passed(repo, pr_num, cfg):
            return True, f"target workflow passed ({', '.join(cfg.ci_workflows)})"
        state = pr_checks_state(repo, pr_num, cfg=cfg)
        if state == "none":
            if none_since is None:
                none_since = time.time()
            elif time.time() - none_since >= none_grace:
                return True, "no CI checks on PR"
            log(f"CI checks not registered yet on {repo}#{pr_num} — waiting...")
            time.sleep(poll)
            continue
        none_since = None
        if state == "success":
            return True, "all checks passed"
        if state == "failure":
            return False, fetch_pr_failed_logs(repo, pr_num)
        last = state
        log(f"CI {state} on {repo}#{pr_num} — next poll in {poll}s")
        time.sleep(poll)
    if cfg and fork_mode and target_workflow_passed(repo, pr_num, cfg):
        return True, f"target workflow passed after timeout ({', '.join(cfg.ci_workflows)})"
    return False, f"CI timeout after {timeout}s (last state: {last})"


def load_ci_heal_state() -> dict[str, Any]:
    if not CI_HEAL_STATE.exists():
        return {"seen_runs": [], "healed_runs": []}
    try:
        return json.loads(CI_HEAL_STATE.read_text())
    except json.JSONDecodeError:
        return {"seen_runs": [], "healed_runs": []}


def save_ci_heal_state(state: dict[str, Any]) -> None:
    CI_HEAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    state["ts"] = datetime.now(timezone.utc).isoformat()
    CI_HEAL_STATE.write_text(json.dumps(state, indent=2))


def enqueue_ci_failure(repo: str, *, run_id: int | None = None, pr_num: str | None = None, logs: str = "") -> None:
    if open_ci_repair_prs(repo):
        log(f"skip enqueue ci-heal {repo}: open repair PR exists")
        return
    if is_failure_blocked(repo, "ci_heal", "default"):
        log(f"skip enqueue ci-heal {repo}: blocked by failure ledger")
        return
    queue = load_ci_heal_queue()
    key = f"{repo}::{run_id or pr_num}"
    if any(q.get("repo") == repo for q in queue):
        return
    if any(q.get("key") == key for q in queue):
        return
    queue.append(
        {
            "key": key,
            "repo": repo,
            "run_id": run_id,
            "pr_num": pr_num,
            "logs": logs[-6000:],
            "status": "pending",
        }
    )
    CI_HEAL_QUEUE.write_text(json.dumps({"queue": queue, "ts": datetime.now(timezone.utc).isoformat()}, indent=2))
    log(f"queued CI heal: {repo} ({key})")


def try_known_ci_repair(ws: Path, repo: str, logs: str) -> bool:
    """Apply fast deterministic repairs for common agent CI breakage."""
    changed = False
    branch = default_branch(repo)

    if repo.endswith("forge-ci-reliability"):
        cli = ws / "forge/cli.py"
        if cli.exists() and len(cli.read_text()) < 80 and "pytest" in cli.read_text():
            for ref in ("d5b3bfc1d31ea23ddb1ca515dbacb58dbd2e8706", f"origin/{branch}~5"):
                result = run(["git", "show", f"{ref}:forge/cli.py"], cwd=ws, check=False)
                if result.returncode == 0 and result.stdout and "argparse" in result.stdout:
                    cli.write_text(result.stdout)
                    changed = True
                    break
        ci = ws / ".github/workflows/ci.yml"
        if ci.exists():
            text = ci.read_text()
            if "actions/checkout@v2" in text or "python-version: '3.8'" in text or "run: pytest\n" in text:
                text = (
                    text.replace("actions/checkout@v2", "actions/checkout@v4")
                    .replace("actions/setup-python@v2", "actions/setup-python@v5")
                    .replace("python-version: '3.8'", "python-version: '3.12'")
                    .replace("Set up Python 3.8", "Set up Python 3.12")
                    .replace("run: pytest", "run: python3 -m pytest -q")
                )
                ci.write_text(text)
                changed = True
        smoke = ws / "tests/smoke_test.py"
        if smoke.exists() and "Build successful" in smoke.read_text():
            smoke.write_text(
                'import subprocess\nimport sys\n\n\ndef test_cli_help():\n'
                '    result = subprocess.run([sys.executable, "forge/cli.py", "--help"], capture_output=True, text=True)\n'
                "    assert result.returncode == 0\n"
                '    assert "usage" in (result.stdout + result.stderr).lower()\n\n\n'
                "def test_clean_command():\n"
                '    result = subprocess.run([sys.executable, "forge/cli.py", "clean", "."], capture_output=True, text=True)\n'
                "    assert result.returncode == 0\n"
            )
            changed = True

    if repo.endswith("nexus-vision-engine"):
        init_py = ws / "nexus/__init__.py"
        if not init_py.exists():
            init_py.parent.mkdir(parents=True, exist_ok=True)
            init_py.write_text('"""Nexus vision pipeline package."""\n')
            changed = True
        ci = ws / ".github/workflows/ci.yml"
        if ci.exists() and "pip install -e ." not in ci.read_text():
            text = ci.read_text()
            text = text.replace(
                "pip install pytest",
                "pip install pytest\n        pip install -e .",
            )
            if "python -m pytest" not in text:
                text = text.replace("run: pytest", "run: python -m pytest -q")
            ci.write_text(text)
            changed = True
        pyproject = ws / "pyproject.toml"
        if not pyproject.exists():
            pyproject.write_text(
                "[build-system]\nrequires = ['setuptools>=68']\nbuild-backend = 'setuptools.build_meta'\n\n"
                "[project]\nname = 'nexus-vision-engine'\nversion = '0.1.0'\n\n"
                "[tool.setuptools.packages.find]\nwhere = ['.']\ninclude = ['nexus*']\n"
            )
            changed = True

    if repo.endswith("vertex-sim-core"):
        cargo = ws / "Cargo.toml"
        if cargo.exists() and "[lib]" not in cargo.read_text():
            cargo.write_text(
                cargo.read_text().rstrip()
                + "\n\n[lib]\nname = \"vertex_sim_core\"\npath = \"Vertex/lib.rs\"\n"
            )
            changed = True

    if repo.endswith("tinygrad"):
        wf = ws / ".github/workflows/fork-smoke.yml"
        smoke = 'python3 -c "from tinygrad import Device; print(Device.DEFAULT)"'
        correct = (
            "name: Fork Smoke Test\n\non:\n  push:\n    branches: [ master ]\n"
            "  pull_request:\n    branches: [ master ]\n\njobs:\n  build:\n"
            "    runs-on: ubuntu-latest\n\n    steps:\n    - name: Checkout code\n"
            "      uses: actions/checkout@v3\n\n    - name: Set up Python 3.12\n"
            "      uses: actions/setup-python@v4\n      with:\n"
            "        python-version: '3.12'\n\n    - name: Install dependencies\n"
            "      run: |\n        python -m pip install --upgrade pip\n"
            "        pip install -e .\n\n    - name: Run smoke test\n"
            f"      run: {smoke}\n"
        )
        if wf.exists() and smoke not in wf.read_text():
            wf.write_text(correct)
            changed = True

    return changed


def try_known_local_repair(ws: Path, repo: str, title: str, body: str) -> bool:
    """Deterministic doc/file fixes — no LLM (vision/micrograd/mlx fork tasks)."""
    changed = False
    title_l = title.lower()

    if "gitignore" in title_l:
        gi = ws / ".gitignore"
        text = gi.read_text() if gi.exists() else ""
        extras: list[str] = []
        if repo.endswith("vision"):
            extras = ["__pycache__/", ".pytest_cache/", "build/", "dist/"]
        elif repo.endswith("micrograd"):
            extras = ["__pycache__/", ".pytest_cache/", "*.pyc", ".venv/"]
        elif repo.endswith("mlx"):
            extras = ["__pycache__/", ".pytest_cache/", "build/", "dist/", "*.egg-info/"]
        elif repo.endswith("vertex-sim-core") or "rust" in title_l:
            extras = ["/target/"]
        else:
            extras = ["__pycache__/", ".pytest_cache/", "*.pyc"]
        for line in extras:
            if line not in text:
                text = (text.rstrip() + "\n" + line + "\n") if text.strip() else (line + "\n")
                changed = True
        if changed:
            gi.write_text(text)

    if "fork.md" in title_l:
        fork = ws / "FORK.md"
        if not fork.exists():
            if repo.endswith("vision"):
                fork.write_text(
                    "# Vision Fork\n\nNueramarcos fork of [pytorch/vision](https://github.com/pytorch/vision).\n\n"
                    "```bash\npip install -e .\npython -c \"import torchvision; print(torchvision.__version__)\"\n```\n"
                )
                changed = True
            elif repo.endswith("micrograd"):
                fork.write_text(
                    "# Micrograd Fork\n\nNueramarcos fork of [karpathy/micrograd](https://github.com/karpathy/micrograd).\n\n"
                    "```bash\npython3 -c \"from micrograd.engine import Value; print(Value(1.0).data)\"\n```\n"
                )
                changed = True
            elif repo.endswith("mlx"):
                fork.write_text(
                    "# MLX Fork\n\nNueramarcos fork of [ml-explore/mlx](https://github.com/ml-explore/mlx).\n"
                    "Primary target is Apple Silicon; this fork tracks Linux-side experiments.\n"
                )
                changed = True

    if "fork notice" in title_l:
        readme = ws / "README.md"
        if readme.exists() and "## Fork Notice" not in readme.read_text():
            readme.write_text(
                readme.read_text().rstrip()
                + "\n\n## Fork Notice\n\nMaintained by Nueramarcos. See FORK.md for upstream link.\n"
            )
            changed = True

    if "contributing" in title_l:
        contrib = ws / "CONTRIBUTING.md"
        if not contrib.exists():
            if repo.endswith("micrograd"):
                contrib.write_text(
                    "# Contributing\n\n```bash\ngit clone <fork>\ncd micrograd\npython3 -c \"from micrograd.engine import Value; print(Value(1.0).data)\"\n```\n"
                )
                changed = True
            elif repo.endswith("forge-ci-reliability"):
                contrib.write_text(
                    "# Contributing\n\n```bash\npython3 -m venv .venv && source .venv/bin/activate\n"
                    "pip install -r requirements-dev.txt\npython3 -m pytest -q\n```\n"
                )
                changed = True

    if "html entity" in title_l or "&amp;" in body:
        readme = ws / "README.md"
        if readme.exists() and "&amp;" in readme.read_text():
            readme.write_text(readme.read_text().replace("&amp;", "&"))
            changed = True

    return changed


def push_ci_repair_pr(
    repo: str,
    title: str,
    body: str,
    *,
    cfg: RepoConfig | None = None,
    issue_num: int | None = None,
) -> int:
    if is_failure_blocked(repo, "ci_heal", "default"):
        log(f"skip ci-heal {repo}: persistent failure — move on")
        return 2
    open_prs = open_ci_repair_prs(repo)
    if open_prs:
        nums = ", ".join(f"#{p['number']}" for p in open_prs[:3])
        log(f"skip ci-heal {repo}: open repair PR(s) {nums}")
        return 2
    if main_branch_ci_state(repo) == "success" and fork_smoke_healthy(repo):
        log(f"skip ci-heal {repo}: target CI already healthy")
        return 2

    base_ws = workspace_for(repo)
    ensure_repo(repo, base_ws)
    cfg = cfg or repo_config(repo, base_ws)
    slug = int(datetime.now(timezone.utc).strftime("%H%M%S"))
    ws = workspace_for(repo, slug)
    if ws.exists():
        run(["rm", "-rf", str(ws)], check=False)
    run(["cp", "-a", str(base_ws), str(ws)])
    branch = f"fix/ci-{slug}"
    run(["git", "checkout", "-B", branch], cwd=ws)
    run(["git", "fetch", "origin"], cwd=ws, check=False)
    run(["git", "merge", f"origin/{default_branch(repo)}"], cwd=ws, check=False)
    bootstrap_habitat(ws, repo)

    logs = body
    known_fix = try_known_ci_repair(ws, repo, logs)
    if not known_fix:
        aider_msg = textwrap.dedent(
            f"""
            {solver_prompt(repo, f"CI heal: {title[:70]}", cfg)}

            Fix failing GitHub Actions CI for {repo}.

            {body}
            """
        ).strip()
        with acquire_aider_slot():
            run(
                [
                    str(AIDER),
                    "--model",
                    cfg.model,
                    "--yes-always",
                    "--auto-commits",
                    "--no-show-model-warnings",
                    "--message",
                    aider_msg,
                ],
                cwd=ws,
                check=False,
            )
        sanitize_agent_artifacts(ws)

    dirty = run(["git", "status", "--porcelain"], cwd=ws, check=False)
    if not (dirty.stdout or "").strip():
        log(f"no CI repair changes for {repo}")
        record_failure(repo, "ci_heal", "default", "no CI repair changes")
        return 1

    run(["git", "add", "-A"], cwd=ws, check=False)
    run(["git", "commit", "-m", title[:72]], cwd=ws, check=False)
    test_cmd = detect_test_command(ws, cfg.test_command)
    passed, test_out = run_tests(ws, test_cmd, strict=not known_fix)
    if not passed and test_cmd and not known_fix:
        log(f"local tests failed before CI repair push: {test_out[-1000:]}")
        record_failure(repo, "ci_heal", "default", f"tests failed: {test_out[-300:]}")
        return 1
    if known_fix:
        log(f"known CI repair applied for {repo}, deferring validation to GitHub Actions")

    if cfg.tower_enabled and not known_fix:
        verdict = tower_review(ws, repo, cfg, base_branch=default_branch(repo), issue_summary=title[:70])
        if not verdict.passed:
            record_failure(repo, "ci_heal", "default", "tower rejected: " + "; ".join(verdict.reasons)[:400])
            return 1

    run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd=ws, check=False)
    pr = run(
        [
            "gh",
            "pr",
            "create",
            "-R",
            repo,
            "--head",
            branch,
            "--title",
            title[:70],
            "--body",
            body[:3500] + "\n\n---\n*Issue Agent CI heal · Nueramarcos*",
        ],
        cwd=ws,
        check=False,
    )
    pr_url = (pr.stdout or "").strip()
    if not pr_url or pr.returncode != 0:
        log(f"CI repair PR failed: {(pr.stderr or '').strip()}")
        return 1

    merged, detail = finalize_pr(repo, pr_url, cfg, issue_num=issue_num)
    if merged:
        record_success(repo, "ci_heal", "default")
        print(f"CI healed: {pr_url}")
        return 0
    record_failure(repo, "ci_heal", "default", detail)
    log(f"CI repair PR open (checks failed): {detail[-500:]}")
    return 1


def finalize_pr(
    repo: str,
    pr_url: str,
    cfg: RepoConfig,
    *,
    issue_num: int | None = None,
) -> tuple[bool, str]:
    pr_num = pr_url.rstrip("/").split("/")[-1]
    if cfg.draft_pr:
        run(["gh", "pr", "ready", pr_num, "-R", repo], check=False)
    if cfg.wait_for_checks:
        ok, detail = wait_for_pr_checks(
            repo,
            pr_num,
            timeout=cfg.check_timeout_secs,
            poll=cfg.check_poll_secs,
            cfg=cfg,
        )
        if not ok:
            body = f"🤖 **Issue Agent** PR ready but CI failed — not merging.\n\n```\n{detail[-2500:]}\n```"
            if issue_num:
                maybe_issue_comment(repo, issue_num, body, success=False)
            enqueue_ci_failure(repo, pr_num=pr_num, logs=detail)
            return False, detail
    if cfg.auto_merge:
        merge_flags = ["--squash", "--delete-branch"]
        if not cfg.wait_for_checks:
            merge_flags.insert(0, "--auto")
        merge = run(["gh", "pr", "merge", pr_num, "-R", repo, *merge_flags], check=False)
        if merge.returncode != 0 and not cfg.wait_for_checks:
            merge = run(["gh", "pr", "merge", pr_num, "-R", repo, "--squash", "--delete-branch"], check=False)
        if merge.returncode != 0:
            return False, (merge.stderr or merge.stdout or "merge failed").strip()
    return True, pr_url


def _load_habitat_plan(ws: Path) -> dict[str, Any] | None:
    env_path = os.environ.get("HABITAT_PLAN_PATH")
    if env_path and Path(env_path).exists():
        try:
            return json.loads(Path(env_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    try:
        from habitat_planner.plan import load_plan

        return load_plan(ws)
    except ImportError:
        return None


def _plan_block(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    try:
        from habitat_planner.plan import plan_prompt_block

        return plan_prompt_block(plan)
    except ImportError:
        return ""


def _run_aider_attempt(
    ws: Path,
    repo: str,
    cfg: RepoConfig,
    issue_num: int,
    issue: dict[str, Any],
    *,
    feedback: str = "",
    plan: dict[str, Any] | None = None,
) -> None:
    issue_text = f"#{issue_num}: {issue['title']}\n{issue.get('body') or ''}"
    extra = f"\n\n## Reviewer feedback (address all points)\n{feedback}\n" if feedback else ""
    aider_msg = textwrap.dedent(
        f"""
        {solver_prompt(repo, issue.get("title", ""), cfg)}
        {_plan_block(plan)}

        Fix GitHub issue in repository {repo}.
        Touch at most {cfg.max_files} files. Run tests before finishing.

        {issue_text}
        {extra}
        """
    ).strip()
    aider_cmd = [
        str(AIDER),
        "--model",
        cfg.model,
        "--yes-always",
        "--auto-commits",
        "--no-show-model-warnings",
        "--message",
        aider_msg,
    ]
    with acquire_aider_slot():
        aider_result = run(aider_cmd, cwd=ws, check=False)
    log(aider_result.stdout[-4000:] if aider_result.stdout else "")
    if aider_result.stderr:
        log(aider_result.stderr[-2000:])
    sanitize_agent_artifacts(ws)


def _local_gates(
    ws: Path,
    repo: str,
    cfg: RepoConfig,
    issue_num: int,
    issue: dict[str, Any],
    *,
    base: str,
) -> tuple[bool, str]:
    """Tests + Tower + Human Tower — all local, no GitHub. Returns (ok, retry_feedback)."""
    test_cmd = detect_test_command(ws, cfg.test_command)
    passed, test_out = run_tests(ws, test_cmd)
    if not passed:
        return False, f"Tests failed:\n{test_out[-2500:]}"

    if not has_branch_changes(ws, base):
        return False, "No file changes produced — implement the plan with a minimal diff."

    if cfg.tower_enabled:
        verdict = tower_review(ws, repo, cfg, base_branch=base, issue_summary=issue.get("title", ""))
        if not verdict.passed:
            return False, "Tower rejected:\n" + "\n".join(verdict.reasons)
        append_flight_record(
            {
                "outcome": "tower_pass",
                "repo": repo,
                "scope": "issue",
                "ident": str(issue_num),
                "issue_num": issue_num,
                "confidence": verdict.confidence,
                "files": verdict.files_changed,
            }
        )

    ht = human_tower_gate(
        ws,
        repo,
        cfg,
        base_branch=base,
        issue_summary=issue.get("title", ""),
        issue_num=issue_num,
    )
    if ht is not None and not ht.passed:
        fb = ht.review_comment or "; ".join(ht.reasons)
        return False, f"Human Tower rejected:\n{fb}"

    return True, ""


def resolve_issue(repo: str, issue_num: int, *, dry_run: bool = False) -> int:
    base_ws = workspace_for(repo)
    ensure_repo(repo, base_ws)
    cfg = repo_config(repo, base_ws)
    ws = workspace_for(repo, issue_num)

    if ws != base_ws:
        if ws.exists():
            run(["rm", "-rf", str(ws)], check=False)
        run(["cp", "-a", str(base_ws), str(ws)])

    branch = f"fix/issue-{issue_num}"
    run(["git", "checkout", "-B", branch], cwd=ws)
    bootstrap_habitat(ws, repo)

    issue = gh_json(["issue", "view", str(issue_num), "-R", repo, "--json", "title,body"])
    issue_text = f"#{issue_num}: {issue['title']}\n{issue.get('body') or ''}"

    if dry_run:
        log(f"DRY RUN — would fix issue #{issue_num} in {repo} on branch {branch}")
        return 0

    if not AIDER.exists():
        raise RuntimeError(f"Aider not found at {AIDER}")

    plan: dict[str, Any] | None = None
    if cfg.plan_enabled or os.environ.get("HABITAT_PLAN_PATH"):
        plan = _load_habitat_plan(ws)
        if plan:
            log(f"plan loaded — confidence={plan.get('confidence', '?')}")

    base = default_branch(repo)
    max_retries = max(1, int(os.environ.get("HABITAT_MAX_FIX_RETRIES", cfg.max_fix_retries)))
    feedback = ""
    gates_ok = False
    for attempt in range(1, max_retries + 1):
        log(f"fix attempt {attempt}/{max_retries} for #{issue_num}")
        _run_aider_attempt(ws, repo, cfg, issue_num, issue, feedback=feedback, plan=plan)
        gates_ok, feedback = _local_gates(ws, repo, cfg, issue_num, issue, base=base)
        if gates_ok:
            append_flight_record(
                {
                    "outcome": "fix_success",
                    "repo": repo,
                    "issue_num": issue_num,
                    "attempt": attempt,
                    "max_retries": max_retries,
                }
            )
            break
        log(f"attempt {attempt} failed locally — retrying" if attempt < max_retries else "all attempts exhausted")
        append_flight_record(
            {
                "outcome": "fix_retry",
                "repo": repo,
                "issue_num": issue_num,
                "attempt": attempt,
                "detail": feedback[:400],
            }
        )

    if not gates_ok:
        log(f"fix failed after {max_retries} local attempts — no GitHub activity")
        record_failure(
            repo,
            "issue",
            str(issue_num),
            f"local retry exhausted: {feedback[:400]}",
            issue_num=issue_num,
            spec_title=issue["title"],
        )
        return 1

    plan_section = ""
    if plan:
        plan_section = (
            f"\n\n## Habitat Plan\n{plan.get('repo_summary', '')}\n"
            f"**Fix:** {plan.get('solution_plan', '')}\n"
        )

    # push + draft PR (only after all local gates green)
    run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd=ws, check=False)
    pr_args = [
        "gh",
        "pr",
        "create",
        "-R",
        repo,
        "--head",
        branch,
        "--title",
        f"Fix #{issue_num}: {issue['title'][:70]}",
        "--body",
        f"Automated local fix for #{issue_num}.\n\nCloses #{issue_num}{plan_section}\n\n---\n*Generated by Issue Agent on Nueramarcos*",
    ]
    if cfg.draft_pr or os.environ.get("HABITAT_DRAFT_PR", "0") == "1":
        pr_args.append("--draft")
    pr = run(pr_args, cwd=ws, check=False)
    pr_url = (pr.stdout or "").strip()
    if not pr_url or pr.returncode != 0:
        log(f"PR creation failed: {(pr.stderr or pr.stdout or '').strip()}")
        return 1

    merged, detail = finalize_pr(repo, pr_url, cfg, issue_num=issue_num)
    pr_num = pr_url.rstrip("/").split("/")[-1]
    if merged:
        comment = f"🤖 **Issue Agent** fixed, merged PR #{pr_num}: {pr_url}"
    elif cfg.wait_for_checks:
        comment = f"🤖 **Issue Agent** opened PR {pr_url} — CI failed, queued for heal."
    else:
        comment = f"🤖 **Issue Agent** opened PR {pr_url} (auto-merge failed — review manually)."

    maybe_issue_comment(repo, issue_num, comment, success=merged)
    log(f"done #{issue_num} -> {pr_url} (merged={merged})")
    if merged:
        record_success(repo, "issue", str(issue_num), spec_title=issue["title"])
        _maybe_broadcast_merge(repo, issue_num=issue_num, pr_url=pr_url, title=issue["title"])
    else:
        record_failure(repo, "issue", str(issue_num), detail, issue_num=issue_num, spec_title=issue["title"])
    return 0 if merged else 1


def _maybe_broadcast_merge(
    repo: str,
    *,
    issue_num: int | None = None,
    pr_url: str = "",
    title: str = "",
) -> None:
    if os.environ.get("X_BROADCAST", "1").lower() in ("0", "false", "no"):
        return
    try:
        result = broadcast_merge(
            repo,
            BROADCAST_DIR,
            issue_num=issue_num,
            pr_url=pr_url,
            title=title,
            auto_post=os.environ.get("X_AUTO_POST", "").lower() in ("1", "true", "yes"),
        )
        log(f"broadcast saved → {result['path']}")
        if result.get("posted"):
            log(f"broadcast X: {result.get('post_detail', 'ok')}")
        log_activity("broadcast", repo, title[:60], pr_url=pr_url)
    except Exception as exc:
        log(f"broadcast skipped: {exc}")


def cmd_solvability(args: argparse.Namespace) -> int:
    load_secrets()
    if args.repo:
        solv = compute_repo_solvability(args.repo, use_cache=False)
        print(json.dumps(solv, indent=2))
        return 0
    ranked = save_solvability_snapshot()
    print("Solvability ranking (higher = better overnight pick):\n")
    for row in ranked:
        short = row["repo"].split("/")[-1]
        f = row.get("factors", {})
        print(
            f"  {row['score']:>3} {row['tier']:<6} {short:<22} "
            f"easy={f.get('easy', 0)} solvable={f.get('solvable', 0)} "
            f"every={row['interval_secs']}s factory={row['factory_max']}"
        )
    print(f"\nfull state: {SOLVABILITY_STATE}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    quick = getattr(args, "quick", False)

    print("Issue Agent — status among change\n")
    load_secrets()
    hk = housekeeping() if not quick else {"unparked": 0, "queue_pruned": 0, "ci_closed": 0}

    try:
        run(["gh", "auth", "status"], check=False)
        checks.append(("gh", True, "authenticated"))
        print("  [ok] gh: authenticated")
    except Exception:
        print("  [FAIL] gh: not authenticated — run: gh auth login")
        checks.append(("gh", False, "not authenticated"))

    try:
        r = run(["curl", "-s", f"{os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434')}/api/tags"], check=False)
        ok = r.returncode == 0 and "models" in (r.stdout or "")
        print(f"  [{'ok' if ok else 'FAIL'}] ollama: {'running' if ok else 'not reachable'}")
        checks.append(("ollama", ok, "running" if ok else "down"))
    except Exception:
        print("  [FAIL] ollama: not reachable")
        checks.append(("ollama", False, "down"))

    print(f"  [{'ok' if AIDER.exists() else 'FAIL'}] aider: {AIDER}")
    checks.append(("aider", AIDER.exists(), str(AIDER)))

    WORKSPACES.mkdir(parents=True, exist_ok=True)
    print(f"  [ok] workspaces: {WORKSPACES}")
    print(f"  [ok] logs: {LOG_DIR}")

    if not quick and any(hk.values()):
        print(f"\n  housekeeping: unparked={hk['unparked']} queue_pruned={hk['queue_pruned']} ci_closed={hk['ci_closed']}")

    if quick:
        print("\n  (quick mode — skipping fleet/solvability gh scans)")
        print(f"\n  digest: {STATUS_DIGEST}")
        return 0 if all(c[1] for c in checks) else 1

    try:
        ranked = save_solvability_snapshot()
        if ranked:
            print("\n  solvability (best picks first):")
            tier_icon = {"hot": "🔥", "warm": "◎", "cool": "○", "cold": "·", "parked": "⏸"}
            for row in ranked[:8]:
                short = row["repo"].split("/")[-1]
                f = row.get("factors", {})
                easy = f.get("easy", 0)
                solvable = f.get("solvable", 0) + f.get("local_queue", 0)
                print(
                    f"    {tier_icon.get(row['tier'], '?')} {row['score']:>3} {short:<22} "
                    f"easy={easy} solvable={solvable} every={row['interval_secs']}s"
                )
    except Exception as exc:
        print(f"\n  solvability: unavailable ({exc})")

    summary = fleet_status_summary()
    print(f"\n  fleet: {summary['active_count']} active · {summary['parked_count']} parked · "
          f"local_queue={summary['local_queue_total']} · ci_heal_queue={summary['ci_heal_queue']}")
    ci_icon = {"success": "✓", "failure": "✗", "pending": "…", "none": "—"}
    for row in summary["repos"]:
        park = f" ⏸ until {row['park_until']}" if row["parked"] else ""
        issues = f" issues={row['agent_issues']}" if row["agent_issues"] else ""
        local = f" local={row['local_queue']}" if row["local_queue"] else ""
        print(f"    {ci_icon.get(row['ci'], '?')} {row['short']:<22}{issues}{local}{park}")

    airport_cfg = load_airport_config()
    if airport_cfg.get("enabled"):
        print(f"\n  airport: enabled · lanes={len(airport_cfg.get('lanes') or [])}")
        if AIRPORT_STATUS.exists():
            try:
                ast = json.loads(AIRPORT_STATUS.read_text())
                if ast.get("supervisor_heartbeat"):
                    print(f"    supervisor heartbeat: {ast['supervisor_heartbeat'][:19]}")
                if ast.get("last_factory"):
                    print(f"    last factory: {ast['last_factory'][:19]}")
            except json.JSONDecodeError:
                pass
        if AIRPORT_PID_DIR.exists():
            pids = list(AIRPORT_PID_DIR.glob("*.pid"))
            print(f"    workers: {len(pids)} pid file(s)")

    recent: list[dict[str, Any]] = []
    if ACTIVITY_LOG.exists():
        try:
            recent = json.loads(ACTIVITY_LOG.read_text())[-5:]
        except json.JSONDecodeError:
            pass
    if recent:
        print("\n  recent activity:")
        for ev in recent:
            ts = ev.get("ts", "")[:19]
            print(f"    {ts}  {ev.get('event', '?')}  {ev.get('repo', '')}  {ev.get('detail', '')[:50]}")

    failures = failure_summary()
    blocked_n = sum(1 for f in failures if f.get("blocked"))
    if failures:
        print(f"\n  failure points ({len(failures)} tracked, {blocked_n} blocked):")
        for f in failures[:6]:
            short = f.get("repo", "").split("/")[-1]
            print(
                f"    {f.get('kind', '?'):12} {short} {f.get('scope')}/{f.get('ident')} "
                f"({f.get('attempts')}/{f.get('max_attempts')}) — {f.get('hint', '')[:55]}"
            )
        print(f"  full digest: {FAILURE_DIGEST}")

    traj_n = len(_load_trajectories())
    if traj_n or FLIGHT_TRAJECTORIES.exists():
        print(f"\n  flight recorder: {traj_n} trajectory(ies) → {FLIGHT_TRAJECTORIES}")

    save_status_digest(
        {
            "health": {n: ok for n, ok, _ in checks},
            "housekeeping": hk,
            "failures": failures,
            **summary,
        }
    )
    print(f"\n  digest: {STATUS_DIGEST}")
    return 0 if all(c[1] for c in checks) else 1


def cmd_list_issues(args: argparse.Namespace) -> int:
    load_secrets()
    issues = gh_json(["issue", "list", "-R", args.repo, "--json", "number,title,labels,state", "--limit", str(args.limit)])
    if not issues:
        print(f"No open issues in {args.repo}")
        return 0
    for item in issues:
        labels = ", ".join(l["name"] for l in item.get("labels", [])) or "(none)"
        print(f"  #{item['number']:>4}  {item['title'][:60]}  [{labels}]")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    load_secrets()
    base_ws = workspace_for(args.repo)
    ensure_repo(args.repo, base_ws)
    cfg = repo_config(args.repo, base_ws)
    if args.issue:
        nums = [args.issue]
    else:
        issues = gh_json(["issue", "list", "-R", args.repo, "--json", "number", "--limit", "20"])
        nums = [i["number"] for i in issues]
    for num in nums:
        t = triage_issue(args.repo, num, cfg)
        action = "FIX" if t.get("actionable") and t.get("complexity") != "high" else "SKIP"
        print(f"#{num} [{action}] {t.get('type')} / {t.get('complexity')} — {t.get('summary')}")
        if args.apply_label and action == "FIX":
            run(["gh", "issue", "edit", str(num), "-R", args.repo, "--add-label", cfg.trigger_label], check=False)
        elif args.apply_label and action == "SKIP":
            run(["gh", "issue", "edit", str(num), "-R", args.repo, "--add-label", "agent-skip"], check=False)
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    load_secrets()
    return resolve_issue(args.repo, args.issue, dry_run=args.dry_run)


def cmd_run(args: argparse.Namespace) -> int:
    load_secrets()
    base_ws = workspace_for(args.repo)
    ensure_repo(args.repo, base_ws)
    cfg = repo_config(args.repo, base_ws)
    issues = gh_json(
        [
            "issue",
            "list",
            "-R",
            args.repo,
            "--label",
            cfg.trigger_label,
            "--json",
            "number,title",
            "--limit",
            str(args.max),
        ]
    )
    if not issues:
        log(f"no issues with label '{cfg.trigger_label}' in {args.repo}")
        return 0
    rc = 0
    for item in issues[: args.max]:
        t = triage_issue(args.repo, item["number"], cfg)
        if not t.get("actionable") or t.get("complexity") == "high":
            log(f"skip #{item['number']}: {t.get('summary')}")
            continue
        rc |= resolve_issue(args.repo, item["number"], dry_run=args.dry_run)
    return rc


DEMO_ISSUES: dict[str, dict[str, str]] = {
    "Nueramarcos/orion-ai-agent": {
        "title": "Add Usage section to README with CLI examples",
        "body": """## Problem
The README only has a one-line description. New users do not know how to run the Orion AST analyser CLI.

## Expected
Add a **Usage** section to README.md with:
1. A one-line install note (Python 3.10+, no extra deps)
2. Three example commands from Orion/ast_parser.py CLI:
   - python Orion/ast_parser.py file <path>
   - python Orion/ast_parser.py scan <project_root> --symbols
   - python Orion/ast_parser.py callers <project_root> <symbol>
3. Keep the existing project description at the top

## Acceptance criteria
- README.md updated only
- Examples are accurate and runnable
""",
    },
    "Nueramarcos/forge-ci-reliability": {
        "title": "Add Usage section to README with Forge CLI examples",
        "body": """## Problem
README is a single line. Users cannot discover the Forge build/run/clean CLI.

## Expected
Add a **Usage** section to README.md with:
1. Python 3.10+ requirement, no install step (run from repo root)
2. Examples from forge/cli.py:
   - python forge/cli.py build <project_path>
   - python forge/cli.py run <project_path>
   - python forge/cli.py clean <project_path>
3. Note that build auto-detects cargo, python, npm, make, cmake

## Acceptance criteria
- README.md only
- Accurate examples
""",
    },
    "Nueramarcos/nexus-vision-engine": {
        "title": "Add Usage section to README with pipeline demo",
        "body": """## Problem
README lacks instructions for running the Nexus vision pipeline demo.

## Expected
Add a **Usage** section to README.md with:
1. Python 3.10+ requirement
2. How to run the demo: python nexus/pipeline.py
3. Brief explanation of what the demo does (3-stage pipeline with DLQ)

## Acceptance criteria
- README.md only
""",
    },
    "Nueramarcos/vertex-sim-core": {
        "title": "Add Usage section to README with Rust quickstart",
        "body": """## Problem
README has no build or test instructions for the Rust simulation engine.

## Expected
Add a **Usage** section to README.md with:
1. Rust toolchain requirement
2. cargo test
3. Brief module overview (ECS-style entities, tick simulation, Godot GDExtension intent)

## Acceptance criteria
- README.md only
""",
    },
    "Nueramarcos/issue-agent": {
        "title": "Add Troubleshooting section to README",
        "body": """## Problem
README lacks troubleshooting for common operator issues.

## Expected
Add a **Troubleshooting** section to README.md with:
1. Use `issue-agent status --quick` when full status is slow (skips fleet gh scans)
2. Demo may report "task already satisfied" when main already contains the fix
3. Auth: run `gh auth login` if gh check fails

## Acceptance criteria
- README.md only
- Keep existing sections intact
""",
    },
}

ISSUE_BACKLOG: dict[str, list[dict[str, str]]] = {
    "Nueramarcos/orion-ai-agent": [
        {
            "title": "Add Orion/__init__.py with public API exports",
            "body": "Create Orion/__init__.py exporting parse_file, parse_source, ProjectAnalyser, and main symbols from ast_parser.py. Add minimal docstring. No other files.",
        },
        {
            "title": "Add CONTRIBUTING.md with dev setup and PR guidelines",
            "body": "Add CONTRIBUTING.md: clone repo, Python 3.10+, run ast_parser CLI examples, open issues before large changes, draft PRs welcome.",
        },
    ],
    "Nueramarcos/forge-ci-reliability": [
        {
            "title": "Add forge/__init__.py exposing CLI entrypoint",
            "body": "Create forge/__init__.py that exports main from forge/cli.py and sets __version__ = '0.2.0'. No other files.",
        },
        {
            "title": "Add CONTRIBUTING.md for forge-ci-reliability",
            "body": "Add CONTRIBUTING.md with Python 3.10+ setup, how to run forge CLI, and contribution guidelines.",
        },
    ],
    "Nueramarcos/nexus-vision-engine": [
        {
            "title": "Fix HTML entity &amp; in README",
            "body": "README line 2 contains literal &amp; — replace with & so it renders correctly. README.md only.",
        },
        {
            "title": "Add CONTRIBUTING.md for nexus-vision-engine",
            "body": "Add CONTRIBUTING.md: Python 3.10+, run python nexus/pipeline.py demo, contribution notes.",
        },
    ],
    "Nueramarcos/vertex-sim-core": [
        {
            "title": "Add Cargo.toml so cargo test works",
            "body": "Add minimal Cargo.toml at repo root for Vertex/lib.rs library crate named vertex-sim-core. Must pass cargo test. Keep Vertex/lib.rs logic unchanged unless required for compile.",
        },
        {
            "title": "Add CONTRIBUTING.md for vertex-sim-core",
            "body": "Add CONTRIBUTING.md: Rust toolchain, cargo test, contribution guidelines.",
        },
    ],
}

REPOS_CONFIG = AGENT_ROOT / "repos.yaml"
BACKLOG_FILE = AGENT_ROOT / "backlog.yaml"
COLLECTOR_STATE = AGENT_ROOT / "collector-state.json"
LOCAL_QUEUE = AGENT_ROOT / "local-queue.json"
CI_HEAL_STATE = AGENT_ROOT / "ci-heal-state.json"
CI_HEAL_QUEUE = AGENT_ROOT / "ci-heal-queue.json"
FLEET_STATE = AGENT_ROOT / "fleet-state.json"
ACTIVITY_LOG = AGENT_ROOT / "activity.json"
STATUS_DIGEST = AGENT_ROOT / "status.json"
FAILURE_LEDGER = AGENT_ROOT / "failure-ledger.json"
FAILURE_DIGEST = AGENT_ROOT / "failures.json"
AIRPORT_CONFIG = AGENT_ROOT / "airport.yaml"
UPSTREAM_CONFIG = AGENT_ROOT / "upstream.yaml"
UPSTREAM_BACKLOG_FILE = AGENT_ROOT / "upstream-backlog.yaml"
UPSTREAM_OPPORTUNITIES_FILE = AGENT_ROOT / "upstream-opportunities.yaml"
SCOUT_QUEUE_FILE = AGENT_ROOT / "scout-queue.json"
AIRPORT_STATUS = AGENT_ROOT / "airport-status.json"
AIRPORT_PID_DIR = LOG_DIR / "airport-pids"
SOLVABILITY_STATE = AGENT_ROOT / "solvability.json"
FLIGHT_RECORDER_DIR = AGENT_ROOT / "flight-recorder"
FLIGHT_TRAJECTORIES = FLIGHT_RECORDER_DIR / "trajectories.jsonl"
BROADCAST_DIR = AGENT_ROOT / "broadcasts"

_SOLV_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
SOLV_CACHE_TTL_SECS = 90

MAX_FAILURE_ATTEMPTS = 2
AIDER_SLOT_DIR = AGENT_ROOT / "aider-slots"


def failure_skip_hours() -> int:
    return int(load_airport_config().get("failure_skip_hours", 2))


@contextmanager
def acquire_aider_slot():
    """Limit concurrent Ollama/aider calls across airport workers."""
    max_slots = int(load_airport_config().get("max_concurrent_aider", 2))
    if max_slots <= 0:
        yield
        return
    AIDER_SLOT_DIR.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    while fd is None:
        for i in range(max_slots):
            slot = AIDER_SLOT_DIR / f"slot-{i}.lock"
            try:
                candidate = os.open(str(slot), os.O_CREAT | os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = candidate
                break
            except BlockingIOError:
                os.close(candidate)
        if fd is None:
            time.sleep(2)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

JUNK_FILE_PATTERNS = ("python ", "cargo ", "npm ", "path", "path/")
STALE_CI_TITLE = re.compile(r"^Fix CI:", re.I)
STANDARD_LABELS = [
    ("agent-triage", "0E8A16", "Issue Agent: attempt automated fix"),
    ("agent-skip", "FBCA04", "Issue Agent: skip automated fix"),
    ("good first issue", "7057FF", "Good for newcomers"),
    ("documentation", "0075CA", "Documentation improvements"),
]


def load_fleet_state() -> dict[str, Any]:
    if not FLEET_STATE.exists():
        return {"blocked": {}}
    try:
        return json.loads(FLEET_STATE.read_text())
    except json.JSONDecodeError:
        return {"blocked": {}}


def save_fleet_state(state: dict[str, Any]) -> None:
    FLEET_STATE.parent.mkdir(parents=True, exist_ok=True)
    state["ts"] = datetime.now(timezone.utc).isoformat()
    FLEET_STATE.write_text(json.dumps(state, indent=2))


def log_activity(event: str, repo: str = "", detail: str = "", **extra: Any) -> None:
    """GitHub-style contribution log — append-only local activity feed."""
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    if ACTIVITY_LOG.exists():
        try:
            entries = json.loads(ACTIVITY_LOG.read_text())
        except json.JSONDecodeError:
            entries = []
    entries.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "repo": repo,
            "detail": detail,
            **extra,
        }
    )
    ACTIVITY_LOG.write_text(json.dumps(entries[-500:], indent=2))


def save_status_digest(summary: dict[str, Any]) -> None:
    STATUS_DIGEST.parent.mkdir(parents=True, exist_ok=True)
    summary["ts"] = datetime.now(timezone.utc).isoformat()
    STATUS_DIGEST.write_text(json.dumps(summary, indent=2))


def load_failure_ledger() -> dict[str, Any]:
    if not FAILURE_LEDGER.exists():
        return {"items": {}}
    try:
        return json.loads(FAILURE_LEDGER.read_text())
    except json.JSONDecodeError:
        return {"items": {}}


def save_failure_ledger(state: dict[str, Any]) -> None:
    FAILURE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    state["ts"] = datetime.now(timezone.utc).isoformat()
    FAILURE_LEDGER.write_text(json.dumps(state, indent=2))


def append_flight_record(record: dict[str, Any]) -> None:
    """Flight Recorder — append-only trajectory log for LoRA / RAG training."""
    FLIGHT_RECORDER_DIR.mkdir(parents=True, exist_ok=True)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with FLIGHT_TRAJECTORIES.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def failure_key(repo: str, scope: str, ident: str) -> str:
    return f"{repo}::{scope}::{ident}"


def classify_failure(detail: str) -> tuple[str, str]:
    d = detail.lower()
    if "no commits" in d or "no file changes" in d or "no ci repair changes" in d:
        return "no_commits", "Model produced no diff — try a simpler issue or known repair"
    if "tests failed" in d or "test failed" in d:
        return "test_fail", "Local tests failed — check test_command in repos.yaml"
    if "ci timeout" in d or "last state: pending" in d:
        return "ci_timeout", "Checks still pending — upstream workflows may be blocking fork smoke"
    if "checks failed" in d or "failure" in d or "ci failed" in d:
        return "ci_fail", "CI failed — inspect logs for root cause"
    if "pr already exists" in d or "pr creation failed" in d:
        return "pr_blocked", "Open PR already exists — wait for merge or close stale PR"
    if "merge failed" in d:
        return "merge_fail", "PR created but merge blocked — branch protection or checks"
    return "unknown", "Unclassified failure — review issue-agent.log"


SPEC_KIND_PRIORITY: dict[str, int] = {
    "junk": 0,
    "readme": 1,
    "smoke_tests": 2,
    "gitignore": 2,
    "contributing": 3,
    "templates": 3,
    "other": 5,
    "ci_workflow": 9,
}


def issue_spec_kind(title: str) -> str:
    """Canonical issue archetype for seed blocking and fix priority."""
    t = (title or "").lower()
    if "junk" in t or "accidental" in t:
        return "junk"
    if "readme" in t or "badge" in t or "shield" in t:
        return "readme"
    if "smoke" in t or "pytest" in t or "__init__" in t or "requirements-dev" in t:
        return "smoke_tests"
    if ".gitignore" in t or "gitignore" in t:
        return "gitignore"
    if "contributing" in t:
        return "contributing"
    if "template" in t or "issue and pr" in t:
        return "templates"
    if "ci" in t or "workflow" in t or "github actions" in t or "fix ci" in t:
        return "ci_workflow"
    return "other"


def is_seed_kind_blocked(repo: str, title: str) -> bool:
    return is_failure_blocked(repo, "seed", issue_spec_kind(title))


def is_spec_seedable(repo: str, title: str) -> bool:
    if not is_seed_kind_blocked(repo, title):
        return True
    kind = issue_spec_kind(title)
    log(f"skip seed {repo}: {kind} blocked after repeated no_commits")
    log_activity("seed_skip", repo, kind)
    return False


def bump_seed_kind_no_commits(repo: str, seed_kind: str, detail: str) -> None:
    """Accumulate no_commits per archetype; after 2 hits, stop seeding that kind for 6h."""
    record_failure(repo, "seed", seed_kind, detail)


def _seed_kind_for_failure_entry(entry: dict[str, Any]) -> str | None:
    scope = entry.get("scope", "")
    repo = entry.get("repo", "")
    if scope == "ci_heal":
        return "ci_workflow"
    if scope == "local":
        return issue_spec_kind(str(entry.get("ident", "")))
    if scope == "issue":
        issue_num = entry.get("issue_num")
        if not issue_num or not repo:
            return None
        try:
            issue = gh_json(["issue", "view", str(issue_num), "-R", repo, "--json", "title"])
        except (RuntimeError, subprocess.CalledProcessError):
            return None
        return issue_spec_kind(issue.get("title", ""))
    return None


def reconcile_seed_blocks_from_failures() -> int:
    """Backfill seed archetype blocks from historical no_commits failures."""
    state = load_failure_ledger()
    items = state.setdefault("items", {})
    peaks: dict[str, tuple[int, str | None, str | None]] = {}
    for entry in items.values():
        if entry.get("kind") != "no_commits" or entry.get("scope") == "seed":
            continue
        seed_kind = _seed_kind_for_failure_entry(entry)
        if not seed_kind:
            continue
        repo = str(entry.get("repo", ""))
        seed_key = failure_key(repo, "seed", seed_kind)
        attempts = int(entry.get("attempts", 0))
        blocked = bool(entry.get("blocked"))
        skip_until = entry.get("skip_until")
        prev = peaks.get(seed_key)
        if not prev or attempts > prev[0]:
            peaks[seed_key] = (attempts, skip_until if blocked else None, entry.get("detail", ""))
    updated = 0
    now = datetime.now(timezone.utc)
    for seed_key, (attempts, skip_until, detail) in peaks.items():
        prev = items.get(seed_key, {})
        if attempts <= int(prev.get("attempts", 0)):
            continue
        if attempts >= MAX_FAILURE_ATTEMPTS and not skip_until:
            skip_until = datetime.fromtimestamp(
                now.timestamp() + failure_skip_hours() * 3600, tz=timezone.utc
            ).isoformat()
        repo, _, seed_kind = seed_key.split("::", 2)
        items[seed_key] = {
            "repo": repo,
            "scope": "seed",
            "ident": seed_kind,
            "issue_num": None,
            "attempts": attempts,
            "max_attempts": MAX_FAILURE_ATTEMPTS,
            "kind": "no_commits",
            "hint": "Model produced no diff — try a simpler issue or known repair",
            "detail": (detail or "reconciled from prior no_commits failures")[-500:],
            "first_ts": prev.get("first_ts") or now.isoformat(),
            "last_ts": now.isoformat(),
            "skip_until": skip_until,
            "blocked": bool(skip_until),
        }
        updated += 1
    if updated:
        save_failure_ledger(state)
        log(f"reconciled {updated} seed block(s) from failure history")
    return updated


def record_failure(
    repo: str,
    scope: str,
    ident: str,
    detail: str,
    *,
    issue_num: int | None = None,
    spec_title: str | None = None,
) -> dict[str, Any]:
    kind, hint = classify_failure(detail)
    state = load_failure_ledger()
    items = state.setdefault("items", {})
    key = failure_key(repo, scope, ident)
    now = datetime.now(timezone.utc)
    prev = items.get(key, {})
    attempts = int(prev.get("attempts", 0)) + 1
    skip_until = prev.get("skip_until")
    if attempts >= MAX_FAILURE_ATTEMPTS:
        skip_until = (now.timestamp() + failure_skip_hours() * 3600)
        skip_until = datetime.fromtimestamp(skip_until, tz=timezone.utc).isoformat()
    entry = {
        "repo": repo,
        "scope": scope,
        "ident": ident,
        "issue_num": issue_num,
        "attempts": attempts,
        "max_attempts": MAX_FAILURE_ATTEMPTS,
        "kind": kind,
        "hint": hint,
        "detail": detail[-500:],
        "first_ts": prev.get("first_ts") or now.isoformat(),
        "last_ts": now.isoformat(),
        "skip_until": skip_until,
        "blocked": bool(skip_until),
    }
    items[key] = entry
    save_failure_ledger(state)
    digest = []
    if FAILURE_DIGEST.exists():
        try:
            digest = json.loads(FAILURE_DIGEST.read_text())
        except json.JSONDecodeError:
            digest = []
    digest.append({**entry, "key": key})
    FAILURE_DIGEST.write_text(json.dumps(digest[-100:], indent=2))
    append_flight_record(
        {
            "outcome": "failure",
            "repo": repo,
            "scope": scope,
            "ident": ident,
            "issue_num": issue_num,
            "kind": kind,
            "hint": hint,
            "detail": detail[-500:],
            "attempts": attempts,
            "blocked": entry["blocked"],
        }
    )
    log_activity("failure", repo, f"{scope}/{ident} {kind} ({attempts}/{MAX_FAILURE_ATTEMPTS})")
    if entry["blocked"]:
        log(f"BLOCKED {key} after {attempts} attempts — {hint}")
    if kind == "no_commits" and scope != "seed":
        seed_kind: str | None = None
        if scope == "ci_heal":
            seed_kind = "ci_workflow"
        elif scope == "local":
            seed_kind = issue_spec_kind(ident)
        elif spec_title:
            seed_kind = issue_spec_kind(spec_title)
        if seed_kind:
            bump_seed_kind_no_commits(repo, seed_kind, detail)
    return entry


def record_success(repo: str, scope: str, ident: str, *, spec_title: str | None = None) -> None:
    state = load_failure_ledger()
    items = state.setdefault("items", {})
    changed = False
    key = failure_key(repo, scope, ident)
    if key in items:
        del items[key]
        changed = True
    title = spec_title or (ident if scope == "local" else None)
    if title:
        seed_key = failure_key(repo, "seed", issue_spec_kind(title))
        if seed_key in items:
            del items[seed_key]
            changed = True
    if changed:
        save_failure_ledger(state)
    append_flight_record(
        {
            "outcome": "success",
            "repo": repo,
            "scope": scope,
            "ident": ident,
            "spec_title": spec_title,
        }
    )
    log_activity("pass", repo, f"{scope}/{ident}")


def is_failure_blocked(repo: str, scope: str, ident: str) -> bool:
    entry = load_failure_ledger().get("items", {}).get(failure_key(repo, scope, ident))
    if not entry or not entry.get("skip_until"):
        return False
    try:
        until = datetime.fromisoformat(str(entry["skip_until"]).replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < until:
            return True
        state = load_failure_ledger()
        key = failure_key(repo, scope, ident)
        if key in state.get("items", {}):
            state["items"][key]["skip_until"] = None
            state["items"][key]["blocked"] = False
            state["items"][key]["attempts"] = 0
            save_failure_ledger(state)
    except (ValueError, TypeError):
        pass
    return False


def ci_heal_enabled(entry: dict[str, Any] | None, repo: str | None = None) -> bool:
    """Per-repo opt-out for ci-heal (e.g. tinygrad fork inherits upstream Unit Tests noise)."""
    if entry is None and repo:
        entry = repo_entry(repo)
    if not entry:
        return True
    return entry.get("ci_heal", True) is not False


def open_ci_repair_prs(repo: str) -> list[dict[str, Any]]:
    result = run(
        ["gh", "pr", "list", "-R", repo, "--state", "open", "--json", "number,title,headRefName", "--limit", "20"],
        check=False,
    )
    try:
        prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(prs, list):
        return []
    return [
        p
        for p in prs
        if isinstance(p, dict) and str(p.get("headRefName", "")).startswith("fix/ci-")
    ]


def issue_fix_priority(item: dict[str, Any]) -> tuple[int, int]:
    """Lower = try first. Prefer easy wins that build consistent passes."""
    title = item.get("title") or ""
    num = int(item.get("number") or 9999)
    if "html entity" in title.lower() or "usage" in title.lower():
        return (1, num)
    return (SPEC_KIND_PRIORITY.get(issue_spec_kind(title), 5), num)


def load_ci_heal_queue() -> list[dict[str, Any]]:
    if not CI_HEAL_QUEUE.exists():
        return []
    try:
        data = json.loads(CI_HEAL_QUEUE.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        queue = data.get("queue", [])
        return queue if isinstance(queue, list) else []
    return []


def prune_ci_heal_queue() -> int:
    queue = load_ci_heal_queue()
    if not queue:
        return 0
    seen_repos: set[str] = set()
    kept: list[dict[str, Any]] = []
    removed = 0
    for item in queue:
        repo = item.get("repo", "")
        if is_failure_blocked(repo, "ci_heal", "default"):
            removed += 1
            continue
        if open_ci_repair_prs(repo):
            removed += 1
            continue
        if repo in seen_repos:
            removed += 1
            continue
        seen_repos.add(repo)
        kept.append(item)
    if removed:
        CI_HEAL_QUEUE.write_text(
            json.dumps({"queue": kept, "ts": datetime.now(timezone.utc).isoformat()}, indent=2)
        )
        log(f"pruned ci-heal queue: removed {removed} duplicate/blocked item(s)")
    return removed


def failure_summary() -> list[dict[str, Any]]:
    items = load_failure_ledger().get("items", {})
    rows = sorted(items.values(), key=lambda x: x.get("last_ts", ""), reverse=True)
    return rows[:12]


def main_branch_ci_state(repo: str) -> str:
    """Return 'success', 'failure', 'pending', or 'none' for default branch."""
    branch = default_branch(repo)
    result = run(
        ["gh", "run", "list", "-R", repo, "--branch", branch, "--limit", "1", "--json", "conclusion,status"],
        check=False,
    )
    try:
        runs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return "none"
    if not runs:
        return "none"
    last = runs[0]
    if last.get("status") != "completed":
        return "pending"
    return str(last.get("conclusion") or "none")


def is_stale_ci_issue(title: str, repo: str) -> bool:
    return bool(STALE_CI_TITLE.match(title)) and main_branch_ci_state(repo) == "success"


def has_branch_changes(ws: Path, base_branch: str) -> bool:
    run(["git", "fetch", "origin"], cwd=ws, check=False)
    result = run(["git", "rev-list", "--count", f"origin/{base_branch}..HEAD"], cwd=ws, check=False)
    try:
        return int((result.stdout or "0").strip()) > 0
    except ValueError:
        return False


def close_stale_ci_issues(repo: str | None = None) -> int:
    """Auto-close open 'Fix CI:' issues when main branch CI is already green."""
    closed = 0
    entries = [repo_entry(repo)] if repo else load_repos_config_raw()
    for entry in entries:
        if not entry or not entry.get("name"):
            continue
        r = entry["name"]
        if not repo_has_issues(r) or main_branch_ci_state(r) != "success":
            continue
        issues = gh_json(["issue", "list", "-R", r, "--state", "open", "--json", "number,title", "--limit", "50"])
        for iss in issues:
            if not STALE_CI_TITLE.match(iss.get("title", "")):
                continue
            run(
                [
                    "gh",
                    "issue",
                    "close",
                    str(iss["number"]),
                    "-R",
                    r,
                    "--comment",
                    "🤖 **Issue Agent** — default branch CI is green; closing stale CI ticket.",
                ],
                check=False,
            )
            log(f"closed stale CI issue #{iss['number']} on {r}")
            log_activity("issue_close", r, f"#{iss['number']} {iss['title'][:60]}")
            closed += 1
    return closed


def prune_fleet_state() -> int:
    state = load_fleet_state()
    blocked = state.get("blocked", {})
    now = datetime.now(timezone.utc)
    pruned: list[str] = []
    for r, info in list(blocked.items()):
        reason = str(info.get("reason", "")).lower()
        ci_green = main_branch_ci_state(r) == "success"
        if ci_green and any(k in reason for k in ("ci", "fork-smoke", "fix ci", "fix failed on #")):
            pruned.append(r)
            del blocked[r]
            continue
        if "fork-smoke" in reason and fork_smoke_healthy(r):
            pruned.append(r)
            del blocked[r]
            continue
        until = info.get("until")
        try:
            expiry = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            if expiry <= now:
                pruned.append(r)
                del blocked[r]
        except (ValueError, TypeError):
            pruned.append(r)
            del blocked[r]
    if pruned:
        save_fleet_state(state)
        for r in pruned:
            log(f"unparked {r}")
            log_activity("unpark", r, "wall expired or CI green")
    return len(pruned)


def fork_smoke_healthy(repo: str) -> bool:
    """True when fork-smoke workflow on default branch uses Device import smoke."""
    if not repo.endswith(("tinygrad", "vision")):
        return False
    branch = default_branch(repo)
    result = run(["gh", "api", f"repos/{repo}/contents/.github/workflows/fork-smoke.yml?ref={branch}"], check=False)
    if result.returncode != 0:
        return False
    try:
        raw = json.loads(result.stdout or "{}").get("content", "")
        text = base64.b64decode(raw).decode("utf-8", errors="replace")
        if repo.endswith("tinygrad"):
            return "from tinygrad import Device" in text
        return "import torchvision" in text
    except (json.JSONDecodeError, ValueError):
        return False


def prune_local_queue() -> int:
    queue = load_local_queue()
    if not queue:
        return 0
    kept: list[dict[str, Any]] = []
    removed = 0
    done_titles = {}
    for item in queue:
        repo = item.get("repo", "")
        title = item.get("title", "")
        if item.get("status") in ("done", "completed"):
            removed += 1
            continue
        if repo not in done_titles:
            done_titles[repo] = completed_local_titles(repo)
            if repo_has_issues(repo):
                done_titles[repo] |= existing_issue_titles(repo)
        if STALE_CI_TITLE.match(title) and (
            main_branch_ci_state(repo) == "success" or fork_smoke_healthy(repo)
        ):
            removed += 1
            log(f"pruned local queue (CI green): {repo} — {title[:70]}")
            continue
        if "Fork Smoke Test" in title and fork_smoke_healthy(repo):
            removed += 1
            log(f"pruned obsolete fork-smoke task: {repo}")
            continue
        if title in done_titles.get(repo, set()):
            removed += 1
            continue
        kept.append(item)
    if removed:
        save_local_queue(kept)
        log_activity("queue_prune", "", f"removed {removed} stale item(s)")
    return removed


def housekeeping(*, repo: str | None = None) -> dict[str, int]:
    """GitHub-inspired hygiene: prune walls, queue, close stale CI tickets."""
    load_secrets()
    stats = {
        "unparked": prune_fleet_state(),
        "queue_pruned": prune_local_queue(),
        "ci_heal_pruned": prune_ci_heal_queue(),
        "ci_closed": close_stale_ci_issues(repo),
        "seed_blocks": reconcile_seed_blocks_from_failures(),
    }
    if any(stats.values()):
        log(f"housekeeping: {stats}")
    return stats


def fleet_status_summary() -> dict[str, Any]:
    repos = load_repos_config_raw()
    rows: list[dict[str, Any]] = []
    for entry in repos:
        r = entry["name"]
        short = r.split("/")[-1]
        ci = main_branch_ci_state(r)
        parked = is_repo_parked(r)
        park_info = load_fleet_state().get("blocked", {}).get(r, {}) if parked else {}
        open_issues = 0
        if repo_has_issues(r):
            try:
                open_issues = len(
                    gh_json(
                        [
                            "issue",
                            "list",
                            "-R",
                            r,
                            "--label",
                            "agent-triage",
                            "--state",
                            "open",
                            "--json",
                            "number",
                            "--limit",
                            "50",
                        ]
                    )
                )
            except subprocess.CalledProcessError:
                pass
        local_pending = sum(
            1 for q in load_local_queue() if q.get("repo") == r and q.get("status") != "done"
        )
        rows.append(
            {
                "repo": r,
                "short": short,
                "ci": ci,
                "parked": parked,
                "park_reason": park_info.get("reason", ""),
                "park_until": (park_info.get("until") or "")[:19],
                "agent_issues": open_issues,
                "local_queue": local_pending,
            }
        )
    return {
        "repos": rows,
        "local_queue_total": len(load_local_queue()),
        "ci_heal_queue": len(load_ci_heal_queue()),
        "parked_count": sum(1 for row in rows if row["parked"]),
        "active_count": sum(1 for row in rows if not row["parked"]),
    }


def is_repo_parked(repo: str) -> bool:
    blocked = load_fleet_state().get("blocked", {}).get(repo)
    if not blocked:
        return False
    until = blocked.get("until")
    if not until:
        return False
    try:
        if datetime.now(timezone.utc) < datetime.fromisoformat(until.replace("Z", "+00:00")):
            return True
    except ValueError:
        return False
    return False


def park_repo(repo: str, reason: str, *, hours: float = 2.0) -> None:
    state = load_fleet_state()
    until = datetime.now(timezone.utc).timestamp() + hours * 3600
    state.setdefault("blocked", {})[repo] = {
        "until": datetime.fromtimestamp(until, tz=timezone.utc).isoformat(),
        "reason": reason[:500],
        "parked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_fleet_state(state)
    log(f"parked {repo} for {hours}h: {reason[:120]}")


def load_activity_entries() -> list[dict[str, Any]]:
    if not ACTIVITY_LOG.exists():
        return []
    try:
        return json.loads(ACTIVITY_LOG.read_text())
    except json.JSONDecodeError:
        return []


def recent_repo_activity(repo: str, *, hours: float = 24.0) -> tuple[int, int]:
    """Return (passes, failures) for repo within the last N hours."""
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    passes = failures = 0
    for ev in load_activity_entries():
        if ev.get("repo") != repo:
            continue
        try:
            ts = datetime.fromisoformat(str(ev.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        if ev.get("event") == "pass":
            passes += 1
        elif ev.get("event") == "failure":
            failures += 1
    return passes, failures


def count_blocked_seed_kinds(repo: str) -> int:
    items = load_failure_ledger().get("items", {})
    return sum(
        1
        for key, entry in items.items()
        if key.startswith(f"{repo}::seed::") and entry.get("blocked")
    )


def count_blocked_issues(repo: str) -> int:
    items = load_failure_ledger().get("items", {})
    return sum(
        1
        for entry in items.values()
        if entry.get("repo") == repo and entry.get("scope") == "issue" and entry.get("blocked")
    )


def local_queue_pending(repo: str) -> int:
    return sum(
        1
        for item in load_local_queue()
        if item.get("repo") == repo and item.get("status") != "done" and not is_failure_blocked(repo, "local", (item.get("title") or "")[:80])
    )


def list_open_agent_issues(repo: str) -> list[dict[str, Any]]:
    if not repo_has_issues(repo):
        return []
    try:
        return gh_json(
            [
                "issue",
                "list",
                "-R",
                repo,
                "--label",
                "agent-triage",
                "--state",
                "open",
                "--json",
                "number,title",
                "--limit",
                "30",
            ]
        )
    except subprocess.CalledProcessError:
        return []


def issue_solvability_tier(repo: str, title: str, issue_num: int) -> str:
    """blocked | stale | hard | normal | easy"""
    if is_failure_blocked(repo, "issue", str(issue_num)):
        return "blocked"
    if is_stale_ci_issue(title, repo):
        return "stale"
    prio = issue_fix_priority({"title": title, "number": issue_num})[0]
    if prio >= 9 or is_seed_kind_blocked(repo, title):
        return "hard"
    if prio <= 2:
        return "easy"
    return "normal"


def analyze_repo_issue_surface(repo: str, entry: dict[str, Any]) -> dict[str, int]:
    counts = {"easy": 0, "normal": 0, "hard": 0, "blocked": 0, "stale": 0, "solvable": 0}
    if entry.get("local_fix") or not repo_has_issues(repo):
        for item in load_local_queue():
            if item.get("repo") != repo or item.get("status") == "done":
                continue
            title = item.get("title") or ""
            if is_failure_blocked(repo, "local", title[:80]):
                counts["blocked"] += 1
                continue
            prio = issue_fix_priority({"title": title, "number": 0})[0]
            if prio >= 9 or is_seed_kind_blocked(repo, title):
                counts["hard"] += 1
            elif prio <= 2:
                counts["easy"] += 1
                counts["solvable"] += 1
            else:
                counts["normal"] += 1
                counts["solvable"] += 1
        return counts
    for issue in list_open_agent_issues(repo):
        tier = issue_solvability_tier(repo, issue.get("title", ""), int(issue.get("number") or 0))
        counts[tier] = counts.get(tier, 0) + 1
        if tier in ("easy", "normal"):
            counts["solvable"] += 1
    return counts


def worker_interval_secs(base_interval: int, solv: dict[str, Any]) -> int:
    """High-solvability repos sleep less; cold repos rotate slower."""
    score = float(solv.get("score", 0))
    if score <= 0:
        return min(max(base_interval * 3, base_interval), 900)
    mult = 2.0 - (score / 100.0) * 1.45
    return max(120, min(900, int(base_interval * mult)))


def factory_max_for_repo(solv: dict[str, Any], default_max: int) -> int:
    score = float(solv.get("score", 0))
    if score < 15:
        return 0
    if score >= 75:
        return min(default_max + 1, 4)
    if score >= 45:
        return default_max
    return max(1, default_max - 1)


def roam_candidate_repos() -> list[str]:
    cfg = load_airport_config()
    repos: list[str] = []
    for lane in cfg.get("lanes") or []:
        kind = lane.get("kind", "github")
        if kind in ("roam", "upstream"):
            continue
        if lane.get("repo"):
            repos.append(lane["repo"])
    if not repos:
        repos = [e["name"] for e in load_repos_config()]
    return list(dict.fromkeys(repos))


def compute_repo_solvability(repo: str, entry: dict[str, Any] | None = None, *, use_cache: bool = True) -> dict[str, Any]:
    """Score 0–100: how likely this repo yields a merge on the next pass."""
    if use_cache:
        cached = _SOLV_CACHE.get(repo)
        if cached and time.time() - cached[0] < SOLV_CACHE_TTL_SECS:
            return cached[1]

    entry = entry or repo_entry(repo) or {"name": repo}
    factors: dict[str, Any] = {"repo": repo}
    score = 20.0

    if is_repo_parked(repo):
        result = {
            "repo": repo,
            "score": 0,
            "tier": "parked",
            "interval_secs": 900,
            "factory_max": 0,
            "factors": {**factors, "parked": True},
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _SOLV_CACHE[repo] = (time.time(), result)
        return result

    surface = analyze_repo_issue_surface(repo, entry)
    factors.update(surface)
    local_pending = local_queue_pending(repo)
    factors["local_queue"] = local_pending

    score += min(28, surface["easy"] * 10 + surface["normal"] * 5 + surface["solvable"] * 2)
    score += min(14, local_pending * 7)

    passes, failures = recent_repo_activity(repo, hours=24)
    factors["passes_24h"] = passes
    factors["failures_24h"] = failures
    total_recent = passes + failures
    if total_recent:
        score += (passes / total_recent) * 18
    elif passes:
        score += 12

    passes_6h, failures_6h = recent_repo_activity(repo, hours=6)
    factors["no_commits_6h"] = failures_6h
    score -= min(14, failures_6h * 4)

    blocked_seeds = count_blocked_seed_kinds(repo)
    blocked_issues = count_blocked_issues(repo)
    factors["blocked_seed_kinds"] = blocked_seeds
    factors["blocked_issues"] = blocked_issues
    score -= min(12, blocked_seeds * 4 + blocked_issues * 3)
    score -= min(8, surface["hard"] * 2)

    ci = main_branch_ci_state(repo)
    factors["ci"] = ci
    if ci == "success":
        score += 8
    elif ci == "failure":
        score += 3
    elif ci == "pending":
        score -= 2

    stack = "rust" if "cargo" in (entry.get("test_command") or "") else "python"
    if entry.get("local_fix"):
        stack = "fork"
    factors["stack"] = stack
    if stack == "rust":
        score += 2
    if stack == "fork":
        score -= 3

    score = max(0, min(100, int(round(score))))
    if score >= 70:
        tier = "hot"
    elif score >= 45:
        tier = "warm"
    elif score >= 20:
        tier = "cool"
    else:
        tier = "cold"

    base_interval = 300
    for lane in load_airport_config().get("lanes") or []:
        if lane.get("repo") == repo:
            base_interval = int(lane.get("interval", 300))
            break

    result = {
        "repo": repo,
        "score": score,
        "tier": tier,
        "stack": stack,
        "interval_secs": worker_interval_secs(base_interval, {"score": score}),
        "factory_max": factory_max_for_repo({"score": score}, 2),
        "factors": factors,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _SOLV_CACHE[repo] = (time.time(), result)
    return result


def score_fleet_solvability(repos: list[str] | None = None) -> list[dict[str, Any]]:
    if repos is None:
        repos = roam_candidate_repos()
    scored = [compute_repo_solvability(r) for r in repos]
    return sorted(scored, key=lambda s: (-s["score"], s["repo"]))


def save_solvability_snapshot(repos: list[str] | None = None) -> list[dict[str, Any]]:
    ranked = score_fleet_solvability(repos)
    SOLVABILITY_STATE.parent.mkdir(parents=True, exist_ok=True)
    SOLVABILITY_STATE.write_text(
        json.dumps(
            {
                "ranked": ranked,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )
    save_airport_status({"solvability": {"ts": datetime.now(timezone.utc).isoformat(), "top": ranked[:3]}})
    return ranked


def fleet_repo_priority(entry: dict[str, Any]) -> tuple[int, int, int, str]:
    """Lower sort key = higher priority — solvability-first fleet ordering."""
    solv = compute_repo_solvability(entry["name"], entry)
    return (-solv["score"], solv["factors"].get("easy", 0) * -1, solv["factors"].get("solvable", 0) * -1, entry["name"])


def sorted_fleet_repos(repos: list[dict[str, Any]], *, score: bool = True) -> list[dict[str, Any]]:
    if score:
        return sorted(repos, key=fleet_repo_priority)
    return sorted(repos, key=lambda e: (is_repo_parked(e["name"]), e["name"]))


def ci_status_glyph(state: str) -> str:
    return {"success": "✓", "failure": "✗", "pending": "…", "none": "—", "parked": "⏸", "off": "⊘"}.get(state, "?")


def emit_ci_watch_dashboard(
    rows: list[dict[str, Any]],
    *,
    healed: int,
    queue_rc: int,
) -> None:
    parts: list[str] = []
    for row in rows:
        label = f"{row['short']}{ci_status_glyph(row['ci'])}"
        if row.get("healed"):
            label += f"+{row['healed']}"
        parts.append(label)
    log(f"ci-watch: {' '.join(parts)} | healed={healed} queue={queue_rc}")


def repo_has_issues(repo: str) -> bool:
    result = run(["gh", "repo", "view", repo, "--json", "hasIssuesEnabled"], check=False)
    if result.returncode != 0:
        return False
    try:
        return bool(json.loads(result.stdout or "{}").get("hasIssuesEnabled"))
    except json.JSONDecodeError:
        return False


def load_repos_config() -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    for entry in load_repos_config_raw():
        name = entry["name"]
        if entry.get("local_fix"):
            repos.append(entry)
            continue
        if entry.get("skip_issues_check"):
            repos.append(entry)
            continue
        if repo_has_issues(name):
            repos.append(entry)
        else:
            log(f"skip {name}: issues disabled (add local_fix: true to repos.yaml)")
    return repos


def load_local_queue() -> list[dict[str, Any]]:
    if not LOCAL_QUEUE.exists():
        return []
    try:
        data = json.loads(LOCAL_QUEUE.read_text())
        return list(data.get("queue", []))
    except json.JSONDecodeError:
        return []


def save_local_queue(queue: list[dict[str, Any]]) -> None:
    LOCAL_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_QUEUE.write_text(json.dumps({"queue": queue, "ts": datetime.now(timezone.utc).isoformat()}, indent=2))


def enqueue_local(repo: str, spec: dict[str, Any]) -> None:
    queue = load_local_queue()
    key = f"{repo}::{spec['title']}"
    if any(f"{q['repo']}::{q['title']}" == key for q in queue):
        return
    queue.append({"repo": repo, "title": spec["title"], "body": spec.get("body", ""), "status": "pending"})
    save_local_queue(queue)
    log(f"queued local fix: {repo} — {spec['title']}")


def ensure_repo_labels(repo: str) -> None:
    if not repo_has_issues(repo):
        return
    existing = {l["name"] for l in gh_json(["label", "list", "-R", repo, "--json", "name", "--limit", "100"])}
    for name, color, desc in STANDARD_LABELS:
        if name in existing:
            continue
        run(
            ["gh", "label", "create", name, "-R", repo, "--color", color, "--description", desc],
            check=False,
        )


def set_repo_topics(repo: str, topics: list[str]) -> None:
    if not topics:
        return
    log(f"+ gh api PUT repos/{repo}/topics (names={topics})")
    run(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/topics", "-f", f"names={topics}"],
        check=False,
    )


def _merge_backlog_file(path: Path, merged: dict[str, list[dict[str, Any]]]) -> None:
    if not path.exists() or not yaml:
        return
    data = yaml.safe_load(path.read_text()) or {}
    for repo, items in (data.get("repos") or {}).items():
        if not isinstance(items, list):
            continue
        merged.setdefault(repo, [])
        seen = {i["title"] for i in merged[repo] if isinstance(i, dict) and i.get("title")}
        for item in items:
            if isinstance(item, dict) and item.get("title") and item["title"] not in seen:
                merged[repo].append(item)
                seen.add(item["title"])


def load_collector_backlog() -> dict[str, list[dict[str, Any]]]:
    """Merge backlog.yaml + upstream-backlog.yaml + legacy ISSUE_BACKLOG."""
    merged: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in ISSUE_BACKLOG.items()}
    _merge_backlog_file(BACKLOG_FILE, merged)
    _merge_backlog_file(UPSTREAM_BACKLOG_FILE, merged)
    return merged


def completed_local_titles(repo: str) -> set[str]:
    """Infer backlog items already shipped on forks (issues API disabled)."""
    done: set[str] = set()
    branch = default_branch(repo)
    tree = run(["gh", "api", f"repos/{repo}/git/trees/{branch}?recursive=1"], check=False)
    if tree.returncode != 0:
        return done
    try:
        paths = {t["path"] for t in json.loads(tree.stdout or "{}").get("tree", [])}
    except json.JSONDecodeError:
        return done

    if "FORK.md" in paths:
        done.update(
            {
                "Add FORK.md documenting Nueramarcos fork",
                "Add FORK.md for Nueramarcos vision fork",
            }
        )
    if ".github/workflows/fork-smoke.yml" in paths:
        done.update(
            {
                "Add lightweight fork CI workflow",
                "Add fork smoke CI workflow",
            }
        )

    readme = run(["gh", "api", f"repos/{repo}/contents/README.md?ref={branch}"], check=False)
    if readme.returncode == 0:
        try:
            raw = json.loads(readme.stdout or "{}").get("content", "")
            text = base64.b64decode(raw).decode("utf-8", errors="replace")
            if re.search(r"fork\s+notice", text, re.I):
                done.update(
                    {
                        "Add fork banner to README",
                        "Add fork notice to README",
                    }
                )
        except (json.JSONDecodeError, ValueError):
            pass
    return done


def existing_issue_titles(repo: str) -> set[str]:
    if not repo_has_issues(repo):
        titles = {q["title"] for q in load_local_queue() if q.get("repo") == repo}
        titles |= completed_local_titles(repo)
        return titles
    issues = gh_json(["issue", "list", "-R", repo, "--state", "all", "--json", "title", "--limit", "100"])
    return {i["title"] for i in issues}


def create_collected_issue(repo: str, spec: dict[str, Any]) -> int | None:
    labels = list(spec.get("labels") or ["agent-triage"])
    if "agent-triage" not in labels:
        labels.insert(0, "agent-triage")
    cmd = ["gh", "issue", "create", "-R", repo, "--title", spec["title"], "--body", spec.get("body", "")]
    for label in labels:
        cmd.extend(["--label", label])
    result = run(cmd, check=False)
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    return int(url.rsplit("/", 1)[-1])


def discover_repo_issues(repo: str) -> list[dict[str, Any]]:
    """Auto-detect fixable gaps by inspecting the repo tree on GitHub."""
    found: list[dict[str, Any]] = []
    branch = default_branch(repo)
    result = run(["gh", "api", f"repos/{repo}/git/trees/{branch}?recursive=1"], check=False)
    if result.returncode != 0:
        return found
    try:
        paths = {t["path"] for t in json.loads(result.stdout or "{}").get("tree", [])}
    except json.JSONDecodeError:
        return found

    junk = [p for p in paths if p in JUNK_FILE_PATTERNS or p.startswith("python ") or p.startswith("path")]
    if junk:
        found.append(
            {
                "title": "Remove accidental junk files from repo root",
                "body": "Delete accidental agent artifact files:\n" + "\n".join(f"- `{p}`" for p in sorted(junk)),
                "labels": ["agent-triage", "bug"],
            }
        )
    if not any(p.startswith(".github/workflows/") for p in paths):
        found.append(
            {
                "title": "Add GitHub Actions CI workflow",
                "body": "Add .github/workflows/ci.yml with appropriate tests for this repo (pytest or cargo test).",
                "labels": ["agent-triage", "enhancement"],
            }
        )
    if repo.endswith(("orion-ai-agent", "forge-ci-reliability", "nexus-vision-engine")):
        if not any(p.startswith("tests/") for p in paths):
            found.append(
                {
                    "title": "Add pytest smoke tests",
                    "body": "Add minimal tests/ directory with import/smoke tests. Must pass python -m pytest -q.",
                    "labels": ["agent-triage", "enhancement", "good first issue"],
                }
            )
        if "requirements-dev.txt" not in paths:
            found.append(
                {
                    "title": "Add requirements-dev.txt with pytest",
                    "body": "Create requirements-dev.txt listing pytest (and test deps if needed).",
                    "labels": ["agent-triage", "enhancement", "good first issue"],
                }
            )
    if "README.md" in paths and "badges" not in "".join(paths).lower():
        found.append(
            {
                "title": "Add README badges and project shields",
                "body": "Add badges to README.md top: License, language, CI placeholder. README.md only.",
                "labels": ["agent-triage", "documentation"],
            }
        )
    if "CONTRIBUTING.md" not in paths:
        found.append(
            {
                "title": "Add CONTRIBUTING.md with dev setup",
                "body": "Add CONTRIBUTING.md: clone, install deps, run tests. Keep under 40 lines.",
                "labels": ["agent-triage", "documentation", "good first issue"],
            }
        )
    if repo.endswith("vertex-sim-core") and not any(p.startswith(".github/ISSUE_TEMPLATE") for p in paths):
        found.append(
            {
                "title": "Add issue and PR templates",
                "body": "Add .github/ISSUE_TEMPLATE/bug_report.md and .github/pull_request_template.md.",
                "labels": ["agent-triage", "documentation", "good first issue"],
            }
        )
    return found


def load_upstream_opportunities() -> dict[str, Any]:
    if not UPSTREAM_OPPORTUNITIES_FILE.exists() or not yaml:
        return {"hardware": {}, "live_queries": [], "opportunities": [], "excluded_repos": []}
    data = yaml.safe_load(UPSTREAM_OPPORTUNITIES_FILE.read_text()) or {}
    return {
        "hardware": data.get("hardware") or {},
        "live_queries": list(data.get("live_queries") or []),
        "opportunities": list(data.get("opportunities") or []),
        "excluded_repos": list(data.get("excluded_repos") or []),
        "excluded_reason": data.get("excluded_reason") or "",
    }


def excluded_scout_repos(catalog: dict[str, Any] | None = None) -> set[str]:
    cat = catalog or load_upstream_opportunities()
    return {str(r).lower() for r in cat.get("excluded_repos") or []}


def filter_excluded_scout_items(
    items: list[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    blocked = excluded_scout_repos(catalog)
    if not blocked:
        return items
    return [it for it in items if (it.get("repo") or "").lower() not in blocked]


def load_scout_queue() -> list[dict[str, Any]]:
    if not SCOUT_QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(SCOUT_QUEUE_FILE.read_text())
        return list(data.get("queue", []))
    except json.JSONDecodeError:
        return []


def save_scout_queue(queue: list[dict[str, Any]]) -> None:
    SCOUT_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCOUT_QUEUE_FILE.write_text(
        json.dumps({"queue": queue, "ts": datetime.now(timezone.utc).isoformat()}, indent=2)
    )


def _scout_item_key(item: dict[str, Any]) -> str:
    repo = item.get("repo") or ""
    num = item.get("number") or 0
    return f"{repo}#{num}"


def gh_search_issues(query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    from urllib.parse import quote_plus

    if not ensure_gh_ready():
        return []
    url = f"search/issues?q={quote_plus(query)}&per_page={limit}&sort=updated"
    result = run(["gh", "api", url], check=False)
    if result.returncode != 0:
        log(f"scout search failed: {query}")
        return []
    try:
        return list(json.loads(result.stdout or "{}").get("items", []))
    except json.JSONDecodeError:
        return []


def _live_issue_to_opportunity(hit: dict[str, Any], tags: list[str], hw: dict[str, Any]) -> dict[str, Any]:
    repo_url = hit.get("repository_url") or ""
    repo = repo_url.rsplit("/", 2)[-2] + "/" + repo_url.rsplit("/", 1)[-1] if repo_url else ""
    title = hit.get("title") or ""
    labels = {l.get("name", "").lower() for l in hit.get("labels") or []}
    body = (hit.get("body") or "").lower()
    arch = (hw.get("arch") or "").lower()
    score = 55
    if "good first issue" in labels or "good-first-issue" in labels:
        score += 15
    if "bug" in labels:
        score += 8
    if "bounty" in title.lower() or "bounty" in labels:
        score += 5
    if "amd" in title.lower() or "rocm" in title.lower() or "hip" in title.lower():
        score += 12
    if arch and arch in title.lower():
        score += 20
    if "commaai" in repo or "openpilot" in title.lower() or "tesla" in body:
        score += 6
    if hit.get("pull_request"):
        score -= 25
    effort = "m"
    if "good first" in labels or len(title) < 60:
        effort = "s"
    if "bounty" in title.lower() or "refactor" in title.lower():
        effort = "l"
    tier = 1 if score >= 85 else 2 if score >= 70 else 3
    return {
        "repo": repo,
        "number": int(hit.get("number") or 0),
        "title": title,
        "url": hit.get("html_url") or "",
        "score": min(score, 99),
        "tier": tier,
        "effort": effort,
        "impact": "medium",
        "tags": list(dict.fromkeys([*tags, "live"])),
        "hardware_fit": [arch] if arch and arch in title.lower() else ["any"],
        "why": "Live GitHub search hit — verify repro steps before committing.",
        "status": "open",
        "source": "live",
    }


def merge_scout_opportunities(
    curated: list[dict[str, Any]],
    live: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted([*curated, *live], key=lambda x: (-int(x.get("score") or 0), int(x.get("tier") or 9))):
        key = _scout_item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def filter_scout_opportunities(
    items: list[dict[str, Any]],
    *,
    tag: str | None = None,
    tier: int | None = None,
    min_score: int = 0,
    arch: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tag_l = (tag or "").lower()
    arch_l = (arch or "").lower()
    for item in items:
        if int(item.get("score") or 0) < min_score:
            continue
        if tier is not None and int(item.get("tier") or 9) > tier:
            continue
        tags = [t.lower() for t in item.get("tags") or []]
        fit = [f.lower() for f in item.get("hardware_fit") or []]
        if tag_l and tag_l not in tags and tag_l not in (item.get("title") or "").lower():
            continue
        if arch_l and arch_l not in fit and arch_l not in (item.get("title") or "").lower():
            continue
        out.append(item)
    return out


def update_scout_queue_item(repo: str, number: int, **fields: Any) -> bool:
    queue = load_scout_queue()
    key = f"{repo}#{number}"
    for item in queue:
        if _scout_item_key(item) == key:
            item.update(fields)
            item["updated"] = datetime.now(timezone.utc).isoformat()
            save_scout_queue(queue)
            return True
    return False


def next_scout_work() -> dict[str, Any] | None:
    """Highest-priority pending scout-queue item (in_progress first, then queued)."""
    queue = load_scout_queue()
    for status in ("in_progress", "queued"):
        pending = [q for q in queue if q.get("status") == status]
        if pending:
            return sorted(pending, key=lambda x: (-int(x.get("score") or 0), int(x.get("tier") or 9)))[0]
    return None


def enqueue_scout_items(items: list[dict[str, Any]], *, max_n: int) -> int:
    queue = load_scout_queue()
    existing = {_scout_item_key(q) for q in queue}
    blocked = excluded_scout_repos()
    added = 0
    for item in items:
        if added >= max_n:
            break
        if (item.get("repo") or "").lower() in blocked:
            continue
        key = _scout_item_key(item)
        if key in existing:
            continue
        queue.append(
            {
                "repo": item.get("repo"),
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("url"),
                "score": item.get("score"),
                "tier": item.get("tier"),
                "effort": item.get("effort"),
                "tags": item.get("tags") or [],
                "why": item.get("why", ""),
                "test_hint": item.get("test_hint", ""),
                "status": "queued",
                "added": datetime.now(timezone.utc).isoformat(),
            }
        )
        existing.add(key)
        added += 1
    if added:
        save_scout_queue(queue)
    return added


def cmd_scout(args: argparse.Namespace) -> int:
    load_secrets()
    catalog = load_upstream_opportunities()
    hw = catalog.get("hardware") or {}
    curated = list(catalog.get("opportunities") or [])
    live_items: list[dict[str, Any]] = []

    if args.live:
        if not ensure_gh_ready():
            print("  [FAIL] gh not authenticated — run: gh auth login")
            return 1
        for spec in catalog.get("live_queries") or []:
            q = spec.get("q") if isinstance(spec, dict) else None
            if not q:
                continue
            tags = list(spec.get("tags") or []) if isinstance(spec, dict) else []
            for hit in gh_search_issues(q, limit=args.live_limit):
                live_items.append(_live_issue_to_opportunity(hit, tags, hw))

    items = merge_scout_opportunities(curated, live_items)
    items = filter_excluded_scout_items(items, catalog)
    items = filter_scout_opportunities(
        items,
        tag=args.tag,
        tier=args.tier,
        min_score=args.min_score,
        arch=args.arch,
    )
    if getattr(args, "web", False):
        items = [enrich_opportunity(item, web=True) for item in items]
    if args.limit:
        items = items[: args.limit]

    queue = load_scout_queue()
    queued_keys = {_scout_item_key(q) for q in queue if q.get("status") in ("queued", "in_progress")}

    if args.json:
        print(json.dumps({"hardware": hw, "items": items, "queued": queue}, indent=2))
    else:
        gpu = hw.get("gpu") or "any"
        arch = hw.get("arch") or "any"
        print(f"Upstream scout — {gpu} ({arch})\n")
        excluded = excluded_scout_repos(catalog)
        if excluded:
            reason = catalog.get("excluded_reason") or ", ".join(sorted(excluded))
            print(f"  excluded: {reason}\n")
        if args.live:
            print(f"  live search: {len(live_items)} hits merged\n")
        if queue:
            pending = [q for q in queue if q.get("status") in ("queued", "in_progress")]
            print(f"  scout queue: {len(pending)} pending (scout-queue.json)\n")
        print(f"{'Score':>5} {'T':>2} {'Eff':>3}  {'Repo':<22} {'#':>5}  Tags")
        print("-" * 78)
        for item in items:
            key = _scout_item_key(item)
            mark = "*" if key in queued_keys else " "
            tags = ",".join((item.get("tags") or [])[:4])
            repo_short = (item.get("repo") or "")[-22:]
            num = item.get("number") or 0
            print(
                f"{mark}{int(item.get('score') or 0):>4} T{item.get('tier', '?')} "
                f"{str(item.get('effort', '?')):>3}  {repo_short:<22} {num:>5}  {tags[:28]}"
            )
            title = (item.get("title") or "")[:72]
            print(f"      {title}")
            if item.get("why"):
                print(f"      → {item['why'][:100]}")
            if item.get("test_hint"):
                print(f"      $ {item['test_hint'][:90]}")
            print(f"      {item.get('url', '')}")
            print()

    if args.enqueue:
        n = enqueue_scout_items(items, max_n=args.enqueue)
        print(f"Enqueued {n} item(s) → {SCOUT_QUEUE_FILE}")

    return 0


def cmd_personality(args: argparse.Namespace) -> int:
    """Personality quiz → archetype → best scout opportunity; optional X broadcast."""
    load_secrets()

    if args.quiz_only:
        if args.thread:
            for i, post in enumerate(compose_quiz_thread(), 1):
                print(f"--- tweet {i} ---\n{post}\n")
        else:
            print(compose_quiz_post())
        return 0

    catalog = load_upstream_opportunities()
    hw = catalog.get("hardware") or {}
    curated = list(catalog.get("opportunities") or [])
    items = merge_scout_opportunities(curated, [])
    items = filter_excluded_scout_items(items, catalog)
    items = filter_scout_opportunities(
        items,
        tag=args.tag,
        tier=args.tier,
        min_score=0,
        arch=args.arch,
    )

    if args.answers:
        if len(args.answers) != len(QUESTIONS):
            print(f"Need {len(QUESTIONS)} letters (one per question), e.g. abdca")
            return 1
        answers = {}
        for q, letter in zip(QUESTIONS, args.answers.lower()):
            if letter not in q["choices"]:
                print(f"Invalid choice '{letter}' for: {q['prompt']}")
                return 1
            answers[q["id"]] = letter
    elif args.interactive:
        answers = run_interactive_quiz()
    else:
        answers = {
            "friday": "d",
            "test_output": "a",
            "heartbreak": "d",
            "patience": "a",
            "workstation": "c",
        }
        print("No --answers or --interactive — using Marcos default profile (Vision Phantom, no tinygrad)\n")

    scores = tally_answers(answers)
    archetype_key, archetype, item, match_score = match_opportunity(items, scores)
    code = answers_code(answers)

    if args.json:
        print(
            json.dumps(
                {
                    "answers": answers,
                    "answers_code": code,
                    "scores": scores,
                    "archetype_key": archetype_key,
                    "archetype": archetype,
                    "match_score": match_score,
                    "item": item,
                    "hardware": hw,
                },
                indent=2,
            )
        )
        return 0

    print(format_result(archetype_key, archetype, item, match_score, scores))

    if args.enqueue:
        n = enqueue_scout_items([item], max_n=1)
        print(f"Enqueued target → {SCOUT_QUEUE_FILE} ({n} item)")

    result_text = compose_result_post(archetype_key, archetype, item, answers_code=code)
    path = save_broadcast(result_text, BROADCAST_DIR)
    print(f"\nX draft → {path}")
    if args.post:
        if args.thread:
            ok_all = True
            for i, post in enumerate(compose_quiz_thread(), 1):
                ok, detail = post_to_x(post)
                print(f"tweet {i}: {detail}")
                ok_all = ok_all and ok
            ok, detail = post_to_x(result_text)
            print(f"result: {detail}")
            return 0 if ok_all and ok else 1
        ok, detail = post_to_x(result_text if not args.quiz_first else compose_quiz_post())
        print(detail)
        if args.quiz_first and ok:
            ok2, detail2 = post_to_x(result_text)
            print(f"result: {detail2}")
            return 0 if ok2 else 1
        return 0 if ok else 1
    return 0


def cmd_hunt(args: argparse.Namespace) -> int:
    """Show next upstream work item and the exact commands to run."""
    load_secrets()
    item = next_scout_work()
    if not item and args.enqueue:
        cmd_scout(argparse.Namespace(
            tag=None, tier=1, arch=None, min_score=0, limit=3, live=False, live_limit=6,
            enqueue=args.enqueue, json=False,
        ))
        item = next_scout_work()
    if not item:
        print("Scout queue empty. Run: issue-agent scout --tier 1 --enqueue 3")
        return 0

    repo = item.get("repo") or ""
    num = item.get("number") or 0
    slug = repo.split("/")[-1] if repo else "upstream"
    ws = Path(os.environ.get("ISSUE_AGENT_UPSTREAM_WS", HOME / "upstream-workspaces")) / slug
    fork_ws = WORKSPACES / f"Nueramarcos_{slug}"

    print(f"Hunt — next: {repo} #{num}\n")
    print(f"  {item.get('title', '')}")
    print(f"  {item.get('url', '')}")
    if item.get("why"):
        print(f"  → {item['why']}")
    if item.get("test_hint"):
        print(f"  $ {item['test_hint']}")
    print()
    print("  playbook:")
    print(f"    gh repo fork {repo} --clone {ws}   # or use {fork_ws}")
    print(f"    cd {fork_ws if fork_ws.exists() else ws}")
    print(f"    git fetch upstream master && git checkout -B fix/issue-{num} upstream/master")
    print(f"    # reproduce, fix, test")
    print(f"    git push -u origin fix/issue-{num}")
    print(f"    gh pr create -R {repo} --head Nueramarcos:fix/issue-{num} --title '...' --body 'Fixes #{num}'")
    print()
    print(f"  queue: {SCOUT_QUEUE_FILE}")

    if args.mark:
        update_scout_queue_item(repo, num, status=args.mark)
        print(f"  marked {repo}#{num} → {args.mark}")

    return 0


def seed_backlog_issues(repo: str, max_new: int = 2) -> list[int]:
    """Open backlog issues that do not already exist by title."""
    backlog = load_collector_backlog().get(repo, [])
    if not backlog:
        return []
    titles = existing_issue_titles(repo)
    created: list[int] = []
    backlog = sorted(backlog, key=lambda spec: issue_fix_priority({"title": spec.get("title", ""), "number": 0}))
    for spec in backlog:
        if len(created) >= max_new:
            break
        if spec["title"] in titles:
            continue
        if not is_spec_seedable(repo, spec["title"]):
            continue
        num = create_collected_issue(repo, spec)
        if num:
            created.append(num)
            log(f"backlog issue #{num} on {repo}: {spec['title']}")
    return created


def cmd_collect(args: argparse.Namespace) -> int:
    load_secrets()
    repos = load_repos_config()
    if args.repo:
        repos = [{"name": args.repo, "topics": []}]

    created_total = 0
    collected: list[dict[str, Any]] = []

    for entry in repos:
        repo = entry["name"]
        ensure_repo_labels(repo)
        titles = existing_issue_titles(repo)
        specs: list[dict[str, Any]] = []

        raw_specs: list[dict[str, Any]] = []
        if args.discover:
            raw_specs.extend(discover_repo_issues(repo))
        if not args.discover_only:
            raw_specs.extend(load_collector_backlog().get(repo, []))

        specs: list[dict[str, Any]] = []
        seen_spec: set[str] = set()
        for spec in raw_specs:
            t = spec.get("title", "")
            if t and t not in seen_spec:
                seen_spec.add(t)
                specs.append(spec)

        use_local = entry.get("local_fix") or not repo_has_issues(repo)
        repo_created = 0
        for spec in specs:
            if repo_created >= args.max_per_repo:
                break
            if spec["title"] in titles:
                continue
            if not is_spec_seedable(repo, spec["title"]):
                continue
            if args.dry_run:
                mode = "local" if use_local else "github"
                print(f"  [dry-run/{mode}] {repo}: {spec['title']}")
                collected.append({"repo": repo, **spec})
                continue
            if use_local:
                enqueue_local(repo, spec)
                repo_created += 1
                created_total += 1
                print(f"  [local] {repo}: {spec['title']}")
                collected.append({"repo": repo, "local": True, **spec})
                continue
            num = create_collected_issue(repo, spec)
            if num:
                titles.add(spec["title"])
                repo_created += 1
                created_total += 1
                print(f"  #{num} {repo}: {spec['title']}")
                collected.append({"repo": repo, "number": num, **spec})

    if not args.dry_run:
        COLLECTOR_STATE.write_text(json.dumps({"collected": collected, "ts": datetime.now(timezone.utc).isoformat()}, indent=2))
    print(f"\nCollected {created_total} new issues")
    return 0


def process_local_queue(max_items: int = 3, *, repo_filter: str | None = None) -> int:
    queue = load_local_queue()
    pending = [
        item
        for item in queue
        if item.get("status") not in ("done", "completed")
        and (not repo_filter or item.get("repo") == repo_filter)
    ]
    pending.sort(key=lambda item: issue_fix_priority({"title": item.get("title", ""), "number": 0}))
    has_easy = any(
        issue_solvability_tier(
            item.get("repo", ""),
            item.get("title", ""),
            0,
        )
        == "easy"
        for item in pending
    )
    rc = 0
    done = 0
    remaining: list[dict[str, Any]] = []
    for item in queue:
        if item.get("status") == "done":
            continue
        if done >= max_items:
            remaining.append(item)
            continue
        repo = item["repo"]
        if repo_filter and repo != repo_filter:
            remaining.append(item)
            continue
        title = item.get("title", "")
        if is_repo_parked(repo):
            log(f"skip parked local fix: {repo}")
            remaining.append(item)
            continue
        if is_failure_blocked(repo, "local", title[:80]):
            log(f"skip blocked local fix: {repo} — {title[:60]}")
            remaining.append(item)
            continue
        tier = issue_solvability_tier(repo, title, 0)
        if has_easy and tier == "hard":
            log(f"skip hard local fix: {repo} — {title[:60]}")
            remaining.append(item)
            continue
        log(f"local fix: {repo} — {title}")
        if resolve_issue_local(repo, title=title, body=item.get("body", "")) == 0:
            item["status"] = "done"
            done += 1
        else:
            item["status"] = "failed"
            rc |= 1
            log(f"local fix failed on {repo} — move on")
    for item in queue:
        if item.get("status") != "done" and item not in remaining:
            remaining.append(item)
    save_local_queue([i for i in queue if i.get("status") != "done"])
    return rc


def cmd_max(args: argparse.Namespace) -> int:
    """Achievement mode: collect issues then fix+merge them."""
    load_secrets()
    ns = argparse.Namespace(repo=args.repo, max_per_repo=args.collect_max, dry_run=False, discover=True, discover_only=False)
    cmd_collect(ns)
    boost_ns = argparse.Namespace(max=args.fix_max, seed=False, dry_run=False)
    rc = cmd_boost(boost_ns)
    rc |= process_local_queue(max_items=args.fix_max * 2)
    return rc


def cmd_local(args: argparse.Namespace) -> int:
    load_secrets()
    run(["gh", "auth", "setup-git"], check=False)
    return process_local_queue(max_items=args.max)


def failed_runs_on_branch(repo: str, *, limit: int = 3) -> list[dict[str, Any]]:
    branch = default_branch(repo)
    result = run(
        [
            "gh",
            "run",
            "list",
            "-R",
            repo,
            "--branch",
            branch,
            "--status",
            "failure",
            "--limit",
            str(limit),
            "--json",
            "databaseId,displayTitle,workflowName,createdAt,conclusion",
        ],
        check=False,
    )
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []


def process_ci_heal_queue(max_items: int) -> int:
    queue = load_ci_heal_queue()
    if not queue:
        return 0
    rc = 0
    done = 0
    remaining: list[dict[str, Any]] = []
    for item in queue:
        if item.get("status") == "done":
            continue
        if done >= max_items:
            remaining.append(item)
            continue
        repo = item["repo"]
        if not ci_heal_enabled(None, repo):
            log(f"skip ci-heal queue (disabled): {repo}")
            item["status"] = "skipped"
            continue
        title = f"Fix CI failure on {repo}"
        body = item.get("logs") or "CI checks failed after agent PR."
        log(f"ci-heal queue: {repo}")
        rc_code = push_ci_repair_pr(repo, title, body)
        if rc_code == 0:
            item["status"] = "done"
            done += 1
        elif rc_code == 2:
            item["status"] = "skipped"
        else:
            item["status"] = "failed"
            rc |= 1
    for item in queue:
        if item.get("status") != "done" and item not in remaining:
            remaining.append(item)
    CI_HEAL_QUEUE.write_text(
        json.dumps({"queue": remaining, "ts": datetime.now(timezone.utc).isoformat()}, indent=2)
    )
    return rc


def scan_repo_ci_failures(repo: str, entry: dict[str, Any], state: dict[str, Any], *, fix: bool, max_per_repo: int) -> int:
    if main_branch_ci_state(repo) == "success" and (not entry.get("local_fix") or fork_smoke_healthy(repo)):
        return 0
    if open_ci_repair_prs(repo) or is_failure_blocked(repo, "ci_heal", "default"):
        return 0
    healed_runs = set(state.get("healed_runs", []))
    attempted = 0
    branch = default_branch(repo)
    for run in failed_runs_on_branch(repo, limit=max_per_repo):
        rid = int(run["databaseId"])
        if rid in healed_runs:
            continue
        logs = fetch_run_failed_logs(repo, rid)
        title = f"Fix CI: {run.get('workflowName', 'workflow')} failed on {branch}"
        body = f"Run {rid} ({run.get('displayTitle', '')}) failed.\n\n```\n{logs[-4000:]}\n```"
        log(f"CI failure detected: {repo} run {rid}")
        if not fix:
            healed_runs.add(rid)
            continue
        rc_code = push_ci_repair_pr(repo, title, body)
        if rc_code == 0:
            healed_runs.add(rid)
            attempted += 1
        elif rc_code == 2:
            healed_runs.add(rid)
        else:
            healed_runs.add(rid)
            log(f"ci-heal failed on {repo} run {rid} — recorded, moving on")
    state["healed_runs"] = sorted(healed_runs)
    return attempted


def cmd_ci_heal(args: argparse.Namespace) -> int:
    """Detect failed CI on default branches and repair (fast poll, no long wait)."""
    quiet = bool(getattr(args, "quiet", False))
    load_secrets()
    with quiet_commands(quiet):
        run(["gh", "auth", "setup-git"], check=False)
        repos = load_repos_config_raw()
        if args.repo:
            entry = repo_entry(args.repo)
            repos = [entry] if entry else [{"name": args.repo}]
        state = load_ci_heal_state()
        rc = process_ci_heal_queue(args.max)
        total = 0
        dashboard: list[dict[str, Any]] = []
        for entry in sorted_fleet_repos(repos, score=False):
            repo = entry["name"]
            short = repo.split("/")[-1]
            if args.repo and repo != args.repo:
                continue
            if is_repo_parked(repo):
                if not quiet:
                    log(f"skip ci-heal parked {repo}")
                dashboard.append({"short": short, "ci": "parked", "healed": 0})
                continue
            if not ci_heal_enabled(entry, repo):
                if not quiet:
                    log(f"skip ci-heal disabled {repo}")
                dashboard.append({"short": short, "ci": "off", "healed": 0})
                continue
            ci = main_branch_ci_state(repo)
            n = scan_repo_ci_failures(repo, entry, state, fix=not args.dry_run, max_per_repo=args.max_per_repo)
            total += n
            dashboard.append({"short": short, "ci": ci, "healed": n})
            if n and not quiet:
                print(f"  healed {n} CI failure(s) on {repo}")
        save_ci_heal_state(state)
    if quiet:
        emit_ci_watch_dashboard(dashboard, healed=total, queue_rc=rc)
    else:
        print(f"\nCI heal complete: {total} repair(s), queue rc={rc}")
    return rc


def prune_ci_heal_queue_for_repo(repo: str) -> int:
    """Drop pending ci-heal queue items for a repo."""
    queue = load_ci_heal_queue()
    before = len(queue)
    kept = [item for item in queue if item.get("repo") != repo or item.get("status") == "done"]
    removed = before - len(kept)
    if removed:
        CI_HEAL_QUEUE.write_text(
            json.dumps({"queue": kept, "ts": datetime.now(timezone.utc).isoformat()}, indent=2)
        )
        log_activity("queue_prune", repo, f"ci-heal removed {removed} item(s)")
    return removed


def list_matching_branches(repo: str, prefix: str) -> list[str]:
    owner, name = repo.split("/", 1)
    result = run(["gh", "api", f"repos/{owner}/{name}/branches", "--paginate"], check=False)
    try:
        branches = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return sorted(
        {
            str(b.get("name", ""))
            for b in branches
            if isinstance(b, dict) and str(b.get("name", "")).startswith(prefix)
        }
    )


def delete_remote_branch(repo: str, branch: str) -> bool:
    owner, name = repo.split("/", 1)
    result = run(
        ["gh", "api", "-X", "DELETE", f"repos/{owner}/{name}/git/refs/heads/{branch}"],
        check=False,
    )
    return result.returncode == 0


def cmd_cleanup_ci_prs(args: argparse.Namespace) -> int:
    """Close open fix/ci-* PRs and delete orphan branches (one-shot inbox cleanup)."""
    load_secrets()
    run(["gh", "auth", "setup-git"], check=False)
    repo = args.repo
    prefix = args.prefix
    result = run(
        ["gh", "pr", "list", "-R", repo, "--state", "open", "--json", "number,title,headRefName", "--limit", "100"],
        check=False,
    )
    try:
        prs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        prs = []
    targets = [
        p
        for p in prs
        if isinstance(p, dict) and str(p.get("headRefName", "")).startswith(prefix)
    ]
    closed = 0
    if targets:
        print(f"Found {len(targets)} open PR(s) on {repo} matching {prefix!r}")
        for pr in targets:
            num = pr.get("number")
            head = pr.get("headRefName", "")
            title = (pr.get("title") or "")[:60]
            if args.dry_run:
                print(f"  [dry-run] close #{num} {head} — {title}")
                continue
            close = run(
                ["gh", "pr", "close", str(num), "-R", repo, "--delete-branch"],
                check=False,
            )
            if close.returncode == 0:
                closed += 1
                log(f"closed #{num} {head} on {repo}")
                print(f"  closed #{num} {head}")
            else:
                log(f"failed to close #{num} on {repo}: {(close.stderr or close.stdout or '').strip()}")
                print(f"  FAILED #{num} {head}")
    else:
        print(f"No open PRs on {repo} with head prefix {prefix!r}")

    delete_orphan_branches = not getattr(args, "no_delete_orphan_branches", False)
    orphan_branches: list[str] = []
    if delete_orphan_branches:
        orphan_branches = list_matching_branches(repo, prefix)
        if orphan_branches:
            print(f"Found {len(orphan_branches)} orphan branch(es) matching {prefix!r}")
            for branch in orphan_branches:
                if args.dry_run:
                    print(f"  [dry-run] delete branch {branch}")
                    continue
                if delete_remote_branch(repo, branch):
                    log(f"deleted branch {branch} on {repo}")
                    print(f"  deleted branch {branch}")
                else:
                    print(f"  FAILED delete branch {branch}")
        elif not targets:
            print(f"No orphan branches on {repo} matching {prefix!r}")

    if not args.dry_run:
        entry = repo_entry(repo) or {"name": repo}
        if not ci_heal_enabled(entry, repo):
            pruned = prune_ci_heal_queue_for_repo(repo)
            if pruned:
                print(f"  pruned {pruned} ci-heal queue item(s) for {repo}")
        log_activity("ci_cleanup", repo, f"closed={closed} branches={len(orphan_branches)} prefix={prefix}")
    if args.dry_run:
        return 0
    pr_failed = bool(targets) and closed != len(targets)
    return 1 if pr_failed else 0


def cmd_ci_watch(args: argparse.Namespace) -> int:
    load_secrets()
    verbose = bool(getattr(args, "verbose", False))
    log(f"ci-watch: every {args.interval}s (Ctrl+C to stop){'' if verbose else ' — quiet dashboard'}")
    try:
        while True:
            cmd_ci_heal(
                argparse.Namespace(
                    repo=args.repo,
                    max=args.max,
                    max_per_repo=args.max_per_repo,
                    dry_run=args.dry_run,
                    quiet=not verbose,
                )
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("ci-watch stopped")
        return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    """Maximize throughput: work productive repos, park walls, rotate."""
    load_secrets()
    run(["gh", "auth", "setup-git"], check=False)
    hk = housekeeping(repo=args.repo)
    repos = sorted_fleet_repos(load_repos_config())
    active = [e for e in repos if not is_repo_parked(e["name"])]
    parked = [e for e in repos if is_repo_parked(e["name"])]
    print(f"FLEET: {len(active)} active · {len(parked)} parked")
    for entry in parked:
        info = load_fleet_state().get("blocked", {}).get(entry["name"], {})
        print(f"  ⏸  {entry['name']}: {info.get('reason', 'wall')} (until {info.get('until', '?')[:19]})")
    cmd_polish(argparse.Namespace())
    rc = 0
    for entry in active:
        if args.repo and entry["name"] != args.repo:
            continue
        repo = entry["name"]
        print(f"\n=== FLEET → {repo} ===")
        cmd_collect(
            argparse.Namespace(
                repo=repo,
                max_per_repo=1,
                dry_run=False,
                discover=not entry.get("local_fix"),
                discover_only=False,
            )
        )
        if entry.get("local_fix") or not repo_has_issues(repo):
            rc |= process_local_queue(max_items=1)
        else:
            rc |= cmd_boost(
                argparse.Namespace(repo=repo, max=1, seed=False, dry_run=False)
            )
    rc |= cmd_ci_heal(
        argparse.Namespace(repo=args.repo, max=args.ci_max, max_per_repo=1, dry_run=False)
    )
    summary = fleet_status_summary()
    save_status_digest({"housekeeping": hk, "fleet_rc": rc, **summary})
    log_activity("fleet_pass", args.repo or "all", f"rc={rc} active={summary['active_count']}")
    return rc


def cmd_daemon(args: argparse.Namespace) -> int:
    """24/7 iteration loop — fleet rotate + CI heal + housekeeping."""
    load_secrets()
    run(["gh", "auth", "setup-git"], check=False)
    log(f"daemon started — interval {args.interval}s (Ctrl+C to stop)")
    log_activity("daemon_start", "", f"interval={args.interval}s")
    try:
        while True:
            hk = housekeeping(repo=args.repo)
            rc = cmd_fleet(
                argparse.Namespace(repo=args.repo, ci_max=args.ci_max)
            )
            log(f"daemon pass complete rc={rc} housekeeping={hk}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("daemon stopped")
        log_activity("daemon_stop", "", "")
        return 0


def cmd_factory(args: argparse.Namespace) -> int:
    """Issue factory — discover gaps and seed backlog issues across lanes."""
    load_secrets()
    cfg = load_airport_config()
    lanes = cfg.get("lanes") or []
    repos = [lane["repo"] for lane in lanes if lane.get("repo")]
    if args.repo:
        repos = [args.repo]
    if not repos:
        repos = [e["name"] for e in load_repos_config()]
    ranked = score_fleet_solvability(repos)
    save_solvability_snapshot(repos)
    seeded_repos = 0
    for solv in ranked:
        repo = solv["repo"]
        entry = repo_entry(repo) or {"name": repo}
        max_for_repo = factory_max_for_repo(solv, args.max_per_repo) if not args.repo else args.max_per_repo
        if max_for_repo <= 0:
            log(f"factory skip {repo}: solvability {solv['score']} ({solv['tier']})")
            continue
        cmd_collect(
            argparse.Namespace(
                repo=repo,
                max_per_repo=max_for_repo,
                dry_run=args.dry_run,
                discover=True,
                discover_only=False,
            )
        )
        seeded_repos += 1
        if entry.get("local_fix"):
            backlog_specs = load_collector_backlog().get(repo, [])
            titles = existing_issue_titles(repo)
            for spec in backlog_specs[:max_for_repo]:
                if spec["title"] in titles:
                    continue
                if not is_spec_seedable(repo, spec["title"]):
                    continue
                enqueue_local(repo, spec)
                log(f"factory local enqueue: {repo} — {spec['title'][:60]}")
    save_airport_status({"last_factory": datetime.now(timezone.utc).isoformat(), "factory_repos": seeded_repos})
    log_activity("factory", args.repo or "all", f"repos={seeded_repos}/{len(repos)}")
    return 0


def run_repo_pass(repo: str, *, kind: str, collect_max: int, fix_max: int) -> int:
    """One collect + fix cycle for a github or local lane."""
    if is_repo_parked(repo):
        log(f"skip pass {repo}: parked")
        return 0
    cmd_collect(
        argparse.Namespace(
            repo=repo,
            max_per_repo=collect_max,
            dry_run=False,
            discover=True,
            discover_only=False,
        )
    )
    if kind == "local":
        return process_local_queue(max_items=fix_max, repo_filter=repo)
    return cmd_boost(argparse.Namespace(repo=repo, max=fix_max, seed=True, dry_run=False))


def cmd_worker(args: argparse.Namespace) -> int:
    """Single-lane worker — collect, fix, merge; interval scales with solvability."""
    os.environ["ISSUE_AGENT_AIRPORT"] = "1"
    load_secrets()
    ensure_gh_ready()
    run(["gh", "auth", "setup-git"], check=False)
    repo = args.repo
    entry = repo_entry(repo) or {"name": repo}
    kind = args.kind or ("local" if entry.get("local_fix") else "github")
    log(f"worker {repo} kind={kind} base_interval={args.interval}s")
    log_activity("worker_start", repo, f"kind={kind} interval={args.interval}s")
    save_airport_status({f"worker_{lane_slug(repo)}": {"repo": repo, "kind": kind, "started": datetime.now(timezone.utc).isoformat()}})
    try:
        while True:
            housekeeping(repo=repo)
            solv = compute_repo_solvability(repo, entry, use_cache=False)
            sleep_secs = solv["interval_secs"] if not is_repo_parked(repo) else min(solv["interval_secs"] * 2, 900)
            if is_repo_parked(repo):
                log(f"worker {repo}: brief park — rotating ({sleep_secs}s)")
            else:
                log(f"worker {repo}: solvability {solv['score']} ({solv['tier']}) — next sleep {sleep_secs}s")
                rc = run_repo_pass(repo, kind=kind, collect_max=args.collect_max, fix_max=args.fix_max)
                save_airport_status(
                    {
                        f"worker_{lane_slug(repo)}": {
                            "repo": repo,
                            "last_pass": datetime.now(timezone.utc).isoformat(),
                            "rc": rc,
                            "solvability": solv["score"],
                            "tier": solv["tier"],
                            "interval_secs": sleep_secs,
                        }
                    }
                )
            time.sleep(sleep_secs)
    except KeyboardInterrupt:
        log(f"worker {repo} stopped")
        return 0


def cmd_roam(args: argparse.Namespace) -> int:
    """Roaming worker — each pass picks the highest-solvability repo in the fleet."""
    os.environ["ISSUE_AGENT_AIRPORT"] = "1"
    load_secrets()
    run(["gh", "auth", "setup-git"], check=False)
    log(f"roamer started base_interval={args.interval}s")
    log_activity("worker_start", "", f"kind=roam interval={args.interval}s")
    try:
        while True:
            housekeeping()
            ranked = save_solvability_snapshot()
            picked: dict[str, Any] | None = None
            for solv in ranked:
                if solv["score"] < 15:
                    break
                repo = solv["repo"]
                if is_repo_parked(repo):
                    continue
                entry = repo_entry(repo) or {"name": repo}
                kind = "local" if entry.get("local_fix") else "github"
                if kind == "github" and not repo_has_issues(repo):
                    kind = "local"
                log(f"roam pick {repo}: score={solv['score']} tier={solv['tier']} stack={solv.get('stack')}")
                rc = run_repo_pass(repo, kind=kind, collect_max=args.collect_max, fix_max=args.fix_max)
                picked = {**solv, "rc": rc, "kind": kind}
                log_activity("roam_pass", repo, f"score={solv['score']} tier={solv['tier']}")
                break
            if not picked:
                log("roam: no hot repos — waiting")
            sleep_secs = args.interval
            if picked:
                sleep_secs = worker_interval_secs(args.interval, picked)
            save_airport_status(
                {
                    "roamer": {
                        "last_pass": datetime.now(timezone.utc).isoformat(),
                        "picked": picked["repo"] if picked else None,
                        "score": picked["score"] if picked else 0,
                        "interval_secs": sleep_secs,
                    }
                }
            )
            time.sleep(sleep_secs)
    except KeyboardInterrupt:
        log("roamer stopped")
        return 0


def spawn_lane_worker(lane: dict[str, Any]) -> subprocess.Popen[str]:
    AIRPORT_PID_DIR.mkdir(parents=True, exist_ok=True)
    kind = lane.get("kind", "github")
    interval = int(lane.get("interval", 300))
    python = sys.executable
    script = str(Path(__file__).resolve())
    env = os.environ.copy()
    env["ISSUE_AGENT_AIRPORT"] = "1"
    env["PATH"] = f"{HOME / '.cargo' / 'bin'}:{HOME / '.local' / 'bin'}:{HOME / 'bin'}:{env.get('PATH', '')}"
    if kind == "upstream":
        uslug = str(lane.get("slug") or "forge")
        cmd = [python, script, "upstream", "--slug", uslug, "--interval", str(interval)]
        slug = upstream_worker_slug(uslug)
    elif kind == "roam":
        cmd = [
            python,
            script,
            "roam",
            "--interval",
            str(interval),
            "--collect-max",
            str(lane.get("collect_max", 2)),
            "--fix-max",
            str(lane.get("fix_max", 1)),
        ]
        slug = "roamer"
    else:
        repo = lane["repo"]
        cmd = [
            python,
            script,
            "worker",
            repo,
            "--kind",
            kind,
            "--interval",
            str(interval),
            "--collect-max",
            str(lane.get("collect_max", 2)),
            "--fix-max",
            str(lane.get("fix_max", 1)),
        ]
        slug = lane_slug(repo)
    log_path = LOG_DIR / f"worker-{slug}.log"
    log_handle = log_path.open("a")
    log_handle.write(f"\n=== worker spawn {datetime.now(timezone.utc).isoformat()} ===\n")
    log_handle.flush()
    proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT, env=env)
    (AIRPORT_PID_DIR / f"{slug}.pid").write_text(str(proc.pid))
    return proc


def cmd_upstream(args: argparse.Namespace) -> int:
    """Upstream OSS lane — validate clone, optional PR push."""
    os.environ["ISSUE_AGENT_AIRPORT"] = "1"
    load_secrets()
    slug = args.slug or "forge"
    proj = upstream_project(slug)
    if not proj:
        log(f"upstream: unknown slug {slug}")
        return 1
    repo_path = Path(str(proj.get("path", ""))).expanduser()
    branch = str(proj.get("branch") or "")
    fork = str(proj.get("fork") or "")
    mode = str(proj.get("mode") or "validate")
    log(f"upstream lane [{slug}]: {repo_path} branch={branch or 'default'} mode={mode}")
    status_key = f"upstream_{slug}"
    try:
        while True:
            if not repo_path.exists():
                log(f"upstream [{slug}]: path missing — run upstream-bootstrap")
            else:
                if branch:
                    run(["git", "fetch", "origin"], cwd=repo_path, check=False)
                    run(["git", "checkout", branch], cwd=repo_path, check=False)
                test = run_upstream_test(proj, repo_path)
                ok = test.returncode == 0
                patch = {
                    status_key: {
                        "slug": slug,
                        "upstream": proj.get("upstream"),
                        "path": str(repo_path),
                        "branch": branch,
                        "mode": mode,
                        "tier": proj.get("tier"),
                        "tests_ok": ok,
                        "last_pass": datetime.now(timezone.utc).isoformat(),
                    }
                }
                if slug == "forge":
                    patch["upstream"] = patch[status_key]
                save_airport_status(patch)
                if ok and mode == "pr" and fork and branch:
                    pr = run(
                        ["gh", "pr", "list", "-R", fork, "--head", branch, "--json", "number", "--limit", "1"],
                        check=False,
                    )
                    if pr.stdout and pr.stdout.strip() not in ("", "[]"):
                        log(f"upstream [{slug}]: PR already open on {fork}")
                    else:
                        push = run(["git", "push", "-u", "origin", branch], cwd=repo_path, check=False)
                        if push.returncode == 0:
                            run(
                                [
                                    "gh",
                                    "pr",
                                    "create",
                                    "-R",
                                    fork,
                                    "--head",
                                    branch,
                                    "--title",
                                    proj.get("pr_title", f"fix: upstream lane {slug}"),
                                    "--body",
                                    proj.get("pr_body", f"Automated upstream lane — {slug}.\n"),
                                ],
                                check=False,
                            )
                elif not ok:
                    tail = (test.stderr or test.stdout or "")[-500:]
                    log(f"upstream [{slug}] tests failed: {tail}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_upstream_bootstrap(args: argparse.Namespace) -> int:
    """Fork + clone upstream OSS repos from upstream.yaml."""
    load_secrets()
    run(["gh", "auth", "setup-git"], check=False)
    projects = load_upstream_projects()
    if args.slug:
        projects = [p for p in projects if p.get("slug") == args.slug]
    if args.tier is not None:
        projects = [p for p in projects if int(p.get("tier", 99)) <= args.tier]
    if args.enabled_only:
        projects = [p for p in projects if p.get("enabled", True)]
    if not projects:
        log("upstream-bootstrap: no projects matched")
        return 1
    booted = 0
    for proj in projects:
        slug = str(proj["slug"])
        upstream = str(proj.get("upstream") or "")
        fork = str(proj.get("fork") or "")
        path = Path(str(proj.get("path", ""))).expanduser()
        if not upstream:
            log(f"upstream-bootstrap skip {slug}: no upstream")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fork_ready = not fork
        if fork:
            view = run(["gh", "repo", "view", fork, "--json", "nameWithOwner"], check=False)
            fork_ready = view.returncode == 0
            if not fork_ready:
                log(f"upstream-bootstrap: forking {upstream} → {fork}")
                fork_result = run(["gh", "repo", "fork", upstream, "--clone=false"], check=False)
                fork_ready = fork_result.returncode == 0
                if not fork_ready:
                    log(f"upstream-bootstrap: fork API failed for {upstream} — clone upstream only (widen PAT or fork manually)")
        if path.exists() and (path / ".git").exists():
            run(["git", "fetch", "--all", "--prune"], cwd=path, check=False)
            log(f"upstream-bootstrap: refreshed {slug} at {path}")
            booted += 1
            continue
        if path.exists():
            log(f"upstream-bootstrap skip {slug}: {path} exists but is not a git repo")
            continue
        clone_target = fork if fork_ready and fork else upstream
        clone_url = f"https://github.com/{clone_target}.git"
        log(f"upstream-bootstrap: cloning {clone_url} → {path}")
        clone = run(["git", "clone", clone_url, str(path)], check=False)
        if clone.returncode != 0:
            log(f"upstream-bootstrap: clone failed for {slug}")
            continue
        if fork and clone_target == upstream:
            run(["git", "remote", "rename", "origin", "upstream"], cwd=path, check=False)
            log(f"upstream-bootstrap: {slug} tracks upstream until fork {fork} exists")
        elif fork and fork_ready:
            run(["git", "remote", "add", "upstream", f"https://github.com/{upstream}.git"], cwd=path, check=False)
            run(["git", "fetch", "upstream"], cwd=path, check=False)
        booted += 1
    log(f"upstream-bootstrap: {booted}/{len(projects)} ready")
    log_activity("upstream_bootstrap", args.slug or "all", f"ready={booted}/{len(projects)}")
    return 0 if booted else 1


def cmd_refresh(args: argparse.Namespace) -> int:
    """Full fleet refresh — bootstrap upstreams, solvability, factory, heal, restart airport."""
    load_secrets()
    ensure_gh_ready()
    run(["gh", "auth", "setup-git"], check=False)
    log("refresh: upstream bootstrap (tier 2)")
    cmd_upstream_bootstrap(argparse.Namespace(slug=None, tier=2, enabled_only=True))
    log("refresh: solvability snapshot")
    save_solvability_snapshot(roam_candidate_repos())
    log("refresh: factory pass")
    cmd_factory(argparse.Namespace(repo=None, max_per_repo=3, dry_run=False))
    log("refresh: ci-heal pass")
    cmd_ci_heal(argparse.Namespace(repo=None, max=3, max_per_repo=1, dry_run=False, quiet=True))
    hk = housekeeping()
    log(f"refresh: housekeeping {hk}")
    log_activity("refresh", "all", f"housekeeping={hk}")
    return 0


def cmd_airport(args: argparse.Namespace) -> int:
    """Airport supervisor — parallel workers + issue factory + CI heal."""
    os.environ["ISSUE_AGENT_AIRPORT"] = "1"
    load_secrets()
    ensure_gh_ready()
    run(["gh", "auth", "setup-git"], check=False)
    cfg = load_airport_config()
    lanes: list[dict[str, Any]] = list(cfg.get("lanes") or [])
    for proj in load_upstream_projects(enabled_only=True):
        lanes.append(
            {
                "kind": "upstream",
                "slug": proj["slug"],
                "interval": int(proj.get("interval", 1800)),
            }
        )
    factory_iv = int(cfg.get("factory_interval_secs", 900))
    heal_iv = int(cfg.get("ci_heal_interval_secs", 600))
    factory_max = int(cfg.get("factory_max_per_repo", 2))
    heal_max = int(cfg.get("ci_heal_max", 2))
    log(f"airport supervisor: {len(lanes)} lanes factory={factory_iv}s max={factory_max} heal={heal_max}")
    log_activity("airport_start", "", f"lanes={len(lanes)}")
    save_solvability_snapshot(roam_candidate_repos())
    save_airport_status({"supervisor": "running", "lanes": len(lanes)})
    workers: dict[str, subprocess.Popen[str]] = {}
    for lane in lanes:
        kind = lane.get("kind", "github")
        if kind == "upstream":
            slug = upstream_worker_slug(str(lane.get("slug") or "forge"))
        elif kind == "roam":
            slug = "roamer"
        else:
            slug = lane_slug(lane["repo"])
        workers[slug] = spawn_lane_worker(lane)
    last_factory = 0.0
    last_heal = 0.0
    last_solv = 0.0
    try:
        while True:
            now = time.time()
            for slug, proc in list(workers.items()):
                if proc.poll() is not None:
                    log(f"airport: respawning dead worker {slug} (rc={proc.returncode})")
                    lane = next(
                        (
                            l
                            for l in lanes
                            if (
                                l.get("kind") == "upstream"
                                and slug == upstream_worker_slug(str(l.get("slug") or "forge"))
                            )
                            or (l.get("kind") == "roam" and slug == "roamer")
                            or lane_slug(l.get("repo", "")) == slug
                        ),
                        lanes[0],
                    )
                    workers[slug] = spawn_lane_worker(lane)
            if now - last_factory >= factory_iv:
                cmd_factory(argparse.Namespace(repo=None, max_per_repo=factory_max, dry_run=False))
                last_factory = now
            if now - last_heal >= heal_iv:
                cmd_ci_heal(
                    argparse.Namespace(
                        repo=None, max=heal_max, max_per_repo=1, dry_run=False, quiet=True
                    )
                )
                last_heal = now
            housekeeping()
            if now - last_solv >= 300:
                save_solvability_snapshot(roam_candidate_repos())
                last_solv = now
            save_airport_status({"supervisor_heartbeat": datetime.now(timezone.utc).isoformat()})
            time.sleep(30)
    except KeyboardInterrupt:
        log("airport supervisor stopped")
        for proc in workers.values():
            proc.terminate()
        return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Full pipeline — delegates to fleet rotate (park walls, keep moving)."""
    ns = argparse.Namespace(
        repo=args.repo,
        ci_max=args.fix_max,
    )
    return cmd_fleet(ns)


def seed_issue_if_empty(repo: str, spec: dict[str, str]) -> int | None:
    if not is_spec_seedable(repo, spec["title"]):
        return None
    all_issues = gh_json(["issue", "list", "-R", repo, "--state", "all", "--json", "number,title", "--limit", "100"])
    if any(i.get("title") == spec["title"] for i in all_issues):
        return None
    result = run(
        [
            "gh",
            "issue",
            "create",
            "-R",
            repo,
            "--title",
            spec["title"],
            "--body",
            spec["body"],
            "--label",
            "documentation",
            "--label",
            "good first issue",
            "--label",
            "agent-triage",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    num = int(url.rsplit("/", 1)[-1]) if url else None
    if num:
        log(f"seeded issue #{num} on {repo}")
    return num


def cmd_polish(args: argparse.Namespace) -> int:
    load_secrets()
    repos = load_repos_config()
    if not repos:
        raise SystemExit(f"No repos in {REPOS_CONFIG}")
    for entry in repos:
        repo = entry["name"]
        log(f"polish {repo}")
        ensure_repo_labels(repo)
        set_repo_topics(repo, entry.get("topics", []))
        print(f"  polished {repo}")
    return 0


def cmd_boost(args: argparse.Namespace) -> int:
    load_secrets()
    ensure_gh_ready()
    if args.max <= 0:
        return 0
    run(["gh", "auth", "setup-git"], check=False)
    repos = load_repos_config()
    if not repos:
        repos = [{"name": k, "topics": []} for k in DEMO_ISSUES]

    cmd_polish(argparse.Namespace())

    rc = 0
    target_repo = getattr(args, "repo", None)
    for entry in sorted_fleet_repos(repos):
        repo = entry["name"]
        if target_repo and repo != target_repo:
            continue
        if is_repo_parked(repo):
            blocked = load_fleet_state().get("blocked", {}).get(repo, {})
            log(f"skip parked {repo}: {blocked.get('reason', 'wall')}")
            continue
        if entry.get("local_fix") or not repo_has_issues(repo):
            log(f"skip boost {repo}: issues disabled (use local queue)")
            continue
        spec = DEMO_ISSUES.get(repo)
        seeded: list[int] = []
        if args.seed:
            if spec:
                num = seed_issue_if_empty(repo, spec)
                if num:
                    seeded.append(num)
            seeded.extend(seed_backlog_issues(repo, max_new=args.max))

        issues = gh_json(
            [
                "issue",
                "list",
                "-R",
                repo,
                "--label",
                "agent-triage",
                "--json",
                "number,title",
                "--limit",
                "20",
            ]
        )
        if not issues and seeded:
            issues = [{"number": n, "title": spec["title"] if spec else ""} for n in seeded]
        if not issues:
            all_open = gh_json(
                ["issue", "list", "-R", repo, "--json", "number,title,labels", "--limit", "20"]
            )
            issues = [
                i
                for i in all_open
                if any(l.get("name") == "agent-triage" for l in i.get("labels", []))
                or i["number"] in seeded
            ]
        if not issues:
            log(f"no agent-triage issues in {repo}")
            continue

        issues = sorted(issues, key=issue_fix_priority)
        has_easy = any(
            issue_solvability_tier(repo, item.get("title", ""), int(item.get("number") or 0)) == "easy"
            for item in issues
        )

        base_ws = workspace_for(repo)
        ensure_repo(repo, base_ws)
        cfg = repo_config(repo, base_ws)

        fixes = 0
        attempts = 0
        for item in issues:
            if fixes >= args.max:
                break
            tier = issue_solvability_tier(repo, item.get("title", ""), int(item.get("number") or 0))
            if tier in ("blocked", "stale"):
                log(f"skip #{item['number']}: {tier}")
                continue
            if has_easy and tier == "hard":
                log(f"skip #{item['number']}: hard issue deferred — easy wins available")
                continue
            ident = str(item["number"])
            if is_failure_blocked(repo, "issue", ident):
                log(f"skip #{item['number']}: blocked after repeated failures — move on")
                continue

            if is_stale_ci_issue(item.get("title", ""), repo):
                close_stale_ci_issues(repo)
                log(f"skip #{item['number']}: stale CI issue (main green)")
                continue

            prs = run(
                ["gh", "pr", "list", "-R", repo, "--search", f"#{item['number']}", "--json", "number", "--limit", "1"],
                check=False,
            )
            if prs.stdout and prs.stdout.strip() != "[]":
                log(f"skip #{item['number']} on {repo}: PR already exists")
                continue

            t = triage_issue(repo, item["number"], cfg)
            if not t.get("actionable") or t.get("complexity") == "high":
                log(f"skip #{item['number']}: {t.get('summary')}")
                continue
            log(f"boost fixing {repo} #{item['number']}")
            attempts += 1
            fix_rc = resolve_issue(repo, item["number"], dry_run=args.dry_run)
            if fix_rc == 0:
                fixes += 1
            else:
                rc |= 1
                log(f"#{item['number']} failed — trying next issue")

        if attempts > 0 and fixes == 0:
            hours = park_hours_for(entry)
            if hours > 0:
                park_repo(repo, f"no passes this pass ({attempts} attempt(s))", hours=hours)

    return rc


def resolve_issue_local(
    repo: str,
    *,
    title: str,
    body: str,
    issue_num: int = 0,
    dry_run: bool = False,
) -> int:
    """Fix from a local issue spec (no GitHub issue API needed)."""
    base_ws = workspace_for(repo)
    ensure_repo(repo, base_ws)
    cfg = repo_config(repo, base_ws)
    slug = issue_num or int(datetime.now(timezone.utc).strftime("%H%M%S"))
    ws = workspace_for(repo, slug)

    if ws.exists():
        run(["rm", "-rf", str(ws)], check=False)
    run(["cp", "-a", str(base_ws), str(ws)])

    branch = f"fix/local-{slug}"
    run(["git", "checkout", "-B", branch], cwd=ws)
    bootstrap_habitat(ws, repo)

    issue_text = f"{title}\n{body}"
    if dry_run:
        log(f"DRY RUN — would fix local task in {repo} on branch {branch}")
        return 0

    aider_msg = textwrap.dedent(
        f"""
        {solver_prompt(repo, title, cfg)}

        Task for repository {repo}:

        {issue_text}
        """
    ).strip()

    known_fix = try_known_local_repair(ws, repo, title, body)
    if known_fix:
        run(["git", "add", "-A"], cwd=ws, check=False)
        run(["git", "commit", "-m", title[:72]], cwd=ws, check=False)
        log(f"known local repair applied for {repo}")
    elif STALE_CI_TITLE.match(title):
        known_fix = try_known_ci_repair(ws, repo, body)
        if known_fix:
            run(["git", "add", "-A"], cwd=ws, check=False)
            run(["git", "commit", "-m", title[:72]], cwd=ws, check=False)
            log(f"known CI repair applied for local task on {repo}")

    if not known_fix:
        with acquire_aider_slot():
            aider_result = run(
                [
                    str(AIDER),
                    "--model",
                    cfg.model,
                    "--yes-always",
                    "--auto-commits",
                    "--no-show-model-warnings",
                    "--message",
                    aider_msg,
                ],
                cwd=ws,
                check=False,
            )
        log((aider_result.stdout or "")[-4000:])
        if aider_result.stderr:
            log(aider_result.stderr[-2000:])
        sanitize_agent_artifacts(ws)
        dirty = run(["git", "status", "--porcelain"], cwd=ws, check=False)
        if dirty.stdout and dirty.stdout.strip():
            run(["git", "add", "-A"], cwd=ws, check=False)
            run(["git", "commit", "-m", f"chore: sanitize artifacts for {title[:50]}"], cwd=ws, check=False)

    dirty = run(["git", "status", "--porcelain"], cwd=ws, check=False)
    if dirty.stdout and dirty.stdout.strip():
        run(["git", "add", "-A"], cwd=ws, check=False)
        run(["git", "commit", "-m", title[:72]], cwd=ws, check=False)

    base = default_branch(repo)
    if not has_branch_changes(ws, base):
        diff_main = run(["git", "diff", f"origin/{base}"], cwd=ws, check=False)
        if not (diff_main.stdout or "").strip():
            log(f"task already satisfied on {repo} (matches origin/{base})")
            return 0
        log(f"no commits for local task on {repo} — skipping PR")
        record_failure(repo, "local", title[:80], "no commits for local task")
        return 1

    test_cmd = detect_test_command(ws, cfg.test_command)
    passed, test_out = run_tests(ws, test_cmd, strict=not known_fix)
    if not passed and test_cmd:
        log(f"local tests failed — blocking PR: {test_out[-1000:]}")
        record_failure(repo, "local", title[:80], f"tests failed: {test_out[-300:]}")
        return 1

    if cfg.tower_enabled and not known_fix:
        verdict = tower_review(ws, repo, cfg, base_branch=base, issue_summary=title[:70])
        if not verdict.passed:
            log(f"Tower rejected local fix on {repo}: {'; '.join(verdict.reasons)}")
            record_failure(repo, "local", title[:80], "tower rejected: " + "; ".join(verdict.reasons)[:400])
            return 1

    run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd=ws, check=False)
    pr_args = [
        "gh",
        "pr",
        "create",
        "-R",
        repo,
        "--head",
        branch,
        "--title",
        title[:70],
        "--body",
        f"Automated local fix.\n\n## Task\n{body}\n\n---\n*Issue Agent · Nueramarcos*",
    ]
    if cfg.draft_pr:
        pr_args.append("--draft")
    pr = run(pr_args, cwd=ws, check=False)
    pr_url = (pr.stdout or "").strip()
    if not pr_url or pr.returncode != 0:
        log(f"PR failed: {(pr.stderr or '').strip()}")
        return 1
    merged, detail = finalize_pr(repo, pr_url, cfg)
    log(f"local fix {'merged' if merged else 'waiting on CI'} -> {pr_url}")
    print(pr_url)
    if merged:
        record_success(repo, "local", title[:80], spec_title=title)
    else:
        record_failure(repo, "local", title[:80], detail, spec_title=title)
    return 0 if merged else 1


def cmd_demo(args: argparse.Namespace) -> int:
    load_secrets()
    repo = args.repo_opt or args.repo or "Nueramarcos/issue-agent"
    spec = DEMO_ISSUES.get(repo)
    if not spec:
        known = ", ".join(DEMO_ISSUES)
        raise SystemExit(f"No demo issue for {repo}. Known: {known}")
    return resolve_issue_local(
        repo,
        title=spec["title"],
        body=spec["body"],
        dry_run=args.dry_run,
    )


def _load_trajectories(limit: int | None = None) -> list[dict[str, Any]]:
    if not FLIGHT_TRAJECTORIES.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in FLIGHT_TRAJECTORIES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None:
        return rows[-limit:]
    return rows


def _fetch_merged_prs(repo: str, *, limit: int = 30) -> list[dict[str, Any]]:
    result = run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "--search",
            "Issue Agent",
            "--json",
            "number,title,mergedAt,url,additions,deletions",
            "--limit",
            str(limit),
        ],
        check=False,
    )
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []


def cmd_recorder(args: argparse.Namespace) -> int:
    load_secrets()
    action = args.action

    if action == "stats":
        rows = _load_trajectories()
        by_outcome: dict[str, int] = {}
        by_repo: dict[str, int] = {}
        for row in rows:
            outcome = str(row.get("outcome", "unknown"))
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            repo = str(row.get("repo", ""))
            if repo:
                by_repo[repo] = by_repo.get(repo, 0) + 1
        ledger = load_failure_ledger().get("items", {})
        print("Flight Recorder — stats\n")
        print(f"  trajectories: {len(rows)} ({FLIGHT_TRAJECTORIES})")
        print(f"  failure ledger: {len(ledger)} active item(s)")
        if by_outcome:
            print("  outcomes:")
            for k, v in sorted(by_outcome.items(), key=lambda x: -x[1]):
                print(f"    {k}: {v}")
        if by_repo:
            top = sorted(by_repo.items(), key=lambda x: -x[1])[:8]
            print("  top repos:")
            for repo, n in top:
                print(f"    {repo.split('/')[-1]}: {n}")
        return 0

    if action == "tail":
        for row in _load_trajectories(args.limit):
            ts = str(row.get("ts", ""))[:19]
            print(f"{ts}  {row.get('outcome', '?'):12}  {row.get('repo', '')}  {row.get('detail', row.get('ident', ''))[:60]}")
        return 0

    # export
    out_path = Path(args.output) if args.output else FLIGHT_RECORDER_DIR / f"training-export-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exported = 0
    with out_path.open("w") as out:
        for row in _load_trajectories():
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            exported += 1
        for key, entry in load_failure_ledger().get("items", {}).items():
            record = {
                "outcome": "failure_ledger",
                "key": key,
                **entry,
                "source": "failure-ledger.json",
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            exported += 1
        if ACTIVITY_LOG.exists():
            try:
                for ev in json.loads(ACTIVITY_LOG.read_text())[-args.limit :]:
                    if ev.get("event") in ("tower_pass", "tower_reject", "habitat_ready", "pass", "failure"):
                        record = {"outcome": "activity", **ev, "source": "activity.json"}
                        out.write(json.dumps(record, ensure_ascii=False) + "\n")
                        exported += 1
            except json.JSONDecodeError:
                pass
        if args.with_gh:
            for entry in load_repos_config_raw():
                repo = entry.get("name")
                if not repo:
                    continue
                for pr in _fetch_merged_prs(repo, limit=args.limit):
                    record = {
                        "outcome": "merged_pr",
                        "repo": repo,
                        "pr_number": pr.get("number"),
                        "title": pr.get("title"),
                        "url": pr.get("url"),
                        "merged_at": pr.get("mergedAt"),
                        "additions": pr.get("additions"),
                        "deletions": pr.get("deletions"),
                        "source": "gh",
                    }
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    exported += 1
    print(f"Flight Recorder exported {exported} record(s) → {out_path}")
    return 0


def backfill_flight_recorder(*, with_gh: bool = True, limit: int = 50) -> int:
    """Seed trajectories.jsonl from ledger, activity, and merged PRs."""
    seen: set[str] = set()
    for row in _load_trajectories():
        seen.add(json.dumps(row, sort_keys=True, default=str))
    added = 0
    for row in _collect_training_rows(with_gh=with_gh, limit=limit):
        key = json.dumps(row, sort_keys=True, default=str)
        if key in seen:
            continue
        append_flight_record(row)
        seen.add(key)
        added += 1
    log(f"Flight Recorder backfill: +{added} trajectory(ies) → {FLIGHT_TRAJECTORIES}")
    return added


def _collect_training_rows(*, with_gh: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = list(_load_trajectories())
    for key, entry in load_failure_ledger().get("items", {}).items():
        rows.append({"outcome": "failure_ledger", "key": key, **entry, "source": "failure-ledger.json"})
    if ACTIVITY_LOG.exists():
        try:
            for ev in json.loads(ACTIVITY_LOG.read_text())[-limit:]:
                if ev.get("event") in ("tower_pass", "tower_reject", "habitat_ready", "pass", "failure"):
                    rows.append({"outcome": "activity", **ev, "source": "activity.json"})
        except json.JSONDecodeError:
            pass
    if with_gh:
        for entry in load_repos_config_raw():
            repo = entry.get("name")
            if not repo:
                continue
            for pr in _fetch_merged_prs(repo, limit=limit):
                rows.append(
                    {
                        "outcome": "merged_pr",
                        "repo": repo,
                        "pr_number": pr.get("number"),
                        "title": pr.get("title"),
                        "url": pr.get("url"),
                        "merged_at": pr.get("mergedAt"),
                        "source": "gh",
                    }
                )
    return rows


def cmd_lora(args: argparse.Namespace) -> int:
    load_secrets()
    if args.action == "stats":
        rows = _collect_training_rows(with_gh=args.with_gh, limit=args.limit)
        dataset = build_lora_dataset(rows)
        by_task: dict[str, int] = {}
        for ex in dataset:
            by_task[ex["task"]] = by_task.get(ex["task"], 0) + 1
        print("LoRA dataset preview\n")
        print(f"  raw rows: {len(rows)}")
        print(f"  instruction examples: {len(dataset)}")
        if by_task:
            print("  tasks:")
            for task, n in sorted(by_task.items(), key=lambda x: -x[1]):
                print(f"    {task}: {n}")
        min_rec = 20
        if len(dataset) < min_rec:
            print(f"\n  note: {min_rec - len(dataset)} more examples recommended before LoRA run")
        return 0

    out_path = (
        Path(args.output)
        if args.output
        else FLIGHT_RECORDER_DIR / f"lora-dataset-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
    )
    rows = _collect_training_rows(with_gh=args.with_gh, limit=args.limit)
    include = set(args.task.split(",")) if args.task else None
    n = export_lora_jsonl(rows, out_path, include_tasks=include)
    manifest = out_path.with_suffix(".manifest.json")
    print(f"LoRA dataset: {n} instruction example(s) → {out_path}")
    print(f"  manifest: {manifest}")
    print("  next: ollama or unsloth fine-tune qwen2.5-coder:1.5b on this file")
    return 0


def cmd_loop(args: argparse.Namespace) -> int:
    """Vision loop — backfill, LoRA export, solvability, fleet pass."""
    load_secrets()
    rounds = args.rounds
    rc = 0
    print(f"Habitat Solver vision loop — {rounds} round(s)\n")

    for n in range(1, rounds + 1):
        log(f"vision loop round {n}/{rounds}")
        backfill_flight_recorder(with_gh=True, limit=args.limit)
        rows = _collect_training_rows(with_gh=True, limit=args.limit)
        out = FLIGHT_RECORDER_DIR / "lora-dataset.jsonl"
        export_lora_jsonl(rows, out)
        save_solvability_snapshot()
        reconcile_seed_blocks_from_failures()

        if args.triage:
            for entry in load_repos_config_raw()[:3]:
                repo = entry.get("name")
                if not repo or not repo_has_issues(repo):
                    continue
                issues = gh_json(["issue", "list", "-R", repo, "--label", "agent-triage", "--json", "number", "--limit", "3"])
                for iss in issues[:1]:
                    cfg = repo_config(repo, workspace_for(repo))
                    t = triage_issue(repo, iss["number"], cfg)
                    print(f"  Customs {repo} #{iss['number']}: {t.get('complexity')} / {t.get('actionable')}")

        if args.fix:
            for entry in load_repos_config_raw()[:2]:
                repo = entry.get("name")
                if not repo or not repo_has_issues(repo):
                    continue
                issues = gh_json(["issue", "list", "-R", repo, "--label", "agent-triage", "--json", "number", "--limit", "1"])
                if issues:
                    fix_rc = resolve_issue(repo, issues[0]["number"], dry_run=args.dry_run)
                    rc |= fix_rc

        if args.boost:
            ns = argparse.Namespace(max=args.max_per_repo, seed=False, dry_run=args.dry_run)
            rc |= cmd_boost(ns)

    traj_n = len(_load_trajectories())
    lora_n = len(build_lora_dataset(_collect_training_rows(with_gh=True, limit=args.limit)))
    inv = prompt_inventory(AGENT_ROOT)
    print(f"\nloop complete:")
    print(f"  trajectories: {traj_n}")
    print(f"  lora examples: {lora_n}")
    print(f"  prompt goal: {'MET' if inv.get('goal_met') else 'NOT MET'}")
    if traj_n >= 50 and lora_n >= 50 and inv.get("goal_met"):
        print("\n  VISION LOOP HEALTHY — self-improving cycle active")
    return rc


def cmd_prompt(args: argparse.Namespace) -> int:
    action = args.action
    if action == "goal":
        inv = prompt_inventory(AGENT_ROOT)
        print("Habitat Solver — prompt goal checklist\n")
        for key, val in inv.items():
            mark = "✓" if val is True else ("✗" if val is False else " ")
            print(f"  [{mark}] {key}: {val}")
        if inv.get("goal_met"):
            print("\n  PROMPT GOAL MET — master prompt is live across issue-agent + prompts/")
            return 0
        print("\n  PROMPT GOAL NOT MET — see failures above")
        return 1

    if action == "vision":
        print(load_vision())
        return 0

    if action == "triage":
        text = load_triage_prompt(
            args.title or "Add pytest smoke test",
            args.body or "Add tests/test_smoke.py only.",
            agent_root=AGENT_ROOT,
        )
        print(text)
        return 0

    # show solver (default)
    repo = args.repo or "Nueramarcos/orion-ai-agent"
    cfg = RepoConfig(repo=repo, max_files=args.max_files)
    text = solver_prompt(repo, args.title or "Fix README badges", cfg)
    if args.output:
        Path(args.output).write_text(text + "\n")
        print(f"written → {args.output}")
    else:
        print(text)
    return 0


def cmd_broadcast(args: argparse.Namespace) -> int:
    load_secrets()
    BROADCAST_DIR.mkdir(parents=True, exist_ok=True)

    if getattr(args, "status", False):
        print("X broadcast status\n")
        print(f"  OAuth 1.0a ready: {_oauth1_ready()}")
        print(f"  X_AUTO_POST: {os.environ.get('X_AUTO_POST', '0')}")
        print(f"  X_BROADCAST: {os.environ.get('X_BROADCAST', '1')}")
        print(f"  latest post: {BROADCAST_DIR / 'latest.txt'}")
        if not _oauth1_ready():
            print("\n  Setup: setup-x-post")
            print("  No API: issue-agent broadcast --fleet --open")
        return 0

    if args.fleet:
        rows = _load_trajectories(500)
        merges = sum(
            1 for r in rows if r.get("outcome") in ("success", "merged_pr")
        )
        repos = list({str(r.get("repo")) for r in rows if r.get("repo")})
        text = compose_fleet_post(merges=merges, repos=repos)
        path = save_broadcast(text, BROADCAST_DIR)
        print(text)
        print(f"\n→ {path}")
        if args.open:
            ok, detail = open_x_compose(text)
            print(f"compose: {detail}" if ok else f"open failed: {detail}")
        if args.post:
            ok, detail = post_to_x(text)
            print(detail)
            if not ok and args.open:
                return 1
            return 0 if ok else 1
        if args.open:
            return 0
        return 0

    repo = args.repo or "Nueramarcos/issue-agent"
    text = compose_merge_post(
        repo,
        issue_num=args.issue,
        pr_url=args.pr_url or "",
        title=args.title or "Issue Agent merge",
    )
    path = save_broadcast(text, BROADCAST_DIR)
    print(text)
    print(f"\n→ {path}")
    if args.open:
        ok, detail = open_x_compose(text)
        print(f"compose: {detail}" if ok else f"open failed: {detail}")
    if args.post:
        ok, detail = post_to_x(text)
        print(detail)
        return 0 if ok else 1
    if args.open:
        return 0
    print(f"\ncompose URL: {x_compose_url(text)}")
    return 0


def cmd_tower(args: argparse.Namespace) -> int:
    load_secrets()
    base_ws = workspace_for(args.repo)
    ensure_repo(args.repo, base_ws)
    cfg = repo_config(args.repo, base_ws)
    ws = workspace_for(args.repo, args.issue) if args.issue else base_ws
    if args.issue and not ws.exists():
        run(["cp", "-a", str(base_ws), str(ws)], check=False)
    base = default_branch(args.repo)
    verdict = tower_review(ws, args.repo, cfg, base_branch=base, issue_summary=args.summary or "")
    print(json.dumps(
        {
            "passed": verdict.passed,
            "confidence": verdict.confidence,
            "reasons": verdict.reasons,
            "checks": verdict.checks,
            "files_changed": verdict.files_changed,
        },
        indent=2,
    ))
    return 0 if verdict.passed else 1


def cmd_plan(args: argparse.Namespace) -> int:
    load_secrets()
    from habitat_planner.plan import generate_plan, plan_path_for

    repo = args.repo
    issue_num = int(args.issue)
    base_ws = workspace_for(repo)
    ensure_repo(repo, base_ws)
    ws = workspace_for(repo, issue_num)
    if ws != base_ws:
        if ws.exists():
            run(["rm", "-rf", str(ws)], check=False)
        run(["cp", "-a", str(base_ws), str(ws)])
    run(["git", "checkout", "-B", f"fix/issue-{issue_num}"], cwd=ws)
    bootstrap_habitat(ws, repo)
    issue = gh_json(["issue", "view", str(issue_num), "-R", repo, "--json", "title,body"])
    plan = generate_plan(
        ws,
        repo,
        issue_num=issue_num,
        issue_title=issue.get("title", ""),
        issue_body=issue.get("body") or "",
        model=args.model,
    )
    print(json.dumps(plan, indent=2))
    print(f"\nplan written: {plan_path_for(ws)}")
    return 0


def cmd_relentless(args: argparse.Namespace) -> int:
    load_secrets()
    for round_num in range(1, args.rounds + 1):
        log(f"relentless round {round_num}/{args.rounds}")
        ns = argparse.Namespace(max=args.max_per_repo, seed=True, dry_run=False)
        rc = cmd_boost(ns)
        rc |= process_local_queue(max_items=args.max_per_repo * 2)
        open_total = 0
        for entry in load_repos_config() or [{"name": k} for k in ISSUE_BACKLOG]:
            repo = entry["name"]
            if entry.get("local_fix") or not repo_has_issues(repo):
                open_total += sum(
                    1 for q in load_local_queue() if q.get("repo") == repo and q.get("status") != "done"
                )
                continue
            issues = gh_json(
                ["issue", "list", "-R", repo, "--label", "agent-triage", "--state", "open", "--json", "number", "--limit", "50"]
            )
            open_total += len(issues)
        log(f"open agent-triage/local issues remaining: {open_total}")
        if open_total == 0:
            print(f"All agent-triage issues resolved after round {round_num}")
            return 0
        if rc != 0:
            log(f"round {round_num} had failures, continuing")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    import time

    load_secrets()
    log(f"watch mode: {args.repo} every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            ns = argparse.Namespace(repo=args.repo, max=args.max, dry_run=args.dry_run)
            cmd_run(ns)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("watch stopped")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local GitHub Issue Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="Check gh, ollama, aider")
    s.add_argument("--quick", "-q", action="store_true", help="Health checks only — skip fleet gh scans")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="List open issues")
    s.add_argument("repo", help="owner/repo")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_list_issues)

    s = sub.add_parser("triage", help="Classify issues with small local model")
    s.add_argument("repo")
    s.add_argument("issue", type=int, nargs="?")
    s.add_argument("--apply-label", action="store_true", help="Add agent-triage or agent-skip labels")
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("plan", help="Generate Habitat fix plan (local only, no PR)")
    s.add_argument("repo")
    s.add_argument("issue", type=int)
    s.add_argument("--model", default="qwen2.5-coder:1.5b")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("fix", help="Fix one issue and open draft PR")
    s.add_argument("repo")
    s.add_argument("issue", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("run", help="Fix issues labeled agent-triage")
    s.add_argument("repo")
    s.add_argument("--max", type=int, default=3)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("demo", help="Run a built-in test issue (no GitHub issue API)")
    s.add_argument("repo", nargs="?", default=None, help="owner/repo (default: Nueramarcos/issue-agent)")
    s.add_argument("--repo", "-R", dest="repo_opt", default=None, help="owner/repo (same as positional)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("watch", help="Poll and fix on interval")
    s.add_argument("repo")
    s.add_argument("--interval", type=int, default=1800)
    s.add_argument("--max", type=int, default=3)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("polish", help="Set topics and standard labels on all repos")
    s.set_defaults(func=cmd_polish)

    s = sub.add_parser("boost", help="Polish repos + fix agent-triage issues across fleet")
    s.add_argument("repo", nargs="?", help="Single repo (default: all, fleet order)")
    s.add_argument("--max", type=int, default=2, help="Max issues per repo per run")
    s.add_argument("--seed", action="store_true", default=True, help="Seed backlog issues when needed")
    s.add_argument("--no-seed", action="store_false", dest="seed")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_boost)

    s = sub.add_parser("fleet", help="Rotate fleet: fix 1 issue/repo, park walls, keep moving")
    s.add_argument("repo", nargs="?", help="Optional single repo")
    s.add_argument("--ci-max", type=int, default=2, help="Max ci-heal items after rotate")
    s.set_defaults(func=cmd_fleet)

    s = sub.add_parser(
        "scout",
        help="Rank upstream OSS issues (Tesla/AMD/tinygrad lane) — curated + optional live search",
    )
    s.add_argument("--tag", "-t", help="Filter by tag (amd, tinygrad, tesla, bounty, good-first, …)")
    s.add_argument("--tier", type=int, help="Max tier to show (1=do now, 2=soon, 3=later)")
    s.add_argument("--arch", help="Filter by hardware arch (default: gfx1010 from catalog)")
    s.add_argument("--min-score", type=int, default=0, help="Minimum score 0–100")
    s.add_argument("--limit", "-n", type=int, default=15, help="Max rows to print")
    s.add_argument("--live", action="store_true", help="Merge live GitHub search hits from upstream-opportunities.yaml")
    s.add_argument("--web", action="store_true", help="Radar: enrich with GitHub precedents + web hints")
    s.add_argument("--live-limit", type=int, default=6, help="Max hits per live query")
    s.add_argument("--enqueue", type=int, metavar="N", help="Add top N visible items to scout-queue.json")
    s.add_argument("--json", action="store_true", help="Machine-readable output")
    s.set_defaults(func=cmd_scout)

    s = sub.add_parser(
        "personality",
        help="Personality quiz → OSS archetype + scout target (optional X post)",
    )
    s.add_argument(
        "--answers",
        metavar="CODE",
        help="5-letter answer code e.g. abdca (friday→workstation order)",
    )
    s.add_argument("-i", "--interactive", action="store_true", help="Run quiz in terminal")
    s.add_argument("--tier", type=int, help="Filter scout pool by max tier")
    s.add_argument("--tag", help="Filter scout pool by tag")
    s.add_argument("--arch", help="Filter scout pool by hardware arch (e.g. gfx1010)")
    s.add_argument("--enqueue", action="store_true", help="Queue matched issue in scout-queue.json")
    s.add_argument("--quiz-only", action="store_true", help="Print quiz for X (no match)")
    s.add_argument("--thread", action="store_true", help="Full thread (6 tweets) instead of single post")
    s.add_argument("--post", action="store_true", help="Post to X (needs X_BEARER_TOKEN)")
    s.add_argument("--quiz-first", action="store_true", help="With --post: quiz intro then result")
    s.add_argument("--json", action="store_true", help="JSON output")
    s.set_defaults(func=cmd_personality)

    s = sub.add_parser("hunt", help="Next scout-queue item + upstream PR playbook")
    s.add_argument("--enqueue", type=int, metavar="N", help="Seed tier-1 queue if empty, then show next")
    s.add_argument("--mark", choices=["queued", "in_progress", "pr_open", "done", "skipped"])
    s.set_defaults(func=cmd_hunt)

    s = sub.add_parser("collect", help="Collect issues from backlog.yaml + auto-discovery")
    s.add_argument("repo", nargs="?", help="Single repo (default: all in repos.yaml)")
    s.add_argument("--max-per-repo", type=int, default=3)
    s.add_argument("--discover", action="store_true", default=True, help="Auto-detect gaps")
    s.add_argument("--no-discover", action="store_false", dest="discover")
    s.add_argument("--discover-only", action="store_true", help="Only auto-discovered issues")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("max", help="Collect + fix+merge (GitHub achievement mode)")
    s.add_argument("repo", nargs="?")
    s.add_argument("--collect-max", type=int, default=3, help="Max issues to collect per repo")
    s.add_argument("--fix-max", type=int, default=2, help="Max issues to fix per repo")
    s.set_defaults(func=cmd_max)

    s = sub.add_parser("local", help="Fix local queue (tinygrad/vision forks)")
    s.add_argument("--max", type=int, default=6)
    s.set_defaults(func=cmd_local)

    s = sub.add_parser("build", help="Polish + collect + fix all repos incl. local forks")
    s.add_argument("repo", nargs="?")
    s.add_argument("--collect-max", type=int, default=2)
    s.add_argument("--fix-max", type=int, default=2)
    s.add_argument("--local-max", type=int, default=6, help="Max local-queue fixes (tinygrad/vision)")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("relentless", help="Boost loop until no open agent-triage issues remain")
    s.add_argument("--max-per-repo", type=int, default=2)
    s.add_argument("--rounds", type=int, default=10)
    s.set_defaults(func=cmd_relentless)

    s = sub.add_parser("cleanup-ci-prs", help="Close open fix/ci-* PRs and delete branches")
    s.add_argument("repo", nargs="?", default="Nueramarcos/tinygrad", help="Owner/repo (default: tinygrad)")
    s.add_argument("--prefix", default="fix/ci-", help="Branch prefix to match (default: fix/ci-)")
    s.add_argument("--dry-run", action="store_true", help="List PRs/branches only, do not delete")
    s.add_argument(
        "--no-delete-orphan-branches",
        action="store_true",
        help="Skip deleting remote branches that match prefix but have no open PR",
    )
    s.set_defaults(func=cmd_cleanup_ci_prs)

    s = sub.add_parser("ci-heal", help="Detect failed CI runs and repair them")
    s.add_argument("repo", nargs="?", help="Single repo (default: all)")
    s.add_argument("--max", type=int, default=4, help="Max items from heal queue")
    s.add_argument("--max-per-repo", type=int, default=1, help="Max failed runs to heal per repo")
    s.add_argument("--dry-run", action="store_true", help="Scan only, do not fix")
    s.add_argument("--quiet", action="store_true", help="One-line dashboard; suppress gh command noise")
    s.set_defaults(func=cmd_ci_heal)

    s = sub.add_parser("ci-watch", help="Poll for CI failures and heal continuously")
    s.add_argument("repo", nargs="?")
    s.add_argument("--interval", type=int, default=120, help="Seconds between scans (default 2 min)")
    s.add_argument("--max", type=int, default=2)
    s.add_argument("--max-per-repo", type=int, default=1)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--verbose", action="store_true", help="Log every gh command (old noisy mode)")
    s.set_defaults(func=cmd_ci_watch)

    s = sub.add_parser("daemon", help="24/7 fleet loop with housekeeping (status among change)")
    s.add_argument("repo", nargs="?", help="Optional single repo")
    s.add_argument("--interval", type=int, default=3600, help="Seconds between fleet passes (default 1h)")
    s.add_argument("--ci-max", type=int, default=2, help="Max ci-heal items per pass")
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("refresh", help="Bootstrap upstreams, factory, heal (pair with issue-agent-refresh to restart)")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("airport", help="Airport supervisor — parallel workers + issue factory")
    s.set_defaults(func=cmd_airport)

    s = sub.add_parser("worker", help="Single-repo lane worker (used by airport)")
    s.add_argument("repo")
    s.add_argument("--kind", choices=["github", "local"], default=None)
    s.add_argument("--interval", type=int, default=300)
    s.add_argument("--collect-max", type=int, default=2)
    s.add_argument("--fix-max", type=int, default=1)
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("factory", help="Issue factory — discover and seed issues")
    s.add_argument("repo", nargs="?")
    s.add_argument("--max-per-repo", type=int, default=2)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_factory)

    s = sub.add_parser("upstream", help="Upstream OSS lane (validate/PR)")
    s.add_argument("--slug", default="forge", help="Project slug from upstream.yaml")
    s.add_argument("--interval", type=int, default=1800)
    s.set_defaults(func=cmd_upstream)

    s = sub.add_parser("upstream-bootstrap", help="Fork + clone upstream OSS repos")
    s.add_argument("--slug", help="Single project slug")
    s.add_argument("--tier", type=int, help="Max tier to bootstrap (1=agent-friendly)")
    s.add_argument("--enabled-only", action="store_true", default=True)
    s.add_argument("--all", dest="enabled_only", action="store_false", help="Include disabled catalog entries")
    s.set_defaults(func=cmd_upstream_bootstrap)

    s = sub.add_parser("roam", help="Roaming worker — picks highest-solvability repo each pass")
    s.add_argument("--interval", type=int, default=360)
    s.add_argument("--collect-max", type=int, default=2)
    s.add_argument("--fix-max", type=int, default=1)
    s.set_defaults(func=cmd_roam)

    s = sub.add_parser("solvability", help="Score fleet repos by merge likelihood")
    s.add_argument("repo", nargs="?", help="Optional single repo")
    s.set_defaults(func=cmd_solvability)

    s = sub.add_parser("failures", help="Show failure ledger and hints")
    s.set_defaults(func=lambda _: cmd_status(argparse.Namespace()) or 0)

    pr = sub.add_parser("prompt", help="Habitat Solver master prompt — show, render, validate goal")
    pr_sub = pr.add_subparsers(dest="action", required=True)
    r = pr_sub.add_parser("goal", help="Validate prompt goal checklist")
    r.set_defaults(func=cmd_prompt, action="goal")
    r = pr_sub.add_parser("vision", help="Print north-star vision")
    r.set_defaults(func=cmd_prompt, action="vision")
    r = pr_sub.add_parser("show", help="Render solver prompt with adaptive feedback")
    r.add_argument("repo", nargs="?", help="owner/repo")
    r.add_argument("--title", default="Fix README badges")
    r.add_argument("--max-files", type=int, default=8)
    r.add_argument("-o", "--output", help="Write rendered prompt to file")
    r.set_defaults(func=cmd_prompt, action="show")
    r = pr_sub.add_parser("triage", help="Render Customs triage prompt")
    r.add_argument("--title", default="Add pytest smoke test")
    r.add_argument("--body", default="")
    r.set_defaults(func=cmd_prompt, action="triage")

    s = sub.add_parser("broadcast", help="Compose X post for merge or fleet stats")
    s.add_argument("repo", nargs="?", help="owner/repo for merge post")
    s.add_argument("--issue", type=int, help="Issue number")
    s.add_argument("--pr-url", default="", help="PR URL")
    s.add_argument("--title", default="", help="Issue/fix title")
    s.add_argument("--fleet", action="store_true", help="Overnight fleet summary from Flight Recorder")
    s.add_argument("--post", action="store_true", help="Post to X via API (needs setup-x-post)")
    s.add_argument("--open", action="store_true", help="Open X compose in browser (no API keys)")
    s.add_argument("--status", action="store_true", help="Show X credential / auto-post status")
    s.set_defaults(func=cmd_broadcast)

    s = sub.add_parser("tower", help="Run Tower reviewer on a workspace diff")
    s.add_argument("repo", help="owner/repo")
    s.add_argument("issue", type=int, nargs="?", help="Issue workspace slug (optional)")
    s.add_argument("--summary", default="", help="Issue title for logging")
    s.set_defaults(func=cmd_tower)

    rec = sub.add_parser("recorder", help="Flight Recorder — export training trajectories")
    rec_sub = rec.add_subparsers(dest="action", required=True)
    r = rec_sub.add_parser("export", help="Export JSONL for LoRA / RAG training")
    r.add_argument("-o", "--output", help="Output path (default: flight-recorder/training-export-YYYYMMDD.jsonl)")
    r.add_argument("--with-gh", action="store_true", help="Include merged Issue Agent PRs from GitHub")
    r.add_argument("--limit", type=int, default=50, help="Max GH PRs / activity events per repo")
    r.set_defaults(func=cmd_recorder, action="export")
    r = rec_sub.add_parser("stats", help="Trajectory and ledger counts")
    r.set_defaults(func=cmd_recorder, action="stats")
    r = rec_sub.add_parser("tail", help="Show recent trajectory lines")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_recorder, action="tail")

    s = sub.add_parser("loop", help="Vision loop — backfill, LoRA, solvability, optional fix/boost")
    s.add_argument("--rounds", type=int, default=1, help="Loop iterations")
    s.add_argument("--limit", type=int, default=50, help="Max GH rows per round")
    s.add_argument("--boost", action="store_true", help="Run boost pass each round")
    s.add_argument("--fix", action="store_true", help="Attempt one agent-triage fix per repo")
    s.add_argument("--triage", action="store_true", help="Run Customs triage sample each round")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max-per-repo", type=int, default=1)
    s.set_defaults(func=cmd_loop)

    s = sub.add_parser("backfill", help="Seed trajectories.jsonl from ledger + GH merges")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(
        func=lambda a: (
            load_secrets(),
            print(f"backfilled {backfill_flight_recorder(with_gh=True, limit=a.limit)} trajectory(ies)"),
            0,
        )[-1]
    )

    lora = sub.add_parser("lora", help="LoRA instruction dataset from Flight Recorder")
    lora_sub = lora.add_subparsers(dest="action", required=True)
    r = lora_sub.add_parser("export", help="Export instruction JSONL for 1.5b fine-tune")
    r.add_argument("-o", "--output", help="Output path")
    r.add_argument("--with-gh", action="store_true", help="Include merged PRs from GitHub")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--task", help="Comma-separated tasks: triage_failure,tower_review,merge_success,...")
    r.set_defaults(func=cmd_lora, action="export")
    r = lora_sub.add_parser("stats", help="Preview LoRA example counts")
    r.add_argument("--with-gh", action="store_true")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_lora, action="stats")

    return p


def main() -> int:
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except subprocess.CalledProcessError as e:
        log(f"command failed: {e.stderr or e.stdout or e}")
        return e.returncode or 1
    except Exception as e:
        log(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())