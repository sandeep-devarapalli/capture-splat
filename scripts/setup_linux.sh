#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,vision]'
printf 'Install COLMAP and Vulkan tools with your distro package manager, then run scripts/setup_vksplat.sh.
'
