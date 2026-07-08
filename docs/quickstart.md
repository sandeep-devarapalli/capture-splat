# Quickstart

1. Build the iPhone app from `apps/ios/CaptureSplat` in Xcode.
2. Capture a Video 3DGS scan on a physical iPhone.
3. Export the capture folder to your host machine.
4. Set up the host environment.
5. Validate, ingest, export COLMAP text, and train with VkSplat.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,vision]'
scripts/setup_vksplat.sh external/vksplat
capture-splat doctor --vksplat-root external/vksplat
capture-splat ingest --capture /path/to/capture --out runs/scan
capture-splat colmap-export --capture /path/to/capture --out runs/scan/colmap_package
capture-splat train-vksplat-ladder --package runs/scan/colmap_package --out runs/scan/vksplat_ladder --vksplat-root external/vksplat
capture-splat qa-render-source --source-dir runs/scan/colmap_package/images --render-dir runs/scan/render_canvases/step_0030000 --out runs/scan/render_qa/step_0030000
```

Use `capture-splat train-vksplat --steps 30000` only when you need one explicit
trainer run. If long rungs show late-reset instability, repeat the ladder with
`--stop-reset-at 9000` and compare the same raw-canvas QA frames. Use the ladder
summary and raw-canvas QA reports before treating a run as promoted rather than
merely finite.
