# iPhone Capture

Capture Splat is designed for physical iPhones. Simulator runs cannot validate camera, LiDAR, motion, or real capture timing.

Use **Video 3DGS** mode for training input. The app records images, timing, camera metadata, optional ARKit pose/depth, and quality reports. It does not train 3DGS on-device.

## Capture-Time Quality Gate

The app accepts smart keyframes instead of exporting every AR frame. A haptic
marks an accepted frame. Accepted frames are chosen from blur/detail,
exposure stability, camera motion rate, ARKit tracking, LiDAR depth coverage,
parallax, overlap, and feature-point support.

Candidates captured while the camera rotates or translates too fast are held
with the `fast_motion` skip reason. This is a motion-blur quality proxy from
ARKit pose deltas, not an image-quality proof. Each saved frame includes
`capture_quality` metadata in `capture.json`, including motion-rate telemetry
(`angular_velocity_deg_s`, `translation_speed_m_s`) so host reports can
separate low-texture holds from fast-motion holds. The host `ingest` and
`colmap-export` commands prefer frames marked accepted and reject a capture if
quality metadata marks every frame rejected.

The capture gate also holds candidates with a large clipped-highlight or
clipped-shadow fraction using the `clipped_exposure` skip reason. Accepted
frames record both fractions in `capture_quality`; treat them as capture-guidance
quality proxies, not image-quality or reconstruction-quality proof.

Candidates can also be held with `weak_feature_distribution` when image-detail
samples are too clustered. Accepted frames record `feature_grid_coverage` as a
lightweight pre-COLMAP proxy for whether useful texture is spread across the
view.

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
