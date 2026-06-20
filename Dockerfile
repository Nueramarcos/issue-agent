FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git gh \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -U pip aider-chat pyyaml

WORKDIR /app
COPY issue_agent.py tui.py config.default.toml requirements.txt ./
COPY repos.yaml airport.yaml backlog.yaml upstream.yaml upstream-backlog.yaml ./
COPY .issue-agent.yml.example ./

ENV ISSUE_AGENT_ROOT=/app \
    ISSUE_AGENT_AIDER=/opt/venv/bin/aider \
    ISSUE_AGENT_WORKSPACES=/workspaces \
    OLLAMA_HOST=http://ollama:11434 \
    PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /app/logs /workspaces

ENTRYPOINT ["python3", "/app/issue_agent.py"]
CMD ["status"]