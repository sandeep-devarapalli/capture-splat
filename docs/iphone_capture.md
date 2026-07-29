# iPhone Capture

Capture Splat is designed for physical iPhones. Simulator runs cannot validate camera, LiDAR, motion, or real capture timing.

For Xcode breakpoints, log classification, and the required two-cycle
record/finalize smoke, see [iPhone Xcode Diagnostics](iphone_xcode_diagnostics.md).

Use **Video 3DGS Max** for training input. The app records quality-gated RGB-D
keyframes plus a continuous HEVC video and timestamped ARKit pose/intrinsics
index. It does not train 3DGS on-device.

The capture view defaults to **Guidance**. On supported LiDAR iPhones it shows
RealityKit scene-understanding wireframe plus a gravity-aligned map of the
camera trail, observed surface cells, accepted-keyframe coverage, and heading.
Switch to **Camera** to hide these inspection overlays without changing the
capture gate. At serious thermal state the live mesh is hidden and the map
remains available. At critical thermal state surface guidance is paused while
blockers, recording, and export continue. The capture overlay states when
thermal protection hides the mesh; this does not mean ARKit mesh accumulation
or RGB-D recording stopped.

Without LiDAR, Guidance degrades to detected planes, feature points, and camera
trajectory where ARKit provides them. This is not dense RGB-only surface
reconstruction, and the normal Capture Splat RGB-D package still requires scene
depth. Mirrors, glass, distant surfaces, harsh sunlight, and moving objects can
also leave LiDAR coverage incomplete.

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
It separately measures pixels near white at the 95% luminance level and uses
`near_clipped_highlights` to hold a view when most of the image is nearly
white. This catches broad window-driven washout without rejecting every frame
that merely contains a bright window.

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

Smart quality-gated keyframes are mandatory in Video 3DGS Max. The app does
not expose a timed/fixed-interval fallback: more frames are useful only when
they retain blur, exposure, tracking, overlap, parallax, and feature-support
evidence. Rejected candidates remain telemetry and are not trainer input.

Desk / Cluster and Detail Repair are full-scene captures. They start without a
single-point subject lock so a nearby bottle, keyboard, or other small item does
not accidentally define the whole reconstruction mask. Preparation keeps the
full static frame, subtracting a person mask when one is available.

Object Orbit uses the stricter subject flow. Keep the object centered briefly
until **Lock Subject** becomes available, tap it explicitly, and then press
**Record**. Record never acquires a target lock. The lock action requires three
consistent LiDAR center-depth observations within 0.6 seconds and records
target-relative azimuth, elevation, and distance coverage. The visible target
control can reset a stale lock. Readiness requires low, middle, and high-angle
support. The saved object depth band follows the target's projected optical
depth per frame instead of remaining fixed at the initial lock distance. This
is capture guidance, not object identity or metric-geometry authority.

Room, Desk / Cluster, Corridor, Wall / Facade, Outdoor Object, RoomPlan +
3DGS, and Detail Repair never acquire or require a single-point subject lock.
They remain full-scene captures even when a stable center-depth sample is
available.

The Record control enters a visible **Starting** state before capture resources
are initialized. `metadata/session_events.jsonl` records
`startup_latency_seconds` on `capture_started` for device-performance
diagnostics.

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
mesh is capped at 200,000 referenced vertices and 300,000 triangles and
exported as `geometry/arkit_mesh.ply` with `arkit_mesh_report.json`. When the
source exceeds that budget, the exporter allocates faces across every eligible
anchor and samples across each anchor's full face range instead of keeping an
anchor-order prefix. Report v0.2 records source/exported totals, anchor and
0.5-meter spatial-cell coverage, per-class coverage, invalid geometry, and the
applied budget. `coverage_preserving` is capture evidence only; the mesh remains
ineligible for collision or measurement authority until host validation accepts
it. Tracking, thermal,
camera-lock, loop, fallback, and finalization transitions are written to
`metadata/session_events.jsonl`. Mask or mesh pressure never relaxes the RGB-D
quality gate; optional writes are dropped or held and reported instead.

After capture, the Projects tab scans the app Documents directory for saved
`capture_splat_*` folders and keeps them available across app launches.
Finalized, partial, and malformed bundles are labeled separately. Selecting a
capture shows its accepted-frame count, finalization evidence, RoomPlan
availability, and a native LiDAR preview backed by
`pointcloud_preview/preview.json` when present. Finalized and partial folders
can be shared directly. A partial folder remains recovery evidence: the app
does not fabricate missing in-memory frame state or claim it can re-finalize
that folder after relaunch. Use the preview as capture guidance evidence; full
mesh or splat quality still needs host validation.

For the **RoomPlan + 3DGS** intent, Apple's RoomPlan processing shares the
existing Video 3DGS AR session instead of opening a second camera. Stopping
capture keeps that AR session alive while RoomBuilder writes
`room_plan/room.usdz`, `room_plan/room_plan_report.json`, and
`room_plan/room_semantics.json` in the same capture folder. RoomPlan failure or
a bounded processing timeout is reported as a hold and does not discard valid
RGB-D/video evidence. The separate Room Plan sheet remains available as a
recovery/debug path during physical validation.

Every finalized capture also writes
`metadata/spatial_guidance_report.json`. It records the resolved sensor mode,
surface-cell and trajectory evidence, update timing, thermal downgrades, and
RoomPlan status. Report v0.2 separates processed anchor updates from
intentionally throttled/coalesced updates, policy-disabled updates, actual
drops, and processing-budget overruns. It also records seconds spent in each
thermal state, guidance policy, and render state, plus the reason a live mesh
was paused. A high throttled count is expected when ARKit updates anchors more
frequently than the 5 Hz or 2 Hz guidance budget; it is not by itself a
renderer failure. Its measurement, collision, semantic-ground-truth,
navigation, and quality authority flags are all false. Treat RoomPlan and live
coverage as capture guidance and scale/context evidence only; neither proves
COLMAP registration, collision geometry, complete coverage, or 3DGS quality.

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
Object Orbit preparation never pads a strict masked package with unmasked
continuous-video frames. For other intents, temporal downselection ranks
candidates by parallax, blur, and distributed feature support while preserving
coverage across the full capture duration.

When a post-capture review identifies specific unusable accepted frames, keep
the originals and provide a strict exclusion manifest:

```bash
capture-splat prepare-capture \
  --capture /path/to/capture \
  --out runs/scan/prepared \
  --frame-exclusions runs/scan/frame_exclusions.json
```

The manifest uses schema `capture_splat.frame_exclusions.v0.1` and contains
one-based `excluded_source_frame_indices`. Preparation copies the applied
record to `frames/metadata/frame_exclusions.json`.

For rooms, move in small connected side steps around the perimeter. Keep the
previous wall, corner, table edge, shelf, or textured object in view while adding
translation. Avoid fast pans, exposure jumps, blank walls, glass, and stopping
after only one height band.

## Future live sender boundary

The Phase 1 live path is replay-only and does not modify the iPhone capture
loop. A future sender must follow the bounded store-and-forward design in
[Live Session Phase 1](live_session.md): enqueue only atomically completed local
files, never retain `ARFrame` or capture pixel buffers, keep one bounded sender
with backpressure and limited in-flight uploads, and always prioritize capture
and local evidence during disk, thermal, background, or network pressure.

Leaving loopback is a separate security phase defined by the strict [live
pairing and authentication contract](live_auth.md). It requires explicit LAN
opt-in, a QR-bound World Studio identity, Bonjour discovery checked against the
QR invitation, TLS certificate pinning, a current scoped grant, signed requests,
and durable anti-replay state before any phone sender can connect.

That security contract does not authorize networking from the capture callback.
The future sender stays downstream of completed atomic writes and uses one
bounded queue with byte/frame caps. Thermal, storage, background, or network
pressure pauses transport before it can affect keyframe acceptance or source
evidence.
