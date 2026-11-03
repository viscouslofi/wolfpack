@echo off
REM WolfPack installer for Windows.
REM WolfPack itself needs only Python 3.11+ (stdlib only, zero pip installs).
REM This script optionally installs the CLI agents it can orchestrate.

echo === WolfPack setup ===
python --version >nul 2>&1 || (echo ERROR: Python 3.11+ required & exit /b 1)

where npm >nul 2>nul
if %errorlevel%==0 (
  echo Installing Codex CLI, OpenCode and Claude Code via npm...
  npm install -g @openai/codex opencode-ai @anthropic-ai/claude-code
) else (
  echo npm not found - skipping Codex/OpenCode/Claude CLI install.
  echo Install Node.js from https://nodejs.org to enable them.
)

where kimi >nul 2>nul
if not %errorlevel%==0 (
  echo Installing Kimi Code CLI...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://code.kimi.com/kimi-code/install.ps1 | iex"
)

where ollama >nul 2>nul
if not %errorlevel%==0 (
  echo Ollama not found - install it from https://ollama.com for free local models.
  echo (WolfPack works without it, but local models are free and unlimited.)
)

if not exist keys.toml (
  copy keys.toml.example keys.toml >nul
  echo Created keys.toml - paste your free API keys there.
)

echo.
echo Done. Next steps:
echo   1. start.bat                       - launches WolfPack and opens the UI
echo   2. The setup wizard will walk you through linking agents on first run.
echo   3. python harness.py doctor        - health-check all agents (optional)
echo   4. python harness.py discover      - list today's free cloud models
