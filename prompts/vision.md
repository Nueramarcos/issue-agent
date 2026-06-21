# Habitat Solver — Vision

**North star:** A self-improving, locally hosted agent fleet that earns OSS credibility, learns from its own merged PRs, and compounds into a personal AI lab — on a gaming PC, not a datacenter.

**Operator:** Marcos (Nueramarcos) · Ubuntu 24.04 · i5-9600K · RX 5700 XT · 23GB RAM · Ollama local

**Flagship project:** [issue-agent](https://github.com/Nueramarcos/issue-agent) — self-hosted GitHub issue resolver

**Constellation:**
- **Airport** — parallel fleet supervisor (5 lanes)
- **Radar** — scout/hunt upstream opportunities (`issue-agent scout --live --web`)
- **Customs** — triage classifier (1.5b model)
- **Habitat** — adaptive per-repo execution shell
- **Habitat Solver** — fix loop (7b + Aider)
- **Wind Tunnel** — local tests (pytest, cargo)
- **Tower** — reviewer gate (Orion AST + ruff + scope)
- **Flight Recorder** — trajectory memory → LoRA (`issue-agent-lora export`)

**Success signals:** merged PRs, green CI, Flight Recorder growth, upstream maintainer trust (tinygrad, Forge, torchvision).

**Non-goals:** cloud LLM dependency for core ops, drive-by refactors, waiting forever on GH Actions queues.

**Adaptive loop:** every merge/failure appends to Flight Recorder → weekly LoRA on 1.5b → better Customs/Tower calibration → higher merge rate.