# Real2Sim Capture Program

Capture Splat is the field-evidence application for the Capture Splat to World Studio
real-to-sim-to-real loop. It records what the sensors observed, how the device moved, which
quality gates passed, and which evidence remains missing.

It does not infer or promote simulator physics on the iPhone.

## Ownership

Capture Splat owns:

- local-first RGB, continuous video, depth, confidence, masks, poses, intrinsics, gravity,
  mesh, RoomPlan proposals, clocks, and capture events;
- capture intent, operator guidance, quality decisions, missing coverage, checksums, and
  finalization;
- synchronized imagery for apparatus, calibration, task demonstration, and deployment
  recapture;
- simulator-neutral World Studio handoffs.

World Studio owns:

- immutable World, Asset, Robot, Sensor, Task, Eval, Policy, Promise, and Deployment
  versions;
- objectization, collider construction, physical parameter estimation, simulation,
  calibration, promotion, and rollback;
- Newton and external simulator adapters.

## Program Checkpoints

These checkpoints extend the three public Capture Splat milestones. They do not replace the
active authenticated-sender work.

### CS-R2S1 Task, Robot And Site Brief

Before capture, bind a session to:

- site and zone;
- robot and sensor profile;
- task and operating envelope;
- required observations and downstream readiness target;
- privacy, exclusion, and retention policy.

The brief is planning input, not evidence that the task is supported.

### CS-R2S2 Asset Capture And Calibration Trials

Record object evidence plus synchronized, operator-guided trials such as:

- direct scale, dimensions, and mass apparatus;
- slide, ramp, push, drop, compression, roll, brake, pendulum, or articulation trials;
- initial state, action script, clocks, repetitions, exclusions, and safety limits.

Capture Splat records the trial. World Studio estimates parameters and validates held-out
behavior.

### CS-R2S3 Matched Open-Loop And Task Demonstration Capture

Record real trials that can be replayed against a fixed simulator job:

- robot, controller or policy, and sensor versions;
- initial object and robot state;
- commanded action timeline;
- synchronized observations, contacts when externally available, and outcomes;
- success/failure reason and operator intervention.

The phone does not claim that real and simulated trajectories match.

### CS-R2S4 Deployment Recapture And Change Evidence

Relocalize to an existing site revision and record:

- before/after anchors and target zones;
- changed, unchanged, and unknown proposals;
- refreshed coverage and source hashes;
- incident, maintenance, or schedule trigger;
- failed relocalization and out-of-envelope evidence as `hold`.

A recapture creates a child site-evidence package. It never overwrites the prior World.

### CS-R2S5 Physical Device Acceptance

This checkpoint is governed by the existing **Authenticated Sender And Device Acceptance**
milestone rather than creating a duplicate milestone. It covers:

- startup and finalization latency;
- thermal, memory, storage, and writer-drop behavior;
- clock and sensor continuity;
- bounded authenticated networking, disconnect, resume, and reconciliation;
- privacy and apparatus evidence;
- two complete physical-device cycles.

## Proposed Records

Future additive records may include:

- task/robot/site brief;
- asset-capture and calibration-trial manifest;
- matched task-demonstration manifest;
- deployment-recapture and site-delta evidence;
- field-Episode reference.

They remain proposals until strict schemas, fixtures, backward compatibility, and
round-trip tests exist.

## Authority

Passive or guided capture does not establish mass, center of mass, inertia, friction,
restitution, rolling resistance, stiffness, damping, force, torque, collision,
navigation, semantics, or physics authority.

The permitted public statement after downstream validation is
“physics-calibrated within a validated task envelope,” not a universal physical claim.
