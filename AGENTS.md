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
- Treat pinned Spirula built-in SfM as the preferred product candidate only
  after same-input evidence-gated promotion. Keep external HLOC/COLMAP as the
  frozen conformance control and fallback.
- Do not equate built-in Vulkan SfM with zero CPU work or an all-GPU pipeline.
  Bind each stage, selected device, host work, and any Apple/MoltenVK
  bundle-adjustment fallback before making backend or speed claims.
- Keep generated datasets, captures, models, renders, and benchmark results out
  of Git.

## Contracts And Claims

- New manifest fields are additive. Preserve v0.2 World Studio handoff resume
  compatibility when extending v0.3.
- Use strict JSON, relative paths, SHA-256 bindings, finite-number checks, and
  explicit coordinate frames and units.
- Portal diagnostics must bind the declared complete contiguous trajectory,
  accept only bounded normal-tracking crossing brackets, and count SfM image
  membership only after canonical-path plus exact byte parity; write no output
  inside either immutable input package.
- Pin portal inputs and exclusive outputs with descriptor-relative no-follow
  operations, exact physical casing, inode/casefold alias rejection, and
  bounded bytes, records, and retained events. A requested portal never
  bypasses the same unique accepted-crossing gate, and COLMAP image/camera IDs
  plus quaternions must pass their strict text-model contract.
- Directory inspection must stream through a bounded descriptor-relative scan;
  never materialize an unbounded listing. Require physical canonical CLI paths,
  compare paired image sizes before hashing, and cap both each image and the
  combined SfM/prepared parity bytes.
- Bound aggregate open directories, scans, entries, name bytes, and path depth,
  not only each individual listing. Read every published report back exactly
  through its held descriptor and revalidate bound inputs and output through
  context exit, including rejected reports.
- Dataset evidence does not prove a trainer consumed it. A finite PLY, viewer
  load, import, or completed job does not prove visual quality, metric scale,
  collision safety, navigation readiness, or physics authority.
- Use `promote|hold|reject` and retain proposal-only authority until the
  corresponding physical or visual evidence gate passes.

## Benchmark Policy

- The first measured efficiency lane is Apple Silicon. Hold cross-vendor,
  8 GB, and multi-million-Gaussian capacity claims until the named hardware,
  commands, repetitions, raw results, noise, and quality rails are recorded.
- The standard matrix is NeRF Synthetic Lego for deterministic smoke,
  original-3DGS Deep Blending Playroom as the completed real-scene control,
  and complete Mip-NeRF 360 Bonsai `images_2` as the active 360 quality lane.
  Validate source, license, expected files, and reference completeness before
  benchmarking.
- Keep local iPhone captures and Room-01 as a separate capture-to-world lane
  with device, thermal, finalization, SfM, metric, and fixed-camera evidence.
- A held benchmark gates only its named claim or promotion. It does not block
  forming a proposal package or downstream Room-01 metric/collision work that
  passes its own gates.

## Validation

Before proposing a commit, run:

```bash
python -m compileall python scripts tests
PYTHONPATH=python .venv/bin/python -m pytest
git diff --check
```

Also scan public files for private names, credentials, and local absolute paths.
