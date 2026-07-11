# iPhone Capture

Capture Splat is designed for physical iPhones. Simulator runs cannot validate camera, LiDAR, motion, or real capture timing.

For Xcode breakpoints, log classification, and the required two-cycle
record/finalize smoke, see [iPhone Xcode Diagnostics](iphone_xcode_diagnostics.md).

Use **Video 3DGS Max** for training input. The app records quality-gated RGB-D
keyframes plus a continuous HEVC video and timestamped ARKit pose/intrinsics
index. It does not train 3DGS on-device.

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

Each indexed frame also records achieved white-balance gains, lens position,
exposure/focus/white-balance adjustment states, pixel-buffer color primaries,
transfer function, YCbCr matrix, pixel format, and projection/calibration
availability. Preparation preserves these values next to the frame used by
SfM. They support exposure clustering and camera diagnostics; they do not make
the camera radiometrically calibrated.

Candidates can also be held with `weak_feature_distribution` when image-detail
samples are too clustered. Accepted frames record `feature_grid_coverage` as a
lightweight pre-COLMAP proxy for whether useful texture is spread across the
view.

For Desk / Cluster, Object Orbit, and Detail Repair intents, keep the subject
centered briefly before pressing **Lock & Record**. The control stays held until
the app has three consistent LiDAR center-depth observations within 0.6 seconds,
then auto-locks the subject
and records target-relative azimuth, elevation, and distance coverage. The
visible target control can reset a stale lock. This is capture guidance, not
object identity or metric-geometry authority.

Stopping enters a short Finalizing state. Export and Share remain disabled
until the continuous video writer and queued RGB-D files have settled.
`metadata/finalization_report.json` records success or partial failure;
`capture_policy.json` and `sensor_capabilities.json` preserve the active gates
and supported optional sensors. Partial artifacts are retained when
finalization fails.

The app labels incomplete folders separately from finalized capture bundles.
Normal Export/Share becomes available only after `capture.json` is written;
recoverable partial data is explicitly shared as **Share Partial**. GNSS is off
for indoor intents and requested only when an Outdoor capture starts, so the
optional location permission does not cover the first Desk-capture screen.

On supported LiDAR iPhones, the quality-first configuration additionally
records non-empty ARKit person stencils under `masks/person/` at no more than
5 Hz and writes `metadata/person_mask_index.jsonl`. A separate classified ARKit
mesh is capped at 200,000 vertices and 300,000 triangles and exported as
`geometry/arkit_mesh.ply` with `arkit_mesh_report.json`. Tracking, thermal,
camera-lock, loop, fallback, and finalization transitions are written to
`metadata/session_events.jsonl`. Mask or mesh pressure never relaxes the RGB-D
quality gate; optional writes are dropped or held and reported instead.

After capture, the Projects tab shows a lightweight review summary with kept and
held keyframe counts, current coverage sectors, and the latest blocker detail.
It also opens a native LiDAR preview backed by
`pointcloud_preview/preview.json`, a capped set of sampled RGB-colored depth
points from accepted keyframes. Use it as capture guidance evidence; full mesh
or splat quality still needs host validation.

Room mode also includes a **Room Plan** review surface on supported LiDAR
iPhones. It opens Apple's RoomPlan scanner so you can inspect wall, opening,
floor, and large-object layout while capturing a room. Stopping the Room Plan
scan writes `room_plan/room.usdz` and `room_plan/room_plan_report.json` in the
current capture folder when available. It also writes
`room_plan/room_semantics.json` with conservative room-element proposals. Treat this as room-layout guidance and
scale/context evidence only; it is not COLMAP registration proof, collision
geometry, or a 3DGS quality claim.

Before COLMAP or VkSplat, run:

```bash
capture-splat capture-quality-report \
  --capture /path/to/capture \
  --out runs/scan/capture_quality
```

Use the report as a pre-training gate. `promote` means the capture is reasonable
to try with COLMAP; `hold` means inspect weak signals first; `reject` means
recapture before training.

`prepare-capture` also writes strict camera and photometric evidence reports and
canonical white-valid masks. ARKit per-frame pinhole intrinsics remain priors;
Capture Splat records that distortion is unavailable instead of inventing
coefficients. COLMAP-refined cameras remain the visual reconstruction baseline.

For rooms, move in small connected side steps around the perimeter. Keep the
previous wall, corner, table edge, shelf, or textured object in view while adding
translation. Avoid fast pans, exposure jumps, blank walls, glass, and stopping
after only one height band.
