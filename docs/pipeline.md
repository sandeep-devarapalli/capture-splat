# Pipeline

```text
iPhone Video 3DGS capture
  -> capture.json + images + metadata
  -> strict host validation
  -> capture-quality-report gate
  -> Nerfstudio-style transforms.json
  -> COLMAP text package
  -> COLMAP refinement or triangulation as needed
  -> VkSplat/Vulkan training
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
5. VkSplat finite-output check: `splat.ply` exists, parses, and has `0` non-finite float values.
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

## Training Ladder

Short runs are smoke tests. Quality should be judged with controlled ladders,
for example `3000 -> 7000 -> 15000 -> 30000`, using the same package and the
same selected proof frames. If a package regresses at a shorter rung, do not
spend a longer run on it without changing the input package or capture quality.

Run the reusable ladder command after COLMAP package creation:

```bash
capture-splat train-vksplat-ladder \
  --package runs/scan/colmap_package \
  --out runs/scan/vksplat_ladder \
  --vksplat-root external/vksplat
```

Each rung records the trainer command, step count, output `.ply`, finite PLY
status, splat count, radius/scale summary when present, attached render/source
QA if supplied, and a `promote`, `hold`, or `reject` decision. A rung with only
finite output is held until render/source QA or other quality evidence supports
promotion.

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
