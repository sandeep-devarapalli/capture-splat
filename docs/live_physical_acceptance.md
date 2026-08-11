# Experimental Live Physical Acceptance

This is an optional transport-research gate. It is not required for production
iPhone capture, manual export, reconstruction, or World Studio package review.
Capture Splat defaults live transfer off on iPhone and treats local
finalization plus Manual Export as the production data path.

`capture_splat.live_physical_acceptance.v0.1` is a host-side evidence gate for
the bounded iPhone sender. It compares a sender-disabled capture with a matched
sender-enabled capture and binds the enabled run to strict sender and receiver
reports. It does not establish reconstruction quality or grant measurement,
collision, semantic, navigation, or physics authority.

The 2026-08-11 physical trial is a `hold`: the device moved from fair to
serious thermal state in 15.4 seconds while transfer was enabled, with zero
upload attempts and zero completed sender runs. That evidence blocks promotion
without proving that Wi-Fi upload alone caused the transition.

## Evidence inputs

The evaluator accepts:

- a sender-disabled Capture Splat directory;
- a sender-enabled Capture Splat directory;
- the enabled sender report at
  `metadata/live/physical_acceptance_report.json`;
- the corresponding World Studio receiver report, snapshot, or finalized
  session directory; and
- an output directory.

Both capture directories must contain `capture.json` and finalized RGB-D frame
evidence. The evaluator reads these sidecars when present:

- `metadata/finalization_report.json`
- `metadata/session_report.json`
- `metadata/sensor_health.json`
- `metadata/spatial_guidance_report.json`

Every JSON input is parsed with non-finite values rejected. Input report paths,
sizes, and SHA-256 values are recorded in the output. A malformed required JSON
input is an evaluator error rather than evidence that a run passed or failed.

## Decision policy

The evaluator writes
`capture_splat_live_physical_acceptance_summary.json` with one decision:

- `promote`: all core checks pass and memory, thermal time-series, and ACK
  latency evidence are present;
- `hold`: no hard failure is present, but optional instrumentation or a core
  comparison value is missing;
- `reject`: a declared hard failure is present.

Hard failures include:

- failed local finalization or inconsistent accepted-frame counts;
- missing accepted RGB/depth evidence or an unsafe frame path;
- any reported writer drop, checksum mismatch, or evidence corruption;
- sender or receiver finalization failure or mismatch;
- a nonempty receiver `missing_ranges` array;
- receiver frame loss;
- sender queue overflow, evidence loss, or a queue that is not drained after
  finalization; and
- sender-enabled accepted-frame throughput below 90 percent of the matched
  sender-disabled baseline.

The 90 percent threshold is based on accepted frames per capture second, not
raw frame count. Duration comes from the spatial-guidance thermal summary,
session/finalization metadata, or accepted-frame timestamps in that order.

Missing memory, thermal time-series, or ACK-latency evidence is a `hold`. The
evaluator does not manufacture a zero, nominal state, or latency value. A final
thermal-state string alone is retained as evidence but is not a complete
thermal trace.

## Sender and receiver reports

The iPhone writes the canonical sender evidence at
`metadata/live/physical_acceptance_report.json`. Its acceptance-critical fields
include:

```json
{
  "schema": "capture_splat.m1b_physical_acceptance_telemetry.v0.1",
  "session_id": "csl_...",
  "finalization_state": "receiver_finalized",
  "final_sequence_id": 120,
  "manifest_sha256": "sha256:<64 lowercase hex>",
  "queue_current_frames": 0,
  "queue_overflow_count": 0,
  "queue_evidence_loss_count": 0,
  "request_acknowledgement_latency_available": true,
  "request_acknowledgement_sample_count": 481,
  "request_acknowledgement_latency_mean_ms": 8.0,
  "request_acknowledgement_latency_p95_ms": 12.0,
  "request_acknowledgement_latency_max_ms": 18.0,
  "request_retry_count": 2
}
```

Capture memory and thermal evidence remain in the capture's ordinary session
and spatial-guidance reports so the sender-disabled baseline and enabled run
use the same measurement path. The evaluator also accepts the documented
nested aliases for independently produced validation reports.

The receiver report also supplies `received_frame_count` and `missing_ranges`.
Instead of manufacturing a summary, `--receiver-report` may point directly to
a finalized World Studio session directory containing `state.json`,
`finalized.json`, `source-manifest-binding.json`, and
`capture-splat.world-studio.json`. The evaluator verifies the component
checksums and normalizes that durable evidence. Duplicate delivery is not
itself a failure when the receiver remains idempotent, complete, and finalized.

## CLI and Python API

Run the public command after collecting both matched captures and the transport
evidence:

```bash
capture-splat live-physical-acceptance \
  --baseline-capture /path/to/sender-disabled-capture \
  --enabled-capture /path/to/sender-enabled-capture \
  --sender-report /path/to/enabled-capture/metadata/live/physical_acceptance_report.json \
  --receiver-report /path/to/world-studio/live-sessions/csl_session_id \
  --receiver-restart-report /path/to/receiver-restart.json \
  --wifi-interruption-report /path/to/wifi-interruption.json \
  --app-relaunch-report /path/to/app-relaunch.json \
  --second-capture-cycle-report /path/to/second-cycle.json \
  --out /path/to/acceptance-output
```

The same evaluator is available as a reusable Python API:

```python
from pathlib import Path

from capture_splat.live_physical_acceptance import run_live_physical_acceptance

summary = run_live_physical_acceptance(
    baseline_capture=Path("/path/to/sender-disabled-capture"),
    enabled_capture=Path("/path/to/sender-enabled-capture"),
    sender_report=Path("/path/to/sender-report.json"),
    receiver_report=Path("/path/to/receiver-snapshot.json"),
    out_dir=Path("/path/to/acceptance-output"),
)
```

## Recovery scenarios

`scenario_evidence` attaches strict JSON reports for
`receiver_restart`, `wifi_interruption`, `app_relaunch`, and
`second_capture_cycle`, plus named future scenarios:

```python
run_live_physical_acceptance(
    ...,
    scenario_evidence={
        "receiver_restart": Path("/path/to/restart-report.json"),
        "wifi_interruption": Path("/path/to/wifi-report.json"),
    },
)
```

Each attachment is checksum-bound and records `executed` and `passed` only when
the supplied report states them. An unprovided required scenario remains
`not_provided`, records `executed: false`, and holds the gate. A provided but
unexecuted scenario also holds; an executed failed scenario rejects. A
`promote` decision therefore requires receiver restart, Wi-Fi interruption,
an app relaunch with durable recovery, and a second physical capture cycle to
be explicitly evidenced.
