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
capture-splat train-vksplat --package runs/scan/colmap_package --out runs/scan/vksplat --vksplat-root external/vksplat --steps 30000
```
