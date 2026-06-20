#!/usr/bin/env bash
# Issue Agent — native Linux install (Ubuntu/Debian)
set -euo pipefail

INSTALL_DIR="${ISSUE_AGENT_HOME:-$HOME/issue-agent}"
VENV_DIR="${ISSUE_AGENT_VENV:-$HOME/.local/venvs/aider}"
REPO_URL="${ISSUE_AGENT_REPO:-https://github.com/Nueramarcos/issue-agent.git}"

echo "==> Issue Agent install"
echo "    dir:  $INSTALL_DIR"
echo "    venv: $VENV_DIR"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1 — install it first." >&2; exit 1; }
}

need git
need python3
need curl

if ! command -v gh >/dev/null 2>&1; then
  echo "Installing gh CLI..."
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt update && sudo apt install -y gh
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "Installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sh
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "==> Updating $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "==> Cloning $REPO_URL"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

mkdir -p "$(dirname "$VENV_DIR")"
if [[ ! -x "$VENV_DIR/bin/aider" ]]; then
  echo "==> Creating Aider venv"
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -U pip aider-chat pyyaml
fi

export ISSUE_AGENT_ROOT="$INSTALL_DIR"
export ISSUE_AGENT_AIDER="$VENV_DIR/bin/aider"
export OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

bash "$INSTALL_DIR/scripts/pull-models.sh"

mkdir -p "$HOME/agent-workspaces"
mkdir -p "$HOME/bin"
cp -f "$INSTALL_DIR/bin/issue-agent" "$HOME/bin/" 2>/dev/null || true
chmod +x "$HOME/bin/issue-agent" 2>/dev/null || true

if ! gh auth status >/dev/null 2>&1; then
  echo ""
  echo "==> Authenticate GitHub (required for fix/PR commands):"
  echo "    gh auth login"
fi

cat <<EOF

==> Install complete

  export ISSUE_AGENT_ROOT="$INSTALL_DIR"
  export ISSUE_AGENT_AIDER="$VENV_DIR/bin/aider"

  issue-agent status          # verify gh + ollama + aider
  issue-agent demo --repo Nueramarcos/orion-ai-agent --dry-run

  Docs: $INSTALL_DIR/docs/QUICKSTART.md

EOF