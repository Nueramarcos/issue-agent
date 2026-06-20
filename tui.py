#!/usr/bin/env python3
"""Interactive terminal UI for Issue Agent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent / "issue_agent.py"
PYTHON = sys.executable
DEFAULT_REPO = "Nueramarcos/orion-ai-agent"

BANNER = r"""
  ___       _                       _                    _   
 |_ _|_ __ | |_ ___  __ _ ___  __ _| |_ _   _ _ __ ___  | |_ 
  | || '_ \| __/ _ \/ _` / __|/ _` | __| | | | '__/ _ \ | __|
  | || | | | ||  __/ (_| \__ \ (_| | |_| |_| | | |  __/ | |_ 
 |___|_| |_|\__\___|\__, |___/\__,_|\__|\__,_|_|  \___|  \__|
                    |___/   Local · Ollama · Aider · gh
"""


def clear() -> None:
    os.system("clear")


def run_agent(*args: str) -> int:
    cmd = [PYTHON, str(AGENT), *args]
    print(f"\n→ {' '.join(args)}\n")
    return subprocess.call(cmd)


def pick_repo() -> str:
    default = os.environ.get("ISSUE_AGENT_REPO", DEFAULT_REPO)
    repo = input(f"Repo [owner/name] ({default}): ").strip()
    return repo or default


def menu() -> None:
    repo = os.environ.get("ISSUE_AGENT_REPO", DEFAULT_REPO)
    while True:
        clear()
        print(BANNER)
        print(f"  Active repo: {repo}")
        print(f"  Workspaces:  ~/agent-workspaces/")
        print(f"  Logs:        ~/issue-agent/logs/issue-agent.log")
        print()
        print("  1) Status check (gh + ollama + aider)")
        print("  2) List open issues")
        print("  3) Triage issues (classify with local LLM)")
        print("  4) Triage + apply labels (agent-triage / agent-skip)")
        print("  5) Fix ONE issue (opens draft PR)")
        print("  6) Run batch (all issues labeled agent-triage)")
        print("  7) Watch mode (poll every 30 min)")
        print("  8) Change active repo")
        print("  9) Help / how to use")
        print("  0) Exit")
        print()
        choice = input("Choose [0-9]: ").strip()

        if choice == "0":
            print("Bye.")
            break
        elif choice == "1":
            run_agent("status")
            input("\nPress Enter...")
        elif choice == "2":
            run_agent("list", repo)
            input("\nPress Enter...")
        elif choice == "3":
            num = input("Issue number (blank = all recent): ").strip()
            args = ["triage", repo]
            if num:
                args.append(num)
            run_agent(*args)
            input("\nPress Enter...")
        elif choice == "4":
            num = input("Issue number (blank = all recent): ").strip()
            args = ["triage", repo, "--apply-label"]
            if num:
                args.insert(2, num)
            run_agent(*args)
            input("\nPress Enter...")
        elif choice == "5":
            num = input("Issue number to fix: ").strip()
            if not num.isdigit():
                print("Need a number.")
                input("\nPress Enter...")
                continue
            dry = input("Dry run only? [y/N]: ").strip().lower() == "y"
            args = ["fix", repo, num]
            if dry:
                args.append("--dry-run")
            run_agent(*args)
            input("\nPress Enter...")
        elif choice == "6":
            dry = input("Dry run only? [y/N]: ").strip().lower() == "y"
            args = ["run", repo]
            if dry:
                args.append("--dry-run")
            run_agent(*args)
            input("\nPress Enter...")
        elif choice == "7":
            print("Watch runs until Ctrl+C in the next screen.")
            input("Press Enter to start...")
            run_agent("watch", repo, "--interval", "1800")
            input("\nPress Enter...")
        elif choice == "8":
            repo = pick_repo()
            os.environ["ISSUE_AGENT_REPO"] = repo
        elif choice == "9":
            clear()
            print(HELP)
            input("\nPress Enter...")
        else:
            input("Invalid choice. Press Enter...")


HELP = """
HOW TO USE ISSUE AGENT
======================

Quick start (3 steps):
  1. Label an issue you want fixed:  agent-triage
  2. In this menu: option 6 (Run batch)
  3. Review the draft PR on GitHub, then mark it ready

CLI (from any terminal):
  issue-agent status
  issue-agent list Nueramarcos/orion-ai-agent
  issue-agent triage Nueramarcos/orion-ai-agent 12 --apply-label
  issue-agent fix  Nueramarcos/orion-ai-agent 12
  issue-agent run  Nueramarcos/orion-ai-agent
  issue-agent watch Nueramarcos/orion-ai-agent

Typical workflow:
  • list        — see open issues
  • triage      — local 1.5B model sorts bug/feature/docs and skips hard ones
  • fix         — clones to ~/agent-workspaces/, aider + qwen2.5-coder:7b edits,
                  runs tests, pushes branch, opens DRAFT PR
  • run         — processes every issue with label agent-triage (max 3 per run)
  • watch       — same as run, every 30 minutes (hands-off)

Labels:
  agent-triage  — agent will attempt a fix
  agent-skip    — auto-added when triage says skip
  wontfix, question, help wanted, architecture — never auto-fixed

Per-repo config (optional .issue-agent.yml in repo root):
  test_command: "python -m pytest -x -q"
  model: ollama/qwen2.5-coder:7b
  draft_pr: true

Safety:
  • Always opens DRAFT PRs — you merge manually
  • Works on clones in ~/agent-workspaces/, not your live project dirs
  • Logs: ~/issue-agent/logs/issue-agent.log

Open this UI anytime:
  issue-agent-ui
  issue-agent-open   # new terminal window
"""


if __name__ == "__main__":
    menu()