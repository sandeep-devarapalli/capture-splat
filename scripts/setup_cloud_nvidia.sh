#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,vision]'
python scripts/doctor.py || true
printf 'Use docker/Dockerfile.linux-nvidia for a reproducible NVIDIA cloud image.
'
