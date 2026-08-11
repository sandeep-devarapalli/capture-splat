# Capture Splat

**iPhone capture to 3DGS with Vulkan/VkSplat, plus optional CUDA evaluation.**

Capture Splat is a brand-neutral starter kit for recording iPhone scan/video data and generating standard 3D Gaussian Splatting `.ply` files. It includes:

- an iPhone app, **Capture Splat**, for guided Video 3DGS capture;
- a Python host pipeline for capture validation, image package creation, and COLMAP text export;
- a VkSplat/Vulkan training wrapper for macOS, Linux, Windows, and cloud GPU workflows;
- an optional gsplat/CUDA wrapper for cloud NVIDIA fallback when Vulkan is unavailable.

The output is a standard 3DGS `.ply` that can be inspected in compatible viewers such as SuperSplat, PlayCanvas-based viewers, Spark-compatible viewers, or other Gaussian viewers.

## What This Is

This repo helps you go from:

```text
iPhone capture folder -> COLMAP package -> VkSplat or optional gsplat trainer -> trained splat.ply
```

It is not a guarantee that every scan becomes high quality. Good splats still depend on sharp frames, strong overlap, enough parallax, stable exposure, COLMAP registration, and finite trainer output.

For the current carry-forward lessons from the iPhone-to-VkSplat validation ladder, see `docs/field_validation_learnings.md`.

## World Compiler Roadmap

Capture Splat is the evidence-producing capture and reconstruction side of the
[World Studio World Compiler Blueprint](https://github.com/sandeep-devarapalli/world-studio/tree/main/docs/blueprints/world-compiler-v0.1).
The
[R2S2R and Newton adoption package](https://github.com/sandeep-devarapalli/world-studio/tree/main/docs/blueprints/world-compiler-v0.1/r2s2r-newton-2026-07-29)
defines World Studio's target simulation architecture; Capture Splat remains
simulator-independent.
It may provide RGB-D, poses, intrinsics, gravity, masks, mesh, RoomPlan proposals,
continuous video, trained splats, and synchronized experiment imagery. Passive capture
does not establish mass, inertia, friction, restitution, stiffness, force, torque, or
physics authority.

Capture Splat tracks three repo-local milestones in [ROADMAP.md](ROADMAP.md): the completed
Live Session Foundation, Authenticated Sender And Device Acceptance, and Calibration
Capture Evidence. World packaging, editing, navigation, simulation, and Physical Asset
Calibration remain World Studio responsibilities. The
[Real2Sim Capture Program](docs/real2sim_capture_program.md) defines task/site briefs,
calibration trials, matched demonstrations, deployment recapture, and physical-device
acceptance. The [Newton Simulation Handoff](docs/newton_simulation_handoff.md) records the
strict boundary without adding a simulator dependency.

## Quickstart: Mac + iPhone

```bash
git clone https://github.com/sandeep-devarapalli/capture-splat.git
cd capture-splat
scripts/setup_macos.sh
scripts/setup_vksplat.sh external/vksplat
# Optional CUDA backend for Linux/cloud NVIDIA machines:
# scripts/setup_gsplat.sh external/gsplat
```

Open `apps/ios/CaptureSplat/CaptureSplat.xcodeproj` in Xcode, set your signing team, run on a physical iPhone, choose **Video 3DGS**, side-step 7-10 cm and briefly hold for each accepted-frame haptic, then export the capture folder to your computer.

Resolve the capture intent into a host-side processing recipe before starting
SfM or training:

```bash
capture-splat plan-reconstruction \
  --capture /path/to/capture_splat_export \
  --out runs/my_capture/plan
```

The strict plan records frame budget, matching strategy, mask/seed policy,
training ladder, viewer preset, missing assets, and `ready|hold`. It is an
execution plan, not a reconstruction-quality claim.

Prepare the actual SfM input before moving it to a GPU host:

```bash
capture-splat prepare-capture \
  --capture /path/to/capture_splat_export \
  --out runs/my_capture/prepared
```

`prepare-capture` keeps accepted RGB-D keyframes, supplements them with the
sharpest pose-matched continuous-video frames up to the intent recipe's real
frame budget, removes shared-clock duplicates within 80 ms, and writes derived
person/object masks only as proposals. Its strict summary includes capture QA,
finalization state, and an `sfm_request` resolved from the actual prepared frame
count. A `hold` preserves usable evidence; it is not a quality claim.

The iPhone app defaults to live spatial Guidance: LiDAR scene-understanding
wireframe, a gravity-aligned trail/surface map, and accepted-keyframe coverage
when supported, with honest plane/feature/pose fallbacks. The `RoomPlan + 3DGS`
intent shares the Video 3DGS AR session and can export `room_plan/room.usdz`
plus conservative semantic and guidance reports. These are capture evidence,
not collision, navigation, measurement, or 3DGS quality proof.

Then run:

```bash
. .venv/bin/activate
CAPTURE=/path/to/exported/capture_splat_session
capture-splat doctor --vksplat-root external/vksplat
capture-splat prepare-capture --capture "$CAPTURE" --out runs/my_scan/prepared
capture-splat sfm \
  --images runs/my_scan/prepared/frames/images \
  --out runs/my_scan/colmap_package \
  --method global --features hloc --matcher retrieval
capture-splat train-vksplat-ladder   --package runs/my_scan/colmap_package   --out runs/my_scan/vksplat_ladder   --vksplat-root external/vksplat
# For long rungs that show late reset instability, record a controlled schedule:
# capture-splat train-vksplat-ladder --package runs/my_scan/colmap_package --out runs/my_scan/vksplat_ladder_stop9000 --vksplat-root external/vksplat --stop-reset-at 9000
```

For Record3D, Roomly-style, or Nerfstudio-style exports that already provide
`transforms.json`, RGB frames, and optional depth frames, first convert them into
a Capture Splat package:

```bash
capture-splat import-transforms \
  --input /path/to/transforms_export \
  --out runs/imported_capture
```

Equirectangular images, image folders, and videos can be projected into a
deterministic 14-view perspective set:

```bash
capture-splat import-360 \
  --input /path/to/panorama_or_video \
  --out runs/imported_360
```

The command preserves full-resolution source panoramas and writes perspective
images, disjoint white-valid feature masks, and
`metadata/equirectangular_rig.json`. It intentionally does not write
`capture.json`: the virtual rotations are projection provenance, not recovered
world poses. Recover panorama poses with the fixed virtual-camera rig:

```bash
capture-splat sfm-360-rig \
  --package runs/imported_360 \
  --out runs/imported_360_colmap \
  --method global
```

This command verifies every projected image and mask against the importer
checksums, configures the zero-translation virtual cameras before sequential
matching, and fixes their known rotations and pinhole intrinsics during
mapping. Its decision is based on registered panorama frames. Registration is
pose evidence, not metric scale or reconstruction-quality proof.

`sfm` now defaults to COLMAP's integrated `global_mapper`. Prepared Capture
Splat packages use per-frame ARKit pinhole intrinsics, complete white-valid
masks, and skip view-graph calibration. Generic image folders retain a
single-camera fallback; imported OPENCV distortion values are preserved when
available. `--method colmap` remains a deprecated alias for incremental
mapping. Caspar is available only as an explicit post-global BA experiment via
`--post-ba-backend caspar`; it is not the global solver.

The VkSplat ladder runs controlled `3000 -> 7000 -> 15000 -> 30000` rungs and writes
`capture_splat_vksplat_ladder_summary.json`. The optional `--stop-reset-at` flag records a VkSplat schedule cutoff for opacity resets, useful when longer rungs show late-reset instability; it is a controlled training setting, not a quality claim by itself. On CUDA cloud machines, `capture-splat train-gsplat-ladder` can run the same conservative ladder through gsplat and writes `capture_splat_gsplat_ladder_summary.json`. Its `--mcmc-refine-every auto|N` setting compensates for shortened-rung schedule scaling and records the effective refinement cadence; `auto` uses the package frame count with a 200-step floor. Single-step training is still
available with `capture-splat train-vksplat --steps 30000`, but a finite `.ply`
is only validated finite output, not a visual-quality claim. If a trainer writes
a `.ply` with a few non-finite splats, `capture-splat sanitize-ply` can write a
strict report and a finite copy that drops only non-finite vertex rows. The
ladder only uses that repair when `--sanitize-non-finite-ply` is set.

For optional web distribution, install the external
[`@playcanvas/splat-transform`](https://github.com/playcanvas/splat-transform)
CLI and export a finite Gaussian PLY through the strict SPZ round-trip gate:

```bash
npm install -g @playcanvas/splat-transform
capture-splat export-spz \
  --input runs/my_scan/splat.finite.ply \
  --out runs/my_scan/scene.spz
```

The command writes SPZ v4, converts it back to PLY, checks splat count,
finiteness, sampled position error, and sampled base-color error, then remains
`hold` until optional checksum-bound viewer evidence confirms load,
orientation, color, and source-camera alignment. Compression does not establish
visual quality, metric scale, collision authority, or correct viewer cameras.

For prepared packages over 250 frames, install the optional HLOC tools
with `PYTHON_BIN=.venv/bin/python scripts/setup_sfm.sh external`, then use
`--features hloc --matcher retrieval`. This runs NetVLAD top-32 retrieval,
ALIKED-N16, LightGlue, COLMAP geometric verification, and the requested mapper.
Missing HLOC is `hloc_missing`; Capture Splat does not silently substitute
exhaustive matching. The setup script does not build standalone GLOMAP unless
`INSTALL_GLOMAP=1` is set; integrated COLMAP `global_mapper` remains the
default.

After SfM, an optional sensor seed can align confidence-filtered iPhone depth
or a report-validated ARKit mesh to the COLMAP camera frame and augment a
copied package:

```bash
capture-splat build-rgbd-seed \
  --capture /path/to/capture \
  --package runs/my_scan/colmap_package \
  --out runs/my_scan/rgbd_seed \
  --seed-source auto
```

The command requires at least eight shared camera centers and gates the fitted
Sim(3) residuals against the COLMAP scene radius. A failed fit is `hold` and
leaves the copied package unaugmented. A passing fit adds `metric_seed.ply` to
the copied text model. Metric-scale promotion additionally requires
`session_config.scale_authority = arkit_vio_metric`; with that evidence, the
command applies `depth_scale`, adapts intrinsics to the actual depth grid,
scales copied COLMAP cameras and points into meters, and writes
`metadata/metric_scale_report.json` bound to the consumed capture and model
assets. Without explicit scale authority, the compatible seed remains in
COLMAP units and is reported as non-metric. The source package is unchanged
and COLMAP-refined cameras remain the visual reconstruction baseline.

Trainer normalization is a separate scale boundary. `--normalization auto`
disables gsplat world normalization only when the package has an accepted
`metric_scale_report.json`, its sparse-model checksums still match, and the
installed trainer exposes a real disable option. VkSplat currently normalizes
internally, so auto mode records
`metric_package_normalized_backend_cannot_disable`; `--normalization off`
blocks rather than pretending the output remained meter-native.

Prepare checksum-bound sensor supervision evidence after SfM or metric seeding:

```bash
capture-splat prepare-training-supervision \
  --package runs/my_scan/colmap_package
```

The command validates NPY depth and confidence maps, applies the recorded
depth scale, and writes optional depth-derived normal proposals plus
`metadata/training_supervision.json`. SfM now copies referenced depth and
confidence sidecars with `capture.json` instead of leaving broken paths.
Trainer flags `--depth-supervision` and `--normal-supervision` use
`auto|off|required`: `auto` preserves unsupported evidence with a warning,
while `required` blocks unless the installed trainer exposes a dedicated
sensor-manifest input. gsplat's upstream `--depth-loss` samples sparse COLMAP
points, so Capture Splat does not mislabel it as iPhone LiDAR supervision.

The same stages can be run through one resumable evidence command:

```bash
capture-splat reconstruct \
  --capture /path/to/capture \
  --out runs/my_scan/reconstruction \
  --backend vksplat \
  --backend-root external/vksplat
```

Use `--dry-run` to inspect the resolved recipe and stage plan, `--stop-after`
for a bounded probe, and `--resume` to reuse completed strict summaries. The
command runs preparation, SfM, optional gated RGB-D seeding, the controlled
training ladder, alpha pruning, optional raw-render QA, and World Studio
export. Supply `--qa-render-dir` when fixed-camera raw renders exist; without
that evidence, the final decision remains `hold` rather than claiming quality.
The render directory must also carry `capture_splat_render_provenance.json`
with the exact selected PLY's `sha256:` value in `gaussian_checksum`; metrics
from an unbound or different model are recorded but cannot promote the run.
Resume revalidates the completed stage configuration plus source/render and
handoff checksums. Rejected or partially written stages require a new output
directory rather than an in-place retry over stale artifacts.

For an Object Orbit package prepared with canonical white-valid masks, derive
premultiplied RGBA training images without changing the source photographs:

```bash
capture-splat remove-background \
  --images runs/object/prepared/frames/images \
  --mask-dir runs/object/prepared/frames/masks/valid \
  --out runs/object/background_removed
```

`--mode auto` prefers the captured depth/person-mask proposal when every image
has a matching mask. `--mode inspyrenet` uses the optional
`capture-splat[matting]` dependency with `fast`, `base`, or `base-nightly`
model mode. Missing optional model support blocks rather than silently
returning unmasked images. Outputs remain derived review/training proposals;
the original images are preserved.

If you have raw rendered canvases from a viewer or app, compare them against the
source images instead of full UI screenshots:

```bash
capture-splat qa-render-source \
  --source-dir runs/my_scan/colmap_package/images \
  --render-dir runs/my_scan/render_canvases/step_0030000 \
  --out runs/my_scan/render_qa/step_0030000

capture-splat qa-weak-frames-report \
  --qa-summary runs/my_scan/render_qa/step_0030000/capture_splat_render_source_qa_summary.json \
  --colmap-images runs/my_scan/colmap_package/sparse/0/images.txt \
  --out runs/my_scan/weak_frames/step_0030000
```

For exact-frame VkSplat diagnosis, rerun the same step count with train renders
enabled and QA only the requested cameras:

```bash
capture-splat vksplat-render-probe \
  --package runs/my_scan/colmap_package \
  --out runs/my_scan/vksplat_7000_render_probe \
  --vksplat-root external/vksplat \
  --steps 7000 \
  --frames 000033,000065,000076,000086,000164
```

This is useful when weak frames were part of the train split and therefore were
not present in the normal validation renders. The output remains diagnostic
render/source evidence, not a high-quality claim.

Use the weak-frame report to build and optionally run a focused COLMAP repair
workspace before retraining:

```bash
capture-splat colmap-focused-repair \
  --package runs/my_scan/colmap_package \
  --weak-report runs/my_scan/weak_frames/step_0030000/capture_splat_weak_frames_report.json \
  --out runs/my_scan/colmap_focused_repair
```

The command writes a focused image/pair plan, runs COLMAP when available, and
rewrites the focused sparse input so COLMAP 4 database image IDs, `frames.txt`,
and `rigs.txt` are aligned. If it reports `blocked`, fix the COLMAP support
package before treating longer training as more than an experiment.

For weak viewpoint neighborhoods, keep the registered package broad and add
bridge pairs instead of training on a tiny subset:

```bash
capture-splat colmap-focused-repair \
  --package runs/my_scan/colmap_package \
  --repair-manifest runs/my_scan/colmap_focused_repair/support_manifest/capture_splat_colmap_support_repair_manifest.json \
  --include-all-registered-images \
  --bridge-ranges 000074-000077,000080-000090 \
  --bridge-window 6 \
  --out runs/my_scan/colmap_broader_repair
```

`--preserve-existing-points` is available only when the database feature rows
match the existing sparse model; if COLMAP re-extracts features, use support
delta to verify the result instead of assuming old tracks were preserved.

After a targeted COLMAP repair pass completes, compare support before
retraining:

```bash
capture-splat colmap-support-delta \
  --original-images runs/my_scan/colmap_package/sparse/0/images.txt \
  --repaired-images runs/my_scan/colmap_repair/sparse/0/images.txt \
  --weak-report runs/my_scan/weak_frames/step_0030000/capture_splat_weak_frames_report.json \
  --out runs/my_scan/colmap_repair/support_delta
```

`proceed_to_training_probe` means the sparse support improved enough to justify
a short 3000-step probe. It is not a quality claim.

To compare two backend outputs, use the package's deterministic
`metadata/fixed_camera_evaluation_set.json` and raw renders from each backend:

```bash
capture-splat compare-backend-renders \
  --package runs/my_scan/colmap_package \
  --gsplat-ply runs/my_scan/gsplat_ladder/step_0007000/ply/point_cloud_6999.ply \
  --vksplat-ply runs/my_scan/vksplat_ladder/step_0007000/splat.ply \
  --gsplat-render-dir runs/my_scan/renders/gsplat_7000 \
  --vksplat-render-dir runs/my_scan/renders/vksplat_7000 \
  --out runs/my_scan/backend_compare_7000
```

If the fixed-camera set is missing, the command blocks. If render directories
are omitted, the command still writes the shared
`camera_pairs.json` and reports `renderer_missing`; that is a setup blocker, not
a quality result.

Build a compact, classification-aware collision candidate from validated ARKit
mesh evidence before packaging a room for interaction review:

```bash
capture-splat build-collision-candidate \
  --mesh captures/scan/geometry/arkit_mesh.ply \
  --mesh-report captures/scan/geometry/arkit_mesh_report.json \
  --out runs/my_scan/collision_candidate
```

The command samples across 0.5-meter spatial cells and ARKit surface classes,
preserves meter units and checksums, and writes a strict `hold` report. It does
not grant collision or navigation authority; floor/wall continuity and
splat/mesh overlap still require physical review.

Optionally validate an already metric COLMAP package against a measured
`tagStandard41h12` AprilTag visible in at least three registered views:

```bash
python -m pip install -e '.[apriltag]'
capture-splat validate-apriltag-scale \
  --package runs/my_scan/colmap_package_metric \
  --tag-size-meters 0.150 \
  --artifact runs/my_scan/rgbd_seed/metric_seed.ply \
  --out runs/my_scan/apriltag_scale
```

The validator triangulates the four tag corners from the registered PINHOLE
cameras, checks reprojection, square-edge consistency, and meter-scale error,
and writes a strict report without modifying the package. Use
`--detections-json` to replay checksum-bound detections without installing the
optional detector. A `promote` decision validates only the known-scale evidence
and exact artifact; it does not establish reconstruction quality, collision
safety, or survey-grade measurement.

To hand a run to World Studio, write a local package with relative references
and conservative authority metadata:

```bash
capture-splat export-world-studio \
  --package runs/my_scan/colmap_package \
  --gaussian runs/my_scan/vksplat_ladder/step_0007000/splat.ply \
  --collision-candidate runs/my_scan/collision_candidate/collision_candidate.ply \
  --collision-report runs/my_scan/collision_candidate/capture_splat_collision_candidate_report.json \
  --known-scale-report runs/my_scan/apriltag_scale/capture_splat_apriltag_scale_report.json \
  --render-source-qa runs/my_scan/render_qa/step_0007000/capture_splat_render_source_qa_summary.json \
  --transforms runs/my_scan/ingest/nerfstudio_dataset/transforms.json \
  --out runs/my_scan/world_studio_package
```

This writes `capture-splat.world-studio.json` with schema
`capture_splat.world_studio_handoff.v0.2`. When a Gaussian PLY is present, the
exporter computes finite/splat statistics from that exact packaged PLY. An
optional strict render/source QA summary is copied as validation evidence.
Prepared iPhone packages also preserve available ARKit mesh, planes, camera
trajectory, spatial-guidance, RoomPlan, source-capture, and metric-scale
sidecars. A checksum-bound `metric_colmap_world` seed may be included as
meter-unit measurement evidence, but `measurement_eligibility` remains held
until a physical known-distance validation passes. Source frames are visual
evidence; trained splats are review proposals, not metric, collision, semantic,
or navigation authority. Finite PLY and QA decisions are not high-quality
claims.

### Optional replay and experimental live transfer

The production iPhone workflow is local-first: record and finalize on device,
then use **Manual Export** from Projects. Live transfer is disabled by default,
is not required for reconstruction, and is not a Capture Splat release gate.
It remains available as an opt-in experiment and as a transport path for future
devices whose thermal budget can support it.

Phase 1 can replay an existing capture into World Studio while it is listening
on loopback:

```bash
capture-splat replay-live-session \
  --capture /path/to/capture_splat_export \
  --receiver http://127.0.0.1:43127 \
  --delay-ms 100
```

The command sends only frames not explicitly rejected, preserves each camera's
original intrinsics and calibration dimensions, and streams every referenced
RGB, depth, confidence, and person/valid/object mask asset. It writes nothing
inside the source capture. The strict printed
`capture_splat.live_replay_summary.v0.1` contains the generated session ID;
provide that ID with `--session-id ... --resume` to continue after a later
process restart. `--shuffle --seed`, `--duplicate-every`,
`--disconnect-after`, and `--disconnect-seconds` exercise receiver recovery.

The receiver is intentionally restricted to HTTP loopback in Phase 1. The live
surface is source-frame and camera evidence with permanent proposal-only
authority; it is not live 3DGS reconstruction and does not replace a world
already loaded in World Studio. See [Live Session Phase 1](docs/live_session.md)
for the contract and receiver recovery behavior, and
[iOS Live Sender M1B](docs/ios_live_sender.md) for the bounded iPhone sender.

M1A adds the strict [live pairing and authentication contract](docs/live_auth.md)
for QR-bound World Studio identity, Bonjour discovery, certificate pinning,
P-256 request authentication, scoped grants, expiry, revocation epochs, and
anti-replay counters. M1B now binds that contract to capture through one
long-lived bounded serial bridge. After the required RGB, depth, and enabled
confidence files are atomically written, the capture path commits one bounded,
immutable accepted-frame journal record and only then notifies the bridge. This
is an O(1)-per-frame write rather than a growing manifest rewrite. Capture
queues never wait for hashing, networking, ACKs, masks, optional previews, or
other sidecars.

The bridge writes canonical live session/frame metadata and checksummed queue
state before upload. Checksummed pending-capture and current-session pointers,
session bindings, and queues persist under Application Support; the immutable
accepted-frame journal remains with the source capture evidence under
`metadata/live/`. Before `captureStarted` can return accepted, the app
synchronously persists the canonical session metadata containing its random
32-byte seed, derives the stable `csl_...` session ID, inspects the exact
metadata path/size/SHA-256, and stores that reference in the pending pointer.
A crash before the first frame therefore cannot replace the live session
identity. Only an explicit journal marker written after atomic `capture.json`
publication may trigger strict v0.2 finalization; it binds the manifest path,
exact byte size, SHA-256, and schema to that same session. A bare manifest is
never inferred as permission to finalize. The replay CLI remains
byte-compatible with live session/finalization v0.1.

If local manifest publication fails, automatic live-transfer abort is allowed
only for a zero-frame capture after the accepted-frame journal is verified
empty and no finalization marker exists. A nonempty journal or manifest-marker
failure keeps the recovery pointer protected for later recovery or explicit
abandonment. The pairing sheet's confirmed **Abandon Pending Live Transfer**
action removes only the fixed Application Support `pending-capture.json` and
`current-session.json` pointers. It never deletes the capture directory,
accepted-frame journal, queue, session binding, or source evidence.

The app now exposes explicit World Studio pairing. Its QR-only scanner resolves
the exact advertised Bonjour identity, then reuses the existing pinned-TLS,
P-256 request, approval, grant, and replay-protection boundary. Device
credentials, pending signed requests, and an authoritative one-Mac recovery
pointer remain in Keychain; a rebuildable non-secret desktop cache and request
counters live under Application Support. App launch performs no unsolicited
Bonjour discovery. It may automatically resume only a previously
user-authorized pending transfer represented by its exact pending-capture or
current-session pointer, after restoring the grant, binding, and queue and while
foreground/network/thermal/storage policy permits. Backgrounding, loss of
network or pairing authorization, and serious or critical thermal transitions
cancel active transport work without changing capture acceptance or source
evidence. Durable recovery is inert until the currently paired desktop exactly
matches the pointer/binding and the app is foreground, the network is
available, and thermal policy permits transfer. Those state transitions update
the gate directly: allowed transitions wake the one sender worker, while
disallowed transitions cancel its current drive.

The sender retains only immutable paths and evidence metadata, never `ARFrame`
or pixel buffers. Its exact acknowledged-frame ledger remains capped at 360
frames after the checksum-bound
[Release benchmark](docs/ios_live_ack_index_benchmark.md) passed on the
designated iPhone 16 Pro Max (`iPhone17,2`), closing
[issue #35](https://github.com/sandeep-devarapalli/capture-splat/issues/35)
without an ACK-index redesign. CaptureController's accepted-frame array remains
in-memory working state; the immutable per-frame live journal is the restart
record. Recovery can rebuild transfer state from the exact pending/current
pointer and journal, but it never synthesizes `capture.json` or finalizes an
interrupted local capture. Frame/byte queue limits bound the current send
window, not the total durable journal: as ACKs drain queued records, the single
worker incrementally refills that window from the journal. The whole capture
does not have to fit in queue state at once, and this does not raise the
separate 360-frame product cap.
The strict [experimental live physical-acceptance gate](docs/live_physical_acceptance.md)
compares matched sender-disabled and sender-enabled captures and binds the
enabled run to the finalized World Studio session. It is retained for transport
research, but it does not block local capture, manual export, reconstruction,
or World Studio package review.

## Linux, Windows, And Cloud GPUs

The iPhone app must be built with Apple tooling, but once you have an exported capture folder, the processing side is intended to work on macOS, Linux, Windows, and cloud NVIDIA machines.

- Linux: see `docs/linux_setup.md`.
- Windows: see `docs/windows_setup.md`.
- Cloud NVIDIA: see `docs/cloud_gpu_setup.md` and `docker/Dockerfile.linux-nvidia`.
- App comparisons: see `docs/app_comparison.md`.

## Optional Backends

VkSplat/Vulkan remains the default baseline because it is cross-platform in principle and keeps Capture Splat independent of CUDA. If a cloud image exposes CUDA but not a usable Vulkan device, `gsplat` is the preferred direct-CUDA fallback:

```bash
scripts/setup_gsplat.sh external/gsplat
capture-splat doctor --gsplat-root external/gsplat
capture-splat train-gsplat-ladder \
  --package runs/my_scan/colmap_package \
  --out runs/my_scan/gsplat_ladder \
  --gsplat-root external/gsplat
```

iPhone packages use `--photometric bilateral-grid` by default on supported
gsplat trainers. `--photometric ppisp` remains experimental and blocks when
the trainer strategy or optional dependency is unavailable. `--masks required`
also blocks rather than silently training without a requested valid mask.

`scripts/setup_external_3dgs_candidates.sh` can clone 3DGS.cpp and AndrewBoessen/3DGS into `external/` for evaluation. 3DGS.cpp is useful for macOS/Vulkan viewer-runtime checks; upstream lists training as TODO. AndrewBoessen/3DGS is a CUDA 13 C++ candidate and is not a default backend.

## Capture Tips

- Move slowly and keep the subject visible.
- Prefer bright, even lighting.
- Avoid motion blur and rolling-shutter sweeps.
- Capture overlapping views and close the loop for room scans.
- Use Room Plan for room-layout guidance when supported, then still validate capture quality and COLMAP/VkSplat evidence on the host.
- For objects, orbit around the object and include slightly elevated/lower views.
- Treat warnings from the app and host QA as real quality blockers.

## License

Apache-2.0. See `THIRD_PARTY.md` for external tool licenses.
