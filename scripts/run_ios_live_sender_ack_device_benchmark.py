#!/usr/bin/env python3
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


COUNTS = (360, 720, 1_000, 10_000, 50_000)
HARD_GATE_COUNTS = (360, 720)
WARMUP_TRIALS = 5
MEASURED_TRIALS = 30
ELIGIBLE_MODELS = ("iPhone13,3", "iPhone13,4")
SCHEME = "CaptureSplatAckBenchmarks"
TARGET = "CaptureSplatAckBenchmarks"
TEST_CLASS = "LiveSenderAckBenchmarkTests"
CONFIGURATION = "Release"
MAXIMUM_JSON_BYTES = 16 * 1024 * 1024
BENCHMARK_MAXIMUM_BYTES = (2**63 - 1) // 4
PRODUCTION_STATE_PAYLOAD_CAP_BYTES = 48 * 1024 * 1024 - 4096

REPORT_SCHEMA = "capture_splat.live_sender_ack_device_benchmark_report.v0.1"
ENVELOPE_SCHEMA = (
    "capture_splat.live_sender_ack_device_benchmark_report_envelope.v0.1"
)
SUMMARY_SCHEMA = "capture_splat.live_sender_ack_device_benchmark_summary.v0.1"
PLAN_SCHEMA = "capture_splat.live_sender_ack_device_benchmark_plan.v0.1"
FIXTURE_SCHEMA = "capture_splat.live_sender_ack_device_benchmark_fixture.v0.1"
RECONCILE_SCHEMA = "capture_splat.live_sender_ack_benchmark_reconcile_phase.v0.1"
REOPEN_SCHEMA = "capture_splat.live_sender_ack_benchmark_reopen_phase.v0.1"
UNPACED_SCHEMA = (
    "capture_splat.live_sender_ack_benchmark_unpaced_stream_phase.v0.1"
)
PACED_SCHEMA = "capture_splat.live_sender_ack_benchmark_paced_stream_phase.v0.1"

HARD_GATE_BUDGETS = {
    "payload_bytes_less_than": 24 * 1024 * 1024,
    "envelope_bytes_less_than": 32 * 1024 * 1024,
    "ack_p50_nanoseconds_at_most": 50_000_000,
    "ack_p95_nanoseconds_at_most": 100_000_000,
    "ack_maximum_nanoseconds_at_most": 200_000_000,
    "cold_reopen_p50_nanoseconds_at_most": 250_000_000,
    "cold_reopen_p95_nanoseconds_at_most": 500_000_000,
    "cold_reopen_maximum_nanoseconds_at_most": 1_000_000_000,
    "footprint_delta_bytes_at_most": 16 * 1024 * 1024,
    "kernel_peak_footprint_bytes_at_most": 128 * 1024 * 1024,
    "unpaced_acknowledgements_per_second_at_least": 10.0,
    "paced_acknowledgements_per_second": 5,
    "paced_duration_seconds": 60,
    "paced_maximum_backlog_frames_at_most": 8,
    "paced_final_backlog_frames": 0,
}

FINGERPRINT_PATHS = (
    "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveSenderQueue.swift",
    "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveAuthContract.swift",
    "tests/swift/LiveSenderAckBenchmarkCore.swift",
    "tests/swift/LiveSenderAckBenchmarkTests.swift",
    (
        "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/xcshareddata/"
        "xcschemes/CaptureSplatAckBenchmarks.xcscheme"
    ),
    "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/project.pbxproj",
    "scripts/run_ios_live_sender_ack_device_benchmark.py",
)

STATE_KEYS = {
    "payload_bytes",
    "envelope_bytes",
    "payload_sha256",
    "envelope_sha256",
    "acknowledged_frame_count",
    "pending_frame_count",
}
QUEUE_LIMIT_KEYS = {
    "maximum_frames",
    "maximum_bytes",
    "maximum_in_flight",
    "scope",
}
PROCESS_KEYS = {"launch_id", "process_id"}
PLATFORM_KEYS = {
    "operating_system",
    "operating_system_version",
    "machine",
    "architecture",
    "thermal_state",
    "is_physical_device",
    "is_oldest_supported_lidar_iphone",
    "optimized_build",
    "physical_gate_result",
}
MEMORY_KEYS = {
    "footprint_before_bytes",
    "footprint_after_bytes",
    "footprint_delta_bytes",
    "kernel_reported_peak_footprint_bytes",
}
PROBE_KEYS = {
    "sequence_id",
    "identical_disposition",
    "conflicting_reference_rejected",
}
STREAM_CORRECTNESS_KEYS = {
    "production_open_validated_external_state",
    "every_acknowledgement_reconciled_exactly_one_frame",
    "sequence_probes",
}
THERMAL_STATES = {"nominal", "fair", "serious", "critical", "unknown"}
PHYSICAL_GATE_RESULTS = {
    "not_evaluated_non_physical",
    "not_evaluated_ineligible_device",
    "not_evaluated_unoptimized_build",
    "physical_trial_requires_aggregate_gate_evaluation",
}
STREAM_GATE_RESULTS = {
    "not_evaluated_non_physical",
    "not_evaluated_ineligible_device",
    "not_evaluated_unoptimized_build",
    "measurement_passed_requires_aggregate_evaluation",
    "failed",
}
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,128}$")
LAUNCH_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)


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


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _load_json_bytes(
    data: bytes,
    label: str,
    *,
    require_canonical: bool = False,
) -> object:
    if not data or len(data) > MAXIMUM_JSON_BYTES:
        raise ValueError(f"{label} is empty or exceeds the JSON size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not strict JSON") from error
    if require_canonical and _canonical(value) != data:
        raise ValueError(f"{label} is not canonical JSON")
    return value


def _load_json_file(path: Path, label: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not a regular file")
    return _load_json_bytes(path.read_bytes(), label)


def _require_dict(
    value: object,
    expected_keys: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError(f"{label} has missing or additional fields")
    return value


def _require_allowed_dict(
    value: object,
    required_keys: set[str],
    allowed_keys: set[str],
    label: str,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required_keys.issubset(value)
        or not set(value).issubset(allowed_keys)
    ):
        raise ValueError(f"{label} has missing or additional fields")
    return value


def _require_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _require_int(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f"{label} is outside the accepted integer range")
    return value


def _require_number(
    value: object,
    label: str,
    *,
    minimum: float = 0.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} is non-finite or outside its range")
    return result


def _require_string(
    value: object,
    label: str,
    *,
    maximum_bytes: int = 4096,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise ValueError(f"{label} is not a bounded non-empty string")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} is not a strict SHA-256")
    return value


def _validate_state(
    value: object,
    *,
    acknowledged_count: int,
    pending_count: int,
    label: str,
) -> dict[str, Any]:
    state = _require_dict(value, STATE_KEYS, label)
    _require_int(state["payload_bytes"], f"{label}.payload_bytes", minimum=1)
    _require_int(state["envelope_bytes"], f"{label}.envelope_bytes", minimum=1)
    _require_sha256(state["payload_sha256"], f"{label}.payload_sha256")
    _require_sha256(state["envelope_sha256"], f"{label}.envelope_sha256")
    if state["acknowledged_frame_count"] != acknowledged_count:
        raise ValueError(f"{label} has the wrong acknowledged frame count")
    if state["pending_frame_count"] != pending_count:
        raise ValueError(f"{label} has the wrong pending frame count")
    return state


def _expected_queue_limits(count: int) -> dict[str, object]:
    return {
        "maximum_frames": count,
        "maximum_bytes": BENCHMARK_MAXIMUM_BYTES,
        "maximum_in_flight": min(8, count),
        "scope": "benchmark_only_not_product_cap",
    }


def _validate_queue_limits(value: object, count: int) -> dict[str, Any]:
    limits = _require_dict(value, QUEUE_LIMIT_KEYS, "queue limits")
    if limits != _expected_queue_limits(count):
        raise ValueError("benchmark queue limits changed or claim product scope")
    return limits


def _validate_process(value: object) -> dict[str, Any]:
    process = _require_dict(value, PROCESS_KEYS, "process evidence")
    if (
        not isinstance(process["launch_id"], str)
        or not LAUNCH_ID_PATTERN.fullmatch(process["launch_id"])
    ):
        raise ValueError("process launch_id is not a lowercase UUID")
    _require_int(
        process["process_id"],
        "process process_id",
        minimum=1,
        maximum=2**31 - 1,
    )
    return process


def _validate_configuration(
    value: object,
    count: int,
) -> dict[str, Any]:
    configuration = _require_dict(
        value,
        {"acknowledged_frame_count", "trial_index"},
        "benchmark configuration",
    )
    if configuration["acknowledged_frame_count"] != count:
        raise ValueError("benchmark configuration has the wrong frame count")
    _require_int(configuration["trial_index"], "trial_index")
    return configuration


def _validate_memory(value: object) -> dict[str, Any]:
    memory = _require_dict(value, MEMORY_KEYS, "phase memory")
    for key in MEMORY_KEYS:
        _require_int(memory[key], f"phase memory.{key}")
    return memory


def _expected_physical_gate(platform: dict[str, Any]) -> str:
    if not platform["is_physical_device"]:
        return "not_evaluated_non_physical"
    if not platform["is_oldest_supported_lidar_iphone"]:
        return "not_evaluated_ineligible_device"
    if not platform["optimized_build"]:
        return "not_evaluated_unoptimized_build"
    return "physical_trial_requires_aggregate_gate_evaluation"


def _validate_platform(value: object) -> dict[str, Any]:
    platform = _require_dict(value, PLATFORM_KEYS, "platform evidence")
    for key in (
        "operating_system",
        "operating_system_version",
        "machine",
        "architecture",
        "thermal_state",
        "physical_gate_result",
    ):
        _require_string(platform[key], f"platform.{key}", maximum_bytes=512)
    for key in (
        "is_physical_device",
        "is_oldest_supported_lidar_iphone",
        "optimized_build",
    ):
        _require_bool(platform[key], f"platform.{key}")
    if platform["thermal_state"] not in THERMAL_STATES:
        raise ValueError("platform thermal state is unknown to this contract")
    if platform["physical_gate_result"] not in PHYSICAL_GATE_RESULTS:
        raise ValueError("platform physical gate result is invalid")
    expected_oldest = (
        platform["is_physical_device"]
        and platform["machine"] in ELIGIBLE_MODELS
    )
    if platform["is_oldest_supported_lidar_iphone"] is not expected_oldest:
        raise ValueError("platform oldest-supported-device claim is inconsistent")
    if platform["is_physical_device"] and (
        platform["operating_system"] != "ios"
        or platform["architecture"] != "arm64"
    ):
        raise ValueError("physical-device platform identity is inconsistent")
    if platform["physical_gate_result"] != _expected_physical_gate(platform):
        raise ValueError("platform physical gate result is inconsistent")
    return platform


def _expected_probe_sequences(count: int) -> list[int]:
    return sorted({1, (count + 1) // 2, count})


def _validate_probes(value: object, count: int) -> list[dict[str, Any]]:
    probes = _require_list(value, "sequence probes")
    expected = _expected_probe_sequences(count)
    if len(probes) != len(expected):
        raise ValueError("sequence probes are incomplete")
    validated: list[dict[str, Any]] = []
    for probe, sequence_id in zip(probes, expected):
        item = _require_dict(probe, PROBE_KEYS, "sequence probe")
        if (
            item["sequence_id"] != sequence_id
            or item["identical_disposition"] != "duplicate"
            or item["conflicting_reference_rejected"] is not True
        ):
            raise ValueError("exact duplicate/conflict probe failed")
        validated.append(item)
    return validated


def _validate_reconcile(value: object, count: int) -> dict[str, Any]:
    phase = _require_dict(
        value,
        {
            "schema",
            "configuration",
            "queue_limits",
            "process",
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
    _validate_configuration(phase["configuration"], count)
    _validate_queue_limits(phase["queue_limits"], count)
    _validate_process(phase["process"])
    _validate_state(
        phase["seed_state"],
        acknowledged_count=count - 1,
        pending_count=1,
        label="reconcile seed state",
    )
    _validate_state(
        phase["persisted_state"],
        acknowledged_count=count,
        pending_count=0,
        label="reconcile persisted state",
    )
    _require_int(
        phase["reconcile_duration_nanoseconds"],
        "reconcile duration",
        minimum=1,
    )
    _validate_memory(phase["memory"])
    _validate_platform(phase["platform"])
    if phase["reconciled_sequence_ids"] != [count]:
        raise ValueError("reconcile phase did not persist exactly the final identity")
    return phase


def _validate_reopen(value: object, count: int) -> dict[str, Any]:
    phase = _require_dict(
        value,
        {
            "schema",
            "configuration",
            "queue_limits",
            "process",
            "persisted_state",
            "reopen_duration_nanoseconds",
            "memory",
            "platform",
            "sequence_probes",
        },
        "cold-reopen phase",
    )
    if phase["schema"] != REOPEN_SCHEMA:
        raise ValueError("unexpected cold-reopen phase schema")
    _validate_configuration(phase["configuration"], count)
    _validate_queue_limits(phase["queue_limits"], count)
    _validate_process(phase["process"])
    _validate_state(
        phase["persisted_state"],
        acknowledged_count=count,
        pending_count=0,
        label="cold-reopen persisted state",
    )
    _require_int(
        phase["reopen_duration_nanoseconds"],
        "cold-reopen duration",
        minimum=1,
    )
    _validate_memory(phase["memory"])
    _validate_platform(phase["platform"])
    _validate_probes(phase["sequence_probes"], count)
    return phase


def _validate_thermal_states(value: object) -> dict[str, Any]:
    states = _require_dict(value, {"before", "after"}, "thermal states")
    for key in ("before", "after"):
        if states[key] not in THERMAL_STATES:
            raise ValueError("stream thermal state is invalid")
    return states


def _validate_stream_correctness(
    value: object,
    count: int,
) -> dict[str, Any]:
    correctness = _require_dict(
        value,
        STREAM_CORRECTNESS_KEYS,
        "stream correctness",
    )
    if (
        correctness["production_open_validated_external_state"] is not True
        or correctness["every_acknowledgement_reconciled_exactly_one_frame"]
        is not True
    ):
        raise ValueError("stream correctness evidence is false")
    _validate_probes(correctness["sequence_probes"], count)
    return correctness


def _validate_durations(
    value: object,
    expected_count: int,
    label: str,
) -> list[int]:
    durations = _require_list(value, label)
    if len(durations) != expected_count:
        raise ValueError(f"{label} has the wrong sample count")
    return [
        _require_int(item, f"{label} sample", minimum=1)
        for item in durations
    ]


def _duration_summary(values: list[int]) -> dict[str, int]:
    if not values:
        raise ValueError("duration summary requires at least one sample")
    ordered = sorted(values)

    def percentile(fraction: float) -> int:
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]

    return {
        "sample_count": len(values),
        "p50_nanoseconds": percentile(0.50),
        "p95_nanoseconds": percentile(0.95),
        "maximum_nanoseconds": ordered[-1],
    }


def _latency_passed(summary: dict[str, int], *, reopen: bool) -> bool:
    prefix = "cold_reopen" if reopen else "ack"
    return (
        summary["p50_nanoseconds"]
        <= HARD_GATE_BUDGETS[f"{prefix}_p50_nanoseconds_at_most"]
        and summary["p95_nanoseconds"]
        <= HARD_GATE_BUDGETS[f"{prefix}_p95_nanoseconds_at_most"]
        and summary["maximum_nanoseconds"]
        <= HARD_GATE_BUDGETS[f"{prefix}_maximum_nanoseconds_at_most"]
    )


def _state_size_passed(state: dict[str, Any]) -> bool:
    return (
        state["payload_bytes"]
        < HARD_GATE_BUDGETS["payload_bytes_less_than"]
        and state["envelope_bytes"]
        < HARD_GATE_BUDGETS["envelope_bytes_less_than"]
    )


def _memory_passed(memory: dict[str, Any]) -> bool:
    return (
        memory["footprint_delta_bytes"]
        <= HARD_GATE_BUDGETS["footprint_delta_bytes_at_most"]
        and memory["kernel_reported_peak_footprint_bytes"]
        <= HARD_GATE_BUDGETS["kernel_peak_footprint_bytes_at_most"]
    )


def _expected_stream_gate(
    platform: dict[str, Any],
    state: dict[str, Any],
    durations: list[int],
    memory: dict[str, Any],
    *,
    throughput: float | None,
    maximum_backlog: int | None,
    final_backlog: int | None,
) -> str:
    physical_gate = _expected_physical_gate(platform)
    if physical_gate != "physical_trial_requires_aggregate_gate_evaluation":
        return physical_gate
    passed = _state_size_passed(state)
    passed = passed and _latency_passed(_duration_summary(durations), reopen=False)
    passed = passed and _memory_passed(memory)
    if throughput is not None:
        passed = (
            passed
            and throughput
            >= HARD_GATE_BUDGETS[
                "unpaced_acknowledgements_per_second_at_least"
            ]
        )
    if maximum_backlog is not None:
        passed = (
            passed
            and maximum_backlog
            <= HARD_GATE_BUDGETS[
                "paced_maximum_backlog_frames_at_most"
            ]
        )
    if final_backlog is not None:
        passed = (
            passed
            and final_backlog
            == HARD_GATE_BUDGETS["paced_final_backlog_frames"]
        )
    return (
        "measurement_passed_requires_aggregate_evaluation"
        if passed
        else "failed"
    )


def _validate_unpaced(value: object) -> dict[str, Any]:
    phase = _require_dict(
        value,
        {
            "schema",
            "final_acknowledged_frame_count",
            "queue_limits",
            "process",
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
    if (
        phase["schema"] != UNPACED_SCHEMA
        or phase["final_acknowledged_frame_count"] != 720
    ):
        raise ValueError("unexpected unpaced stream schema or frame count")
    _validate_queue_limits(phase["queue_limits"], 720)
    _validate_process(phase["process"])
    _validate_state(
        phase["seed_state"],
        acknowledged_count=0,
        pending_count=720,
        label="unpaced seed state",
    )
    persisted = _validate_state(
        phase["persisted_state"],
        acknowledged_count=720,
        pending_count=0,
        label="unpaced persisted state",
    )
    durations = _validate_durations(
        phase["acknowledgement_durations_nanoseconds"],
        720,
        "unpaced ACK durations",
    )
    elapsed = _require_int(
        phase["elapsed_nanoseconds"],
        "unpaced elapsed duration",
        minimum=1,
    )
    throughput = _require_number(
        phase["durable_acknowledgements_per_second"],
        "unpaced throughput",
        minimum=0.000_001,
    )
    calculated = 720 * 1_000_000_000.0 / elapsed
    if not math.isclose(throughput, calculated, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("unpaced throughput does not match elapsed evidence")
    memory = _validate_memory(phase["memory"])
    _validate_thermal_states(phase["thermal_states"])
    platform = _validate_platform(phase["platform"])
    _validate_stream_correctness(phase["correctness"], 720)
    if phase["gate_result"] not in STREAM_GATE_RESULTS:
        raise ValueError("unpaced stream gate result is invalid")
    if phase["gate_result"] != _expected_stream_gate(
        platform,
        persisted,
        durations,
        memory,
        throughput=throughput,
        maximum_backlog=None,
        final_backlog=None,
    ):
        raise ValueError("unpaced stream gate result is inconsistent")
    return phase


def _validate_paced(value: object) -> dict[str, Any]:
    phase = _require_dict(
        value,
        {
            "schema",
            "configuration",
            "queue_limits",
            "process",
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
    if phase["schema"] != PACED_SCHEMA:
        raise ValueError("unexpected paced stream schema")
    configuration = _require_dict(
        phase["configuration"],
        {
            "initial_acknowledged_frame_count",
            "final_acknowledged_frame_count",
            "acknowledgement_count",
            "acknowledgements_per_second",
            "nominal_duration_seconds",
        },
        "paced stream configuration",
    )
    if configuration != {
        "initial_acknowledged_frame_count": 420,
        "final_acknowledged_frame_count": 720,
        "acknowledgement_count": 300,
        "acknowledgements_per_second": 5,
        "nominal_duration_seconds": 60,
    }:
        raise ValueError("paced stream configuration changed")
    _validate_queue_limits(phase["queue_limits"], 720)
    _validate_process(phase["process"])
    _validate_state(
        phase["seed_state"],
        acknowledged_count=420,
        pending_count=300,
        label="paced seed state",
    )
    persisted = _validate_state(
        phase["persisted_state"],
        acknowledged_count=720,
        pending_count=0,
        label="paced persisted state",
    )
    durations = _validate_durations(
        phase["acknowledgement_durations_nanoseconds"],
        300,
        "paced ACK durations",
    )
    elapsed = _require_int(
        phase["elapsed_nanoseconds"],
        "paced elapsed duration",
        minimum=60_000_000_000,
    )
    drain = _require_int(
        phase["drain_duration_nanoseconds"],
        "paced drain duration",
    )
    if drain > elapsed:
        raise ValueError("paced drain duration exceeds total elapsed time")
    maximum_backlog = _require_int(
        phase["maximum_backlog_frames"],
        "paced maximum backlog",
        maximum=300,
    )
    backlog_at_end = _require_int(
        phase["backlog_at_nominal_end_frames"],
        "paced backlog at nominal end",
        maximum=300,
    )
    final_backlog = _require_int(
        phase["final_backlog_frames"],
        "paced final backlog",
        maximum=300,
    )
    if backlog_at_end > maximum_backlog:
        raise ValueError("paced nominal-end backlog exceeds recorded maximum")
    memory = _validate_memory(phase["memory"])
    _validate_thermal_states(phase["thermal_states"])
    platform = _validate_platform(phase["platform"])
    _validate_stream_correctness(phase["correctness"], 720)
    if phase["gate_result"] not in STREAM_GATE_RESULTS:
        raise ValueError("paced stream gate result is invalid")
    if phase["gate_result"] != _expected_stream_gate(
        platform,
        persisted,
        durations,
        memory,
        throughput=None,
        maximum_backlog=maximum_backlog,
        final_backlog=final_backlog,
    ):
        raise ValueError("paced stream gate result is inconsistent")
    return phase


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git(
    repository: Path,
    arguments: list[str],
) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_fingerprints(repository: Path) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for relative in FINGERPRINT_PATHS:
        path = repository / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"fingerprinted source is missing or a symlink: {relative}")
        fingerprints[relative] = _sha256(path.read_bytes())
    return fingerprints


def _repository_identity(
    repository: Path,
    *,
    require_clean: bool,
) -> dict[str, Any]:
    top = Path(_git(repository, ["rev-parse", "--show-toplevel"])).resolve()
    if top != repository.resolve():
        raise ValueError("runner repository root does not match Git")
    commit = _git(repository, ["rev-parse", "HEAD"])
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("repository HEAD is not a full lowercase commit")
    status = _git(
        repository,
        ["status", "--porcelain=v1", "--untracked-files=all"],
    )
    clean = status == ""
    if require_clean and not clean:
        raise ValueError(
            "acceptance device benchmark requires a completely clean repository"
        )
    return {
        "commit": commit,
        "clean": clean,
        "source_fingerprints": _source_fingerprints(repository),
    }


def _output_is_inside_git(output: Path) -> bool:
    for candidate in (output.parent, *output.parent.parents):
        if (candidate / ".git").exists():
            return True
    return False


def _resolve_output(value: Path | None) -> Path:
    if value is None:
        timestamp = (
            dt.datetime.now(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            .replace(":", "-")
        )
        value = Path(tempfile.gettempdir()) / (
            f"capture-splat-live-ack-device-benchmark-{timestamp}.json"
        )
    output = value.expanduser().resolve()
    if _output_is_inside_git(output):
        raise ValueError("the checksummed benchmark report must be written outside Git")
    if output.exists() or output.is_symlink():
        raise ValueError("the benchmark report output already exists")
    return output


def _atomic_write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.incoming"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ValueError("the benchmark report output already exists") from error
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_envelope(path: Path, payload: dict[str, Any]) -> dict[str, str]:
    payload_data = _canonical(payload)
    envelope = {
        "schema": ENVELOPE_SCHEMA,
        "payload_sha256": _sha256(payload_data),
        "payload_base64": base64.b64encode(payload_data).decode("ascii"),
    }
    _atomic_write_new(path, _canonical(envelope))
    return {
        "report": str(path),
        "payload_sha256": envelope["payload_sha256"],
    }


def _method_name(phase: str, count: int | None = None) -> str:
    if phase == "reconcile" and count is not None:
        return f"testReconcileAcknowledgedFrames{count}"
    if phase == "cold_reopen" and count is not None:
        return f"testColdReopenAcknowledgedFrames{count}"
    if phase == "unpaced":
        return "testUnpacedAcknowledgementStream720"
    if phase == "paced":
        return "testPacedAcknowledgementStream720"
    raise ValueError("unknown benchmark test phase")


def _test_identifier(method: str) -> str:
    return f"{TARGET}/{TEST_CLASS}/{method}"


def _expected_attachment_name(
    phase: str,
    count: int | None,
) -> str:
    if phase in {"reconcile", "cold_reopen"} and count is not None:
        return (
            "capture_splat.live_sender_ack_benchmark."
            f"{phase}.{count}.0.json"
        )
    if phase == "unpaced":
        return "capture_splat.live_sender_ack_benchmark.unpaced.720.json"
    if phase == "paced":
        return "capture_splat.live_sender_ack_benchmark.paced.720.json"
    raise ValueError("unknown benchmark attachment phase")


def _base_xcodebuild_command(
    repository: Path,
    device_id: str,
    derived_data: Path,
) -> list[str]:
    return [
        "xcodebuild",
        "-project",
        str(
            repository
            / "apps/ios/CaptureSplat/CaptureSplat.xcodeproj"
        ),
        "-scheme",
        SCHEME,
        "-configuration",
        CONFIGURATION,
        "-destination",
        f"id={device_id}",
        "-derivedDataPath",
        str(derived_data),
    ]


def _test_command(
    repository: Path,
    device_id: str,
    derived_data: Path,
    result_bundle: Path,
    method: str,
) -> list[str]:
    return [
        *_base_xcodebuild_command(repository, device_id, derived_data),
        "-resultBundlePath",
        str(result_bundle),
        f"-only-testing:{_test_identifier(method)}",
        "test-without-building",
    ]


def _build_settings_command(
    repository: Path,
) -> list[str]:
    return [
        "xcodebuild",
        "-project",
        str(
            repository
            / "apps/ios/CaptureSplat/CaptureSplat.xcodeproj"
        ),
        "-target",
        TARGET,
        "-configuration",
        CONFIGURATION,
        "-sdk",
        "iphoneos",
        "-showBuildSettings",
    ]


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    allow_failure: bool = False,
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"command timed out; stdout={_sha256(stdout)} stderr={_sha256(stderr)}"
        )
    result = {
        "returncode": process.returncode,
        "process_id": process.pid,
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
    }
    if process.returncode != 0 and not allow_failure:
        raise RuntimeError(
            "command failed with exit code "
            f"{process.returncode}; stdout={result['stdout_sha256']} "
            f"stderr={result['stderr_sha256']}"
        )
    return result


def _parse_build_settings(output: str) -> dict[str, str]:
    required = {
        "CONFIGURATION",
        "SWIFT_OPTIMIZATION_LEVEL",
        "SWIFT_COMPILATION_MODE",
        "SWIFT_ACTIVE_COMPILATION_CONDITIONS",
        "ENABLE_NS_ASSERTIONS",
        "PLATFORM_NAME",
        "PRODUCT_NAME",
        "PRODUCT_BUNDLE_IDENTIFIER",
        "PRODUCT_TYPE",
        "TARGET_NAME",
        "WRAPPER_EXTENSION",
    }
    tracked = required | {"BUNDLE_LOADER", "TEST_HOST"}
    settings: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^\s{4}([A-Z0-9_]+) = (.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        if key not in tracked:
            continue
        if key in settings and settings[key] != value:
            raise ValueError(f"resolved build setting changed within output: {key}")
        settings[key] = value
    if not required.issubset(settings):
        raise ValueError("resolved build settings are incomplete")
    settings.setdefault("BUNDLE_LOADER", "")
    settings.setdefault("TEST_HOST", "")
    conditions = settings["SWIFT_ACTIVE_COMPILATION_CONDITIONS"].split()
    if (
        settings["CONFIGURATION"] != "Release"
        or settings["SWIFT_OPTIMIZATION_LEVEL"] != "-O"
        or settings["SWIFT_COMPILATION_MODE"] != "wholemodule"
        or "CAPTURE_SPLAT_ACK_BENCHMARK_OPTIMIZED" not in conditions
        or "DEBUG" in conditions
        or settings["ENABLE_NS_ASSERTIONS"] != "NO"
        or settings["PLATFORM_NAME"] != "iphoneos"
        or settings["PRODUCT_NAME"] != TARGET
        or not settings["PRODUCT_BUNDLE_IDENTIFIER"].endswith(".AckBenchmarks")
        or settings["PRODUCT_TYPE"]
        != "com.apple.product-type.bundle.unit-test"
        or settings["TARGET_NAME"] != TARGET
        or settings["WRAPPER_EXTENSION"] != "xctest"
        or settings["BUNDLE_LOADER"] != ""
        or settings["TEST_HOST"] != ""
    ):
        raise ValueError(
            "resolved benchmark build is not an optimized hostless iOS Release target"
        )
    return {
        key: settings[key]
        for key in sorted(required | {"BUNDLE_LOADER", "TEST_HOST"})
    }


def _show_build_settings(
    repository: Path,
    timeout_seconds: int,
) -> dict[str, str]:
    completed = subprocess.run(
        _build_settings_command(repository),
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError("xcodebuild could not resolve benchmark build settings")
    return _parse_build_settings(completed.stdout)


def _tool_version(command: list[str], repository: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return _require_string(
        completed.stdout.strip(),
        "tool version",
        maximum_bytes=4096,
    )


def _tree_sha256(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Xcode result bundle is missing or not a directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        else:
            raise ValueError("Xcode result bundle contains a special filesystem node")
    return f"sha256:{digest.hexdigest()}"


def _extract_attachment(
    *,
    repository: Path,
    result_bundle: Path,
    export_directory: Path,
    expected_method: str,
    expected_name: str,
    device_id: str,
    command_succeeded: bool,
    timeout_seconds: int,
) -> tuple[object, dict[str, Any]]:
    if export_directory.exists():
        raise ValueError("attachment export directory already exists")
    completed = subprocess.run(
        [
            "xcrun",
            "xcresulttool",
            "export",
            "attachments",
            "--path",
            str(result_bundle),
            "--output-path",
            str(export_directory),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError("xcresulttool could not export benchmark attachments")
    manifest = _load_json_file(
        export_directory / "manifest.json",
        "xcresult attachment manifest",
    )
    groups = _require_list(manifest, "xcresult attachment manifest")
    manifest_required = {"testIdentifier", "attachments"}
    manifest_allowed = {
        "testIdentifier",
        "testIdentifierURL",
        "attachments",
    }
    attachment_required = {
        "exportedFileName",
        "suggestedHumanReadableName",
        "isAssociatedWithFailure",
        "configurationName",
        "deviceName",
        "deviceId",
    }
    attachment_allowed = {
        *attachment_required,
        "timestamp",
        "testIdentifierURL",
        "repetitionNumber",
        "arguments",
    }
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group_value in groups:
        group = _require_allowed_dict(
            group_value,
            manifest_required,
            manifest_allowed,
            "xcresult attachment group",
        )
        identifier = _require_string(
            group["testIdentifier"],
            "xcresult test identifier",
            maximum_bytes=4096,
        )
        if identifier != f"{TEST_CLASS}/{expected_method}()":
            raise ValueError("xcresult contains an unexpected test identifier")
        for attachment_value in _require_list(
            group["attachments"],
            "xcresult attachments",
        ):
            attachment = _require_allowed_dict(
                attachment_value,
                attachment_required,
                attachment_allowed,
                "xcresult attachment",
            )
            matches.append((group, attachment))
    if len(matches) != 1:
        raise ValueError("expected exactly one benchmark JSON attachment")
    group, attachment = matches[0]
    suggested_name = _require_string(
        attachment["suggestedHumanReadableName"],
        "xcresult suggested attachment name",
        maximum_bytes=512,
    )
    expected_stem = expected_name.removesuffix(".json")
    suffixed_name_pattern = re.compile(
        re.escape(expected_stem)
        + r"_0_[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-"
        + r"[0-9A-F]{4}-[0-9A-F]{12}\.json"
    )
    if suggested_name == expected_name:
        name_shape = "exact"
    elif suffixed_name_pattern.fullmatch(suggested_name):
        name_shape = "xcode_single_attachment_suffix"
    else:
        raise ValueError("xcresult benchmark attachment name changed")
    associated_failure = _require_bool(
        attachment["isAssociatedWithFailure"],
        "attachment failure association",
    )
    if command_succeeded and associated_failure:
        raise ValueError("successful benchmark command produced a failure attachment")
    configuration_name = _require_string(
        attachment["configurationName"],
        "xcresult attachment action",
        maximum_bytes=128,
    )
    if configuration_name != "Test Scheme Action":
        raise ValueError("xcresult attachment is not from the scheme test action")
    _require_string(attachment["deviceName"], "xcresult device name")
    if attachment["deviceId"] != device_id:
        raise ValueError("xcresult attachment came from a different device")
    if "timestamp" in attachment:
        _require_number(attachment["timestamp"], "attachment timestamp")
    if "repetitionNumber" in attachment:
        _require_int(
            attachment["repetitionNumber"],
            "attachment repetition number",
            maximum=1,
        )
    if "arguments" in attachment and not all(
        isinstance(item, str)
        for item in _require_list(
            attachment["arguments"],
            "attachment arguments",
        )
    ):
        raise ValueError("attachment arguments are not strings")
    filename = _require_string(
        attachment["exportedFileName"],
        "exported attachment filename",
        maximum_bytes=255,
    )
    if (
        filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or Path(filename).name != filename
    ):
        raise ValueError("exported attachment filename is unsafe")
    attachment_path = export_directory / filename
    if attachment_path.is_symlink() or not attachment_path.is_file():
        raise ValueError("exported attachment is not a regular file")
    raw = attachment_path.read_bytes()
    evidence = _load_json_bytes(
        raw,
        "benchmark attachment",
        require_canonical=True,
    )
    metadata = {
        "test_identifier": _test_identifier(expected_method),
        "attachment_name": expected_name,
        "manifest_attachment_name": suggested_name,
        "manifest_attachment_name_shape": name_shape,
        "manifest_configuration_name": configuration_name,
        "attachment_sha256": _sha256(raw),
        "is_associated_with_failure": associated_failure,
        "manifest_test_identifier_sha256": _sha256(
            str(group["testIdentifier"]).encode("utf-8")
        ),
    }
    return evidence, metadata


def _run_test_invocation(
    *,
    repository: Path,
    device_id: str,
    derived_data: Path,
    result_root: Path,
    phase: str,
    count: int | None,
    sample_kind: str,
    sample_index: int,
    timeout_seconds: int,
    required_evidence: bool = True,
) -> dict[str, Any]:
    invocation_uuid = str(uuid.uuid4())
    result_bundle = result_root / f"{invocation_uuid}.xcresult"
    export_directory = result_root / f"{invocation_uuid}-attachments"
    method = _method_name(phase, count)
    command = _test_command(
        repository,
        device_id,
        derived_data,
        result_bundle,
        method,
    )
    command_result = _run_command(
        command,
        cwd=repository,
        timeout_seconds=timeout_seconds,
        allow_failure=True,
    )
    command_succeeded = command_result["returncode"] == 0
    result_bundle_sha256 = (
        _tree_sha256(result_bundle)
        if result_bundle.exists()
        else None
    )
    evidence: object | None = None
    attachment: dict[str, Any] | None = None
    diagnostic: dict[str, Any] | None = None
    try:
        evidence, attachment = _extract_attachment(
            repository=repository,
            result_bundle=result_bundle,
            export_directory=export_directory,
            expected_method=method,
            expected_name=_expected_attachment_name(phase, count),
            device_id=device_id,
            command_succeeded=command_succeeded,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        if required_evidence:
            raise RuntimeError(
                "benchmark test did not yield one valid canonical attachment; "
                f"invocation={invocation_uuid} exit={command_result['returncode']} "
                f"result={result_bundle_sha256}"
            ) from error
        diagnostic_text = f"{type(error).__name__}:{error}"
        diagnostic = {
            "missing_or_invalid_evidence": True,
            "error_type": type(error).__name__,
            "error_sha256": _sha256(diagnostic_text.encode("utf-8")),
        }
    finally:
        if export_directory.exists():
            shutil.rmtree(export_directory)
        if result_bundle.exists():
            shutil.rmtree(result_bundle)
    record: dict[str, Any] = {
        "orchestrator_invocation_uuid": invocation_uuid,
        "separate_xcodebuild_invocation": True,
        "sample_kind": sample_kind,
        "sample_index": sample_index,
        "xcodebuild_process_id": command_result["process_id"],
        "xcodebuild_succeeded": command_succeeded,
        "xcodebuild_exit_code": command_result["returncode"],
        "xcodebuild_stdout_sha256": command_result["stdout_sha256"],
        "xcodebuild_stderr_sha256": command_result["stderr_sha256"],
        "result_bundle_sha256": result_bundle_sha256,
        "evidence": evidence,
        "diagnostic": diagnostic,
    }
    if attachment is not None:
        record.update(attachment)
    else:
        record.update(
            {
                "test_identifier": _test_identifier(method),
                "attachment_name": _expected_attachment_name(phase, count),
                "manifest_attachment_name": None,
                "manifest_attachment_name_shape": None,
                "manifest_configuration_name": None,
                "attachment_sha256": None,
                "is_associated_with_failure": None,
                "manifest_test_identifier_sha256": None,
            }
        )
    print(
        _canonical(
            {
                "schema": (
                    "capture_splat.live_sender_ack_device_benchmark_progress.v0.1"
                ),
                "phase": phase,
                "count": count,
                "sample_kind": sample_kind,
                "sample_index": sample_index,
                "xcodebuild_succeeded": command_succeeded,
            }
        ).decode("utf-8"),
        file=sys.stderr,
        flush=True,
    )
    return record


def _fixture_record(
    *,
    phase: str,
    count: int | None,
    sample_kind: str,
    sample_index: int,
    evidence: object,
) -> dict[str, Any]:
    canonical = _canonical(evidence)
    seed = f"{phase}:{count}:{sample_kind}:{sample_index}"
    return {
        "orchestrator_invocation_uuid": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"capture-splat-fixture:{seed}")
        ),
        "separate_xcodebuild_invocation": False,
        "sample_kind": sample_kind,
        "sample_index": sample_index,
        "xcodebuild_process_id": None,
        "xcodebuild_succeeded": False,
        "xcodebuild_exit_code": None,
        "xcodebuild_stdout_sha256": _sha256(b"fixture"),
        "xcodebuild_stderr_sha256": _sha256(b"fixture"),
        "result_bundle_sha256": _sha256(b"fixture-result:" + canonical),
        "test_identifier": _test_identifier(_method_name(phase, count)),
        "attachment_name": _expected_attachment_name(phase, count),
        "manifest_attachment_name": _expected_attachment_name(phase, count),
        "manifest_attachment_name_shape": "exact",
        "manifest_configuration_name": "fixture",
        "attachment_sha256": _sha256(canonical),
        "is_associated_with_failure": False,
        "manifest_test_identifier_sha256": _sha256(
            _test_identifier(_method_name(phase, count)).encode("utf-8")
        ),
        "evidence": evidence,
        "diagnostic": None,
    }


def _binding(record: dict[str, Any]) -> dict[str, Any]:
    binding = {
        key: record[key]
        for key in (
            "orchestrator_invocation_uuid",
            "separate_xcodebuild_invocation",
            "sample_kind",
            "sample_index",
            "xcodebuild_process_id",
            "xcodebuild_succeeded",
            "xcodebuild_exit_code",
            "xcodebuild_stdout_sha256",
            "xcodebuild_stderr_sha256",
            "result_bundle_sha256",
            "test_identifier",
            "attachment_name",
            "manifest_attachment_name",
            "manifest_attachment_name_shape",
            "manifest_configuration_name",
            "attachment_sha256",
            "is_associated_with_failure",
            "manifest_test_identifier_sha256",
        )
    }
    binding["test_process"] = (
        _validate_process(record["evidence"]["process"])
        if record["evidence"] is not None
        else None
    )
    binding["diagnostic"] = record["diagnostic"]
    return binding


def _collection_records(
    collection: dict[str, Any],
) -> list[tuple[bool, dict[str, Any]]]:
    records: list[tuple[bool, dict[str, Any]]] = []
    for count in collection["matrix"]["counts"]:
        required = count in HARD_GATE_COUNTS
        records.extend(
            (required, record)
            for record in collection["reconcile"][count]
        )
        records.extend(
            (required, record)
            for record in collection["cold_reopen"][count]
        )
    records.extend(
        (
            (True, collection["unpaced"]),
            (True, collection["paced"]),
        )
    )
    return records


def _validate_process_provenance(
    collection: dict[str, Any],
) -> dict[str, Any]:
    records = _collection_records(collection)
    matrix = collection["matrix"]
    expected_count = (
        len(matrix["counts"])
        * 2
        * (
            matrix["warmup_trials_per_phase_per_count"]
            + matrix["measured_trials_per_phase_per_count"]
        )
        + 2
    )
    if len(records) != expected_count:
        raise ValueError("test-process provenance is incomplete")
    launch_ids: list[str] = []
    required_launch_ids: list[str] = []
    orchestrator_ids: list[str] = []
    missing_future_process_evidence = 0
    required_record_count = 0
    for required, record in records:
        if required:
            required_record_count += 1
        if record["evidence"] is None:
            if required:
                raise ValueError(
                    "required test-process provenance is missing"
                )
            missing_future_process_evidence += 1
        else:
            process = _validate_process(record["evidence"]["process"])
            launch_ids.append(process["launch_id"])
            if required:
                required_launch_ids.append(process["launch_id"])
        invocation_id = record["orchestrator_invocation_uuid"]
        if (
            not isinstance(invocation_id, str)
            or not LAUNCH_ID_PATTERN.fullmatch(invocation_id)
        ):
            raise ValueError(
                "orchestrator invocation identifier is not a lowercase UUID"
            )
        orchestrator_ids.append(invocation_id)
    if len(set(launch_ids)) != len(launch_ids):
        raise ValueError(
            "a test-process launch_id was reused across xcodebuild invocations"
        )
    if len(set(orchestrator_ids)) != len(orchestrator_ids):
        raise ValueError("an orchestrator invocation UUID was reused")
    return {
        "expected_test_process_count": expected_count,
        "observed_test_process_count": len(records),
        "required_test_process_count": required_record_count,
        "observed_required_test_process_count": len(required_launch_ids),
        "required_test_process_evidence_complete": (
            len(required_launch_ids) == required_record_count
        ),
        "available_test_process_launch_ids_unique": True,
        "required_test_process_launch_ids_unique": (
            len(set(required_launch_ids)) == len(required_launch_ids)
        ),
        "missing_future_scale_test_process_evidence_count": (
            missing_future_process_evidence
        ),
        "orchestrator_invocation_uuids_unique": True,
        "separate_xcodebuild_invocation_per_sample": all(
            record["separate_xcodebuild_invocation"]
            for _, record in records
        ),
    }


def _same_platform(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    if not records:
        raise ValueError("platform comparison requires evidence")
    platforms = [record["evidence"]["platform"] for record in records]
    stable_keys = PLATFORM_KEYS - {"thermal_state"}
    first = platforms[0]
    for platform in platforms[1:]:
        if any(platform[key] != first[key] for key in stable_keys):
            raise ValueError("platform identity changed across benchmark samples")
    return {
        **{key: first[key] for key in sorted(stable_keys)},
        "thermal_states": sorted(
            {platform["thermal_state"] for platform in platforms}
        ),
    }


def _consistent_state(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    states = [record["evidence"]["persisted_state"] for record in records]
    if not states or any(state != states[0] for state in states[1:]):
        raise ValueError("persisted state size or digest changed across trials")
    return states[0]


def _maximum_memory(
    records: list[dict[str, Any]],
) -> dict[str, int]:
    if not records:
        raise ValueError("memory aggregate requires evidence")
    return {
        "maximum_phase_footprint_delta_bytes": max(
            record["evidence"]["memory"]["footprint_delta_bytes"]
            for record in records
        ),
        "maximum_kernel_reported_peak_footprint_bytes": max(
            record["evidence"]["memory"][
                "kernel_reported_peak_footprint_bytes"
            ]
            for record in records
        ),
    }


def _incomplete_future_scale_aggregate(
    count: int,
    reconcile_records: list[dict[str, Any]],
    reopen_records: list[dict[str, Any]],
    *,
    warmups: int,
    trials: int,
) -> dict[str, Any]:
    records = reconcile_records + reopen_records
    available = [
        record for record in records if record["evidence"] is not None
    ]
    missing_count = len(records) - len(available)
    return {
        "acknowledged_frame_count": count,
        "gate_scope": "future_scale_diagnostic",
        "warmup_trials_per_phase": warmups,
        "measured_trials_per_phase": trials,
        "queue_limits": _expected_queue_limits(count),
        "persisted_state": None,
        "final_ack_reconcile_persistence": None,
        "process_cold_reopen": None,
        "memory": None,
        "platform": _same_platform(available) if available else None,
        "correctness": {
            "production_seed_open_validated": False,
            "process_cold_reopen_validated": False,
            "exact_first_middle_last_duplicate_conflict": False,
            "state_digest_stable": False,
            "separate_xcodebuild_invocation_per_sample": all(
                record["separate_xcodebuild_invocation"]
                for record in records
            ),
        },
        "checks": {
            "evidence_complete": False,
            "missing_or_invalid_evidence_count": missing_count,
            "all_xcode_test_commands_succeeded": all(
                record["xcodebuild_succeeded"] for record in records
            ),
        },
        "gate_result": "diagnostic_future_scale_failed",
        "warmup_bindings": {
            "reconcile": [
                _binding(record) for record in reconcile_records[:warmups]
            ],
            "cold_reopen": [
                _binding(record) for record in reopen_records[:warmups]
            ],
        },
        "measured_evidence": {
            "reconcile": reconcile_records[warmups:],
            "cold_reopen": reopen_records[warmups:],
        },
    }


def _aggregate_count(
    count: int,
    reconcile_records: list[dict[str, Any]],
    reopen_records: list[dict[str, Any]],
    *,
    warmups: int,
    trials: int,
) -> dict[str, Any]:
    expected = warmups + trials
    if (
        len(reconcile_records) != expected
        or len(reopen_records) != expected
    ):
        raise ValueError("per-count evidence is incomplete")
    for index, record in enumerate(reconcile_records):
        if record["sample_index"] != index:
            raise ValueError("reconcile sample indices are not contiguous")
        if record["evidence"] is not None:
            _validate_reconcile(record["evidence"], count)
    for index, record in enumerate(reopen_records):
        if record["sample_index"] != index:
            raise ValueError("reopen sample indices are not contiguous")
        if record["evidence"] is not None:
            _validate_reopen(record["evidence"], count)
    if any(
        record["evidence"] is None
        for record in reconcile_records + reopen_records
    ):
        if count in HARD_GATE_COUNTS:
            raise ValueError("required per-count evidence is missing")
        return _incomplete_future_scale_aggregate(
            count,
            reconcile_records,
            reopen_records,
            warmups=warmups,
            trials=trials,
        )
    reconcile_measured = reconcile_records[warmups:]
    reopen_measured = reopen_records[warmups:]
    if (
        len(reconcile_measured) != trials
        or len(reopen_measured) != trials
    ):
        raise ValueError("measured evidence count is incomplete")
    persisted = _consistent_state(reconcile_measured + reopen_measured)
    platform = _same_platform(reconcile_measured + reopen_measured)
    reconcile_summary = _duration_summary(
        [
            record["evidence"]["reconcile_duration_nanoseconds"]
            for record in reconcile_measured
        ]
    )
    reopen_summary = _duration_summary(
        [
            record["evidence"]["reopen_duration_nanoseconds"]
            for record in reopen_measured
        ]
    )
    memory = _maximum_memory(reconcile_measured + reopen_measured)
    all_commands_succeeded = all(
        record["xcodebuild_succeeded"]
        for record in reconcile_records + reopen_records
    )
    cold_gate_checks = {
        "state_size": _state_size_passed(persisted),
        "final_ack_reconcile_latency": _latency_passed(
            reconcile_summary,
            reopen=False,
        ),
        "cold_reopen_latency": _latency_passed(
            reopen_summary,
            reopen=True,
        ),
        "memory": (
            memory["maximum_phase_footprint_delta_bytes"]
            <= HARD_GATE_BUDGETS["footprint_delta_bytes_at_most"]
            and memory["maximum_kernel_reported_peak_footprint_bytes"]
            <= HARD_GATE_BUDGETS["kernel_peak_footprint_bytes_at_most"]
        ),
        "exact_duplicate_conflict_after_reopen": True,
        "state_digest_stable": True,
        "all_xcode_test_commands_succeeded": all_commands_succeeded,
    }
    platform_evidence = reopen_measured[0]["evidence"]["platform"]
    if count not in HARD_GATE_COUNTS:
        gate_result = "diagnostic_future_scale"
    elif (
        _expected_physical_gate(platform_evidence)
        != "physical_trial_requires_aggregate_gate_evaluation"
    ):
        gate_result = _expected_physical_gate(platform_evidence)
    else:
        gate_result = (
            "passed" if all(cold_gate_checks.values()) else "failed"
        )
    return {
        "acknowledged_frame_count": count,
        "gate_scope": (
            "current_and_two_times_product_cap"
            if count in HARD_GATE_COUNTS
            else "future_scale_diagnostic"
        ),
        "warmup_trials_per_phase": warmups,
        "measured_trials_per_phase": trials,
        "queue_limits": _expected_queue_limits(count),
        "persisted_state": persisted,
        "final_ack_reconcile_persistence": reconcile_summary,
        "process_cold_reopen": reopen_summary,
        "memory": memory,
        "platform": platform,
        "correctness": {
            "production_seed_open_validated": True,
            "process_cold_reopen_validated": True,
            "exact_first_middle_last_duplicate_conflict": True,
            "state_digest_stable": True,
            "separate_xcodebuild_invocation_per_sample": all(
                record["separate_xcodebuild_invocation"]
                for record in reconcile_records + reopen_records
            ),
        },
        "checks": cold_gate_checks,
        "gate_result": gate_result,
        "warmup_bindings": {
            "reconcile": [
                _binding(record) for record in reconcile_records[:warmups]
            ],
            "cold_reopen": [
                _binding(record) for record in reopen_records[:warmups]
            ],
        },
        "measured_evidence": {
            "reconcile": reconcile_measured,
            "cold_reopen": reopen_measured,
        },
    }


def _summarize_stream(
    record: dict[str, Any],
    *,
    paced: bool,
) -> dict[str, Any]:
    phase = (
        _validate_paced(record["evidence"])
        if paced
        else _validate_unpaced(record["evidence"])
    )
    durations = phase["acknowledgement_durations_nanoseconds"]
    memory = phase["memory"]
    result = {
        "queue_limits": phase["queue_limits"],
        "persisted_state": phase["persisted_state"],
        "ack_persistence": _duration_summary(durations),
        "memory": {
            "maximum_phase_footprint_delta_bytes": memory[
                "footprint_delta_bytes"
            ],
            "maximum_kernel_reported_peak_footprint_bytes": memory[
                "kernel_reported_peak_footprint_bytes"
            ],
        },
        "platform": {
            **{
                key: phase["platform"][key]
                for key in sorted(PLATFORM_KEYS - {"thermal_state"})
            },
            "thermal_states": sorted(
                {
                    phase["platform"]["thermal_state"],
                    phase["thermal_states"]["before"],
                    phase["thermal_states"]["after"],
                }
            ),
        },
        "core_gate_result": phase["gate_result"],
        "xcode_test_command_succeeded": record["xcodebuild_succeeded"],
        "evidence": record,
    }
    if paced:
        result["paced"] = {
            "acknowledgements_per_second": phase["configuration"][
                "acknowledgements_per_second"
            ],
            "nominal_duration_seconds": phase["configuration"][
                "nominal_duration_seconds"
            ],
            "maximum_backlog_frames": phase["maximum_backlog_frames"],
            "backlog_at_nominal_end_frames": phase[
                "backlog_at_nominal_end_frames"
            ],
            "final_backlog_frames": phase["final_backlog_frames"],
            "drain_duration_nanoseconds": phase[
                "drain_duration_nanoseconds"
            ],
        }
    else:
        result["unpaced"] = {
            "elapsed_nanoseconds": phase["elapsed_nanoseconds"],
            "durable_acknowledgements_per_second": phase[
                "durable_acknowledgements_per_second"
            ],
        }
    return result


def _platform_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: summary["platform"][key]
        for key in PLATFORM_KEYS - {"thermal_state"}
    }


def _evaluate_hard_gate(
    *,
    run_profile: str,
    matrix: dict[str, Any],
    aggregates: list[dict[str, Any]],
    unpaced: dict[str, Any],
    paced: dict[str, Any],
    optimized_build_settings: bool,
    process_provenance: dict[str, Any],
) -> dict[str, Any]:
    checks: dict[str, bool] = {
        "acceptance_matrix_complete": matrix == {
            "counts": list(COUNTS),
            "warmup_trials_per_phase_per_count": WARMUP_TRIALS,
            "measured_trials_per_phase_per_count": MEASURED_TRIALS,
            "separate_xcodebuild_invocation_per_sample": True,
        },
        "optimized_release_build_settings": optimized_build_settings,
        "both_360_and_720_per_count_gates_present": all(
            any(
                aggregate["acknowledged_frame_count"] == count
                for aggregate in aggregates
            )
            for count in HARD_GATE_COUNTS
        ),
        "unpaced_and_paced_evidence_present": bool(unpaced) and bool(paced),
        "expected_test_process_count_observed": (
            process_provenance["observed_test_process_count"]
            == process_provenance["expected_test_process_count"]
        ),
        "required_test_process_evidence_complete": process_provenance[
            "required_test_process_evidence_complete"
        ],
        "required_test_process_launch_ids_unique": process_provenance[
            "required_test_process_launch_ids_unique"
        ],
        "available_test_process_launch_ids_unique": process_provenance[
            "available_test_process_launch_ids_unique"
        ],
        "orchestrator_invocation_uuids_unique": process_provenance[
            "orchestrator_invocation_uuids_unique"
        ],
        "separate_xcodebuild_process_per_sample": process_provenance[
            "separate_xcodebuild_invocation_per_sample"
        ],
    }
    if run_profile == "fixture":
        return {
            "status": "not_evaluated_fixture",
            "reasons": ["fixture evidence cannot satisfy a physical gate"],
            "checks": checks,
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        }
    if not all(checks.values()):
        return {
            "status": "failed",
            "reasons": ["acceptance evidence or optimized build settings are incomplete"],
            "checks": checks,
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        }
    by_count = {
        aggregate["acknowledged_frame_count"]: aggregate
        for aggregate in aggregates
    }
    summaries = [
        *(by_count[count] for count in HARD_GATE_COUNTS),
        unpaced,
        paced,
    ]
    platform_values = [
        _platform_from_summary(summary)
        for summary in summaries
    ]
    first = platform_values[0]
    if any(platform != first for platform in platform_values[1:]):
        return {
            "status": "failed",
            "reasons": ["platform identity changed across physical evidence"],
            "checks": checks,
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        }
    if not first["is_physical_device"]:
        return {
            "status": "not_evaluated_non_physical",
            "reasons": ["host or Simulator evidence is diagnostic only"],
            "checks": checks,
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        }
    if (
        first["machine"] not in ELIGIBLE_MODELS
        or not first["is_oldest_supported_lidar_iphone"]
    ):
        return {
            "status": "not_evaluated_ineligible_device",
            "reasons": ["device is not iPhone13,3 or iPhone13,4"],
            "checks": checks,
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        }
    if not first["optimized_build"]:
        return {
            "status": "not_evaluated_unoptimized_build",
            "reasons": ["device evidence is not marked as optimized"],
            "checks": checks,
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        }
    checks.update(
        {
            "count_360_gate_passed": by_count[360]["gate_result"] == "passed",
            "count_720_gate_passed": by_count[720]["gate_result"] == "passed",
            "unpaced_ack_latency_passed": _latency_passed(
                unpaced["ack_persistence"],
                reopen=False,
            ),
            "unpaced_throughput_passed": (
                unpaced["unpaced"][
                    "durable_acknowledgements_per_second"
                ]
                >= HARD_GATE_BUDGETS[
                    "unpaced_acknowledgements_per_second_at_least"
                ]
            ),
            "unpaced_memory_passed": (
                unpaced["memory"]["maximum_phase_footprint_delta_bytes"]
                <= HARD_GATE_BUDGETS["footprint_delta_bytes_at_most"]
                and unpaced["memory"][
                    "maximum_kernel_reported_peak_footprint_bytes"
                ]
                <= HARD_GATE_BUDGETS[
                    "kernel_peak_footprint_bytes_at_most"
                ]
            ),
            "paced_ack_latency_passed": _latency_passed(
                paced["ack_persistence"],
                reopen=False,
            ),
            "paced_backlog_passed": (
                paced["paced"]["maximum_backlog_frames"]
                <= HARD_GATE_BUDGETS[
                    "paced_maximum_backlog_frames_at_most"
                ]
                and paced["paced"]["final_backlog_frames"]
                == HARD_GATE_BUDGETS["paced_final_backlog_frames"]
            ),
            "paced_memory_passed": (
                paced["memory"]["maximum_phase_footprint_delta_bytes"]
                <= HARD_GATE_BUDGETS["footprint_delta_bytes_at_most"]
                and paced["memory"][
                    "maximum_kernel_reported_peak_footprint_bytes"
                ]
                <= HARD_GATE_BUDGETS[
                    "kernel_peak_footprint_bytes_at_most"
                ]
            ),
            "final_720_state_matches_all_paths": (
                by_count[720]["persisted_state"]
                == unpaced["persisted_state"]
                == paced["persisted_state"]
            ),
            "all_xcode_test_commands_succeeded": (
                all(
                    by_count[count]["checks"][
                        "all_xcode_test_commands_succeeded"
                    ]
                    for count in HARD_GATE_COUNTS
                )
                and unpaced["xcode_test_command_succeeded"]
                and paced["xcode_test_command_succeeded"]
            ),
            "phase_results_never_claim_passed": (
                unpaced["core_gate_result"] != "passed"
                and paced["core_gate_result"] != "passed"
            ),
        }
    )
    status = "passed" if all(checks.values()) else "failed"
    return {
        "status": status,
        "reasons": (
            []
            if status == "passed"
            else [key for key, passed in checks.items() if not passed]
        ),
        "checks": checks,
        "budgets": HARD_GATE_BUDGETS,
        "eligible_device_models": list(ELIGIBLE_MODELS),
    }


def _collect_real(
    *,
    repository: Path,
    device_id: str,
    work_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    derived_data = work_root / "DerivedData"
    result_root = work_root / "results"
    result_root.mkdir(parents=True)
    resolved_settings = _show_build_settings(
        repository,
        timeout_seconds,
    )
    build_result = _run_command(
        [
            *_base_xcodebuild_command(
                repository,
                device_id,
                derived_data,
            ),
            "build-for-testing",
        ],
        cwd=repository,
        timeout_seconds=timeout_seconds,
    )
    records: dict[str, dict[int, dict[str, list[dict[str, Any]]]]] = {
        "reconcile": {},
        "cold_reopen": {},
    }
    for count in COUNTS:
        for phase in ("reconcile", "cold_reopen"):
            phase_records: list[dict[str, Any]] = []
            for sample_index in range(WARMUP_TRIALS + MEASURED_TRIALS):
                sample_kind = (
                    "warmup"
                    if sample_index < WARMUP_TRIALS
                    else "measured"
                )
                phase_records.append(
                    _run_test_invocation(
                        repository=repository,
                        device_id=device_id,
                        derived_data=derived_data,
                        result_root=result_root,
                        phase=phase,
                        count=count,
                        sample_kind=sample_kind,
                        sample_index=sample_index,
                        timeout_seconds=timeout_seconds,
                        required_evidence=count in HARD_GATE_COUNTS,
                    )
                )
            records[phase][count] = phase_records
    unpaced = _run_test_invocation(
        repository=repository,
        device_id=device_id,
        derived_data=derived_data,
        result_root=result_root,
        phase="unpaced",
        count=None,
        sample_kind="measured",
        sample_index=0,
        timeout_seconds=timeout_seconds,
    )
    paced = _run_test_invocation(
        repository=repository,
        device_id=device_id,
        derived_data=derived_data,
        result_root=result_root,
        phase="paced",
        count=None,
        sample_kind="measured",
        sample_index=0,
        timeout_seconds=timeout_seconds,
    )
    return {
        "run_profile": "acceptance",
        "matrix": {
            "counts": list(COUNTS),
            "warmup_trials_per_phase_per_count": WARMUP_TRIALS,
            "measured_trials_per_phase_per_count": MEASURED_TRIALS,
            "separate_xcodebuild_invocation_per_sample": True,
        },
        "resolved_build_settings": resolved_settings,
        "build_invocation": {
            "xcodebuild_process_id": build_result["process_id"],
            "xcodebuild_stdout_sha256": build_result["stdout_sha256"],
            "xcodebuild_stderr_sha256": build_result["stderr_sha256"],
            "succeeded": True,
        },
        "reconcile": records["reconcile"],
        "cold_reopen": records["cold_reopen"],
        "unpaced": unpaced,
        "paced": paced,
    }


def _parse_fixture_matrix(value: object) -> dict[str, Any]:
    matrix = _require_dict(
        value,
        {
            "counts",
            "warmup_trials_per_phase_per_count",
            "measured_trials_per_phase_per_count",
        },
        "fixture matrix",
    )
    counts = [
        _require_int(item, "fixture count", minimum=1, maximum=99_999_999)
        for item in _require_list(matrix["counts"], "fixture counts")
    ]
    if not counts or len(counts) != len(set(counts)) or 720 not in counts:
        raise ValueError("fixture counts must be unique and include 720")
    warmups = _require_int(
        matrix["warmup_trials_per_phase_per_count"],
        "fixture warmup count",
        maximum=100,
    )
    trials = _require_int(
        matrix["measured_trials_per_phase_per_count"],
        "fixture measured count",
        minimum=1,
        maximum=100,
    )
    return {
        "counts": counts,
        "warmup_trials_per_phase_per_count": warmups,
        "measured_trials_per_phase_per_count": trials,
        "separate_xcodebuild_invocation_per_sample": False,
    }


def _collect_fixture(path: Path) -> dict[str, Any]:
    fixture = _require_dict(
        _load_json_file(path, "device benchmark fixture"),
        {
            "schema",
            "matrix",
            "reconcile",
            "cold_reopen",
            "unpaced_720",
            "paced_720",
        },
        "device benchmark fixture",
    )
    if fixture["schema"] != FIXTURE_SCHEMA:
        raise ValueError("unexpected device benchmark fixture schema")
    matrix = _parse_fixture_matrix(fixture["matrix"])
    counts = matrix["counts"]
    warmups = matrix["warmup_trials_per_phase_per_count"]
    trials = matrix["measured_trials_per_phase_per_count"]
    expected_keys = {str(count) for count in counts}
    reconcile_values = _require_dict(
        fixture["reconcile"],
        expected_keys,
        "fixture reconcile matrix",
    )
    reopen_values = _require_dict(
        fixture["cold_reopen"],
        expected_keys,
        "fixture cold-reopen matrix",
    )
    reconcile: dict[int, list[dict[str, Any]]] = {}
    reopen: dict[int, list[dict[str, Any]]] = {}
    for count in counts:
        total = warmups + trials
        reconcile_phases = _require_list(
            reconcile_values[str(count)],
            "fixture reconcile phases",
        )
        reopen_phases = _require_list(
            reopen_values[str(count)],
            "fixture cold-reopen phases",
        )
        if len(reconcile_phases) != total or len(reopen_phases) != total:
            raise ValueError("fixture per-count phase count is incomplete")
        reconcile[count] = []
        reopen[count] = []
        for index, evidence in enumerate(reconcile_phases):
            _validate_reconcile(evidence, count)
            reconcile[count].append(
                _fixture_record(
                    phase="reconcile",
                    count=count,
                    sample_kind="warmup" if index < warmups else "measured",
                    sample_index=index,
                    evidence=evidence,
                )
            )
        for index, evidence in enumerate(reopen_phases):
            _validate_reopen(evidence, count)
            reopen[count].append(
                _fixture_record(
                    phase="cold_reopen",
                    count=count,
                    sample_kind="warmup" if index < warmups else "measured",
                    sample_index=index,
                    evidence=evidence,
                )
            )
    unpaced_evidence = _validate_unpaced(fixture["unpaced_720"])
    paced_evidence = _validate_paced(fixture["paced_720"])
    return {
        "run_profile": "fixture",
        "matrix": matrix,
        "resolved_build_settings": {
            "CONFIGURATION": "fixture",
            "SWIFT_OPTIMIZATION_LEVEL": "unverified",
            "SWIFT_COMPILATION_MODE": "unverified",
            "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "unverified",
            "ENABLE_NS_ASSERTIONS": "unverified",
            "PLATFORM_NAME": "fixture",
            "PRODUCT_NAME": TARGET,
            "PRODUCT_BUNDLE_IDENTIFIER": "fixture.AckBenchmarks",
            "TEST_HOST": "",
        },
        "build_invocation": {
            "xcodebuild_process_id": None,
            "xcodebuild_stdout_sha256": _sha256(b"fixture"),
            "xcodebuild_stderr_sha256": _sha256(b"fixture"),
            "succeeded": False,
        },
        "reconcile": reconcile,
        "cold_reopen": reopen,
        "unpaced": _fixture_record(
            phase="unpaced",
            count=None,
            sample_kind="measured",
            sample_index=0,
            evidence=unpaced_evidence,
        ),
        "paced": _fixture_record(
            phase="paced",
            count=None,
            sample_kind="measured",
            sample_index=0,
            evidence=paced_evidence,
        ),
    }


def _aggregate_collection(collection: dict[str, Any]) -> dict[str, Any]:
    matrix = collection["matrix"]
    warmups = matrix["warmup_trials_per_phase_per_count"]
    trials = matrix["measured_trials_per_phase_per_count"]
    process_provenance = _validate_process_provenance(collection)
    aggregates = [
        _aggregate_count(
            count,
            collection["reconcile"][count],
            collection["cold_reopen"][count],
            warmups=warmups,
            trials=trials,
        )
        for count in matrix["counts"]
    ]
    unpaced = _summarize_stream(collection["unpaced"], paced=False)
    paced = _summarize_stream(collection["paced"], paced=True)
    return {
        "aggregates": aggregates,
        "unpaced": unpaced,
        "paced": paced,
        "process_provenance": process_provenance,
        "hard_gate": _evaluate_hard_gate(
            run_profile=collection["run_profile"],
            matrix=matrix,
            aggregates=aggregates,
            unpaced=unpaced,
            paced=paced,
            optimized_build_settings=(
                collection["run_profile"] == "acceptance"
            ),
            process_provenance=process_provenance,
        ),
    }


def _queue_limit_declaration() -> dict[str, Any]:
    return {
        "benchmark_scope": "benchmark_only_not_product_cap",
        "maximum_bytes": BENCHMARK_MAXIMUM_BYTES,
        "maximum_in_flight": 8,
        "maximum_frames_by_case": {
            str(count): count for count in COUNTS
        },
        "production_state_payload_cap_bytes": (
            PRODUCTION_STATE_PAYLOAD_CAP_BYTES
        ),
        "current_product_accepted_frame_cap": 360,
        "hard_gate_two_times_frame_count": 720,
    }


def _make_payload(
    *,
    repository_identity: dict[str, Any],
    collection: dict[str, Any],
    aggregate: dict[str, Any],
    build_versions: dict[str, str],
    device_id: str | None,
) -> dict[str, Any]:
    generated_at = (
        dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    hard_gate = aggregate["hard_gate"]
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at,
        "repository": repository_identity,
        "run_profile": collection["run_profile"],
        "build": {
            "scheme": SCHEME,
            "target": TARGET,
            "configuration": CONFIGURATION,
            "hostless_test_bundle": True,
            "resolved_settings": collection["resolved_build_settings"],
            "versions": build_versions,
            "destination_device_id_sha256": (
                _sha256(device_id.encode("utf-8"))
                if device_id is not None
                else None
            ),
            "build_invocation": collection["build_invocation"],
        },
        "matrix": collection["matrix"],
        "process_provenance": aggregate["process_provenance"],
        "queue_limits": _queue_limit_declaration(),
        "hard_gate": hard_gate,
        "capture_isolation": {
            "capture_loop_connected": False,
            "writer_drops": "unmeasured",
            "capture_wait": "unmeasured",
            "keyframe_acceptance_changed": False,
        },
        "m1b_physical_capture_acceptance": {
            "status": "not_evaluated_capture_loop_disconnected",
            "ack_index_benchmark_status": hard_gate["status"],
            "writer_drops": "unmeasured",
            "capture_wait": "unmeasured",
            "two_cycle_interruption_acceptance": "not_run",
        },
        "aggregates": aggregate["aggregates"],
        "streams": {
            "unpaced_720": aggregate["unpaced"],
            "paced_720": aggregate["paced"],
        },
        "diagnostic": None,
    }


def _failure_payload(
    *,
    repository_identity: dict[str, Any],
    stage: str,
    error: Exception,
    device_id: str,
) -> dict[str, Any]:
    error_text = f"{type(error).__name__}:{error}"
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": (
            dt.datetime.now(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "repository": repository_identity,
        "run_profile": "acceptance_failed_diagnostic",
        "build": {
            "scheme": SCHEME,
            "target": TARGET,
            "configuration": CONFIGURATION,
            "hostless_test_bundle": True,
            "resolved_settings": None,
            "versions": None,
            "destination_device_id_sha256": _sha256(
                device_id.encode("utf-8")
            ),
            "build_invocation": None,
        },
        "matrix": {
            "counts": list(COUNTS),
            "warmup_trials_per_phase_per_count": WARMUP_TRIALS,
            "measured_trials_per_phase_per_count": MEASURED_TRIALS,
            "separate_xcodebuild_invocation_per_sample": True,
        },
        "process_provenance": None,
        "queue_limits": _queue_limit_declaration(),
        "hard_gate": {
            "status": "failed",
            "reasons": [
                "collection command failed or required canonical evidence was missing"
            ],
            "checks": {
                "collection_complete": False,
                "never_promoted_from_xcode_test_status": True,
            },
            "budgets": HARD_GATE_BUDGETS,
            "eligible_device_models": list(ELIGIBLE_MODELS),
        },
        "capture_isolation": {
            "capture_loop_connected": False,
            "writer_drops": "unmeasured",
            "capture_wait": "unmeasured",
            "keyframe_acceptance_changed": False,
        },
        "m1b_physical_capture_acceptance": {
            "status": "not_evaluated_capture_loop_disconnected",
            "ack_index_benchmark_status": "failed",
            "writer_drops": "unmeasured",
            "capture_wait": "unmeasured",
            "two_cycle_interruption_acceptance": "not_run",
        },
        "aggregates": [],
        "streams": {
            "unpaced_720": None,
            "paced_720": None,
        },
        "diagnostic": {
            "stage": stage,
            "error_type": type(error).__name__,
            "error_sha256": _sha256(error_text.encode("utf-8")),
            "missing_or_invalid_evidence": True,
        },
    }


def _dry_run_plan(
    repository: Path,
    device_id: str,
) -> dict[str, Any]:
    root = Path("<temporary-root>")
    derived = root / "DerivedData"
    commands: list[dict[str, Any]] = []
    for count in COUNTS:
        for phase in ("reconcile", "cold_reopen"):
            for sample_index in range(WARMUP_TRIALS + MEASURED_TRIALS):
                method = _method_name(phase, count)
                result = root / "results" / (
                    f"{phase}-{count}-{sample_index}.xcresult"
                )
                commands.append(
                    {
                        "phase": phase,
                        "count": count,
                        "sample_kind": (
                            "warmup"
                            if sample_index < WARMUP_TRIALS
                            else "measured"
                        ),
                        "sample_index": sample_index,
                        "test_identifier": _test_identifier(method),
                        "command": _test_command(
                            repository,
                            device_id,
                            derived,
                            result,
                            method,
                        ),
                    }
                )
    for phase in ("unpaced", "paced"):
        method = _method_name(phase)
        commands.append(
            {
                "phase": phase,
                "count": 720,
                "sample_kind": "measured",
                "sample_index": 0,
                "test_identifier": _test_identifier(method),
                "command": _test_command(
                    repository,
                    device_id,
                    derived,
                    root / "results" / f"{phase}-720-0.xcresult",
                    method,
                ),
            }
        )
    return {
        "schema": PLAN_SCHEMA,
        "scheme": SCHEME,
        "configuration": CONFIGURATION,
        "destination_device_id_sha256": _sha256(device_id.encode("utf-8")),
        "build_settings_command": [
            *_build_settings_command(repository),
        ],
        "build_for_testing_command": [
            *_base_xcodebuild_command(
                repository,
                device_id,
                derived,
            ),
            "build-for-testing",
        ],
        "test_invocation_count": len(commands),
        "one_separate_xcodebuild_process_per_sample": True,
        "test_invocations": commands,
        "writes_report": False,
        "capture_loop_connected": False,
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect strict physical-device evidence for the Capture Splat "
            "durable acknowledged-frame index."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--fixture", type=Path)
    parser.add_argument("--device-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--command-timeout-seconds",
        type=int,
        default=7_200,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.command_timeout_seconds < 60:
        parser.error("command timeout must be at least 60 seconds")
    if arguments.dry_run:
        if (
            arguments.device_id is None
            or not DEVICE_ID_PATTERN.fullmatch(arguments.device_id)
        ):
            parser.error("--dry-run requires a bounded device identifier")
        print(
            _canonical(
                _dry_run_plan(_repository_root(), arguments.device_id)
            ).decode("utf-8")
        )
        return 0
    if arguments.fixture is None and (
        arguments.device_id is None
        or not DEVICE_ID_PATTERN.fullmatch(arguments.device_id)
    ):
        parser.error("an acceptance run requires --device-id")
    if arguments.fixture is not None and arguments.device_id is not None:
        parser.error("--fixture does not accept --device-id")

    repository = _repository_root()
    try:
        output = _resolve_output(arguments.output)
    except Exception as error:
        parser.error(str(error))
    repository_identity: dict[str, Any] | None = None
    acceptance_started = False
    stage = "repository_validation"
    try:
        if arguments.fixture is not None:
            repository_identity = _repository_identity(
                repository,
                require_clean=False,
            )
            stage = "fixture_validation"
            collection = _collect_fixture(arguments.fixture.resolve())
            versions = {
                "xcodebuild": "not_run_fixture",
                "xcresulttool": "not_run_fixture",
            }
            device_id = None
        else:
            repository_identity = _repository_identity(
                repository,
                require_clean=True,
            )
            acceptance_started = True
            device_id = arguments.device_id
            versions = {
                "xcodebuild": _tool_version(
                    ["xcodebuild", "-version"],
                    repository,
                ),
                "xcresulttool": _tool_version(
                    ["xcrun", "xcresulttool", "--version"],
                    repository,
                ),
            }
            stage = "physical_collection"
            with tempfile.TemporaryDirectory(
                prefix="capture-splat-ack-device-benchmark-"
            ) as temporary:
                collection = _collect_real(
                    repository=repository,
                    device_id=device_id,
                    work_root=Path(temporary),
                    timeout_seconds=arguments.command_timeout_seconds,
                )
        stage = "strict_aggregation"
        aggregate = _aggregate_collection(collection)
        if collection["run_profile"] == "acceptance":
            ending_identity = _repository_identity(
                repository,
                require_clean=True,
            )
            if ending_identity != repository_identity:
                raise ValueError(
                    "repository commit or source fingerprints changed during collection"
                )
        stage = "report_write"
        payload = _make_payload(
            repository_identity=repository_identity,
            collection=collection,
            aggregate=aggregate,
            build_versions=versions,
            device_id=device_id,
        )
        written = _write_envelope(output, payload)
        print(
            _canonical(
                {
                    "schema": SUMMARY_SCHEMA,
                    **written,
                    "hard_gate_status": payload["hard_gate"]["status"],
                    "m1b_physical_capture_acceptance": (
                        "not_evaluated_capture_loop_disconnected"
                    ),
                }
            ).decode("utf-8")
        )
        return 0
    except Exception as error:
        if (
            acceptance_started
            and repository_identity is not None
            and not output.exists()
        ):
            try:
                written = _write_envelope(
                    output,
                    _failure_payload(
                        repository_identity=repository_identity,
                        stage=stage,
                        error=error,
                        device_id=arguments.device_id,
                    ),
                )
                print(
                    _canonical(
                        {
                            "schema": SUMMARY_SCHEMA,
                            **written,
                            "hard_gate_status": "failed",
                            "m1b_physical_capture_acceptance": (
                                "not_evaluated_capture_loop_disconnected"
                            ),
                        }
                    ).decode("utf-8")
                )
            except Exception as report_error:
                print(
                    f"failed to write diagnostic report: {report_error}",
                    file=sys.stderr,
                )
        print(f"device ACK benchmark failed at {stage}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
