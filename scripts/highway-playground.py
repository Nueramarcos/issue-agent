#!/usr/bin/env python3
"""
Solvability Highway — terminal playground.

Run from anywhere (adds issue-agent to sys.path automatically):

  python3 ~/issue-agent/scripts/highway-playground.py files
  python3 ~/issue-agent/scripts/highway-playground.py route --title "Add SECURITY.md"
  python3 ~/issue-agent/scripts/highway-playground.py route --repo Nueramarcos/forge-ci-reliability --title "Add README badges"
  python3 ~/issue-agent/scripts/highway-playground.py admit --repo Nueramarcos/orion-ai-agent --title "Add pytest smoke test"
  python3 ~/issue-agent/scripts/highway-playground.py samples
  python3 ~/issue-agent/scripts/highway-playground.py stats --hours 24
  python3 ~/issue-agent/scripts/highway-playground.py dry-run --lane 0 --title "Add CHANGELOG.md" --dir /tmp/shf-test

Or via issue-agent (same stats/lanes):

  issue-agent highway stats --hours 24
  issue-agent highway bottlenecks
  issue-agent highway heal
  issue-agent highway lanes
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from highway import (  # noqa: E402
    admit_l1,
    admit_seed,
    admit_to_aider,
    apply_lane0,
    apply_lane1,
    format_stats_report,
    highway_stats,
    route_issue,
    spec_highway_lane,
)
from highway.archetype import detect_archetype  # noqa: E402

MODULE_MAP = """
Solvability Highway — source files
──────────────────────────────────
  highway/registry.yaml     archetype → lane 0|1|2 + handler
  highway/archetype.py      detect_archetype(), is_lane0_candidate()
  highway/router.py         route_issue(), apply_lane0(), apply_lane1()
  highway/templates.py      L0 deterministic templates (0 Ollama tokens)
  highway/micro.py            L1 micro-LLM README edits (~500 tokens)
  highway/admission.py      admit_seed(), admit_l1(), admit_to_aider()
  highway/metrics.py        highway_stats(), format_stats_report()
  highway/package_root.py   detect_package_root() for forge/Orion/nexus

Wired into production fix loop:
  issue_agent.py            _try_highway_lane0(), _try_highway_lane1(), cmd_fix

Fleet config:
  repos.yaml                highway: { l0_enabled, l1_enabled, l2_enabled, package_root }
  airport.yaml              worker lanes (demo, forge, orion)
  backlog.yaml              per-issue highway_lane tags

This playground:
  scripts/highway-playground.py
"""


def _load_repo_meta(repo: str) -> dict[str, Any] | None:
    repos_yaml = AGENT_ROOT / "repos.yaml"
    if not repos_yaml.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    data = yaml.safe_load(repos_yaml.read_text()) or {}
    for entry in data.get("repos") or []:
        if entry.get("name") == repo:
            return entry
    return None


def _mock_solv(score: int = 50, tier: str = "warm", no_commits_6h: int = 0) -> dict[str, Any]:
    return {
        "score": score,
        "tier": tier,
        "factors": {"no_commits_6h": no_commits_6h},
    }


def _plan_dict(plan: Any) -> dict[str, Any]:
    return {
        "lane": plan.lane,
        "archetype": plan.archetype,
        "handler": plan.handler,
        "ollama_budget": plan.ollama_budget,
        "skip_reason": plan.skip_reason or None,
    }


def cmd_files(_: argparse.Namespace) -> int:
    print(MODULE_MAP.strip())
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    issue = {"title": args.title, "body": args.body or ""}
    meta = _load_repo_meta(args.repo) if args.repo else None
    plan = route_issue(args.repo or "", issue, meta)
    out = {
        "repo": args.repo,
        "title": args.title,
        "detected_archetype": detect_archetype(args.title, args.body or ""),
        "backlog_lane": spec_highway_lane({"title": args.title, "body": args.body or ""}),
        "plan": _plan_dict(plan),
    }
    if meta and meta.get("highway"):
        out["repo_highway"] = meta["highway"]
    print(json.dumps(out, indent=2))
    return 0


def cmd_admit(args: argparse.Namespace) -> int:
    issue = {"title": args.title, "body": args.body or ""}
    spec = {"title": args.title, "body": args.body or ""}
    meta = _load_repo_meta(args.repo) if args.repo else None
    plan = route_issue(args.repo or "", issue, meta)
    solv = _mock_solv(args.score, args.tier, args.no_commits_6h)

    seed_ok, seed_reason = admit_seed(args.repo or "", spec, meta)
    l1_ok, l1_reason = admit_l1(args.repo or "", issue, plan, meta, solv)
    aider_ok, aider_reason = admit_to_aider(args.repo or "", issue, plan, meta, solv)

    print(json.dumps(
        {
            "plan": _plan_dict(plan),
            "admit_seed": {"ok": seed_ok, "reason": seed_reason},
            "admit_l1": {"ok": l1_ok, "reason": l1_reason},
            "admit_aider": {"ok": aider_ok, "reason": aider_reason},
            "solvability_mock": solv,
        },
        indent=2,
    ))
    return 0


SAMPLES = [
    ("Nueramarcos/forge-ci-reliability", "Add SECURITY.md vulnerability reporting policy", ""),
    ("Nueramarcos/forge-ci-reliability", "Add CHANGELOG.md for initial release", ""),
    ("Nueramarcos/orion-ai-agent", "Add README troubleshooting section", "README.md only"),
    ("Nueramarcos/nexus-vision-engine", "Add SECURITY.md vulnerability reporting policy", ""),
    ("Nueramarcos/vertex-sim-core", "Add CHANGELOG.md for initial release", ""),
    ("Nueramarcos/orion-ai-agent", "Add pytest smoke test for ast_parser", ""),
    ("Nueramarcos/forge-ci-reliability", "Add GitHub Actions CI for forge", ""),
]


def cmd_samples(_: argparse.Namespace) -> int:
    print("Sample routing (repos.yaml highway policy applied):\n")
    for repo, title, body in SAMPLES:
        meta = _load_repo_meta(repo)
        plan = route_issue(repo, {"title": title, "body": body}, meta)
        lane_label = {0: "L0", 1: "L1", 2: "L2", -1: "SKIP"}[plan.lane]
        print(f"  [{lane_label:4s}] {plan.handler:20s}  {repo}")
        print(f"         {title}")
        if plan.skip_reason:
            print(f"         skip: {plan.skip_reason}")
        print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print(format_stats_report(highway_stats(args.hours)))
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Apply L0/L1 handler to a temp or user dir (no git, no PR)."""
    issue = {"title": args.title, "body": args.body or ""}
    meta = _load_repo_meta(args.repo) if args.repo else None
    plan = route_issue(args.repo or "", issue, meta)

    if args.lane is not None and plan.lane != args.lane:
        print(f"Plan is lane {plan.lane}, not {args.lane}. Aborting.", file=sys.stderr)
        print(json.dumps(_plan_dict(plan), indent=2))
        return 1

    work = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="shf-"))
    if args.dir:
        work.mkdir(parents=True, exist_ok=True)
    if not (work / "README.md").exists():
        (work / "README.md").write_text(f"# {args.repo or 'test-repo'}\n\nDemo README for highway dry-run.\n")

    print(f"workspace: {work}")
    print(f"plan: {json.dumps(_plan_dict(plan))}")

    changed = False
    if plan.lane == 0:
        changed = apply_lane0(work, issue, plan, meta)
    elif plan.lane == 1:
        ok, reason = admit_l1(args.repo or "", issue, plan, meta, _mock_solv())
        if not ok:
            print(f"L1 admission denied: {reason}", file=sys.stderr)
            return 1
        changed = apply_lane1(work, issue, plan, meta, repo=args.repo or "")
    else:
        print(f"dry-run only supports L0/L1 (plan lane={plan.lane})", file=sys.stderr)
        return 1

    print(f"changed: {changed}")
    for p in sorted(work.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            rel = p.relative_to(work)
            print(f"\n── {rel} ──")
            print(p.read_text(encoding="utf-8", errors="replace")[:2000])
    if not args.dir and work.exists():
        print(f"\n(temp dir kept: {work})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Solvability Highway terminal playground",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Source map: highway-playground.py files",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("files", help="Print highway module map (all source filenames)")
    s.set_defaults(func=cmd_files)

    s = sub.add_parser("route", help="Route an issue title → lane plan")
    s.add_argument("--repo", default="", help="owner/repo (loads repos.yaml highway block)")
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.set_defaults(func=cmd_route)

    s = sub.add_parser("admit", help="Check seed / L1 / Aider admission gates")
    s.add_argument("--repo", default="Nueramarcos/orion-ai-agent")
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--score", type=int, default=50)
    s.add_argument("--tier", default="warm")
    s.add_argument("--no-commits-6h", type=int, default=0)
    s.set_defaults(func=cmd_admit)

    s = sub.add_parser("samples", help="Route built-in sample issues")
    s.set_defaults(func=cmd_samples)

    s = sub.add_parser("stats", help="Flight Recorder highway stats")
    s.add_argument("--hours", type=int, default=24)
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("dry-run", help="Apply L0/L1 to a directory (no git)")
    s.add_argument("--lane", type=int, choices=[0, 1], help="Require this lane")
    s.add_argument("--repo", default="Nueramarcos/forge-ci-reliability")
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--dir", help="Workspace dir (default: temp)")
    s.set_defaults(func=cmd_dry_run)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())