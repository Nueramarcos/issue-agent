# Issue Agent

**Self-hosted GitHub issue resolver.** Triage, fix, test, and open PRs with local **Ollama** + **Aider** — no cloud LLM required.

```bash
curl -fsSL https://raw.githubusercontent.com/Nueramarcos/issue-agent/main/scripts/install.sh | bash
gh auth login
issue-agent status
issue-agent fix --repo your-user/your-repo --issue 1
```

Full walkthrough: **[docs/QUICKSTART.md](docs/QUICKSTART.md)** (Airport in 5 minutes)

## Why this exists

Most coding agents are cloud-only. Issue Agent runs on **your** machine, uses **your** models, and operates on **your** repos (or upstream forks) through `gh` CLI — with a fleet supervisor that keeps working while you sleep.

## What it does

| Capability | Command |
|------------|---------|
| Health check | `issue-agent status` |
| Fast health check | `issue-agent status --quick` |
| Fix one issue | `issue-agent fix --repo owner/repo --issue N` |
| Fix all `agent-triage` | `issue-agent run --repo owner/repo` |
| Rotate fleet | `issue-agent fleet` |
| Parallel supervisor | `issue-agent airport` |
| Upstream OSS PRs | `issue-agent upstream` |

## Install

### One-liner (Linux)

```bash
curl -fsSL https://raw.githubusercontent.com/Nueramarcos/issue-agent/main/scripts/install.sh | bash
```

Installs: clone, Aider venv, Ollama models (`qwen2.5-coder:7b` + `1.5b`), `~/bin/issue-agent`.

### Docker

```bash
git clone https://github.com/Nueramarcos/issue-agent.git && cd issue-agent
cp .env.example .env   # GH_TOKEN=...
docker compose up -d ollama && docker compose run --rm models
docker compose run --rm agent status
```

### Manual

Requirements: Python 3.12+, [gh](https://cli.github.com/), [Ollama](https://ollama.com/), [Aider](https://aider.chat/), PyYAML.

```bash
git clone https://github.com/Nueramarcos/issue-agent.git ~/issue-agent
pip install pyyaml
python3 -m venv ~/.local/venvs/aider && ~/.local/venvs/aider/bin/pip install aider-chat
ollama pull qwen2.5-coder:7b && ollama pull qwen2.5-coder:1.5b
cp examples/repos.starter.yaml repos.yaml   # edit your repo
issue-agent status
```

## Configure your repo

1. Copy `examples/repos.starter.yaml` → `repos.yaml`, set `your-user/your-repo`
2. Optional: `examples/issue-agent.yml` → `.issue-agent.yml` in repo root
3. Create label `agent-triage` on GitHub
4. Optional: add `examples/workflows/agent-triage-on-failure.yml` to auto-open issues when CI fails

## Demo (no GitHub needed)

```bash
issue-agent demo --dry-run
issue-agent demo --repo Nueramarcos/issue-agent
```

## Docs

| Doc | Contents |
|-----|----------|
| [QUICKSTART.md](docs/QUICKSTART.md) | Airport in 5 minutes |
| [CONTRIBUTING-UPSTREAM.md](docs/CONTRIBUTING-UPSTREAM.md) | Land PRs on tinygrad, torchvision, etc. |
| [AIRPORT-DESIGN.md](AIRPORT-DESIGN.md) | Full architecture |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ISSUE_AGENT_ROOT` | `~/issue-agent` | Config and state directory |
| `ISSUE_AGENT_AIDER` | `~/.local/venvs/aider/bin/aider` | Aider binary |
| `ISSUE_AGENT_WORKSPACES` | `~/agent-workspaces` | Clone directory |
| `ISSUE_AGENT_SECRETS` | `~/.config/cockpit/secrets.env` | Optional secrets file |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama API |
| `GH_TOKEN` | — | GitHub token (or use `gh auth login`) |

## Part of the local agent stack

Issue Agent is the fleet layer. Related projects by [Nueramarcos](https://github.com/Nueramarcos):

- **[linux-cockpit](https://github.com/Nueramarcos/linux-cockpit)** — terminal + agent conventions
- **[orion-ai-agent](https://github.com/Nueramarcos/orion-ai-agent)** — AST bug tracing
- **[build-composer](https://github.com/Nueramarcos/build-composer)** — LangGraph multi-agent coder

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `status` takes 30–60s | Use `issue-agent status --quick` (skips fleet gh scans) |
| Demo: "task already satisfied" | Main already has the fix — expected, exit 0 |
| Demo: "no commits" | Aider made no diff; try a different repo or issue |
| `gh` check fails | Run `gh auth login` |

## License

MIT