# Issue Agent

Local autonomous GitHub issue resolver. Triage, fix, test, and open PRs using **Ollama** + **Aider** — no cloud LLM required for core operations.

Powers the [Nueramarcos](https://github.com/Nueramarcos) repo fleet (orion, forge, nexus, vertex, and upstream forks).

## Features

- **Triage** — classify issues with a small local model (`qwen2.5-coder:1.5b`)
- **Fix** — surgical patches via Aider + `qwen2.5-coder:7b`
- **Fleet** — rotate across repos, auto-merge squash PRs
- **Airport** — parallel worker supervisor with failure ledger and CI heal
- **Collect** — seed issues from `backlog.yaml` and auto-discovery
- **Upstream** — optional lane for OSS contribution (e.g. tinygrad bounties)

## Requirements

- Ubuntu/Linux workstation
- [gh](https://cli.github.com/) CLI authenticated
- [Ollama](https://ollama.com/) with `qwen2.5-coder:7b` and `qwen2.5-coder:1.5b`
- [Aider](https://aider.chat/) in a venv (`~/.local/venvs/aider`)
- Python 3.12+, PyYAML

## Quick start

```bash
git clone https://github.com/Nueramarcos/issue-agent.git ~/issue-agent
pip install pyyaml

# Optional per-repo config — copy to repo root as .issue-agent.yml
cp examples/issue-agent.yml.example /path/to/repo/.issue-agent.yml

# Check toolchain
python3 ~/issue-agent/issue_agent.py status

# List open issues on a repo
python3 ~/issue-agent/issue_agent.py list --repo owner/repo

# Fix one issue
python3 ~/issue-agent/issue_agent.py fix --repo owner/repo --issue 42

# Fleet mode (rotate repos from repos.yaml)
python3 ~/issue-agent/issue_agent.py fleet
```

## CLI wrappers

Install `bin/` scripts to `~/bin` and ensure they're on PATH. The main entrypoint is `issue-agent`.

```bash
cp bin/issue-agent ~/bin/
chmod +x ~/bin/issue-agent
```

## Configuration

| File | Purpose |
|------|---------|
| `config.default.toml` | Global defaults (model, labels, workspace paths) |
| `repos.yaml` | Fleet repo list with test commands and CI workflows |
| `airport.yaml` | Airport supervisor lanes and intervals |
| `backlog.yaml` | Issue seeds for `collect` / `factory` |
| `upstream.yaml` | Upstream OSS repos (optional lane) |

See [AIRPORT-DESIGN.md](AIRPORT-DESIGN.md) for the full architecture.

## Commands

```
status  list  triage  fix  run  demo  watch  polish  boost  fleet
collect  max  local  build  relentless  cleanup-ci-prs  ci-heal  ci-watch
daemon  refresh  airport  worker  factory  upstream  upstream-bootstrap
roam  solvability  failures
```

Run `issue_agent.py --help` or `issue_agent.py <cmd> --help` for details.

## Secrets

Never commit API keys. Load from `~/.config/cockpit/secrets.env` (mode 600) or export `GH_TOKEN` / `GITHUB_TOKEN` before running.

## License

MIT — see [LICENSE](LICENSE).