#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,vision]'
if command -v brew >/dev/null 2>&1; then
  brew list colmap >/dev/null 2>&1 || brew install colmap
  brew list vulkan-tools >/dev/null 2>&1 || brew install vulkan-tools || true
fi
printf 'Run scripts/setup_vksplat.sh next.
'
