# Roadmap

## Current Status

Capture Splat has implemented the core public capture-to-3DGS path:

- Video 3DGS Max records quality-gated RGB-D keyframes, indexed continuous
  HEVC video, per-frame ARKit poses/intrinsics, exposure telemetry, person
  masks, mesh evidence, and capture events.
- Intent-aware guidance covers Room, Desk / Cluster, Object Orbit, Corridor,
  Wall / Facade, Outdoor, RoomPlan + 3DGS, and Detail Repair. Only Object
  Orbit permits an explicit subject lock.
- The on-device Projects library persists finalized, partial, and invalid
  capture evidence across launches and supports historical preview/share
  without fabricating a re-finalization.
- The host pipeline prepares captures, preserves per-frame camera and mask
  evidence, builds a gated RGB-D or ARKit mesh metric seed, and trains
  controlled VkSplat or gsplat ladders. Pinned Spirula built-in SfM is the
  preferred product candidate after same-input evidence-gated promotion;
  external HLOC/COLMAP remains the frozen conformance control and fallback.
- Strict capture, camera, photometric, PLY, weak-frame, raw-render, and
  World Studio handoff reports preserve `promote|hold|reject` decisions.
- World Studio handoff v0.3 adds a sanitized, checksum-bound
  `training_dataset` inventory: canonical source-frame-set digest, observed
  COLMAP camera models, projection provenance, capture profile, and available
  capture/SfM/depth/confidence/mask/mesh evidence. It does not claim that a
  trainer consumed those inputs.
- Package orientation and trainer normalization are recorded as separate
  transforms so viewers do not have to guess how a trained PLY relates to
  source cameras.
- Replay-first live sessions now send strict source-frame, camera, quality, and
  optional sensor-sidecar evidence to an explicitly listening loopback World
  Studio receiver with duplicate, gap, disconnect, resume, and finalization
  semantics. This is proposal-only evidence transport, not live 3DGS.
- The authenticated-LAN boundary now has a canonical QR pairing, device
  identity, TLS pin, scoped grant, revocation epoch, and per-request anti-replay
  contract. A dormant bounded Swift sender foundation now persists identity,
  grants, counters, paired desktop/device-bound queue state, ACK/resume
  progress, and capture-first pressure policy without entering the capture
  loop. The additive v0.2 session-seed/final-manifest contract now permits
  progressive transfer before `capture.json` exists while preserving v0.1
  replay. The app now performs explicit QR scanning, exact Bonjour resolution,
  Mac approval, and Application Support recovery without starting discovery at
  launch or opening a frame queue. Capture integration and physical acceptance
  remain open.

These are implemented capabilities, not a blanket high-quality claim. A
physical capture, finite PLY, viewer load, or aligned frame remains evidence
for one gate only.

## Public Milestones

Capture Splat supplies evidence to the
[World Studio World Compiler Blueprint](https://github.com/sandeep-devarapalli/world-studio/tree/main/docs/blueprints/world-compiler-v0.1).
The broader M0-M10 world, editor, navigation, simulation, R2S2R, and Physical Asset
Calibration outcomes are owned by World Studio.

| Milestone | Outcome | Status |
|---|---|---|
| Live Session Foundation | Strict replay-first source-frame, camera, quality, sidecar, ACK, resume, and finalization contract | completed |
| Authenticated Sender And Device Acceptance | Optional paired TLS transport with bounded store-and-forward, thermal safety, and device evidence | experimental and evidence-blocked on iPhone; production capture uses local finalization and manual export |
| Calibration Capture Evidence | Guided and synchronized experimental imagery with apparatus/scale provenance, without inferred physics authority | planned and evidence-blocked |
| Training Dataset And External Provider Evidence | Additive v0.3 dataset inventory and self-contained Room-01 producer evidence for pinned external trainers | producer contract and Room-01 visual proposal complete; portal and collision producer held |

### Immediate Capture Slice: Room-01

- Use finalized local capture plus Manual Export; optional live transport is not required.
- Export a fresh self-contained v0.3 package whose source images, COLMAP model, RGB-D,
  depth/confidence, masks, ARKit mesh/report, trajectory, planes, RoomPlan, and scale evidence
  are checksum-bound when present.
- Record actual registered-image and registered-RGB-D overlap counts from the produced model.
  The previously observed 168-camera overlap is a verification target, not a hard-coded value.
- Preserve a source mesh marked `truncated: true` as evidence only; it cannot acquire collision
  or navigation authority.
- Preserve the prior closed capture as a control. The current open-door revision carries
  RoomPlan and a trajectory crossing, while its one-sided registered RGB-D coverage remains a
  held producer result; opening-aware reduction must retain that uncertainty explicitly.
- Obtain a World Studio consumer receipt. Spirula execution, canonical publication, collision
  promotion, OpenUSD, Newton, and robot/UAV Episodes remain downstream World Studio work.

#### 2026-08-23 Evidence Checkpoint

- Pinned Spirula built-in SfM registered `411 / 450` prepared images:
  `217 / 246` continuous-video frames and `194 / 204` RGB-D frames. This is
  Room-01 registration evidence, not a performance or all-GPU claim.
- Metric alignment was accepted at `0.455587656 m / COLMAP unit`, with
  `0.029027 m` median and `0.057314 m` p95 camera-center residuals. The
  checksum-bound metric seed contains `92,906` points; physical measurement
  authority remains separately gated.
- The 7,000-step Spirula run produced a finite SH3 PLY with `1,498,066`
  Gaussians and `371,521,900` bytes. Its SHA-256 is
  `56dc6ab645f099bef670f07516046ce9ddcd65d94c44c007e08f35374bb37bd8`.
  Only the Spark functional load/orbit/zoom/reset contract promoted; visual,
  metric, collision, navigation, physics, and performance authority did not.
- Exterior clouding/floaters are expected outside this interior-only capture's
  observed-ray volume and do not downgrade supported interior views. `7,000`
  is a training-step count, not the number of retained Gaussians.
- The open-door trajectory contains one clean `door_1` crossing at a measured
  width of `0.7616868 m`, but accepted RGB-D support by portal region is
  `side_a / through / side_b = 0 / 0 / 204`. The portal producer and collision
  authority therefore remain `hold`. Side A and side B are opposite sides of
  this one portal, not two doors. A supplemental reverse pass is useful but is
  not required for World Studio to form a non-authoritative proxy with unknown
  space no-go and explicit hypothesis surfaces.
- PR [#67](https://github.com/sandeep-devarapalli/capture-splat/pull/67)
  fixes the zero-based native image-ID defect with a track-aware derived model.
  Native binding now completes for `411` registered prepared frames and reports
  registered accepted RGB-D support `0 / 0 / 194`; this confirms the remaining
  hold is missing spatial evidence, not a camera-identity parser failure.
- The RGB-D TSDF has `136,810` vertices and `260,038` faces; the hybrid surface
  is `59.1417%` unknown. Reduction produced `59,999` faces but raised unknown
  coverage to `91.0382%` and failed floor, wall, door-retention, and probe
  rails. It is not a collision candidate for promotion.
- Rapier consumption remains downstream in World Studio. Capture Splat records
  and preserves the producer holds rather than granting downstream authority.

Live sender, equirectangular, cross-vendor, capacity, and timing holds do not block this slice.

The completed foundation is permanent `proposal_only` evidence transport. Manual export is
the production iPhone boundary and does not depend on the authenticated sender. The sender
may be revisited for other devices or after measured thermal optimization, but promotion
requires physical evidence for throughput, finalization, disconnect/recovery, thermal
behavior, and receiver identity. Calibration recording cannot close from code alone; it
requires measured apparatus, synchronized trials, checksums, and declared downstream
validation.

## R2S2R Capture Program

The [Real2Sim Capture Program](docs/real2sim_capture_program.md) reconciles five requested
checkpoints with the existing milestone structure:

| Checkpoint | Outcome | Status |
|---|---|---|
| CS-R2S1 Task, Robot And Site Brief | Bind capture to site, robot, sensors, task, operating envelope, and evidence needs | planned |
| CS-R2S2 Asset Capture And Calibration Trials | Record apparatus-backed object and interaction evidence without inferring physics | planned and evidence-blocked |
| CS-R2S3 Matched Open-Loop And Task Demonstration Capture | Record initial state, command timeline, observations, and outcomes for real/sim comparison | planned |
| CS-R2S4 Deployment Recapture And Change Evidence | Relocalize to a site revision and emit immutable changed/unchanged/unknown evidence | planned |
| CS-R2S5 Physical Device Acceptance | Thermal, storage, clocks, local finalization, manual export, privacy, and apparatus acceptance; networking is an optional sub-gate | tracked independently from experimental live transport |

Capture Splat stays simulator-independent. The
[Newton Simulation Handoff](docs/newton_simulation_handoff.md) describes evidence World
Studio may compile into its target Newton runtime after separate validation.

## Acceptance Gates

- Treat local finalization plus Manual Export as the required iPhone data path;
  live transfer is optional and cannot block reconstruction work.
- Finish physical acceptance of live spatial guidance and shared-session
  RoomPlan using a controlled 90-120 second capture.
- Verify mesh/map update p95, keyframe throughput, thermal downgrade duration,
  RoomPlan coordinate continuity, and absence of retained-frame warnings.
- Run every retained capture through global SfM, fixed-camera `3000 -> 7000`
  render QA, and only then consider `15000 -> 30000`.
- Start measured efficiency work on Apple Silicon and keep cross-vendor, 8 GB,
  and multi-million-Gaussian claims at `hold` until named hardware, commands,
  repetitions, raw results, noise, and the same quality rails are recorded.
- Use Lego, Playroom, and complete Bonsai `images_2` for their declared standard
  lanes after validating source, license, expected files, and completeness.
  Keep physical iPhone captures and Room-01 as a separate lane.
- Compare observable Capture Splat, SplatKing, KIRI Engine, and similar
  outputs without claiming access to proprietary internals.

## Remaining Public Work

### Capture App

- Replace the debug scene-understanding wireframe with a bounded,
  class-colored production mesh renderer after the physical throughput gate.
- Complete TestFlight packaging and distribution metadata.
- Add release-level startup, long-session thermal, and two-cycle finalization
  evidence across supported LiDAR iPhones.
- Issue #35's exact ACK-index benchmark passed on the designated iPhone 16 Pro
  Max, retaining the exact ledger and current 360-frame cap. Keep the dormant
  bounded store-and-forward foundation optional. Any future capture hookup must
  pass current-grant, TLS-pin, replay-protection, pressure, and physical thermal
  gates without weakening local capture or manual export.
- Add task/robot/site briefs, calibration-trial recording, matched demonstration capture,
  and deployment recapture as additive evidence workflows after the active sender/device
  gate. None may weaken local-first capture or infer physical parameters.

### Reconstruction

- Keep Spirula-derived capability work behind a pinned, user-installed external
  process boundary. Do not vendor or copy GPL implementation into Capture
  Splat. A future World Studio job contract may request a provider run, but
  `training_dataset` remains input evidence rather than execution evidence.
- Treat pinned Spirula built-in SfM as the preferred product candidate only
  after a same-input evidence-gated promotion. Keep external HLOC/COLMAP as the
  frozen conformance control and fallback. Bind the selected Vulkan device and
  host stages; the accurate Room-01 Apple run used Vulkan/MoltenVK for ALIKED
  and LightGlue but CPU double-precision bundle adjustment. Do not convert that
  stage-level result into a speed or all-GPU claim.
- Benchmark the shipped checksum-bound sensor depth/normal supervision
  contract on trainers that expose dedicated metric-sensor inputs. Current
  public VkSplat and gsplat baselines preserve the evidence but do not consume
  it as metric LiDAR supervision.
- Validate the shipped rig-constrained equirectangular SfM path on a real 360
  sequence; recovered panorama poses remain non-metric registration evidence.
- Physically validate the shipped optional AprilTag scale checker on a measured
  target before using its checksum-bound report for World Studio measurement.
- Validate the shipped strict SPZ export on World Studio and mobile browsers,
  then evaluate worker-backed SOG/tiled LOD only after orientation, color,
  camera, and viewer checks pass.
- Keep VGGT preview and splat-to-mesh as optional experiments behind explicit
  runtime and authority gates.

### World Studio

- Consume handoff v0.3 while continuing to accept v0.2, validate the canonical
  frame-set and evidence fields, and keep trainer execution/result receipts in
  a separate future World job contract.
- Consume the shipped handoff `world_up`, frame-aware `initial_camera`, source
  capture, planes, RoomPlan, trajectory, and metric-evidence sidecars end to
  end.
- Accept a coverage-preserving collision candidate only after floor continuity,
  wall retention, and splat/mesh registration pass.
- Add metric surface picking, distance/height/polyline/area measurement,
  uncertainty, and JSON export after the handoff's checksum-bound metric points
  pass a physical known-distance gate. The Gaussian remains a visual proposal.
- Add worker-backed compact-asset loading and mobile performance validation
  before claiming large-scene web readiness.

## Stable Policy

- VkSplat/Vulkan remains the public baseline; gsplat/CUDA is the cloud
  alternative. OpenSplat/MPS remains comparison-only.
- Spirula is an external research/provider boundary, not vendored Capture Splat
  code and not evidence of cross-vendor support until measured here.
- Pinned Spirula built-in SfM is the preferred product candidate after
  same-input evidence-gated promotion; external HLOC/COLMAP is the frozen
  conformance control and fallback. ARKit pose/depth remains a prior and metric
  evidence.
- Short runs are smoke tests. Serious quality gates remain
  `3000 -> 7000 -> 15000 -> 30000`.
- Longer training cannot repair weak capture, poor registration, bad
  intrinsics, blur, exposure discontinuity, or missing viewpoint support.
- Capture Splat remains simulator-neutral. Newton, Isaac, ROS, collision promotion, and
  physical-parameter authority belong downstream in World Studio.
