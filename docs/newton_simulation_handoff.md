# Newton Simulation Handoff

Capture Splat remains simulator-independent. It does not install or execute Newton and does
not add Newton, Warp, MuJoCo, Isaac, or ROS dependencies to the iPhone or host pipeline.

World Studio may compile a Capture Splat handoff into a Newton job only after import,
registration, geometry, collision, and task-specific validation.

## Existing Evidence

The current World Studio handoff can reference:

- source frames and camera metadata;
- ordinary metric-point proposals;
- trained Gaussian appearance;
- Capture Splat and transforms manifests;
- ARKit mesh, planes, trajectory, and mesh reports;
- RoomPlan geometry and semantic proposals;
- metric registration, PLY statistics, and render/source QA.

Every path is relative and checksum-bound where the exporter has evidence. Gaussian
appearance remains a review proposal.

## Future Additive Fields

A Newton-ready handoff may additionally reference:

- canonical units, gravity, handedness, and frame graph;
- effective-collider candidate type and provenance;
- finite vertex/index counts and triangle winding;
- primitive, heightfield, convex decomposition, SDF, or simplification settings;
- floor, wall, opening, contact-surface, and unknown-region coverage;
- object-local visual, metric, collision, semantic, and articulation frames;
- apparatus, direct measurements, synchronized calibration trials, and uncertainty;
- requested task, robot, sensor, and operating envelope;
- approved and prohibited downstream uses.

These are design fields, not an implemented schema.

## Required World Studio Gates

Before a handoff can drive Newton:

1. Validate every referenced size, checksum, path, numeric value, frame, and unit.
2. Preserve the source metric geometry separately from the effective collider.
3. Compare collider distance, floor continuity, wall retention, openings, route clearance,
   and contact surfaces against metric evidence.
4. Bind an exact World, Asset, Robot, Sensor, Task, Newton, Warp, solver, contact pipeline,
   timestep, seed, platform, and device.
5. Run import, spawn, route, contact, reset, sensor, and Episode conformance.
6. Record `promote|hold|reject`, uncertainty, `approved_for`, and `not_approved_for`.

A finite mesh, successful Newton import, or completed simulation remains `hold` without
these gates.

## Public Roadmap

World Studio owns the target runtime and the
[R2S2R and Newton adoption package](https://github.com/sandeep-devarapalli/world-studio/tree/main/docs/blueprints/world-compiler-v0.1/r2s2r-newton-2026-07-29).
Capture Splat owns the [Real2Sim Capture Program](real2sim_capture_program.md).
