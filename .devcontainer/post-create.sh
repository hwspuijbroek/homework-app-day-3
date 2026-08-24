#!/usr/bin/env bash
# Provisions the dev container: Databricks CLI, Python deps, Databricks AI Dev Kit.
set -euo pipefail

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }

log "Installing Databricks CLI"
if ! command -v databricks >/dev/null 2>&1; then
  curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sudo sh
fi
databricks --version

log "Installing Python dependencies (CPU-only torch)"
pip install --upgrade pip
# Pull torch from the CPU wheel index first; the default PyPI wheel bundles CUDA
# (~2.5 GB) and nothing here trains anything — the venue model only encodes one
# short question per call.
# Pinned to the same version as requirements.txt: `torch==2.13.0` there is
# satisfied by the 2.13.0+cpu wheel installed here (a local version tag matches
# the base version), but only if the versions line up — otherwise the next line
# quietly pulls the CUDA build from PyPI on top of it.
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0
# requirements-dev.txt pulls in requirements.txt, so the container gets both the
# runtime deps the deployed app uses and the test-only ones it must not.
pip install -r requirements-dev.txt

log "Installing Claude Code CLI"
# Required on PATH: 'databricks aitools install' shells out to 'claude' to
# register the databricks plugin.
npm install -g @anthropic-ai/claude-code
claude --version

log "Installing Databricks AI Dev Kit"
# Non-interactive, project-scoped install of all skills for Claude Code.
bash <(curl -sL https://raw.githubusercontent.com/databricks-solutions/ai-dev-kit/main/install.sh) \
  --tools claude \
  --skills-profile all \
  --yes

log "Done. Run 'databricks auth login --host <workspace-url>' to authenticate."
