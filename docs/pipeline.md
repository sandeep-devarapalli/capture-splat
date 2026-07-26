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

Before extracting frames, resolve the iPhone capture intent into one public
host recipe:

```bash
capture-splat plan-reconstruction \
  --capture /path/to/capture \
  --out runs/scan/plan
```

Desk/Object use a 300-frame retrieval-SfM target; Room/Semantic Room,
Corridor, and Outdoor use 450; Detail Repair uses 180 with exhaustive
matching. Missing continuous video or its frame index produces `hold` while
preserving accepted RGB-D keyframes as evidence.

Turn the plan into a reusable, non-destructive frame package with:

```bash
capture-splat prepare-capture \
  --capture /path/to/capture \
  --out runs/scan/prepared
```

The command writes plan and capture-quality reports beside
`frames/capture.json` and `frames/images/`. Accepted RGB-D frames take
precedence over video supplements. New iPhone frame indexes carry both the
video-relative timestamp and the AR-session timestamp, so cross-source
duplicates can be removed without guessing clock offsets. Older indexes remain
readable but report that cross-source deduplication is unavailable.

For Object Orbit packages, the prepared white-valid masks can produce a
separate premultiplied RGBA image set before SfM or training:

```bash
capture-splat remove-background \
  --images runs/scan/prepared/frames/images \
  --mask-dir runs/scan/prepared/frames/masks/valid \
  --out runs/scan/background_removed
```

The default `auto` mode uses complete Capture Splat masks first. Optional
InSPyReNet matting is available through `pip install -e '.[matting]'` and
`--mode inspyrenet`; it is never a mandatory dependency or a fallback that
runs silently. Derived alpha and RGB values are proposals, while the original
capture images remain the source evidence.

To omit accepted frames that a post-capture diagnostic has held or rejected,
pass `--frame-exclusions` with a strict
`capture_splat.frame_exclusions.v0.1` JSON manifest. The source capture remains
untouched, and the prepared package records the applied one-based source-frame
indices and reason.

For viewer-only cleanup, `prune-ply` can combine its alpha threshold with an
optional `--max-radius` in trainer-scene units. This writes a separate PLY and
strict report; it does not modify the trained PLY or improve reconstruction
quality.

For a resumable end-to-end run, use:

```bash
capture-splat reconstruct \
  --capture /path/to/capture \
  --out runs/scan/reconstruction \
  --backend vksplat \
  --backend-root external/vksplat
```

The command writes one strict top-level summary while retaining every stage
summary under numbered directories. `--dry-run` plans without invoking SfM or
training, `--stop-after sfm` (or another named stage) bounds a probe, and
`--resume` reuses completed summaries. A held RGB-D fit continues with the
unaugmented COLMAP package. Missing fixed-camera raw renders skip QA and keep
the final decision at `hold`; they are not inferred from full viewer screens.
Raw-render QA promotion also requires `capture_splat_render_provenance.json`
beside the renders. Its `gaussian_checksum` must match the selected pruned PLY,
so images from another rung or reconstruction cannot promote the current run.
Resume hashes both source and render image sets and verifies every copied file
declared by the World Studio handoff. A rejected/partially written stage is not
rewritten in place; fix the blocker and use a new output directory.

External Record3D, Roomly-style, or Nerfstudio-style captures can enter at the
`capture.json` stage when they expose RGB frames and `transforms.json`:

```bash
capture-splat import-transforms \
  --input /path/to/transforms_export \
  --out runs/imported_capture
```

Depth files such as `.exr` or `.npy` are preserved when frame paths are present.
This is a format conversion, not a reconstruction-quality claim.

Equirectangular inputs use an image-stage importer:

```bash
capture-splat import-360 \
  --input /path/to/panorama_or_video \
  --out runs/imported_360
```

It preserves source panoramas, emits six equatorial plus four upper and four
lower perspective views per panorama, writes matching white-valid masks, and
records intrinsics plus virtual-camera rotations in
`metadata/equirectangular_rig.json`. The importer does not invent translations
or world poses and therefore does not emit `capture.json`. Use the output as
projection evidence while rig-constrained SfM remains pending.

Finite Gaussian PLYs can be packaged for optional web delivery with the
external `splat-transform` CLI:

```bash
capture-splat export-spz --input /path/to/splat.ply --out runs/scene.spz
```

The strict report checks the SPZ v4 header and mandatory PLY round trip,
including count, finite values, sampled coordinate error relative to scene
diagonal, and sampled base-color error. The default decision is `hold`.
Promotion for distribution additionally requires a
`capture_splat.spz_viewer_evidence.v0.1` file bound to the SPZ checksum with
passing viewer load, orientation, color, and source-camera-alignment checks.
This is a distribution gate, not a reconstruction-quality or metric-authority
claim.

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
capture-splat prepare-capture --capture /path/to/capture --out runs/scan/prepared
capture-splat sfm --images runs/scan/prepared/frames/images \
  --out runs/scan/colmap_package --method global \
  --features hloc --matcher retrieval
# Optional metric initialization after refined cameras exist:
capture-splat build-rgbd-seed \
  --capture runs/scan/prepared/frames \
  --package runs/scan/colmap_package \
  --out runs/scan/rgbd_seed
# or, to keep ARKit poses as the prior:
capture-splat triangulate --package runs/scan/colmap_package --out runs/scan/triangulate
capture-splat train-gsplat-ladder ... # bilateral grid + random background are
                                      # on by default when capabilities pass;
                                      # rungs compress the full schedule
capture-splat prune-ply --input .../splat.ply
capture-splat export-world-studio --package ... --capture-profile room_interior
```

`sfm` runs COLMAP feature extraction, exhaustive matching by default
(`--matcher sequential` with an optional vocab tree remains available), the
integrated `global_mapper`, best-model selection, and
`model_orientation_aligner`. It gates the result on registration ratio (reject
below 60%, hold below 85%). Standalone GLOMAP remains available through
`--method glomap`; incremental COLMAP is `--method incremental`, while the old
`--method colmap` spelling is a deprecated incremental alias. CUDA COLMAP is
required: `sfm` and
`triangulate` block with `colmap_cuda_missing` when the local COLMAP build
reports `without CUDA`. Exhaustive matching recovers revisit pairs that
sequential matching misses on room orbits, and it is only practical on GPU
builds. Pass `--allow-cpu-matching` to run a deliberate CPU job; the
summary records `cpu_matching_override` so the evidence trail shows it.
Prepared packages above 250 frames request the optional HLOC retrieval frontend:
NetVLAD top-32 retrieval, ALIKED-N16 features, LightGlue matches, and COLMAP
geometric verification before GLOMAP/COLMAP mapping. Install it through
`scripts/setup_sfm.sh`; `hloc_missing` blocks that requested route rather than
silently changing the experiment to exhaustive SIFT.

Prepared Capture Splat packages carry per-frame ARKit intrinsics into separate
COLMAP cameras. `--camera-policy auto` selects that path only for a
`capture_splat.prepare_capture` manifest with complete finite intrinsics;
generic image folders use the single-camera fallback and report it. Imported
OPENCV distortion values are passed through explicitly rather than replaced by
zeros. `--view-graph-calibration auto` skips calibration for complete prepared
ARKit priors and otherwise modifies a copied database before global mapping.

Canonical feature/training masks are white where pixels are valid. Room masks
are full-frame minus available people/dynamic evidence. Desk/object masks
require object support and intersect it with inverse person evidence. Auto mode
disables an incomplete mask set; `--masks required` blocks. SIFT receives
COLMAP masks, while HLOC filters keypoints and descriptors before LightGlue
matching and rejects required masks with missing files or wrong dimensions.

`--post-ba-backend caspar` is an explicit post-global experiment. It blocks
unless COLMAP exposes the Caspar options and the result uses only `PINHOLE` or
`SIMPLE_RADIAL`. Caspar is not the global solver and does not use ARKit pose
priors in this path.
`build-rgbd-seed` estimates a Sim(3) from shared ARKit and COLMAP camera
centers and proceeds only when median and tail residual gates pass. It applies
the recorded depth-unit scale and adapts camera intrinsics when the depth grid
differs from the stored intrinsics resolution. When the capture also asserts
`arkit_vio_metric` scale authority, the copied COLMAP camera translations,
sparse points, and RGB-D seed are written in meters with a checksum-bound
`metadata/metric_scale_report.json`. Older captures without explicit scale
authority retain compatible seed augmentation in COLMAP units and report that
metric continuity is unavailable. The command keeps an unmodified sparse-model
backup. A failed fit is held and training can continue from the original
COLMAP package; ARKit depth is a metric prior, not a substitute for
COLMAP-refined image support.

Package scale and trainer scale are reported independently. All trainer
commands accept `--normalization auto|on|off`. Auto mode preserves meter-native
coordinates only when the metric report is accepted, its output checksums
match the current sparse model, and the selected trainer exposes a real
normalization-disable option. Current gsplat trainers can provide that option.
Current VkSplat normalizes internally and therefore records a limitation in
auto mode; an explicit `off` request blocks. A metric input package alone does
not establish that a trained PLY is in meters.

`--background-sphere` seeds distant background points for room and outdoor
scenes. Both trainers write `capture_splat_scene_transform.json` next to the
PLY so viewers can map package cameras into the trained splat world. New SfM
runs also fit and record `metadata/package_orientation_transform.json` from
matched camera centers before and after COLMAP orientation alignment. The
scene sidecar keeps that pre-alignment-to-package transform separate from the
package-to-trainer transform and records camera-center residuals. These are
registration and alignment evidence, not quality claims.

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

VkSplat consumes `masks/valid` when its installed trainer exposes `mask_dir`.
gsplat capability probing accepts modern
`post_processing=bilateral_grid|ppisp` and the legacy bilateral-grid flag.
iPhone packages default to bilateral grid; PPISP is experimental. Required
masks or photometric modes block when unsupported instead of silently changing
the training recipe. Every completed SfM package writes
`metadata/fixed_camera_evaluation_set.json`; backend comparisons should render
that same set.

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
When these renders are attached to `capture-splat reconstruct`, add a strict
provenance sidecar:

```json
{
  "schema": "capture_splat.render_provenance.v0.1",
  "gaussian_checksum": "sha256:<checksum-of-the-rendered-ply>"
}
```

This binds the image metrics to one Gaussian artifact. It does not make that
artifact metric, collision, semantic, or navigation authority.

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

For backend comparisons, use the deterministic fixed-camera set emitted by SfM
and compare raw renders from each backend against that same list:

```bash
capture-splat compare-backend-renders \
  --package runs/scan/colmap_package \
  --gsplat-render-dir runs/scan/renders/gsplat_7000 \
  --vksplat-render-dir runs/scan/renders/vksplat_7000 \
  --out runs/scan/backend_compare_7000
```

The command blocks if `metadata/fixed_camera_evaluation_set.json` is missing or
an explicit frame list differs from it. If backend render directories are not
available, the command writes the shared
frame contract and reports `renderer_missing`. That is a blocked comparison, not
visual-quality evidence.

## World Studio Handoff

Use `capture-splat export-world-studio` when a Capture Splat run should be
opened in World Studio:

```bash
capture-splat export-world-studio \
  --package runs/scan/colmap_package \
  --gaussian runs/scan/vksplat_ladder/step_0007000/splat.ply \
  --render-source-qa runs/scan/render_qa/step_0007000/capture_splat_render_source_qa_summary.json \
  --capture-manifest captures/scan/capture.json \
  --transforms runs/scan/ingest/nerfstudio_dataset/transforms.json \
  --out runs/scan/world_studio_package
```

The command writes `capture-splat.world-studio.json` with schema
`capture_splat.world_studio_handoff.v0.2` and relative paths only. It can include
source frames, ordinary `points.ply`, Gaussian `.ply`, `capture.json`,
`transforms.json`, COLMAP sparse text files, and optional `.splat`/`.spz`
references when present. The handoff keeps source frames as visual evidence and
trained splats as review proposals, not metric, collision, semantic, or
navigation authority.

For a Gaussian PLY, the exporter always writes
`quality/ply_stats.json` from the exact packaged PLY. With
`--render-source-qa`, it validates and includes the strict
`capture_splat.render_source_qa.v0.1` summary as
`quality/render_source_qa.json`. These sidecars support `promote|hold|reject`
review decisions; they do not establish high quality or metric authority.

When `--capture-manifest` points to an iPhone `capture.json`, the exporter also
copies the available ARKit navigation mesh, mesh report, RoomPlan semantic
proposal, and continuous camera trajectory. It estimates an
`arkit_world -> colmap_world` Sim(3) from matched camera centers and composes it
with the trainer transform. The strict registration report records matched
cameras, residuals, units, scale conversion, and `accepted|held|unavailable`.
Walk is only marked eligible when a navigation mesh exists and registration is
accepted. This is interaction eligibility from capture evidence, not validated
collision or navigation authority.

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
