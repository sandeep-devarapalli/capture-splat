#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-external/vksplat}"
REF="${VKSPLAT_REF:-main}"
if [ ! -d "$ROOT/.git" ]; then
  mkdir -p "$(dirname "$ROOT")"
  git clone https://github.com/harry7557558/vksplat.git "$ROOT"
fi
git -C "$ROOT" fetch --tags --quiet
git -C "$ROOT" checkout "$REF"
python -m pip install -e "$ROOT/vksplat"
if [ -f "$ROOT/compile_shaders.py" ]; then
  python "$ROOT/compile_shaders.py" || true
fi
printf 'VkSplat ready at %s
' "$ROOT"
