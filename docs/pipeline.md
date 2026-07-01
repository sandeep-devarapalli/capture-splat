# Pipeline

```text
iPhone Video 3DGS capture
  -> capture.json + images + metadata
  -> strict host validation
  -> Nerfstudio-style transforms.json
  -> COLMAP text package
  -> COLMAP refinement or triangulation as needed
  -> VkSplat/Vulkan training
  -> standard splat.ply
```

COLMAP registration and trainer health are quality gates. A successful file export is not the same as a high-quality reconstruction.

## Evidence Gates

A run should not be called high quality just because it produced `splat.ply`.
Use these gates in order:

1. Strict capture/package JSON parse with non-finite values rejected.
2. COLMAP registration summary: registered images, sparse points, observations, and weak-frame track counts.
3. VkSplat finite-output check: `splat.ply` exists, parses, and has `0` non-finite float values.
4. Radius/outlier check before and after any clamp.
5. Viewer/app proof for selected source frames.
6. Raw render canvas versus source-frame quality metrics.
7. Explicit promote/hold/reject decision.

## Training Ladder

Short runs are smoke tests. Quality should be judged with controlled ladders,
for example `3000 -> 7000 -> 15000 -> 30000`, using the same package and the
same selected proof frames. If a package regresses at a shorter rung, do not
spend a longer run on it without changing the input package or capture quality.
