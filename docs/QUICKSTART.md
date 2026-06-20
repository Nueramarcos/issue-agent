# Airport in 5 minutes

Get Issue Agent running, fix one issue, and understand the fleet loop.

## 1. Install (pick one)

### Native Linux (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Nueramarcos/issue-agent/main/scripts/install.sh | bash
gh auth login
issue-agent status
```

You want three green checks: `gh`, `ollama`, `aider`.

### Docker

```bash
git clone https://github.com/Nueramarcos/issue-agent.git
cd issue-agent
cp .env.example .env   # set GH_TOKEN=ghp_...
docker compose up -d ollama
docker compose run --rm models
docker compose run --rm agent status
```

## 2. Point at your repo

Edit `repos.yaml` — start with one repo:

```yaml
repos:
  - name: your-user/your-repo
    branch: main
    test_command: "python3 -m pytest -q"
    wait_for_checks: false
    park_minutes: 5
```

Or copy the starter:

```bash
cp examples/repos.starter.yaml repos.yaml
# edit your-user/your-repo
```

Optional per-repo overrides — add `.issue-agent.yml` in the repo root (see `examples/issue-agent.yml`).

## 3. Seed a fixable issue

Create a GitHub issue on your repo with label `agent-triage`:

```bash
gh issue create -R your-user/your-repo \
  --title "Add pytest smoke test for main module" \
  --body "Add tests/test_smoke.py with one import test. pytest -q must pass." \
  --label agent-triage
```

Or seed from backlog:

```bash
issue-agent collect --repo your-user/your-repo
```

## 4. Fix one issue (the core loop)

```bash
issue-agent list --repo your-user/your-repo
issue-agent fix --repo your-user/your-repo --issue 1
```

What happens:

1. Clone to `~/agent-workspaces/`
2. Aider reads the issue, edits files
3. Local `test_command` runs
4. Branch pushed, PR opened (squash-merge if configured)

Dry-run without GitHub:

```bash
issue-agent demo --dry-run
issue-agent demo --repo Nueramarcos/issue-agent
```

## 5. Run the fleet (Airport)

Single-repo rotation:

```bash
issue-agent fleet --max 1
```

Full Airport supervisor (parallel workers):

```bash
issue-agent airport          # foreground supervisor
# or detached:
issue-agent-airport-start    # if bin/ on PATH
```

Airport reads `airport.yaml` — lanes per repo, factory interval, CI heal.

## 6. Watch it work

```bash
issue-agent status           # fleet digest + solvability
issue-agent ci-watch         # live CI dashboard
tail -f ~/issue-agent/logs/issue-agent.log
```

## Common commands

| Goal | Command |
|------|---------|
| Health check | `issue-agent status` |
| Fix labeled issues | `issue-agent run --repo owner/repo` |
| Polish all repos | `issue-agent polish` |
| Seed + fix fleet | `issue-agent boost` |
| Upstream OSS lane | `issue-agent upstream` |
| Never stop | `issue-agent relentless` |

## Next

- [CONTRIBUTING-UPSTREAM.md](CONTRIBUTING-UPSTREAM.md) — land PRs on tinygrad, torchvision, etc.
- [AIRPORT-DESIGN.md](../AIRPORT-DESIGN.md) — full architecture
- Add [agent-triage-on-failure.yml](../examples/workflows/agent-triage-on-failure.yml) to your repo CI