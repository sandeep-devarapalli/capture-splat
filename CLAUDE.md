# Claude Code Guide

## Read First

Start with these files before suggesting or editing code:

- `README.md`
- `ROADMAP.md`
- `docs/field_validation_learnings.md`
- `docs/pipeline.md`
- `docs/quality_caveats.md`
- `docs/app_comparison.md`

## Project Shape

Capture Splat is a public, brand-neutral toolkit for iPhone capture to standard
3D Gaussian Splatting `.ply` output. The Python package is `capture_splat`; the
public CLI entry point is `capture-splat`; the iOS app is `Capture Splat`.

The default training baseline is VkSplat/Vulkan. Treat VkSplat, COLMAP, and
viewers as external dependencies. Do not vendor trainer code or viewer outputs.

## Quality Language

Be conservative. A finite `.ply`, viewer load, or alignment proof is necessary
evidence, not a quality claim. Prefer terms like:

- `validated finite output`
- `alignment proof`
- `quality proxy improved`
- `promote`
- `hold`
- `reject`

Do not claim a scan is high quality unless controlled gates support it:
COLMAP-refined package health, finite PLY checks, raw-canvas render/source QA,
and the `3000 -> 7000 -> 15000 -> 30000` training ladder.

Longer training alone does not fix weak capture, weak COLMAP support, bad
intrinsics, bad frame matching, blur, or weak supervision.

## Public Commands

Important commands and modules:

- `capture-splat ingest`
- `capture-splat capture-quality`
- `capture-splat colmap-export`
- `capture-splat qa-render-source`
- `capture-splat train-vksplat`
- `capture-splat train-vksplat-ladder`
- `capture-splat compare-app-output`

Keep reusable behavior in `python/capture_splat/` and wire public commands
through `python/capture_splat/cli.py`.

## Validation

Before proposing a commit, run:

```bash
python -m compileall python scripts tests
PYTHONPATH=python .venv/bin/python -m pytest
git diff --check
```

Also scan public docs and code for internal product names, private repo names,
and local absolute paths. That scan should return no matches.

## Repository Hygiene

Keep generated captures, render canvases, PLYs, logs, trainer outputs, app
exports, and comparison reports out of git. Use local `runs/` and `external/`
directories for generated or downloaded material.

Prefer focused edits to existing files. Add new modules only when they create a
reusable public command or a clear test boundary.

## App Comparisons

When comparing Capture Splat with third-party iPhone 3DGS apps, use observable
evidence only: exported artifacts, metadata, screenshots, screen recordings,
and matching rendered views. Do not infer proprietary internals.

Use `capture-splat compare-app-output` to produce a strict JSON summary, then
use raw-canvas `qa-render-source` when comparable render frames are available.
