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

These are implemented capabilities, not a blanket high-quality claim. A
physical capture, finite PLY, viewer load, or aligned frame remains evidence
for one gate only.

## Acceptance Gates

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
- Implement the documented bounded store-and-forward live sender only after
  replay transport and physical capture stability pass. LAN transport remains
  blocked on explicit opt-in, pairing credentials, authentication, and TLS.

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
