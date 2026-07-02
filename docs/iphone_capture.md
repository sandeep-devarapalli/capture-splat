# iPhone Capture

Capture Splat is designed for physical iPhones. Simulator runs cannot validate camera, LiDAR, motion, or real capture timing.

Use **Video 3DGS** mode for training input. The app records images, timing, camera metadata, optional ARKit pose/depth, and quality reports. It does not train 3DGS on-device.

## Capture-Time Quality Gate

The app accepts smart keyframes instead of exporting every AR frame. A haptic
marks an accepted frame. Accepted frames are chosen from blur/detail,
exposure stability, ARKit tracking, LiDAR depth coverage, parallax, overlap, and
feature-point support.

Each saved frame includes `capture_quality` metadata in `capture.json`. The host
`ingest` and `colmap-export` commands prefer frames marked accepted and reject a
capture if quality metadata marks every frame rejected.

Before COLMAP or VkSplat, run:

```bash
capture-splat capture-quality-report \
  --capture /path/to/capture \
  --out runs/scan/capture_quality
```

Use the report as a pre-training gate. `promote` means the capture is reasonable
to try with COLMAP; `hold` means inspect weak signals first; `reject` means
recapture before training.

For rooms, move in small connected side steps around the perimeter. Keep the
previous wall, corner, table edge, shelf, or textured object in view while adding
translation. Avoid fast pans, exposure jumps, blank walls, glass, and stopping
after only one height band.
