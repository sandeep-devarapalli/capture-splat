#!/usr/bin/env python3
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any


TRIAL_COUNTS = (360, 720, 1_000, 10_000, 50_000)
DEFAULT_WARMUPS = 5
DEFAULT_TRIALS = 30
ELIGIBLE_MODELS = ("iPhone17,2",)
REPORT_SCHEMA = "capture_splat.live_sender_ack_benchmark_report.v0.2"
ENVELOPE_SCHEMA = "capture_splat.live_sender_ack_benchmark_report_envelope.v0.2"
RECONCILE_SCHEMA = "capture_splat.live_sender_ack_benchmark_reconcile_phase.v0.2"
REOPEN_SCHEMA = "capture_splat.live_sender_ack_benchmark_reopen_phase.v0.2"
UNPACED_STREAM_SCHEMA = (
    "capture_splat.live_sender_ack_benchmark_unpaced_stream_phase.v0.2"
)
PACED_STREAM_SCHEMA = (
    "capture_splat.live_sender_ack_benchmark_paced_stream_phase.v0.2"
)
DEFAULT_STREAM_COUNT = 720
DEFAULT_PACED_ACKNOWLEDGEMENTS_PER_SECOND = 5
DEFAULT_PACED_DURATION_SECONDS = 60

HARD_GATE_BUDGETS = {
    "supported_frame_count": 360,
    "hard_gate_frame_count": 720,
    "payload_bytes_less_than": 24 * 1024 * 1024,
    "envelope_bytes_less_than": 32 * 1024 * 1024,
    "ack_p50_nanoseconds_at_most": 50_000_000,
    "ack_p95_nanoseconds_at_most": 100_000_000,
    "ack_max_nanoseconds_at_most": 200_000_000,
    "cold_reopen_p50_nanoseconds_at_most": 250_000_000,
    "cold_reopen_p95_nanoseconds_at_most": 500_000_000,
    "cold_reopen_max_nanoseconds_at_most": 1_000_000_000,
    "footprint_delta_bytes_at_most": 16 * 1024 * 1024,
    "kernel_peak_footprint_bytes_at_most": 128 * 1024 * 1024,
    "sustained_acknowledgements_per_second_at_least": 10,
    "paced_acknowledgements_per_second": 5,
    "paced_duration_seconds": 60,
    "paced_backlog_frames_at_most": 8,
    "paced_final_backlog_frames": 0,
}

STATE_KEYS = {
    "payload_bytes",
    "envelope_bytes",
    "payload_sha256",
    "envelope_sha256",
    "acknowledged_frame_count",
    "pending_frame_count",
}
PLATFORM_KEYS = {
    "operating_system",
    "operating_system_version",
    "machine",
    "architecture",
    "thermal_state",
    "is_physical_device",
    "is_designated_ack_benchmark_device",
    "optimized_build",
    "physical_gate_result",
}
PHASE_MEMORY_KEYS = {
    "footprint_before_bytes",
    "footprint_after_bytes",
    "footprint_delta_bytes",
    "kernel_reported_peak_footprint_bytes",
}
THERMAL_STATE_KEYS = {"before", "after"}
STREAM_CORRECTNESS_KEYS = {
    "production_open_validated_external_state",
    "every_acknowledgement_reconciled_exactly_one_frame",
    "sequence_probes",
}
QUEUE_LIMIT_KEYS = {
    "maximum_frames",
    "maximum_bytes",
    "maximum_in_flight",
    "scope",
}
PROCESS_KEYS = {"launch_id", "process_id"}
HARD_GATE_CORRECTNESS_KEYS = {
    "unpaced_production_open_reconcile",
    "paced_production_open_reconcile",
    "stream_process_launches_unique",
    "required_per_count_evidence_present",
    "process_cold_reopen",
    "process_launches_unique",
    "exact_duplicate_conflict_after_restart",
    "exact_duplicate_conflict_after_stream",
    "state_digest_stable",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _require_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} has missing or additional fields")
    return value


def _validate_state(value: object, expected_count: int, pending: int) -> dict[str, Any]:
    state = _require_keys(value, STATE_KEYS, "state evidence")
    if state["acknowledged_frame_count"] != expected_count:
        raise ValueError("state evidence has the wrong acknowledged frame count")
    if state["pending_frame_count"] != pending:
        raise ValueError("state evidence has the wrong pending frame count")
    for field in ("payload_sha256", "envelope_sha256"):
        digest = state[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
        ):
            raise ValueError(f"state evidence {field} is not a strict SHA-256")
    return state


def _validate_platform(value: object) -> dict[str, Any]:
    platform = _require_keys(value, PLATFORM_KEYS, "platform evidence")
    if platform["optimized_build"] is not True:
        raise ValueError("benchmark CLI was not compiled with the optimized-build marker")
    expected_designated = (
        platform["is_physical_device"]
        and platform["machine"] in ELIGIBLE_MODELS
    )
    if platform["is_designated_ack_benchmark_device"] is not expected_designated:
        raise ValueError("platform designated benchmark-device claim is inconsistent")
    return platform


def _validate_phase_memory(value: object) -> dict[str, Any]:
    return _require_keys(value, PHASE_MEMORY_KEYS, "phase memory evidence")


def _validate_queue_limits(value: object, count: int) -> dict[str, Any]:
    limits = _require_keys(value, QUEUE_LIMIT_KEYS, "queue limits evidence")
    expected = {
        "maximum_frames": count,
        "maximum_bytes": (2**63 - 1) // 4,
        "maximum_in_flight": min(8, count),
        "scope": "benchmark_only_not_product_cap",
    }
    if limits != expected:
        raise ValueError("benchmark queue limits changed")
    return limits


def _validate_process(value: object) -> dict[str, Any]:
    process = _require_keys(value, PROCESS_KEYS, "process evidence")
    launch_id = process["launch_id"]
    try:
        parsed = uuid.UUID(launch_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("process launch ID is not a UUID") from error
    if str(parsed) != launch_id:
        raise ValueError("process launch ID is not canonical lowercase UUID")
    if (
        not isinstance(process["process_id"], int)
        or isinstance(process["process_id"], bool)
        or process["process_id"] <= 0
    ):
        raise ValueError("process ID is invalid")
    return process


def _validate_reconcile(value: object, count: int, trial_index: int) -> dict[str, Any]:
    phase = _require_keys(
        value,
        {
            "schema",
            "configuration",
            "process",
            "queue_limits",
            "seed_state",
            "persisted_state",
            "reconcile_duration_nanoseconds",
            "memory",
            "platform",
            "reconciled_sequence_ids",
        },
        "reconcile phase",
    )
    if phase["schema"] != RECONCILE_SCHEMA:
        raise ValueError("unexpected reconcile phase schema")
    if phase["configuration"] != {
        "acknowledged_frame_count": count,
        "trial_index": trial_index,
    }:
        raise ValueError("reconcile phase configuration mismatch")
    _validate_process(phase["process"])
    _validate_queue_limits(phase["queue_limits"], count)
    _validate_state(phase["seed_state"], count - 1, 1)
    _validate_state(phase["persisted_state"], count, 0)
    _validate_phase_memory(phase["memory"])
    _validate_platform(phase["platform"])
    if phase["reconciled_sequence_ids"] != [count]:
        raise ValueError("reconcile phase did not persist the final exact identity")
    return phase


def _expected_probe_sequences(count: int) -> list[int]:
    return sorted({1, (count + 1) // 2, count})


def _validate_sequence_probes(value: object, count: int) -> list[dict[str, Any]]:
    expected = _expected_probe_sequences(count)
    if not isinstance(value, list) or [
        probe.get("sequence_id") for probe in value if isinstance(probe, dict)
    ] != expected:
        raise ValueError("phase did not probe first, middle, and last identities")
    for probe in value:
        _require_keys(
            probe,
            {
                "sequence_id",
                "identical_disposition",
                "conflicting_reference_rejected",
            },
            "sequence probe",
        )
        if (
            probe["identical_disposition"] != "duplicate"
            or probe["conflicting_reference_rejected"] is not True
        ):
            raise ValueError("exact duplicate or conflict behavior changed after restart")
    return value


def _validate_reopen(value: object, count: int, trial_index: int) -> dict[str, Any]:
    phase = _require_keys(
        value,
        {
            "schema",
            "configuration",
            "process",
            "queue_limits",
            "persisted_state",
            "reopen_duration_nanoseconds",
            "memory",
            "platform",
            "sequence_probes",
        },
        "reopen phase",
    )
    if phase["schema"] != REOPEN_SCHEMA:
        raise ValueError("unexpected reopen phase schema")
    if phase["configuration"] != {
        "acknowledged_frame_count": count,
        "trial_index": trial_index,
    }:
        raise ValueError("reopen phase configuration mismatch")
    _validate_process(phase["process"])
    _validate_queue_limits(phase["queue_limits"], count)
    _validate_state(phase["persisted_state"], count, 0)
    _validate_phase_memory(phase["memory"])
    _validate_platform(phase["platform"])
    _validate_sequence_probes(phase["sequence_probes"], count)
    return phase


def _validate_stream_correctness(
    value: object,
    count: int,
) -> dict[str, Any]:
    correctness = _require_keys(
        value,
        STREAM_CORRECTNESS_KEYS,
        "stream correctness evidence",
    )
    if (
        correctness["production_open_validated_external_state"] is not True
        or correctness["every_acknowledgement_reconciled_exactly_one_frame"] is not True
    ):
        raise ValueError("stream did not use production open/reconcile exactly")
    _validate_sequence_probes(correctness["sequence_probes"], count)
    return correctness


def _validate_durations(value: object, expected_count: int) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != expected_count
        or any(not isinstance(item, int) or item <= 0 for item in value)
    ):
        raise ValueError("stream acknowledgement durations are invalid")
    return value


def _validate_thermal_states(value: object) -> dict[str, Any]:
    states = _require_keys(value, THERMAL_STATE_KEYS, "thermal state evidence")
    if any(not isinstance(states[field], str) or not states[field] for field in states):
        raise ValueError("thermal state evidence is invalid")
    return states


def _validate_stream_gate_result(value: object, platform: dict[str, Any]) -> str:
    if not isinstance(value, str):
        raise ValueError("stream gate result is invalid")
    if not platform["is_physical_device"]:
        expected = "not_evaluated_non_physical"
    elif not platform["is_designated_ack_benchmark_device"]:
        expected = "not_evaluated_ineligible_device"
    elif not platform["optimized_build"]:
        expected = "not_evaluated_unoptimized_build"
    else:
        if value not in {
            "failed",
            "measurement_passed_requires_aggregate_evaluation",
        }:
            raise ValueError("eligible stream evidence claimed an aggregate gate result")
        return value
    if value != expected:
        raise ValueError("ineligible stream evidence claimed a gating result")
    return value


def _validate_unpaced_stream(value: object, count: int) -> dict[str, Any]:
    phase = _require_keys(
        value,
        {
            "schema",
            "final_acknowledged_frame_count",
            "process",
            "queue_limits",
            "seed_state",
            "persisted_state",
            "acknowledgement_durations_nanoseconds",
            "elapsed_nanoseconds",
            "durable_acknowledgements_per_second",
            "memory",
            "thermal_states",
            "platform",
            "correctness",
            "gate_result",
        },
        "unpaced stream phase",
    )
    if phase["schema"] != UNPACED_STREAM_SCHEMA:
        raise ValueError("unexpected unpaced stream schema")
    if phase["final_acknowledged_frame_count"] != count:
        raise ValueError("unpaced stream count mismatch")
    _validate_process(phase["process"])
    _validate_queue_limits(phase["queue_limits"], count)
    _validate_state(phase["seed_state"], 0, count)
    _validate_state(phase["persisted_state"], count, 0)
    _validate_durations(phase["acknowledgement_durations_nanoseconds"], count)
    if not isinstance(phase["elapsed_nanoseconds"], int) or phase["elapsed_nanoseconds"] <= 0:
        raise ValueError("unpaced stream elapsed time is invalid")
    throughput = phase["durable_acknowledgements_per_second"]
    if (
        not isinstance(throughput, (int, float))
        or isinstance(throughput, bool)
        or not math.isfinite(throughput)
        or throughput <= 0
    ):
        raise ValueError("unpaced durable ACK throughput is invalid")
    _validate_phase_memory(phase["memory"])
    _validate_thermal_states(phase["thermal_states"])
    platform = _validate_platform(phase["platform"])
    _validate_stream_correctness(phase["correctness"], count)
    _validate_stream_gate_result(phase["gate_result"], platform)
    return phase


def _validate_paced_stream(
    value: object,
    final_count: int,
    rate: int,
    duration_seconds: int,
) -> dict[str, Any]:
    acknowledgement_count = rate * duration_seconds
    initial_count = final_count - acknowledgement_count
    phase = _require_keys(
        value,
        {
            "schema",
            "configuration",
            "process",
            "queue_limits",
            "seed_state",
            "persisted_state",
            "acknowledgement_durations_nanoseconds",
            "elapsed_nanoseconds",
            "drain_duration_nanoseconds",
            "maximum_backlog_frames",
            "backlog_at_nominal_end_frames",
            "final_backlog_frames",
            "memory",
            "thermal_states",
            "platform",
            "correctness",
            "gate_result",
        },
        "paced stream phase",
    )
    if phase["schema"] != PACED_STREAM_SCHEMA:
        raise ValueError("unexpected paced stream schema")
    if phase["configuration"] != {
        "initial_acknowledged_frame_count": initial_count,
        "final_acknowledged_frame_count": final_count,
        "acknowledgement_count": acknowledgement_count,
        "acknowledgements_per_second": rate,
        "nominal_duration_seconds": duration_seconds,
    }:
        raise ValueError("paced stream configuration mismatch")
    _validate_process(phase["process"])
    _validate_queue_limits(phase["queue_limits"], final_count)
    _validate_state(phase["seed_state"], initial_count, acknowledgement_count)
    _validate_state(phase["persisted_state"], final_count, 0)
    _validate_durations(
        phase["acknowledgement_durations_nanoseconds"],
        acknowledgement_count,
    )
    if (
        not isinstance(phase["elapsed_nanoseconds"], int)
        or phase["elapsed_nanoseconds"] < duration_seconds * 1_000_000_000
        or not isinstance(phase["drain_duration_nanoseconds"], int)
        or phase["drain_duration_nanoseconds"] < 0
    ):
        raise ValueError("paced stream elapsed or drain time is invalid")
    for field in (
        "maximum_backlog_frames",
        "backlog_at_nominal_end_frames",
        "final_backlog_frames",
    ):
        if not isinstance(phase[field], int) or phase[field] < 0:
            raise ValueError("paced stream backlog evidence is invalid")
    if phase["final_backlog_frames"] != 0:
        raise ValueError("paced stream did not drain its final backlog")
    _validate_phase_memory(phase["memory"])
    _validate_thermal_states(phase["thermal_states"])
    platform = _validate_platform(phase["platform"])
    _validate_stream_correctness(phase["correctness"], final_count)
    _validate_stream_gate_result(phase["gate_result"], platform)
    return phase


def _run_phase(
    executable: Path,
    count: int,
    trial_index: int,
    workspace: Path,
    phase: str,
    environment: dict[str, str],
    extra_arguments: tuple[str, ...] = (),
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(executable),
            "--count",
            str(count),
            "--trial-index",
            str(trial_index),
            "--workspace",
            str(workspace),
            "--phase",
            phase,
            *extra_arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout, parse_constant=lambda value: (_ for _ in ()).throw(
        ValueError(f"non-finite JSON value: {value}")
    ))


def _run_trial(
    executable: Path,
    count: int,
    trial_index: int,
    workspace: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    reconcile_workspace = workspace / "reconcile"
    cold_reopen_workspace = workspace / "cold-reopen"
    reconcile = _validate_reconcile(
        _run_phase(
            executable,
            count,
            trial_index,
            reconcile_workspace,
            "reconcile",
            environment,
        ),
        count,
        trial_index,
    )
    externally_seeded_state = _validate_state(
        _run_phase(
            executable,
            count,
            trial_index,
            cold_reopen_workspace,
            "seed-complete",
            environment,
        ),
        count,
        0,
    )
    reopen = _validate_reopen(
        _run_phase(
            executable,
            count,
            trial_index,
            cold_reopen_workspace,
            "reopen",
            environment,
        ),
        count,
        trial_index,
    )
    if (
        reconcile["persisted_state"] != externally_seeded_state
        or externally_seeded_state != reopen["persisted_state"]
    ):
        raise ValueError(
            "external complete seed, production persistence, and cold reopen differ"
        )
    if reconcile["queue_limits"] != reopen["queue_limits"]:
        raise ValueError("phase queue limits changed within one trial")
    if reconcile["process"]["launch_id"] == reopen["process"]["launch_id"]:
        raise ValueError("cold reopen reused the reconcile process")
    for field in PLATFORM_KEYS - {"thermal_state"}:
        if reconcile["platform"][field] != reopen["platform"][field]:
            raise ValueError(f"phase platform field changed within one trial: {field}")
    return {
        "trial_index": trial_index,
        "seed_state": reconcile["seed_state"],
        "persisted_state": reopen["persisted_state"],
        "queue_limits": reopen["queue_limits"],
        "processes": {
            "reconcile": reconcile["process"],
            "process_cold_reopen": reopen["process"],
        },
        "reconcile_duration_nanoseconds": reconcile["reconcile_duration_nanoseconds"],
        "process_cold_reopen_duration_nanoseconds": reopen["reopen_duration_nanoseconds"],
        "reconcile_memory": reconcile["memory"],
        "process_cold_reopen_memory": reopen["memory"],
        "platform": reopen["platform"],
        "thermal_states": {
            "reconcile": reconcile["platform"]["thermal_state"],
            "process_cold_reopen": reopen["platform"]["thermal_state"],
        },
        "reconciled_sequence_ids": reconcile["reconciled_sequence_ids"],
        "sequence_probes": reopen["sequence_probes"],
    }


def _run_streams(
    executable: Path,
    final_count: int,
    rate: int,
    duration_seconds: int,
    workspace: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    acknowledgement_count = rate * duration_seconds
    initial_count = final_count - acknowledgement_count
    if initial_count < 0:
        raise ValueError(
            "paced rate times duration must not exceed the stream frame count"
        )
    unpaced = _validate_unpaced_stream(
        _run_phase(
            executable,
            final_count,
            0,
            workspace / "unpaced",
            "unpaced-stream",
            environment,
        ),
        final_count,
    )
    paced = _validate_paced_stream(
        _run_phase(
            executable,
            final_count,
            0,
            workspace / "paced",
            "paced-stream",
            environment,
            (
                "--initial-count",
                str(initial_count),
                "--rate",
                str(rate),
                "--duration-seconds",
                str(duration_seconds),
            ),
        ),
        final_count,
        rate,
        duration_seconds,
    )
    if unpaced["persisted_state"] != paced["persisted_state"]:
        raise ValueError("paced and unpaced streams produced different exact ledgers")
    if unpaced["process"]["launch_id"] == paced["process"]["launch_id"]:
        raise ValueError("paced and unpaced streams reused one process launch")
    return {
        "final_acknowledged_frame_count": final_count,
        "unpaced": unpaced,
        "unpaced_acknowledgement_persistence": _duration_summary(
            unpaced["acknowledgement_durations_nanoseconds"]
        ),
        "paced": paced,
        "paced_acknowledgement_persistence": _duration_summary(
            paced["acknowledgement_durations_nanoseconds"]
        ),
        "process_launches_unique": True,
        "final_state_digest_stable": True,
    }


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _duration_summary(values: list[int]) -> dict[str, int]:
    return {
        "p50_nanoseconds": _percentile(values, 0.50),
        "p95_nanoseconds": _percentile(values, 0.95),
        "maximum_nanoseconds": max(values),
    }


def _aggregate(count: int, warmups: int, trials: list[dict[str, Any]]) -> dict[str, Any]:
    reconcile_durations = [
        int(trial["reconcile_duration_nanoseconds"]) for trial in trials
    ]
    reopen_durations = [
        int(trial["process_cold_reopen_duration_nanoseconds"]) for trial in trials
    ]
    payload_sizes = {trial["persisted_state"]["payload_bytes"] for trial in trials}
    envelope_sizes = {trial["persisted_state"]["envelope_bytes"] for trial in trials}
    payload_digests = {trial["persisted_state"]["payload_sha256"] for trial in trials}
    envelope_digests = {trial["persisted_state"]["envelope_sha256"] for trial in trials}
    queue_limits = {
        _canonical(trial["queue_limits"]) for trial in trials
    }
    process_launch_ids = [
        trial["processes"][phase]["launch_id"]
        for trial in trials
        for phase in ("reconcile", "process_cold_reopen")
    ]
    if len(process_launch_ids) != len(set(process_launch_ids)):
        raise ValueError("measured phases reused a process launch identity")
    if not all(len(values) == 1 for values in (
        payload_sizes,
        envelope_sizes,
        payload_digests,
        envelope_digests,
        queue_limits,
    )):
        raise ValueError(
            "durable state, digest, or queue limits changed across identical trials"
        )
    return {
        "acknowledged_frame_count": count,
        "warmup_trials": warmups,
        "measured_trials": len(trials),
        "queue_limits": json.loads(next(iter(queue_limits))),
        "persisted_state": {
            "payload_bytes": next(iter(payload_sizes)),
            "envelope_bytes": next(iter(envelope_sizes)),
            "payload_sha256": next(iter(payload_digests)),
            "envelope_sha256": next(iter(envelope_digests)),
        },
        "reconcile_persistence": _duration_summary(reconcile_durations),
        "process_cold_reopen": _duration_summary(reopen_durations),
        "memory": {
            "maximum_phase_footprint_delta_bytes": max(
                max(
                    trial["reconcile_memory"]["footprint_delta_bytes"],
                    trial["process_cold_reopen_memory"]["footprint_delta_bytes"],
                )
                for trial in trials
            ),
            "maximum_kernel_reported_peak_footprint_bytes": max(
                max(
                    trial["reconcile_memory"]["kernel_reported_peak_footprint_bytes"],
                    trial["process_cold_reopen_memory"][
                        "kernel_reported_peak_footprint_bytes"
                    ],
                )
                for trial in trials
            ),
        },
        "correctness": {
            "production_seed_open_validated": True,
            "process_cold_reopen_validated": True,
            "process_launches_unique": True,
            "exact_first_middle_last_duplicate_conflict": True,
            "state_digest_stable": True,
        },
        "gate_result": "not_evaluated_non_physical",
        "trials": trials,
    }


def _hard_gate_inputs(
    aggregates: list[dict[str, Any]],
    streams: dict[str, Any],
) -> dict[str, Any]:
    unpaced = streams["unpaced"]
    paced = streams["paced"]
    by_count = {
        aggregate["acknowledged_frame_count"]: aggregate
        for aggregate in aggregates
    }
    required_counts = (360, 720)
    required = {
        str(count): {
            "queue_limits": by_count[count]["queue_limits"],
            "state": by_count[count]["persisted_state"],
            "acknowledgement_persistence": by_count[count][
                "reconcile_persistence"
            ],
            "process_cold_reopen": by_count[count]["process_cold_reopen"],
            "memory": by_count[count]["memory"],
            "correctness": by_count[count]["correctness"],
        }
        for count in required_counts
        if count in by_count
    }
    memory_evidence = [
        aggregate["memory"] for aggregate in by_count.values()
        if aggregate["acknowledged_frame_count"] in required_counts
    ]
    memory_evidence.extend([unpaced["memory"], paced["memory"]])
    return {
        "required_counts": list(required_counts),
        "required_counts_present": all(count in by_count for count in required_counts),
        "per_count": required,
        "stream_final_state": unpaced["persisted_state"],
        "stream_queue_limits": {
            "unpaced": unpaced["queue_limits"],
            "paced": paced["queue_limits"],
        },
        "memory": {
            "maximum_phase_footprint_delta_bytes": max(
                evidence.get(
                    "maximum_phase_footprint_delta_bytes",
                    evidence.get("footprint_delta_bytes", 0),
                )
                for evidence in memory_evidence
            ),
            "maximum_kernel_reported_peak_footprint_bytes": max(
                evidence.get(
                    "maximum_kernel_reported_peak_footprint_bytes",
                    evidence.get("kernel_reported_peak_footprint_bytes", 0),
                )
                for evidence in memory_evidence
            ),
        },
        "sustained_acknowledgements_per_second": unpaced[
            "durable_acknowledgements_per_second"
        ],
        "paced": {
            "acknowledgements_per_second": paced["configuration"][
                "acknowledgements_per_second"
            ],
            "nominal_duration_seconds": paced["configuration"][
                "nominal_duration_seconds"
            ],
            "maximum_backlog_frames": paced["maximum_backlog_frames"],
            "backlog_at_nominal_end_frames": paced[
                "backlog_at_nominal_end_frames"
            ],
            "final_backlog_frames": paced["final_backlog_frames"],
            "drain_duration_nanoseconds": paced["drain_duration_nanoseconds"],
        },
        "correctness": {
            "unpaced_production_open_reconcile": True,
            "paced_production_open_reconcile": True,
            "stream_process_launches_unique": streams[
                "process_launches_unique"
            ],
            "required_per_count_evidence_present": all(
                count in by_count for count in required_counts
            ),
            "process_cold_reopen": all(
                by_count[count]["correctness"]["process_cold_reopen_validated"]
                for count in required_counts
                if count in by_count
            ),
            "process_launches_unique": all(
                by_count[count]["correctness"]["process_launches_unique"]
                for count in required_counts
                if count in by_count
            ),
            "exact_duplicate_conflict_after_restart": all(
                by_count[count]["correctness"][
                    "exact_first_middle_last_duplicate_conflict"
                ]
                for count in required_counts
                if count in by_count
            ),
            "exact_duplicate_conflict_after_stream": True,
            "state_digest_stable": (
                all(
                    by_count[count]["correctness"]["state_digest_stable"]
                    for count in required_counts
                    if count in by_count
                )
                and streams["final_state_digest_stable"]
            ),
        },
    }


def _hard_gate_status(
    acceptance_profile: bool,
    platform: dict[str, Any],
    inputs: dict[str, Any],
) -> str:
    if not platform["is_physical_device"]:
        return "not_evaluated_non_physical"
    if not platform["is_designated_ack_benchmark_device"]:
        return "not_evaluated_ineligible_device"
    if not platform["optimized_build"]:
        return "not_evaluated_unoptimized_build"
    if not acceptance_profile:
        return "not_evaluated_test_override"

    if (
        not isinstance(inputs, dict)
        or inputs.get("required_counts") != [360, 720]
        or not isinstance(inputs.get("required_counts_present"), bool)
        or not isinstance(inputs.get("per_count"), dict)
        or not isinstance(inputs.get("memory"), dict)
        or not isinstance(inputs.get("paced"), dict)
        or not isinstance(inputs.get("correctness"), dict)
        or set(inputs["correctness"]) != HARD_GATE_CORRECTNESS_KEYS
        or not {
            "maximum_phase_footprint_delta_bytes",
            "maximum_kernel_reported_peak_footprint_bytes",
        }.issubset(inputs["memory"])
        or not isinstance(
            inputs.get("sustained_acknowledgements_per_second"),
            (int, float),
        )
        or isinstance(
            inputs.get("sustained_acknowledgements_per_second"),
            bool,
        )
        or not math.isfinite(
            float(inputs.get("sustained_acknowledgements_per_second", math.nan))
        )
        or not {
            "acknowledgements_per_second",
            "nominal_duration_seconds",
            "maximum_backlog_frames",
            "final_backlog_frames",
        }.issubset(inputs["paced"])
    ):
        return "failed"
    memory = inputs["memory"]
    paced = inputs["paced"]
    per_count_checks: list[bool] = []
    for count in inputs["required_counts"]:
        evidence = inputs["per_count"].get(str(count))
        if (
            not isinstance(evidence, dict)
            or not {
                "state",
                "acknowledgement_persistence",
                "process_cold_reopen",
            }.issubset(evidence)
            or not isinstance(evidence["state"], dict)
            or not isinstance(evidence["acknowledgement_persistence"], dict)
            or not isinstance(evidence["process_cold_reopen"], dict)
            or not {"payload_bytes", "envelope_bytes"}.issubset(
                evidence["state"]
            )
            or not {
                "p50_nanoseconds",
                "p95_nanoseconds",
                "maximum_nanoseconds",
            }.issubset(evidence["acknowledgement_persistence"])
            or not {
                "p50_nanoseconds",
                "p95_nanoseconds",
                "maximum_nanoseconds",
            }.issubset(evidence["process_cold_reopen"])
        ):
            per_count_checks.append(False)
            continue
        state = evidence["state"]
        ack = evidence["acknowledgement_persistence"]
        reopen = evidence["process_cold_reopen"]
        per_count_checks.extend((
            state["payload_bytes"] < HARD_GATE_BUDGETS["payload_bytes_less_than"],
            state["envelope_bytes"] < HARD_GATE_BUDGETS["envelope_bytes_less_than"],
            ack["p50_nanoseconds"]
            <= HARD_GATE_BUDGETS["ack_p50_nanoseconds_at_most"],
            ack["p95_nanoseconds"]
            <= HARD_GATE_BUDGETS["ack_p95_nanoseconds_at_most"],
            ack["maximum_nanoseconds"]
            <= HARD_GATE_BUDGETS["ack_max_nanoseconds_at_most"],
            reopen["p50_nanoseconds"]
            <= HARD_GATE_BUDGETS["cold_reopen_p50_nanoseconds_at_most"],
            reopen["p95_nanoseconds"]
            <= HARD_GATE_BUDGETS["cold_reopen_p95_nanoseconds_at_most"],
            reopen["maximum_nanoseconds"]
            <= HARD_GATE_BUDGETS["cold_reopen_max_nanoseconds_at_most"],
        ))
    checks = (
        inputs["required_counts_present"],
        *per_count_checks,
        memory["maximum_phase_footprint_delta_bytes"]
        <= HARD_GATE_BUDGETS["footprint_delta_bytes_at_most"],
        memory["maximum_kernel_reported_peak_footprint_bytes"]
        <= HARD_GATE_BUDGETS["kernel_peak_footprint_bytes_at_most"],
        inputs["sustained_acknowledgements_per_second"]
        >= HARD_GATE_BUDGETS[
            "sustained_acknowledgements_per_second_at_least"
        ],
        paced["acknowledgements_per_second"]
        == HARD_GATE_BUDGETS["paced_acknowledgements_per_second"],
        paced["nominal_duration_seconds"]
        == HARD_GATE_BUDGETS["paced_duration_seconds"],
        paced["maximum_backlog_frames"]
        <= HARD_GATE_BUDGETS["paced_backlog_frames_at_most"],
        paced["final_backlog_frames"]
        == HARD_GATE_BUDGETS["paced_final_backlog_frames"],
        all(
            isinstance(value, bool) and value
            for value in inputs["correctness"].values()
        ),
    )
    return "passed" if all(checks) else "failed"


def _compile(repository: Path, build_root: Path) -> tuple[Path, str, dict[str, str]]:
    build_root.mkdir(parents=True, exist_ok=True)
    executable = build_root / "live-sender-ack-benchmark"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(build_root / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(build_root / "swift-module-cache")
    sources = [
        repository / "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveAuthContract.swift",
        repository / "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveSenderQueue.swift",
        repository / "tests/swift/LiveSenderAckBenchmarkCore.swift",
        repository / "tests/swift/LiveSenderAckBenchmarkCLI.swift",
    ]
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-swift-version",
            "5",
            "-O",
            "-whole-module-optimization",
            "-D",
            "CAPTURE_SPLAT_ACK_BENCHMARK_OPTIMIZED",
            "-parse-as-library",
            *map(str, sources),
            "-o",
            str(executable),
        ],
        check=True,
        env=environment,
    )
    version = subprocess.run(
        ["xcrun", "swiftc", "--version"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    fingerprints = {
        str(source.relative_to(repository)): _sha256(source.read_bytes())
        for source in sources
    }
    return executable, version, environment | {
        "CAPTURE_SPLAT_ACK_BENCHMARK_SOURCE_FINGERPRINTS": _canonical(
            fingerprints
        ).decode("utf-8")
    }


def _output_is_inside_git(output: Path) -> bool:
    for candidate in (output.parent, *output.parent.parents):
        if (candidate / ".git").exists():
            return True
    return False


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.incoming")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parse_counts(value: str) -> tuple[int, ...]:
    counts = tuple(int(part) for part in value.split(","))
    if not counts or any(count <= 0 for count in counts) or len(set(counts)) != len(counts):
        raise argparse.ArgumentTypeError("counts must be unique positive integers")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark exact durable Capture Splat ACK identities."
    )
    parser.add_argument(
        "--counts",
        type=_parse_counts,
        default=TRIAL_COUNTS,
        help="Comma-separated frame counts (default: 360,720,1000,10000,50000).",
    )
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--stream-count", type=int, default=DEFAULT_STREAM_COUNT)
    parser.add_argument(
        "--paced-rate",
        type=int,
        default=DEFAULT_PACED_ACKNOWLEDGEMENTS_PER_SECOND,
    )
    parser.add_argument(
        "--paced-duration-seconds",
        type=int,
        default=DEFAULT_PACED_DURATION_SECONDS,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.warmups < 0 or arguments.trials < 1:
        parser.error("warmups must be non-negative and trials must be positive")
    if (
        arguments.stream_count <= 0
        or arguments.paced_rate <= 0
        or arguments.paced_duration_seconds <= 0
        or arguments.paced_rate * arguments.paced_duration_seconds
        > arguments.stream_count
    ):
        parser.error(
            "stream count, paced rate, and duration must be positive and "
            "rate times duration must not exceed stream count"
        )
    if arguments.stream_count not in arguments.counts:
        parser.error("stream count must also be present in the count matrix")

    repository = Path(__file__).resolve().parents[1]
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    output = (
        arguments.output
        or Path(tempfile.gettempdir())
        / f"capture-splat-live-ack-benchmark-{generated_at.replace(':', '-')}.json"
    ).expanduser().resolve()
    if _output_is_inside_git(output):
        parser.error("the checksummed benchmark report must be written outside Git")

    with tempfile.TemporaryDirectory(prefix="capture-splat-ack-benchmark-") as temporary:
        root = Path(temporary)
        executable, swift_version, environment = _compile(repository, root / "build")
        aggregates: list[dict[str, Any]] = []
        for count in arguments.counts:
            for index in range(arguments.warmups):
                workspace = root / "workspaces" / f"warmup-{count}-{index}"
                _run_trial(executable, count, index, workspace, environment)
                shutil.rmtree(workspace)
            measured: list[dict[str, Any]] = []
            for index in range(arguments.trials):
                workspace = root / "workspaces" / f"trial-{count}-{index}"
                measured.append(
                    _run_trial(executable, count, index, workspace, environment)
                )
                shutil.rmtree(workspace)
            aggregates.append(_aggregate(count, arguments.warmups, measured))

        streams = _run_streams(
            executable,
            arguments.stream_count,
            arguments.paced_rate,
            arguments.paced_duration_seconds,
            root / "workspaces" / "progressive-streams",
            environment,
        )
        fingerprints = json.loads(
            environment["CAPTURE_SPLAT_ACK_BENCHMARK_SOURCE_FINGERPRINTS"]
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        acceptance_profile = (
            arguments.counts == TRIAL_COUNTS
            and arguments.warmups == DEFAULT_WARMUPS
            and arguments.trials == DEFAULT_TRIALS
            and arguments.stream_count == DEFAULT_STREAM_COUNT
            and arguments.paced_rate
            == DEFAULT_PACED_ACKNOWLEDGEMENTS_PER_SECOND
            and arguments.paced_duration_seconds
            == DEFAULT_PACED_DURATION_SECONDS
        )
        gate_inputs = _hard_gate_inputs(aggregates, streams)
        gate_platform = streams["unpaced"]["platform"]
        if any(
            streams["paced"]["platform"][field] != gate_platform[field]
            for field in PLATFORM_KEYS - {"thermal_state"}
        ):
            raise ValueError("paced and unpaced stream platforms differ")
        hard_gate_status = _hard_gate_status(
            acceptance_profile,
            gate_platform,
            gate_inputs,
        )
        payload = {
            "schema": REPORT_SCHEMA,
            "generated_at": generated_at,
            "repository_commit": commit,
            "run_profile": "acceptance" if acceptance_profile else "test_override",
            "build": {
                "compiler": swift_version,
                "optimization": "-O -whole-module-optimization",
                "optimized_build_marker": True,
                "source_fingerprints": fingerprints,
            },
            "matrix": {
                "counts": list(arguments.counts),
                "warmup_trials_per_count": arguments.warmups,
                "measured_trials_per_count": arguments.trials,
                "stream_count": arguments.stream_count,
                "paced_acknowledgements_per_second": arguments.paced_rate,
                "paced_duration_seconds": arguments.paced_duration_seconds,
            },
            "hard_gate": {
                "status": hard_gate_status,
                "eligible_device_models": list(ELIGIBLE_MODELS),
                "required_evidence": (
                    "optimized physical-device run on the designated "
                    "iPhone 16 Pro Max (iPhone17,2)"
                ),
                "budgets": HARD_GATE_BUDGETS,
                "evaluation_inputs": gate_inputs,
            },
            "capture_isolation": {
                "capture_loop_connected": False,
                "writer_drops": "unmeasured",
                "capture_wait": "unmeasured",
                "keyframe_acceptance_changed": False,
            },
            "physical_run_fields": {
                "thermal_states": {
                    "unpaced": streams["unpaced"]["thermal_states"],
                    "paced": streams["paced"]["thermal_states"],
                },
                "sustained_acknowledgements_per_second": streams["unpaced"][
                    "durable_acknowledgements_per_second"
                ],
                "paced_backlog": {
                    "maximum_frames": streams["paced"][
                        "maximum_backlog_frames"
                    ],
                    "at_nominal_end_frames": streams["paced"][
                        "backlog_at_nominal_end_frames"
                    ],
                    "final_frames": streams["paced"]["final_backlog_frames"],
                    "drain_duration_nanoseconds": streams["paced"][
                        "drain_duration_nanoseconds"
                    ],
                },
                "capture_writer_interference": "unmeasured",
            },
            "progressive_streams": streams,
            "aggregates": aggregates,
        }
        payload_data = _canonical(payload)
        envelope = {
            "schema": ENVELOPE_SCHEMA,
            "payload_sha256": _sha256(payload_data),
            "payload_base64": base64.b64encode(payload_data).decode("ascii"),
        }
        _atomic_write(output, _canonical(envelope))
        print(_canonical({
            "schema": "capture_splat.live_sender_ack_benchmark_summary.v0.2",
            "report": str(output),
            "payload_sha256": envelope["payload_sha256"],
            "hard_gate_status": hard_gate_status,
        }).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
