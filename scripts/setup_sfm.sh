#!/bin/bash
# Install optional SfM upgrades for `capture-splat sfm`: GLOMAP (global
# mapper) and hloc (EigenPlaces retrieval + ALIKED + LightGlue features).
# COLMAP remains the always-available baseline; sfm degrades gracefully
# when these are missing.
set -euo pipefail

EXTERNAL_DIR="${1:-external}"
mkdir -p "$EXTERNAL_DIR"

if ! command -v colmap >/dev/null 2>&1; then
  echo "colmap not found. Install it first (macOS: brew install colmap)." >&2
  exit 1
fi

if command -v glomap >/dev/null 2>&1; then
  echo "glomap already installed: $(command -v glomap)"
else
  if [ ! -d "$EXTERNAL_DIR/glomap" ]; then
    git clone https://github.com/colmap/glomap.git "$EXTERNAL_DIR/glomap"
  fi
  echo "Building GLOMAP (requires cmake + the COLMAP build dependencies)..."
  cmake -S "$EXTERNAL_DIR/glomap" -B "$EXTERNAL_DIR/glomap/build" -DCMAKE_BUILD_TYPE=Release
  cmake --build "$EXTERNAL_DIR/glomap/build" -j "$(sysctl -n hw.ncpu 2>/dev/null || nproc)"
  echo "GLOMAP built at $EXTERNAL_DIR/glomap/build/glomap/glomap"
  echo "Add it to PATH or symlink it, e.g.:"
  echo "  ln -s \"\$PWD/$EXTERNAL_DIR/glomap/build/glomap/glomap\" /usr/local/bin/glomap"
fi

if python3 -c "import hloc" >/dev/null 2>&1; then
  echo "hloc already importable"
else
  if [ ! -d "$EXTERNAL_DIR/hloc" ]; then
    git clone --recursive https://github.com/cvg/Hierarchical-Localization.git "$EXTERNAL_DIR/hloc"
  fi
  echo "Installing hloc into the active Python environment..."
  python3 -m pip install -e "$EXTERNAL_DIR/hloc"
fi

echo "Done. Verify with: capture-splat doctor"
