#!/usr/bin/env python3
"""CLI for Human Reviewer corpus + Human Tower."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from human_reviewer.collector import collect_all_deep, collect_curated, collect_repo, load_sources
from human_reviewer.export import export_lora_dataset, stats
from human_reviewer.gate import human_tower_review

AGENT_ROOT = Path(__file__).resolve().parent.parent


def cmd_collect(args: argparse.Namespace) -> int:
    from human_reviewer.collector import append_corpus, collect_pr

    cfg = load_sources()
    hunters = set(str(h) for h in (cfg.get("bounty_hunters") or []))
    if args.pr and args.repo:
        record = collect_pr(args.repo, int(args.pr), bounty_hunters=hunters)
        if not record:
            print(f"failed to collect {args.repo}#{args.pr}")
            return 1
        append_corpus(record)
        print(f"  {args.repo}#{args.pr}: 1 record (verdict={record.get('verdict')}, voice={bool(record.get('maintainer_voice'))})")
        return 0
    repos = [args.repo] if args.repo else list(cfg.get("repos") or [])
    if not repos:
        print("no repos configured — edit human-reviewer/sources.yaml")
        return 1
    cfg = load_sources()
    total = 0
    if getattr(args, "deep", False):
        c = collect_curated(bounty_hunters=hunters)
        print(f"  curated: +{c} record(s)")
        total += c
    for repo in repos:
        n = collect_repo(
            repo,
            limit=args.limit,
            include_closed=args.include_closed,
            bounty_hunters=hunters,
            deep=getattr(args, "deep", False),
            cfg=cfg,
        )
        print(f"  {repo}: +{n} human review record(s)")
        total += n
    print(f"\nCorpus: +{total} total → {AGENT_ROOT / 'flight-recorder' / 'human-reviews.jsonl'}")
    return 0


def cmd_collect_deep(args: argparse.Namespace) -> int:
    cfg = load_sources()
    hunters = set(str(h) for h in (cfg.get("bounty_hunters") or []))
    total = 0
    if args.repo:
        total += collect_curated(bounty_hunters=hunters)
        n = collect_repo(args.repo, limit=args.limit, deep=True, bounty_hunters=hunters, cfg=cfg)
        print(f"  {args.repo}: +{n}")
        total += n
    else:
        print("Archivist deep collect — versatile complex PR discourse\n")
        total += collect_curated(bounty_hunters=hunters)
        print(f"  curated seeds: done")
        for repo in cfg.get("repos") or []:
            n = collect_repo(repo, limit=args.limit, deep=True, bounty_hunters=hunters, cfg=cfg)
            print(f"  {repo}: +{n}")
            total += n
    export_lora_dataset()
    s = stats()
    print(f"\n═══ Deep collect complete: +{total} this run ═══")
    print(f"  corpus: {s['corpus_rows']} rows | LoRA-ready: {s['lora_examples']} | voice: {s['with_maintainer_voice']}")
    print(f"  high complexity: {s.get('high_complexity', 0)}")
    if s.get("by_tag"):
        print("  top tags:", ", ".join(f"{k}({v})" for k, v in list(s["by_tag"].items())[:8]))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    n = export_lora_dataset(out_path=Path(args.output) if args.output else None)
    out = Path(args.output) if args.output else AGENT_ROOT / "flight-recorder" / "human-reviewer-lora.jsonl"
    print(f"Human Reviewer LoRA: {n} example(s) → {out}")
    manifest = out.with_suffix(".manifest.json")
    if manifest.exists():
        print(f"  manifest: {manifest}")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    s = stats()
    print("Human Reviewer corpus\n")
    print(f"  corpus rows:           {s['corpus_rows']}")
    print(f"  LoRA-ready examples:   {s['lora_examples']}")
    print(f"  with maintainer voice: {s['with_maintainer_voice']}")
    print(f"  bounty hunter PRs:     {s['bounty_hunter_prs']}")
    if s["by_repo"]:
        print("  by repo:")
        for repo, n in sorted(s["by_repo"].items(), key=lambda x: -x[1]):
            print(f"    {repo}: {n}")
    if s["by_verdict"]:
        print("  by verdict:")
        for v, n in s["by_verdict"].items():
            print(f"    {v}: {n}")
    if s.get("high_complexity"):
        print(f"  high complexity (40+): {s['high_complexity']}")
    if s.get("by_tag"):
        print("  top complexity tags:")
        for tag, n in list(s["by_tag"].items())[:10]:
            print(f"    {tag}: {n}")
    min_target = 200
    if s["lora_examples"] < min_target:
        print(f"\n  target: {min_target}+ examples before fine-tune (currently {s['lora_examples']})")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    ws = Path(args.workspace).expanduser().resolve()
    if not ws.is_dir():
        print(f"workspace not found: {ws}")
        return 1
    verdict = human_tower_review(
        ws,
        args.repo,
        issue_summary=args.summary,
        model=args.model,
        k=args.k,
    )
    print(json.dumps(
        {
            "passed": verdict.passed,
            "confidence": verdict.confidence,
            "review_comment": verdict.review_comment,
            "reasons": verdict.reasons,
            "similar_prs": verdict.similar_prs,
            "model": verdict.model,
        },
        indent=2,
    ))
    return 0 if verdict.passed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="issue-agent-human-review", description="Human Reviewer corpus + Human Tower")
    sub = p.add_subparsers(dest="action", required=True)

    c = sub.add_parser("collect", help="Harvest merged/closed PRs + review comments from GitHub")
    c.add_argument("--repo", help="Single owner/repo (default: all in sources.yaml)")
    c.add_argument("--pr", type=int, help="Collect one PR by number (requires --repo)")
    c.add_argument("--limit", type=int, default=30, help="Max PRs per repo per state")
    c.add_argument("--include-closed", action="store_true", default=True, help="Include rejected/closed PRs with review text")
    c.add_argument("--deep", action="store_true", help="Paginated REST + search + complexity filter")
    c.set_defaults(func=cmd_collect)

    d = sub.add_parser("collect-deep", help="Full Archivist pass: all repos, curated seeds, rejections")
    d.add_argument("--limit", type=int, default=40, help="Max merged PRs per repo")
    d.add_argument("--repo", help="Single repo only")
    d.set_defaults(func=cmd_collect_deep)

    e = sub.add_parser("export", help="Export LoRA instruction JSONL from corpus")
    e.add_argument("-o", "--output", help="Output path")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="Corpus and training readiness")
    s.set_defaults(func=cmd_stats)

    r = sub.add_parser("review", help="Run Human Tower on a workspace diff")
    r.add_argument("repo", help="owner/repo")
    r.add_argument("workspace", help="Path to git workspace")
    r.add_argument("--summary", default="", help="Issue title")
    r.add_argument("--model", default="customs-reviewer-1.5b")
    r.add_argument("--k", type=int, default=3, help="Similar corpus examples for RAG")
    r.set_defaults(func=cmd_review)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())