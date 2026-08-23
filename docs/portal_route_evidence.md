# Portal and Route Evidence

`capture-splat validate-portal-route-evidence` is a producer-side evidence
gate. It validates a future open-door Room-01 capture without modifying the
capture, the v0.3 World Studio handoff, the unsimplified hybrid surface, or the
held reduced collider.

## Derive a capture diagnostic

Use the prepared capture to measure whether its RoomPlan proposal, full video
trajectory, and retained RGB-D frames could support a future evidence package:

```bash
capture-splat derive-portal-route-evidence \
  --prepared-capture runs/room_01/prepare/frames/capture.json \
  --out runs/room_01/portal_route_derivation
```

After SfM, add `--sfm-package /path/to/reconstruction`. The package must contain
`images/` and `sparse/0/images.txt`. A registered name counts only when its
canonical, case-sensitive path selects regular non-symlink bytes whose size and
SHA-256 exactly match the prepared RGB. The report binds those matches with a
deterministic aggregate parity digest. This is not a metric
RoomPlan-to-COLMAP registration receipt.

The command streams and hashes the complete `0..video_frame_count-1`
trajectory, verifies prepared video and retained RGB-D
pose/timestamp/intrinsics bindings plus prepared asset presence, and selects a
portal only when exactly one RoomPlan proposal has a bounded, contiguous
normal-tracking crossing inside its rectangle. It never fabricates RGB-D, free
space, a threshold, a route, or a closed-state control. Its deterministic
`capture_splat.portal_route_derivation.v0.1` report is diagnostic and always
held with all authority false; it is not a
`capture_splat.portal_route_evidence.v0.1` producer package.

Running without `--evidence` is intentional. It writes
`capture_splat_portal_route_validation_report.json` with every missing
RoomPlan, portal, route, free-space, registered-RGB-D, and closed-control rail
named explicitly. The current closed-door Room-01 package must use this path;
it is not traversable evidence.

## Evidence package

The strict top-level schema is
`capture_splat.portal_route_evidence.v0.1`. Its `source_handoff` size and
SHA-256 must bind the exact supplied `capture-splat.world-studio.json` v0.3
file. All referenced paths are relative to the evidence package, must remain
inside it, and must resolve to regular, non-symlink files with the declared
size and SHA-256.

The package requires:

- `coordinate_contract`: `arkit_world`, meters, scale 1, +Y world up, and
  positive position, dimension, and plane-residual uncertainties.
- `roomplan`: the exact handoff `room_plan` asset and a checksum-bound
  `capture_splat.roomplan_arkit_registration.v0.1` receipt with a rigid
  transform into `arkit_world`, scale, uncertainty, method, and provenance. A
  raw unregistered RoomPlan asset alone is insufficient.
- `portal`: an ordered finite convex quadrilateral on a unit-normal vertical plane,
  clear width and height consistent with that polygon, and a finite threshold
  segment on its lower edge.
- `free_space`: a checksum-bound
  `capture_splat.portal_free_space_evidence.v0.1` JSON artifact. Every sample
  carries a metric position, horizontal and vertical clearance, and exact
  supporting capture-frame indices. The declared count and bounded sample
  spacing must match the artifact.
- `route_corridor`: a checksum-bound
  `capture_splat.portal_route_corridor_evidence.v0.1` JSON artifact. Its finite
  centerline must start and end on opposite portal sides and intersect inside
  the portal polygon. The validator samples every segment at the declared
  spacing; each sample must have nearby observed free-space support with enough
  horizontal and vertical clearance.
- `registered_rgbd_support`: a checksum-bound
  `capture_splat.portal_rgbd_support.v0.1` JSON inventory. Every observation
  selects an exact v0.3 capture-manifest frame whose regular RGB, depth, and
  confidence files are hash-bound by the handoff inventory. The frame must
  retain finite intrinsics, timestamp, rigid ARKit pose, and a name present in
  the handoff's exact COLMAP `images.txt`. At least one observation is required
  on side A, inside the through-opening band, and on side B. Its metric
  registration digest must match the handoff, and route free-space samples may
  cite only these selected frame indices.
- `prior_closed_state_control`: checksum-bound copies of the prior closed-state
  v0.3 handoff, reduced candidate, held reducer report, and held software-probe
  report. The reducer's top-level candidate and the probe's reduced-collider
  input must bind the same exact file; the probe must retain a closed-door
  result. All authority stays false. The control may remain held; binding it is
  not a claim that the old reduction passed.
- non-empty capture and measurement provenance, with every authority field
  false.

## Receipt boundary

A structurally valid package sets only
`outcome.producer_contract_valid=true` and
`outcome.evidence_complete_for_future_reduction_design=true`. Overall decision
remains `hold`, while `reduction_started`, `traversable`, and
`collision_candidate_promoted` remain false. World Studio must separately run
the source and reduced collider doorway probes, reset/route test, and physical
clearance validation before any downstream promotion. Raw Gaussian splats and
this receipt never become collision or Newton authority.
