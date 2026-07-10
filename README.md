# Capture Splat

**iPhone capture to 3DGS with Vulkan/VkSplat, plus optional CUDA evaluation.**

Capture Splat is a brand-neutral starter kit for recording iPhone scan/video data and generating standard 3D Gaussian Splatting `.ply` files. It includes:

- an iPhone app, **Capture Splat**, for guided Video 3DGS capture;
- a Python host pipeline for capture validation, image package creation, and COLMAP text export;
- a VkSplat/Vulkan training wrapper for macOS, Linux, Windows, and cloud GPU workflows;
- an optional gsplat/CUDA wrapper for cloud NVIDIA fallback when Vulkan is unavailable.

The output is a standard 3DGS `.ply` that can be inspected in compatible viewers such as SuperSplat, PlayCanvas-based viewers, Spark-compatible viewers, or other Gaussian viewers.

## What This Is

This repo helps you go from:

```text
iPhone capture folder -> COLMAP package -> VkSplat or optional gsplat trainer -> trained splat.ply
```

It is not a guarantee that every scan becomes high quality. Good splats still depend on sharp frames, strong overlap, enough parallax, stable exposure, COLMAP registration, and finite trainer output.

For the current carry-forward lessons from the iPhone-to-VkSplat validation ladder, see `docs/field_validation_learnings.md`.

## Quickstart: Mac + iPhone

```bash
git clone https://github.com/sandeep-devarapalli/capture-splat.git
cd capture-splat
scripts/setup_macos.sh
scripts/setup_vksplat.sh external/vksplat
# Optional CUDA backend for Linux/cloud NVIDIA machines:
# scripts/setup_gsplat.sh external/gsplat
```

Open `apps/ios/CaptureSplat/CaptureSplat.xcodeproj` in Xcode, set your signing team, run on a physical iPhone, choose **Video 3DGS**, record a slow overlapping scan, and export the capture folder to your computer.

Resolve the capture intent into a host-side processing recipe before starting
SfM or training:

```bash
capture-splat plan-reconstruction \
  --capture /path/to/capture_splat_export \
  --out runs/my_capture/plan
```

The strict plan records frame budget, matching strategy, mask/seed policy,
training ladder, viewer preset, missing assets, and `ready|hold`. It is an
execution plan, not a reconstruction-quality claim.

For room scans, the iPhone app also has a Room Plan review path on supported LiDAR iPhones. It can export `room_plan/room.usdz` plus a conservative layout report as capture guidance, not as 3DGS quality proof.

Then run:

```bash
. .venv/bin/activate
CAPTURE=/path/to/exported/capture_splat_session
capture-splat doctor --vksplat-root external/vksplat
capture-splat ingest --capture "$CAPTURE" --out runs/my_scan
capture-splat colmap-export --capture "$CAPTURE" --out runs/my_scan/colmap_package
capture-splat train-vksplat-ladder   --package runs/my_scan/colmap_package   --out runs/my_scan/vksplat_ladder   --vksplat-root external/vksplat
# For long rungs that show late reset instability, record a controlled schedule:
# capture-splat train-vksplat-ladder --package runs/my_scan/colmap_package --out runs/my_scan/vksplat_ladder_stop9000 --vksplat-root external/vksplat --stop-reset-at 9000
```

For Record3D, Roomly-style, or Nerfstudio-style exports that already provide
`transforms.json`, RGB frames, and optional depth frames, first convert them into
a Capture Splat package:

```bash
capture-splat import-transforms \
  --input /path/to/transforms_export \
  --out runs/imported_capture
```

The VkSplat ladder runs controlled `3000 -> 7000 -> 15000 -> 30000` rungs and writes
`capture_splat_vksplat_ladder_summary.json`. The optional `--stop-reset-at` flag records a VkSplat schedule cutoff for opacity resets, useful when longer rungs show late-reset instability; it is a controlled training setting, not a quality claim by itself. On CUDA cloud machines, `capture-splat train-gsplat-ladder` can run the same conservative ladder through gsplat and writes `capture_splat_gsplat_ladder_summary.json`. Single-step training is still
available with `capture-splat train-vksplat --steps 30000`, but a finite `.ply`
is only validated finite output, not a visual-quality claim. If a trainer writes
a `.ply` with a few non-finite splats, `capture-splat sanitize-ply` can write a
strict report and a finite copy that drops only non-finite vertex rows. The
ladder only uses that repair when `--sanitize-non-finite-ply` is set.

If you have raw rendered canvases from a viewer or app, compare them against the
source images instead of full UI screenshots:

```bash
capture-splat qa-render-source \
  --source-dir runs/my_scan/colmap_package/images \
  --render-dir runs/my_scan/render_canvases/step_0030000 \
  --out runs/my_scan/render_qa/step_0030000

capture-splat qa-weak-frames-report \
  --qa-summary runs/my_scan/render_qa/step_0030000/capture_splat_render_source_qa_summary.json \
  --colmap-images runs/my_scan/colmap_package/sparse/0/images.txt \
  --out runs/my_scan/weak_frames/step_0030000
```

For exact-frame VkSplat diagnosis, rerun the same step count with train renders
enabled and QA only the requested cameras:

```bash
capture-splat vksplat-render-probe \
  --package runs/my_scan/colmap_package \
  --out runs/my_scan/vksplat_7000_render_probe \
  --vksplat-root external/vksplat \
  --steps 7000 \
  --frames 000033,000065,000076,000086,000164
```

This is useful when weak frames were part of the train split and therefore were
not present in the normal validation renders. The output remains diagnostic
render/source evidence, not a high-quality claim.

Use the weak-frame report to build and optionally run a focused COLMAP repair
workspace before retraining:

```bash
capture-splat colmap-focused-repair \
  --package runs/my_scan/colmap_package \
  --weak-report runs/my_scan/weak_frames/step_0030000/capture_splat_weak_frames_report.json \
  --out runs/my_scan/colmap_focused_repair
```

The command writes a focused image/pair plan, runs COLMAP when available, and
rewrites the focused sparse input so COLMAP 4 database image IDs, `frames.txt`,
and `rigs.txt` are aligned. If it reports `blocked`, fix the COLMAP support
package before treating longer training as more than an experiment.

For weak viewpoint neighborhoods, keep the registered package broad and add
bridge pairs instead of training on a tiny subset:

```bash
capture-splat colmap-focused-repair \
  --package runs/my_scan/colmap_package \
  --repair-manifest runs/my_scan/colmap_focused_repair/support_manifest/capture_splat_colmap_support_repair_manifest.json \
  --include-all-registered-images \
  --bridge-ranges 000074-000077,000080-000090 \
  --bridge-window 6 \
  --out runs/my_scan/colmap_broader_repair
```

`--preserve-existing-points` is available only when the database feature rows
match the existing sparse model; if COLMAP re-extracts features, use support
delta to verify the result instead of assuming old tracks were preserved.

After a targeted COLMAP repair pass completes, compare support before
retraining:

```bash
capture-splat colmap-support-delta \
  --original-images runs/my_scan/colmap_package/sparse/0/images.txt \
  --repaired-images runs/my_scan/colmap_repair/sparse/0/images.txt \
  --weak-report runs/my_scan/weak_frames/step_0030000/capture_splat_weak_frames_report.json \
  --out runs/my_scan/colmap_repair/support_delta
```

`proceed_to_training_probe` means the sparse support improved enough to justify
a short 3000-step probe. It is not a quality claim.

To compare two backend outputs, use one explicit source-frame list and raw
renders from each backend:

```bash
capture-splat compare-backend-renders \
  --package runs/my_scan/colmap_package \
  --frames 000001,000017,000025 \
  --gsplat-ply runs/my_scan/gsplat_ladder/step_0007000/ply/point_cloud_6999.ply \
  --vksplat-ply runs/my_scan/vksplat_ladder/step_0007000/splat.ply \
  --gsplat-render-dir runs/my_scan/renders/gsplat_7000 \
  --vksplat-render-dir runs/my_scan/renders/vksplat_7000 \
  --out runs/my_scan/backend_compare_7000
```

If render directories are omitted, the command still writes the shared
`camera_pairs.json` and reports `renderer_missing`; that is a setup blocker, not
a quality result.

To hand a run to World Studio, write a local package with relative references
and conservative authority metadata:

```bash
capture-splat export-world-studio \
  --package runs/my_scan/colmap_package \
  --gaussian runs/my_scan/vksplat_ladder/step_0007000/splat.ply \
  --transforms runs/my_scan/ingest/nerfstudio_dataset/transforms.json \
  --out runs/my_scan/world_studio_package
```

This writes `capture-splat.world-studio.json` with schema
`capture_splat.world_studio_handoff.v0.1`. Source frames are visual evidence;
trained splats are review proposals, not metric, collision, semantic, or
navigation authority.

## Linux, Windows, And Cloud GPUs

The iPhone app must be built with Apple tooling, but once you have an exported capture folder, the processing side is intended to work on macOS, Linux, Windows, and cloud NVIDIA machines.

- Linux: see `docs/linux_setup.md`.
- Windows: see `docs/windows_setup.md`.
- Cloud NVIDIA: see `docs/cloud_gpu_setup.md` and `docker/Dockerfile.linux-nvidia`.
- App comparisons: see `docs/app_comparison.md`.

## Optional Backends

VkSplat/Vulkan remains the default baseline because it is cross-platform in principle and keeps Capture Splat independent of CUDA. If a cloud image exposes CUDA but not a usable Vulkan device, `gsplat` is the preferred direct-CUDA fallback:

```bash
scripts/setup_gsplat.sh external/gsplat
capture-splat doctor --gsplat-root external/gsplat
capture-splat train-gsplat-ladder \
  --package runs/my_scan/colmap_package \
  --out runs/my_scan/gsplat_ladder \
  --gsplat-root external/gsplat
```

`scripts/setup_external_3dgs_candidates.sh` can clone 3DGS.cpp and AndrewBoessen/3DGS into `external/` for evaluation. 3DGS.cpp is useful for macOS/Vulkan viewer-runtime checks; upstream lists training as TODO. AndrewBoessen/3DGS is a CUDA 13 C++ candidate and is not a default backend.

## Capture Tips

- Move slowly and keep the subject visible.
- Prefer bright, even lighting.
- Avoid motion blur and rolling-shutter sweeps.
- Capture overlapping views and close the loop for room scans.
- Use Room Plan for room-layout guidance when supported, then still validate capture quality and COLMAP/VkSplat evidence on the host.
- For objects, orbit around the object and include slightly elevated/lower views.
- Treat warnings from the app and host QA as real quality blockers.

## License

Apache-2.0. See `THIRD_PARTY.md` for external tool licenses.
