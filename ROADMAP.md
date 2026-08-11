# Roadmap

## Current Status

Capture Splat has implemented the core public capture-to-3DGS path:

- Video 3DGS Max records quality-gated RGB-D keyframes, indexed continuous
  HEVC video, per-frame ARKit poses/intrinsics, exposure telemetry, person
  masks, mesh evidence, and capture events.
- Video 3DGS candidate selection arms expensive quality analysis only after
  the existing 5 cm baseline, or 7 cm in the same sector, and reports
  lightweight movement waits separately from quality holds. Thermal pressure
  reduces candidate-analysis cadence without weakening acceptance thresholds.
- Intent-aware guidance covers Room, Desk / Cluster, Object Orbit, Corridor,
  Wall / Facade, Outdoor, RoomPlan + 3DGS, and Detail Repair. Only Object
  Orbit permits an explicit subject lock.
- The on-device Projects library persists finalized, partial, and invalid
  capture evidence across launches and supports historical preview/share
  without fabricating a re-finalization.
- The host pipeline prepares captures, runs integrated global COLMAP or HLOC
  retrieval, preserves per-frame camera and mask evidence, builds a gated
  RGB-D or ARKit mesh metric seed, and trains controlled VkSplat or gsplat
  ladders.
- Strict capture, camera, photometric, PLY, weak-frame, raw-render, and
  World Studio handoff reports preserve `promote|hold|reject` decisions.
- Package orientation and trainer normalization are recorded as separate
  transforms so viewers do not have to guess how a trained PLY relates to
  source cameras.
- Replay-first live sessions now send strict source-frame, camera, quality, and
  optional sensor-sidecar evidence to an explicitly listening loopback World
  Studio receiver with duplicate, gap, disconnect, resume, and finalization
  semantics. This is proposal-only evidence transport, not live 3DGS.
- The authenticated-LAN boundary now has a canonical QR pairing, device
  identity, TLS pin, scoped grant, revocation epoch, and per-request anti-replay
  contract. One long-lived bounded serial bridge now receives nonblocking
  events only after atomic RGB/depth/enabled-confidence writes and an immutable,
  bounded accepted-frame journal commit. The O(1)-per-frame journal, canonical
  live metadata, checksummed pending/current recovery pointers, paired
  desktop/device binding, and queue make an authorized pending transfer
  restart-safe without retaining `ARFrame` or pixel buffers or waiting on
  capture queues for hashing, networking, ACKs, masks, optional previews, or
  other sidecars. The seed-bearing canonical session metadata and its exact
  path/size/SHA-256 reference are durable before capture start returns accepted,
  so a first-frame crash cannot replace session identity. Queue limits are a
  sliding send window that is incrementally refilled from the journal as ACKs
  drain it. QR/Bonjour pairing is active, but restart recovery performs no
  unsolicited discovery. The exact ledger remains capped at 360 frames, and
  only an exact path/size/SHA-256 marker committed after atomic `capture.json`
  publication triggers the v0.2 final binding. Durable recovery wakes only for
  the exact paired desktop while foreground, network, and thermal gates permit.
  A confirmed abandon action removes only fixed pending/current Application
  Support pointers, preserving capture, journal, queue, and binding evidence.
  Physical LAN acceptance remains open.

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
| Authenticated Sender And Device Acceptance | Optional paired TLS transport with bounded store-and-forward, thermal safety, and device evidence | experimental and evidence-blocked on iPhone: software boundary implemented; production capture uses manual export after a live-enabled trial reached serious thermal state before any upload attempt started |
| Calibration Capture Evidence | Guided and synchronized experimental imagery with apparatus/scale provenance, without inferred physics authority | planned and evidence-blocked |

The completed foundation is permanent `proposal_only` evidence transport. Manual export is
the production iPhone boundary and does not depend on the authenticated sender. The sender
may be revisited for other devices or after measured thermal optimization, but it cannot be
promoted on iPhone until physical tests cover capture throughput, finalization,
disconnect/recovery, thermal downgrade, and receiver identity without harming capture.
Calibration recording cannot close from code alone; it requires measured apparatus,
synchronized trials, checksums, and declared downstream validation.

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
  live transfer is an optional experiment and cannot block reconstruction work.
- Finish physical acceptance of live spatial guidance and shared-session
  RoomPlan using a controlled 90-120 second capture.
- Verify mesh/map update p95, keyframe throughput, thermal downgrade duration,
  RoomPlan coordinate continuity, and absence of retained-frame warnings.
- Run every retained capture through global SfM, fixed-camera `3000 -> 7000`
  render QA, and only then consider `15000 -> 30000`.
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
  Max, retaining the exact ledger and current 360-frame cap. The capture sender
  binding uses one bounded serial bridge and commits one immutable
  accepted-frame journal record after each required atomic frame write before
  notifying it. Previously authorized pending transfers recover from
  checksummed pending/current Application Support pointers without unsolicited
  discovery. The synchronous seed/metadata/reference claim closes the
  first-frame crash window, while incremental journal refill keeps the bounded
  queue a send window rather than a whole-capture limit. Background, network,
  pairing, and serious/critical thermal transitions cancel transport work
  without changing local capture. A zero-frame manifest failure clears recovery
  only after confirming the journal is empty; nonempty failures remain
  protected for recovery or the confirmed, evidence-preserving abandon action.
  Run two physical iPhone-to-Mac cycles with receiver restart and Wi-Fi
  interruption before accepting LAN transfer, device restart recovery,
  throughput, thermal behavior, writer-drop behavior, or finalization.
- Add task/robot/site briefs, calibration-trial recording, matched demonstration capture,
  and deployment recapture as additive evidence workflows after the active sender/device
  gate. None may weaken local-first capture or infer physical parameters.

### Reconstruction

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
- COLMAP-refined cameras remain the visual reconstruction baseline. ARKit
  pose/depth is a prior and metric evidence.
- Short runs are smoke tests. Serious quality gates remain
  `3000 -> 7000 -> 15000 -> 30000`.
- Longer training cannot repair weak capture, poor registration, bad
  intrinsics, blur, exposure discontinuity, or missing viewpoint support.
- Capture Splat remains simulator-neutral. Newton, Isaac, ROS, collision promotion, and
  physical-parameter authority belong downstream in World Studio.
