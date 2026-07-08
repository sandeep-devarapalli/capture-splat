# Pipeline

```text
iPhone Video 3DGS capture
  -> capture.json + images + metadata
  -> strict host validation
  -> capture-quality-report gate
  -> Nerfstudio-style transforms.json
  -> COLMAP text package
  -> COLMAP refinement or triangulation as needed
  -> VkSplat/Vulkan training, or optional gsplat/CUDA training on cloud NVIDIA
  -> standard splat.ply
```

External Record3D, Roomly-style, or Nerfstudio-style captures can enter at the
`capture.json` stage when they expose RGB frames and `transforms.json`:

```bash
capture-splat import-transforms \
  --input /path/to/transforms_export \
  --out runs/imported_capture
```

Depth files such as `.exr` or `.npy` are preserved when frame paths are present.
This is a format conversion, not a reconstruction-quality claim.

COLMAP registration and trainer health are quality gates. A successful file export is not the same as a high-quality reconstruction.

## Evidence Gates

A run should not be called high quality just because it produced `splat.ply`.
Use these gates in order:

1. Strict capture/package JSON parse with non-finite values rejected.
2. Capture-time keyframe selection: use accepted frames and keep rejected candidates as diagnostics.
3. Capture quality report: accepted count, blur, parallax, overlap, depth, and skip reasons.
4. COLMAP registration summary: registered images, sparse points, observations, and weak-frame track counts.
5. Trainer finite-output check: `splat.ply` or `point_cloud_<step>.ply` exists, parses, and has `0` non-finite float values.
6. Radius/outlier check before and after any clamp.
7. Viewer/app proof for selected source frames.
8. Raw render canvas versus source-frame quality metrics.
9. Explicit promote/hold/reject decision.

Run the pre-COLMAP gate before exporting packages:

```bash
capture-splat capture-quality-report \
  --capture /path/to/capture \
  --out runs/scan/capture_quality
```

## Recommended Room-Scan Flow

For room interiors, prefer the automated reconstruction path over the raw
ARKit-pose export:

```bash
capture-splat extract-frames --video capture.mov --out runs/scan/frames \
  --target-frames 300            # sharpest-per-window; add --frame-index when
                                 # the app exported metadata/frame_index.jsonl
capture-splat sfm --images runs/scan/frames/images --out runs/scan/colmap_package
# or, to keep ARKit poses as the prior:
capture-splat triangulate --package runs/scan/colmap_package --out runs/scan/triangulate
capture-splat train-gsplat-ladder ... # bilateral grid + random background are
                                      # on by default; rungs compress the full
                                      # schedule via steps_scaler
capture-splat prune-ply --input .../splat.ply
capture-splat export-world-studio --package ... --capture-profile room_interior
```

`sfm` runs COLMAP feature extraction, exhaustive matching by default
(`--matcher sequential` with an optional vocab tree remains available),
mapping, best-model selection, and `model_orientation_aligner`, and gates
the result on registration ratio (reject below 60%, hold below 85%).
GLOMAP is used when installed. CUDA COLMAP is required: `sfm` and
`triangulate` block with `colmap_cuda_missing` when the local COLMAP build
reports `without CUDA`. Exhaustive matching recovers revisit pairs that
sequential matching misses on room orbits, and it is only practical on GPU
builds. Pass `--allow-cpu-matching` to run a deliberate CPU job; the
summary records `cpu_matching_override` so the evidence trail shows it.
`--background-sphere` seeds distant background points for room and outdoor
scenes. Both trainers write `capture_splat_scene_transform.json` next to the
PLY so viewers can map package cameras into the trained splat world; these
are registration and alignment evidence, not quality claims.

## Training Ladder

Short runs are smoke tests. Quality should be judged with controlled ladders,
for example `3000 -> 7000 -> 15000 -> 30000`, using the same package and the
same selected proof frames. If a package regresses at a shorter rung, do not
spend a longer run on it without changing the input package or capture quality.

Run the reusable VkSplat ladder command after COLMAP package creation:

```bash
capture-splat train-vksplat-ladder \
  --package runs/scan/colmap_package \
  --out runs/scan/vksplat_ladder \
  --vksplat-root external/vksplat

# Optional schedule-control probe for long-rung instability:
capture-splat train-vksplat-ladder \
  --package runs/scan/colmap_package \
  --out runs/scan/vksplat_ladder_stop9000 \
  --vksplat-root external/vksplat \
  --stop-reset-at 9000
```

Each rung records the trainer command, step count, output `.ply`, finite PLY
status, splat count, radius/scale summary when present, VkSplat schedule settings such as `--stop-reset-at`, attached render/source
QA if supplied, and a `promote`, `hold`, or `reject` decision. The optional
`capture-splat train-gsplat-ladder` command records the same evidence for gsplat
CUDA runs and writes `capture_splat_gsplat_ladder_summary.json`. A rung with only
finite output is held until render/source QA or other quality evidence supports
promotion.

For any trainer output with isolated non-finite splats, use the explicit repair
path instead of editing files by hand:

```bash
capture-splat sanitize-ply \
  --input runs/scan/vksplat_ladder/step_0003000/run/splat.ply \
  --out runs/scan/vksplat_ladder/step_0003000/run/splat.finite_drop_nonfinite.ply
```

This drops vertices with non-finite numeric properties and writes a strict
`*.sanitize_report.json`. The ladder can do the same repair with
`--sanitize-non-finite-ply`, but the report still records the original rejected
PLY and the sanitized finite candidate separately.

## Raw-Canvas Render QA

Use raw render canvases for image metrics. Do not compare full app or viewer
screenshots because source panes, labels, and UI chrome can dominate the score.
See `docs/viewer_compatibility.md` for the raw-canvas export contract and
explicit pairs JSON format.

```bash
capture-splat qa-render-source \
  --source-dir runs/scan/colmap_package/images \
  --render-dir runs/scan/render_canvases/step_0030000 \
  --out runs/scan/render_qa/step_0030000
```

The report includes per-frame PSNR, SSIM, MAE, normalized correlation,
edge-density, and sharpness proxies, plus weak-frame and tail-frame lists.

If the weak frames are not part of the validation split, run an exact-frame
VkSplat probe. It retrains the configured step count with train renders enabled,
then builds render/source pairs only for the requested cameras:

```bash
capture-splat vksplat-render-probe \
  --package runs/scan/colmap_package \
  --out runs/scan/vksplat_7000_render_probe \
  --vksplat-root external/vksplat \
  --steps 7000 \
  --frames 000033,000065,000076,000086,000164
```

The probe writes `capture_splat_vksplat_render_probe_summary.json`,
`render_source_pairs.json`, and a `render_qa/` summary. Treat the result as
exact-frame diagnostic evidence.

For backend comparisons, first build one shared source-frame list and compare
raw renders from each backend against that same list:

```bash
capture-splat compare-backend-renders \
  --package runs/scan/colmap_package \
  --frames 000001,000017,000025 \
  --gsplat-render-dir runs/scan/renders/gsplat_7000 \
  --vksplat-render-dir runs/scan/renders/vksplat_7000 \
  --out runs/scan/backend_compare_7000
```

If backend render directories are not available, the command writes the shared
frame contract and reports `renderer_missing`. That is a blocked comparison, not
visual-quality evidence.

## World Studio Handoff

Use `capture-splat export-world-studio` when a Capture Splat run should be
opened in World Studio:

```bash
capture-splat export-world-studio \
  --package runs/scan/colmap_package \
  --gaussian runs/scan/vksplat_ladder/step_0007000/splat.ply \
  --transforms runs/scan/ingest/nerfstudio_dataset/transforms.json \
  --out runs/scan/world_studio_package
```

The command writes `capture-splat.world-studio.json` with schema
`capture_splat.world_studio_handoff.v0.1` and relative paths only. It can include
source frames, ordinary `points.ply`, Gaussian `.ply`, `capture.json`,
`transforms.json`, COLMAP sparse text files, and optional `.splat`/`.spz`
references when present. The handoff keeps source frames as visual evidence and
trained splats as review proposals, not metric, collision, semantic, or
navigation authority.

When a rung is finite but render/source QA still holds, diagnose weak and tail
frames before spending a longer run:

```bash
capture-splat qa-weak-frames-report \
  --qa-summary runs/scan/render_qa/step_0030000/capture_splat_render_source_qa_summary.json \
  --colmap-images runs/scan/colmap_package/sparse/0/images.txt \
  --capture /path/to/capture_splat_session \
  --out runs/scan/weak_frames/step_0030000
```

The weak-frame report attaches COLMAP observation support, optional capture
quality proxies, render/source sharpness ratios, possible reason buckets, and a
source/render contact sheet. It is diagnostic evidence, not a quality claim.

Use that report to prepare and run a focused COLMAP repair workspace:

```bash
capture-splat colmap-focused-repair \
  --package runs/scan/colmap_package \
  --weak-report runs/scan/weak_frames/step_0030000/capture_splat_weak_frames_report.json \
  --out runs/scan/colmap_focused_repair
```

`colmap-focused-repair` keeps generated databases, logs, and repaired sparse
files under the requested output directory. It also rewrites the focused sparse
input so COLMAP 4 database image IDs, `frames.txt`, and `rigs.txt` line up with
the selected weak-frame repair images. A blocked repair is a sparse-support
blocker, not a training-quality result.

For weak viewpoint neighborhoods, use a broader bridge pass so the repair keeps
the registered package context:

```bash
capture-splat colmap-focused-repair \
  --package runs/scan/colmap_package \
  --repair-manifest runs/scan/colmap_focused_repair/support_manifest/capture_splat_colmap_support_repair_manifest.json \
  --include-all-registered-images \
  --bridge-ranges 000074-000077,000080-000090 \
  --bridge-window 6 \
  --out runs/scan/colmap_broader_repair
```

Use `--preserve-existing-points` only when the feature database matches the
existing sparse model. If features are re-extracted, old point tracks may not be
consistent with the new database rows, so support delta remains the authority.

After rerunning a targeted COLMAP support repair, compare the original and
repaired `images.txt` files before retraining:

```bash
capture-splat colmap-support-delta \
  --original-images runs/scan/colmap_package/sparse/0/images.txt \
  --repaired-images runs/scan/colmap_repair/sparse/0/images.txt \
  --weak-report runs/scan/weak_frames/step_0030000/capture_splat_weak_frames_report.json \
  --out runs/scan/colmap_repair/support_delta
```

Only treat `proceed_to_training_probe` as permission to run a short training
probe. It records better sparse support, not better 3DGS quality by itself.


## External Backend Candidates

`gsplat` is the first direct-CUDA fallback because its example trainer supports COLMAP input, controlled step counts, disabled viewer/video, and PLY export. Keep it optional and run it on Linux/cloud NVIDIA.

`3DGS.cpp` is a macOS-friendly Vulkan viewer/runtime candidate. It is useful for loading and inspecting produced splats on Mac, but upstream lists training as TODO.

`AndrewBoessen/3DGS` is an experimental CUDA 13 C++ trainer candidate. Do not treat it as production-ready until it passes the same finite PLY and render/source QA gates on Capture Splat packages.
