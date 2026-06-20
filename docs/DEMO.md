# Terminal demo script

Record this flow for a README GIF or video. Commands assume native install.

```bash
# 1. Health
issue-agent status

# 2. List triage queue
issue-agent list --repo your-user/your-repo --limit 5

# 3. Dry-run (no PR)
issue-agent status --quick
issue-agent demo --dry-run
issue-agent demo --repo Nueramarcos/issue-agent

# 4. Real fix
issue-agent fix --repo your-user/your-repo --issue 1

# 5. Fleet snapshot
issue-agent status | tail -20
```

Expected `status` output (abbreviated):

```
Issue Agent — status among change

  [ok] gh: authenticated
  [ok] ollama: running
  [ok] aider: /home/you/.local/venvs/aider/bin/aider
  [ok] workspaces: /home/you/agent-workspaces
  [ok] logs: /home/you/issue-agent/logs

  fleet: 1 active · 0 parked · local_queue=0 · ci_heal_queue=0
    ✓ your-repo
```