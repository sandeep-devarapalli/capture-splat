# Quickstart

1. Build the iPhone app from `apps/ios/CaptureSplat` in Xcode.
2. Capture a Video 3DGS Max scan on a physical iPhone.
3. Confirm the bundle is **Ready** in Projects, then share it to the host.
4. Set up COLMAP/HLOC and the external VkSplat backend.
5. Run the resumable evidence pipeline.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,vision]'
scripts/setup_sfm.sh external
scripts/setup_vksplat.sh external/vksplat
capture-splat doctor --vksplat-root external/vksplat

CAPTURE=/path/to/capture_splat_session

capture-splat capture-quality-report \
  --capture "$CAPTURE" \
  --out runs/scan/capture_quality

capture-splat reconstruct \
  --capture "$CAPTURE" \
  --out runs/scan/reconstruction \
  --backend vksplat \
  --backend-root external/vksplat
```

`reconstruct` preserves strict reports for preparation, integrated global SfM,
metric-seed alignment, sensor-supervision availability, the controlled
`3000 -> 7000 -> 15000 -> 30000` ladder, pruning, and World Studio export. It
is resumable with `--resume`. Intent recipes use HLOC retrieval for larger
packages; missing optional HLOC dependencies block explicitly rather than
silently changing the requested method.

Raw render generation remains backend-specific. Once model-only canvases and
their provenance exist, attach them to the same run:

```bash
capture-splat reconstruct \
  --capture "$CAPTURE" \
  --out runs/scan/reconstruction \
  --backend vksplat \
  --backend-root external/vksplat \
  --resume \
  --qa-render-dir runs/scan/render_canvases/step_0030000 \
  --qa-pairs-json runs/scan/render_canvases/camera_pairs.json \
  --qa-provenance-json runs/scan/render_canvases/step_0030000/capture_splat_render_provenance.json
```

Use the individual `prepare-capture`, `sfm`, `build-rgbd-seed`,
`prepare-training-supervision`, `train-vksplat-ladder`, `sanitize-ply`,
`qa-render-source`, and `export-world-studio` commands when debugging one
stage. Use `capture-splat train-vksplat --steps 30000` only for an explicit
single run. A finite PLY or viewer load remains evidence for that gate, not a
high-quality claim.
