#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-external/gsplat}"
REF="${CAPTURE_SPLAT_GSPLAT_REF:-8b6319f8335df7de18d4514feb90b60e3941a073}"
if [ ! -d "$ROOT/.git" ]; then
  mkdir -p "$(dirname "$ROOT")"
  git clone https://github.com/nerfstudio-project/gsplat.git "$ROOT"
fi
git -C "$ROOT" fetch --depth 1 origin "$REF"
git -C "$ROOT" checkout --detach "$REF"
if [ "${CAPTURE_SPLAT_SKIP_GSPLAT_INSTALL:-0}" != "1" ]; then
  python -m pip install -e "$ROOT"
fi
printf 'gsplat source ready at %s (%s).\n' "$ROOT" "$REF"
