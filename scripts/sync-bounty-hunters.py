#!/usr/bin/env python3
"""Promote prolific corpus authors into bounty-hunters.yaml."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "flight-recorder" / "human-reviews.jsonl"
OUT = ROOT / "human-reviewer" / "bounty-hunters.yaml"

KEEP_LOGINS = {"geohot", "syb0rg", "dmlc"}


def main() -> int:
    try:
        import yaml  # type: ignore
    except ImportError:
        print("pyyaml required")
        return 1
    if not CORPUS.exists():
        print(f"missing {CORPUS}")
        return 1
    merged_authors: Counter[str] = Counter()
    voice_authors: Counter[str] = Counter()
    for line in CORPUS.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        author = str(row.get("author", ""))
        if not author or author.endswith("[bot]"):
            continue
        if row.get("verdict") == "merged" and row.get("maintainer_voice"):
            merged_authors[author] += 1
        if row.get("maintainer_voice"):
            voice_authors[author] += 1
    promoted = []
    for login, n in merged_authors.most_common(25):
        if n >= 2 or login in KEEP_LOGINS:
            promoted.append({"login": login, "note": f"corpus: {n} merged PR(s) with maintainer discourse"})
    existing = []
    if OUT.exists():
        data = yaml.safe_load(OUT.read_text()) or {}
        for a in data.get("authors") or []:
            if isinstance(a, dict) and a.get("login"):
                existing.append(a)
    seen = {a["login"] for a in existing if isinstance(a, dict)}
    for p in promoted:
        if p["login"] not in seen:
            existing.append(p)
            seen.add(p["login"])
    OUT.write_text(
        yaml.safe_dump(
            {"authors": existing[:40], "filter_authors_only": False},
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    print(f"✓ {OUT} — {len(existing)} bounty hunter(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())