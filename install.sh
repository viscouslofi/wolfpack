#!/usr/bin/env bash
# WolfPack installer for macOS/Linux.
# WolfPack itself needs only Python 3.11+ (stdlib only, zero pip installs).
set -e

echo "=== WolfPack setup ==="
python3 --version >/dev/null 2>&1 || { echo "ERROR: Python 3.11+ required"; exit 1; }

if command -v npm >/dev/null; then
  echo "Installing Codex CLI, OpenCode and Claude Code via npm..."
  npm install -g @openai/codex opencode-ai @anthropic-ai/claude-code
else
  echo "npm not found - skipping Codex/OpenCode/Claude CLI install."
  echo "Install Node.js from https://nodejs.org to enable them."
fi

if ! command -v kimi >/dev/null; then
  echo "Installing Kimi Code CLI..."
  curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash || \
    echo "Kimi CLI install skipped."
fi

if ! command -v ollama >/dev/null; then
  echo "Ollama not found - install it from https://ollama.com for free local models."
  echo "(WolfPack works without it, but local models are free and unlimited.)"
fi

[ -f keys.toml ] || { cp keys.toml.example keys.toml; echo "Created keys.toml - paste your free API keys there."; }

cat <<'EOF'

Done. Next steps:
  1. ./start.sh                       - launches WolfPack and opens the UI
  2. The setup wizard will walk you through linking agents on first run.
  3. python3 harness.py doctor        - health-check all agents (optional)
  4. python3 harness.py discover      - list today's free cloud models
EOF
