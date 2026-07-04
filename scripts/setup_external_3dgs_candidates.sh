#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-external}"
mkdir -p "$BASE"
clone_pin() {
  local url="$1"
  local dir="$2"
  local ref="$3"
  if [ ! -d "$dir/.git" ]; then
    git clone "$url" "$dir"
  fi
  git -C "$dir" fetch --depth 1 origin "$ref"
  git -C "$dir" checkout --detach "$ref"
  printf 'external candidate ready: %s (%s)\n' "$dir" "$ref"
}
clone_pin https://github.com/shg8/3DGS.cpp.git "$BASE/3DGS.cpp" "${CAPTURE_SPLAT_3DGS_CPP_REF:-8fe4b2fba09306e9fcb0308fa11c6aa7b8d0ac41}"
clone_pin https://github.com/AndrewBoessen/3DGS.git "$BASE/3DGS-AndrewBoessen" "${CAPTURE_SPLAT_ANDREW_3DGS_REF:-a6358b83407660eed3ccd8d83d3d537f4c1fe688}"
