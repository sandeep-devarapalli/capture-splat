# Capture Splat

**iPhone capture to 3DGS with Vulkan/VkSplat.**

Capture Splat is a brand-neutral starter kit for recording iPhone scan/video data and generating standard 3D Gaussian Splatting `.ply` files. It includes:

- an iPhone app, **Capture Splat**, for guided Video 3DGS capture;
- a Python host pipeline for capture validation, image package creation, and COLMAP text export;
- a VkSplat/Vulkan training wrapper for macOS, Linux, Windows, and cloud GPU workflows.

The output is a standard 3DGS `.ply` that can be inspected in compatible viewers such as SuperSplat, PlayCanvas-based viewers, Spark-compatible viewers, or other Gaussian viewers.

## What This Is

This repo helps you go from:

```text
iPhone capture folder -> COLMAP/VkSplat package -> trained splat.ply
```

It is not a guarantee that every scan becomes high quality. Good splats still depend on sharp frames, strong overlap, enough parallax, stable exposure, COLMAP registration, and finite trainer output.

For the current carry-forward lessons from the iPhone-to-VkSplat validation ladder, see `docs/field_validation_learnings.md`.

## Quickstart: Mac + iPhone

```bash
git clone https://github.com/sandeep-devarapalli/capture-splat.git
cd capture-splat
scripts/setup_macos.sh
scripts/setup_vksplat.sh external/vksplat
```

Open `apps/ios/CaptureSplat/CaptureSplat.xcodeproj` in Xcode, set your signing team, run on a physical iPhone, choose **Video 3DGS**, record a slow overlapping scan, and export the capture folder to your computer.

Then run:

```bash
. .venv/bin/activate
CAPTURE=/path/to/exported/capture_splat_session
capture-splat doctor --vksplat-root external/vksplat
capture-splat ingest --capture "$CAPTURE" --out runs/my_scan
capture-splat colmap-export --capture "$CAPTURE" --out runs/my_scan/colmap_package
capture-splat train-vksplat-ladder   --package runs/my_scan/colmap_package   --out runs/my_scan/vksplat_ladder   --vksplat-root external/vksplat
```

The ladder runs controlled `3000 -> 7000 -> 15000 -> 30000` rungs and writes
`capture_splat_vksplat_ladder_summary.json`. Single-step training is still
available with `capture-splat train-vksplat --steps 30000`, but a finite `.ply`
is only validated finite output, not a visual-quality claim.

If you have raw rendered canvases from a viewer or app, compare them against the
source images instead of full UI screenshots:

```bash
capture-splat qa-render-source \
  --source-dir runs/my_scan/colmap_package/images \
  --render-dir runs/my_scan/render_canvases/step_0030000 \
  --out runs/my_scan/render_qa/step_0030000
```

## Linux, Windows, And Cloud GPUs

The iPhone app must be built with Apple tooling, but once you have an exported capture folder, the processing side is intended to work on macOS, Linux, Windows, and cloud NVIDIA machines.

- Linux: see `docs/linux_setup.md`.
- Windows: see `docs/windows_setup.md`.
- Cloud NVIDIA: see `docs/cloud_gpu_setup.md` and `docker/Dockerfile.linux-nvidia`.

## Capture Tips

- Move slowly and keep the subject visible.
- Prefer bright, even lighting.
- Avoid motion blur and rolling-shutter sweeps.
- Capture overlapping views and close the loop for room scans.
- For objects, orbit around the object and include slightly elevated/lower views.
- Treat warnings from the app and host QA as real quality blockers.

## License

Apache-2.0. See `THIRD_PARTY.md` for external tool licenses.
