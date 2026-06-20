# Contributing upstream with Issue Agent

How to use Issue Agent for real OSS PRs (not just fleet polish).

## The playbook

1. **Find a small, test-backed fix** — emulator bugs, type hints, CUDA stream args, doc gaps.
2. **Fork upstream** — `gh repo fork tinygrad/tinygrad --clone`
3. **Reproduce locally** — run the exact CI test subset before touching code.
4. **Fix minimally** — one logical change per PR; no drive-by refactors.
5. **Open PR upstream** — reference the issue; include test plan checklist.
6. **Respond to review** — rebase, force-push only on your fork branch.

Issue Agent automates steps 3–5 on your forks via `upstream.yaml`.

## Configure upstream lane

`upstream.yaml` example:

```yaml
workspace_root: ~/upstream-workspaces

projects:
  - slug: tinygrad
    upstream: tinygrad/tinygrad
    fork: your-user/tinygrad
    test_command: "PYTHON=1 python3 -m pytest -x -q test/test_tiny.py"
    enabled: true
    mode: pr
```

Bootstrap workspaces:

```bash
issue-agent upstream-bootstrap --tier 1
issue-agent upstream --slug tinygrad
```

## What lands well upstream

| Type | Example | Why maintainers merge |
|------|---------|----------------------|
| Emulator correctness | WMMA accum starts at float32 zero | Fixes NaNs in CI mock GPU |
| Type annotations | `Union[Tensor, PILImage]` on torchvision | Zero runtime change, fixes mypy |
| CUDA streams | Pass `getCurrentCUDAStream()` to kernels | Matches repo patterns |
| Test gaps | CUDA regression for device mismatch | Reproduces reported issue |

## What to avoid

- Skipping tests to green the suite (maintainers will ask you to fix root cause)
- Large architectural changes from local 7B models
- PRs without linked issues on mature repos

## Bounty / emulator tips (tinygrad)

```bash
cd ~/upstream-workspaces/tinygrad  # or your fork path
MOCKKFD=1 AMD=1 LLVM=gfx1201 python3 -m pytest -x -q test/test_ops.py
```

- Run the **exact CI backend selection** before pushing.
- Cherry-pick small fixes; don't bundle deprecation + skips + hacks.
- Comment on the PR with the exact test command and pass count.

## torchvision / PyTorch pattern

1. Find issue with reproduction steps (device mismatch, wrong stream).
2. Add test in `test/` that fails before fix.
3. Fix in one file; mention "zero functional change" when true.

## Scout upstream issues (Tesla / AMD / tinygrad lane)

Curated queue lives in `upstream-opportunities.yaml`. Ranked view:

```bash
issue-agent scout                    # top 15 by score
issue-agent scout --tag amd          # AMD/ROCm/HIP only
issue-agent scout --tag tesla        # commaai / autopilot adjacency
issue-agent scout --tier 1           # do-now tier only
issue-agent scout --live             # merge live GitHub search hits
issue-agent scout --enqueue 5        # queue top 5 into scout-queue.json
```

Work one item:

1. `issue-agent scout --tier 1 --limit 3` — pick the highest-score row.
2. Fork + clone: `gh repo fork tinygrad/tinygrad --clone`
3. Reproduce with the `test_hint` line from scout output.
4. Fix, push, open PR — track in scout queue (`status: in_progress` → `pr_open`).

## Tracking open upstream PRs

```bash
gh pr list --author @me --state open
issue-agent scout --tag tinygrad     # includes your open PRs in curated list
issue-agent failures    # blocked items in failure ledger
```

## Fleet vs upstream

| Mode | Target | Auto-merge |
|------|--------|------------|
| Fleet (`repos.yaml`) | Your repos | Yes (squash) |
| Upstream (`upstream.yaml`) | OSS forks | No — human review |

Keep upstream PRs human-reviewed. Use Issue Agent to draft and test, not to spam maintainers.