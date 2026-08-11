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
remains available; the UI instructs the operator to stop, preserve, and cool
to nominal. At critical thermal state surface guidance is paused and the UI
instructs an immediate stop. Capture persistence remains available until the
operator stops, but a serious/critical run is not valid thermal acceptance
evidence.

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

For Video 3DGS, use a stop-and-step motion: side-step 7-10 cm while keeping
textured edges visible, then hold briefly for the haptic before moving again.
The app first checks camera baseline without sampling image quality. A new
sector needs the existing 5 cm baseline; the same sector needs the existing
7 cm baseline. Insufficient movement is reported separately as a lightweight
move wait. It is not a rejected keyframe and does not increment quality holds.
At serious and critical thermal state, full candidate checks slow from 5 Hz to
2 Hz and 1 Hz respectively; no quality threshold is relaxed.

Candidates captured while the camera rotates or translates too fast are held
with the `fast_motion` quality-hold reason. This is a motion-blur quality proxy
from ARKit pose deltas, not an image-quality proof. Each saved frame includes
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
evidence. Quality-held candidates remain telemetry and are not trainer input.
The v0.1 report retains `skipped_keyframe_candidates` for compatibility and
also labels that value as `quality_gate_hold_count`; novelty waits have
separate counts, reasons, and bounded events.

Begin a measured capture only after the phone returns to nominal thermal
state. If it reaches serious, stop and preserve the local capture, let the
phone cool, and restart the acceptance cycle from nominal. Serious/critical
sender suspension protects capture persistence but does not make a warm run
valid thermal-performance evidence.

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

## Live sender boundary

For iPhone, record and finalize locally, then use **Manual Export** from
Projects. This is the production workflow. Live transfer is disabled by
default and retained only as an opt-in experiment; it is not required for
preparation, SfM, training, QA, or World Studio review.

The Phase 1 replay path remains compatible. M1B now binds the [Swift sender
foundation](ios_live_sender.md) downstream of local capture persistence:
persistent device identity and grants, pinned authenticated transport, a
durable frame/byte-bounded queue, limited in-flight uploads, retry/resume, and
capture-first pressure policy. It accepts only immutable local file references
and value metadata and never retains `ARFrame` or capture pixel buffers.

For each accepted frame, Capture Splat first atomically writes RGB, depth, and
enabled confidence evidence. It then commits one bounded canonical journal
record under `metadata/live/accepted-frames/` before notifying the single
serial bridge. This is an O(1)-per-frame write. Hashing, networking, ACKs,
masks, optional previews, and other sidecars never block the capture queues; a
journal failure stops live publication without rejecting the locally accepted
frame.

The additive v0.2 contract removes the finalized-manifest timing dependency:
before capture start returns accepted, the phone synchronously persists
canonical session metadata containing a random 32-byte seed, derives the stable
live session ID, inspects the exact metadata path/size/SHA-256, and records that
reference in the pending pointer. A crash before the first frame cannot replace
the session identity. The final `capture.json` reference is bound only after
local finalization. Existing replay remains v0.1-compatible. Only a strict
finalization marker committed after atomic `capture.json` publication,
containing its exact path, byte size, and SHA-256, can trigger the live final
binding. A bare manifest is never inferred as a finalization event.

Leaving loopback is a separate security phase defined by the strict [live
pairing and authentication contract](live_auth.md). It requires explicit LAN
opt-in, a QR-bound World Studio identity, Bonjour discovery checked against the
QR invitation, TLS certificate pinning, a current scoped grant, signed requests,
and durable anti-replay state before any phone sender can connect.

The pairing sheet now implements that opt-in without touching the capture loop.
It is disabled while recording, temporarily releases the AR preview while its
QR scanner owns the camera, ignores every Bonjour identity except the one in
the short-lived QR, and performs no discovery after app restart. Keys, grants,
pending signed requests, and the authoritative one-Mac recovery pointer remain
in Keychain; only a rebuildable non-secret desktop cache and counters are
stored under Application Support.

Networking never runs on the capture callback. Checksummed
`pending-capture.json` and `current-session.json` pointers under Application
Support identify the only recoverable transfer; restart follows that exact
previously user-authorized capture, validates its pinned grant/binding/queue,
and replays the journal. It never scans capture folders and performs no
unsolicited Bonjour discovery. Backgrounding, network loss, loss of pairing
authorization, and serious/critical thermal transitions cancel transport work;
storage policy prevents new work below its floor. None of those conditions may
change keyframe acceptance or source evidence. The durable transfer stays
inert unless the currently paired desktop exactly matches its pointer/binding,
the app is foreground, the network is available, and thermal policy permits;
allowed transitions wake only the single sender worker.

Serious/critical thermal pressure also defers live hashing and queue admission,
leaving only the durable accepted-frame journal for later backfill. Stop and
finalize the local capture, then use **Manual Export** or the **Projects** share
action without abandoning the pending transfer. Cooling or relaunch may resume
that exact transfer later; the warm run still does not count as thermal or live
acceptance evidence.

If `capture.json` publication fails with zero accepted frames, the bridge
automatically clears its pending/current recovery pointers only after verifying
that the accepted-frame journal is empty and no finalization marker exists. A
nonempty journal or manifest/finalization failure remains protected. The
pairing sheet's confirmed **Abandon Pending Live Transfer** recovery removes
only the fixed Application Support `pending-capture.json` and
`current-session.json` pointers. It never deletes the capture folder, source
files, accepted-frame journal, queue, session binding, or metadata.

Frame/byte queue limits are a sliding send window, not a requirement that the
whole capture fit at once. Journal records beyond current capacity remain
durable in the capture; as ACKs drain queued records, the one worker
incrementally refills the window and eventually admits finalization. The
separate 360-frame product cap remains unchanged.

The exact [ACK-index benchmark](ios_live_ack_index_benchmark.md) in issue #35
retains the 360-frame product cap and exact duplicate/conflict ledger. Physical
LAN behavior remains an optional held experiment until controlled device
evidence covers thermal behavior, receiver restart, and Wi-Fi interruption
without degrading capture. All live frames, cameras, depth, masks, meshes, and
later reconstruction output remain `proposal_only`, not measurement,
collision, navigation, semantic, or physics authority.
