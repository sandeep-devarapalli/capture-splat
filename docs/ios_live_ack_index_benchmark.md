# iOS Live ACK Index Benchmark

Capture Splat issue
[#35](https://github.com/sandeep-devarapalli/capture-splat/issues/35)
decides whether the exact acknowledged-frame identity ledger in
`capture_splat.live_sender_queue_state.v0.1` is safe to keep before the dormant
sender is connected to capture persistence.

The benchmark measures the existing representation. It does not prune hashes,
raise the 48 MiB payload limit, modify source captures, activate networking, or
enter `CaptureController`.

## Decision boundary

The current product accepts at most 360 video frames. The current
representation passes only when both 360 identities and the 720-identity
two-times safety case satisfy every hard gate below on the designated physical
acceptance device: an iPhone 16 Pro Max reporting `iPhone17,2`, running an
optimized Release build.

This is an iPhone 16 Pro Max-specific decision gate. A pass does not establish
ACK latency, reopen latency, memory, throughput, backlog, storage scheduling, or
thermal behavior on older supported LiDAR iPhones. Broader supported-device
acceptance remains separate.

The 1,000, 10,000, and 50,000 identity cases characterize future scaling. A
failure outside the supported 720-identity envelope does not by itself require
the compact index or increase the product frame limit.

The budgets are fixed before the physical run:

| Metric at 720 identities | Hard gate |
|---|---:|
| Canonical queue payload | less than 24 MiB |
| Checksummed on-disk envelope | less than 32 MiB |
| ACK reconcile plus durable persistence p50 / p95 / maximum | at most 50 / 100 / 200 ms |
| Queue reopen plus validation p50 / p95 / maximum | at most 250 / 500 / 1,000 ms |
| Physical-footprint delta / peak | at most 16 / 128 MiB |
| Unpaced durable ACK throughput | at least 10 ACK/s |
| Five-ACK/s, 60-second paced maximum backlog | at most 8 |
| Backlog after paced drain | 0 |
| First, middle, and last identical retry after reopen | exact duplicate |
| First, middle, and last altered retry after reopen | exact conflict |

Any corruption, state-cap failure, noncanonical state, digest mismatch, lost
identity, or non-exact retry decision fails the gate.

## Required runs

Use the same shared Swift benchmark core in two isolated harnesses: the host
diagnostic CLI and the `CaptureSplatAckBenchmarks` device test bundle. On iOS,
XCTest runs inside the dedicated `CaptureSplatAckBenchmarkHost` app. That host
contains only its minimal UIKit application delegate; the test bundle depends
on that host and compiles only the live-auth contract, sender queue, benchmark
core, and benchmark tests. The production `CaptureSplat` app and
`CaptureController` are not target dependencies.

- counts: `360,720,1000,10000,50000`;
- warmups: 5;
- measured trials: 30;
- paced case: 5 ACK/s for 60 seconds at 720 identities;
- optimized whole-module Release code;
- one result per clean process for measured reopen iterations;
- exact production `LiveSenderQueue.open` and `reconcile` paths.

The harness may construct a canonical reachable queue envelope so 50,000
acknowledged identities do not require 50,000 fake capture files. Production
code must open, checksum, canonicalize, and validate that state before a
measurement is accepted. The measured ACK path must use the production atomic
write, file synchronization, rename, and directory synchronization.

The strict result records:

- repository commit, benchmark schema, configuration, build, device model, OS,
  thermal state, and physical-device eligibility;
- canonical payload and envelope byte sizes and SHA-256 digests;
- individual ACK and reopen samples plus p50, p95, and maximum;
- current and peak physical footprint;
- unpaced throughput and paced backlog;
- exact duplicate/conflict decisions after reopen; and
- whether the hard gate was evaluated, passed, failed, or blocked.

Benchmark artifact v0.2 records
`is_designated_ack_benchmark_device`; it does not reinterpret the v0.1
`is_oldest_supported_lidar_iphone` field. Published v0.1 evidence remains
historical evidence for its original device policy.

Host and Simulator results are diagnostic only. They must report
`not_evaluated_non_physical` and cannot close issue #35.

## Runbook

Run both harnesses from the repository root on a Mac with the repository's
supported Xcode installed. Before a physical acceptance run:

- connect, trust, unlock, and enable Developer Mode on the designated iPhone 16
  Pro Max, and verify that it reports `iPhone17,2`;
- obtain its device identifier with `xcrun xctrace list devices`;
- copy
  `apps/ios/CaptureSplat/CaptureSplat/Config/CaptureSplat.local.xcconfig.example`
  to the ignored `CaptureSplat.local.xcconfig` beside it, then set a unique
  bundle identifier and the local development-team identifier;
- commit the benchmark implementation and require
  `git status --porcelain=v1 --untracked-files=all` to print nothing; and
- choose a new report path outside the repository. Both harnesses refuse an
  in-repository path, and the physical collector also refuses to overwrite an
  existing report.

The host diagnostic uses the full default matrix but can never satisfy the
physical gate:

```bash
python3 scripts/benchmark_ios_live_sender_ack_index.py \
  --output /tmp/capture-splat-live-ack-host-v0.2.json
```

Set the connected device identifier, then inspect the physical execution plan
without building, running tests, or writing a report:

```bash
CAPTURE_SPLAT_ACK_DEVICE_ID='replace-with-xctrace-device-id'
python3 scripts/run_ios_live_sender_ack_device_benchmark.py \
  --dry-run \
  --device-id "$CAPTURE_SPLAT_ACK_DEVICE_ID"
```

The plan must report 352 separate test invocations: five counts, two measured
phases, and 5 warmups plus 30 measured trials produce 350 invocations, followed
by one unpaced and one paced stream invocation. The paced invocation alone is
at least 60 seconds and can run longer while draining. Device launches plus the
10,000- and 50,000-identity cases make the complete run a multi-hour operation;
keep the Mac and unlocked iPhone powered and connected. The collector's default
7,200-second command timeout applies to each command, not to the total run.

Run the acceptance collector only after reviewing that plan:

```bash
CAPTURE_SPLAT_ACK_REPORT='/tmp/capture-splat-live-ack-device-v0.2.json'
python3 scripts/run_ios_live_sender_ack_device_benchmark.py \
  --device-id "$CAPTURE_SPLAT_ACK_DEVICE_ID" \
  --output "$CAPTURE_SPLAT_ACK_REPORT"
```

The collector builds and signs the dedicated benchmark host and test bundle as
optimized, whole-module Release code using the local signing configuration. A
2026-07-30 physical `build-for-testing` preflight succeeded for both targets,
with the dependency graph limited to the tests and dedicated host. The device
then became unavailable before the hosted test launch, so that preflight
produced no physical runtime or result-attachment evidence. A passing run still
requires the eligible iPhone to remain connected, trusted, available, and
unlocked for the complete collection.

Interpret the strict aggregate status as follows:

- `passed`: every required 360- and 720-identity physical-device gate passed
  with complete checksummed evidence;
- `failed`: at least one required command, attachment, aggregation, budget,
  correctness, process-isolation, or evidence check failed, including an
  incomplete collection;
- `not_evaluated_non_physical`: host or Simulator evidence only;
- `not_evaluated_ineligible_device`: a physical device other than the
  designated iPhone 16 Pro Max reporting `iPhone17,2`;
- `not_evaluated_unoptimized_build`: the device evidence did not report the
  optimized-build marker; and
- `not_evaluated_fixture`: synthetic collector-fixture evidence, not a physical
  acceptance run.

Only `passed` closes the ACK-index decision. A command failure, a checksummed
`failed` diagnostic, or a missing report is not passing acceptance evidence.
Even `passed` leaves capture-writer interference unmeasured and does not
authorize the capture-loop callback or physical two-cycle acceptance.

## Proof boundary

The dedicated benchmark host does not build, launch, or depend on the
production Capture Splat app or its capture loop. This benchmark does not yet
connect the sender to capture writers. It must therefore report capture-loop
integration as false and writer drops and capture-side wait as unmeasured, not
zero. The later nonblocking callback PR and two physical iPhone-to-Mac cycles
must prove that networking does not change keyframe acceptance, wait on capture
persistence, or create writer drops.

If the designated physical 720-identity run fails any hard gate, implement the
exact chunked, checksummed, atomically replaced index specified by issue #35
before capture-loop integration. If it passes, retain the current exact ledger
and its 360-frame product cap for this designated-device gate. Neither outcome
replaces broader supported-device or capture-loop acceptance.
