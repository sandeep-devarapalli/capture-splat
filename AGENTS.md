# Capture Splat Agent Guide

## Read First

Read `README.md`, `ROADMAP.md`, `docs/pipeline.md`,
`docs/quality_caveats.md`, `docs/field_validation_learnings.md`, and
`docs/spirulae_splat_learnings.md` before changing capture, reconstruction,
training, QA, or World Studio handoffs.

## Product Boundaries

- Local iPhone finalization plus Projects -> Manual Export is the production
  capture path. Live transfer is optional research and cannot block it.
- Capture Splat owns simulator-neutral capture, preparation, SfM, trainer
  adaptation, and evidence. World Studio owns jobs, review, promotion, Spark
  visualization, and Newton integration.
- Treat GPL-licensed Spirula implementations as pinned, user-installed external
  processes. Do not vendor, import, or copy their implementation into this
  Apache-2.0 repository.
- Keep generated datasets, captures, models, renders, and benchmark results out
  of Git.

## Contracts And Claims

- New manifest fields are additive. Preserve v0.2 World Studio handoff resume
  compatibility when extending v0.3.
- Use strict JSON, relative paths, SHA-256 bindings, finite-number checks, and
  explicit coordinate frames and units.
- Dataset evidence does not prove a trainer consumed it. A finite PLY, viewer
  load, import, or completed job does not prove visual quality, metric scale,
  collision safety, navigation readiness, or physics authority.
- Use `promote|hold|reject` and retain proposal-only authority until the
  corresponding physical or visual evidence gate passes.

## Benchmark Policy

- The first measured efficiency lane is Apple Silicon. Hold cross-vendor,
  8 GB, and multi-million-Gaussian capacity claims until the named hardware,
  commands, repetitions, raw results, noise, and quality rails are recorded.
- Reference-data hydration is limited to NeRF Synthetic Lego and the original
  3DGS Deep Blending Playroom scene. Validate source, license, expected files,
  and reference completeness before benchmarking. Do not substitute Bonsai or
  another scene.
- Keep local iPhone captures as a separate real-capture lane with device,
  thermal, finalization, SfM, and fixed-camera QA evidence.

## Validation

Before proposing a commit, run:

```bash
python -m compileall python scripts tests
PYTHONPATH=python .venv/bin/python -m pytest
git diff --check
```

Also scan public files for private names, credentials, and local absolute paths.
