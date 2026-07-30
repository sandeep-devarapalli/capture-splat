import base64
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Swift benchmark requires macOS")


def _decode_report(path: Path) -> dict[str, object]:
    envelope = json.loads(path.read_bytes())
    assert set(envelope) == {"schema", "payload_sha256", "payload_base64"}
    assert envelope["schema"] == (
        "capture_splat.live_sender_ack_benchmark_report_envelope.v0.1"
    )
    payload_bytes = base64.b64decode(envelope["payload_base64"], validate=True)
    assert envelope["payload_sha256"] == (
        f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
    )
    payload = json.loads(
        payload_bytes,
        parse_constant=lambda value: pytest.fail(f"non-finite JSON value: {value}"),
    )
    return payload


def _load_runner_module() -> object:
    repository = Path(__file__).resolve().parents[1]
    path = repository / "scripts/benchmark_ios_live_sender_ack_index.py"
    spec = importlib.util.spec_from_file_location("live_ack_benchmark_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _passing_gate_inputs() -> dict[str, object]:
    per_count = {}
    for count in (360, 720):
        per_count[str(count)] = {
            "state": {
                "payload_bytes": 1024,
                "envelope_bytes": 2048,
            },
            "acknowledgement_persistence": {
                "p50_nanoseconds": 10_000_000,
                "p95_nanoseconds": 20_000_000,
                "maximum_nanoseconds": 30_000_000,
            },
            "process_cold_reopen": {
                "p50_nanoseconds": 50_000_000,
                "p95_nanoseconds": 100_000_000,
                "maximum_nanoseconds": 200_000_000,
            },
        }
    return {
        "required_counts": [360, 720],
        "required_counts_present": True,
        "per_count": per_count,
        "memory": {
            "maximum_phase_footprint_delta_bytes": 1024,
            "maximum_kernel_reported_peak_footprint_bytes": 2048,
        },
        "sustained_acknowledgements_per_second": 10.0,
        "paced": {
            "acknowledgements_per_second": 5,
            "nominal_duration_seconds": 60,
            "maximum_backlog_frames": 8,
            "final_backlog_frames": 0,
        },
        "correctness": {
            "unpaced_production_open_reconcile": True,
            "paced_production_open_reconcile": True,
            "stream_process_launches_unique": True,
            "required_per_count_evidence_present": True,
            "process_cold_reopen": True,
            "process_launches_unique": True,
            "exact_duplicate_conflict_after_restart": True,
            "exact_duplicate_conflict_after_stream": True,
            "state_digest_stable": True,
        },
    }


def _eligible_platform() -> dict[str, object]:
    return {
        "is_physical_device": True,
        "is_oldest_supported_lidar_iphone": True,
        "optimized_build": True,
    }


def test_release_runner_uses_process_cold_reopen_and_checksums_report(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    report = tmp_path / "ack-benchmark-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/benchmark_ios_live_sender_ack_index.py"),
            "--counts",
            "1,3",
            "--warmups",
            "0",
            "--trials",
            "1",
            "--stream-count",
            "3",
            "--paced-rate",
            "2",
            "--paced-duration-seconds",
            "1",
            "--output",
            str(report),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(completed.stdout)
    assert summary == {
        "hard_gate_status": "not_evaluated_non_physical",
        "payload_sha256": summary["payload_sha256"],
        "report": str(report),
        "schema": "capture_splat.live_sender_ack_benchmark_summary.v0.1",
    }

    payload = _decode_report(report)
    assert set(payload) == {
        "schema",
        "generated_at",
        "repository_commit",
        "run_profile",
        "build",
        "matrix",
        "hard_gate",
        "capture_isolation",
        "physical_run_fields",
        "progressive_streams",
        "aggregates",
    }
    assert payload["schema"] == "capture_splat.live_sender_ack_benchmark_report.v0.1"
    assert payload["run_profile"] == "test_override"
    assert payload["matrix"] == {
        "counts": [1, 3],
        "warmup_trials_per_count": 0,
        "measured_trials_per_count": 1,
        "stream_count": 3,
        "paced_acknowledgements_per_second": 2,
        "paced_duration_seconds": 1,
    }
    assert payload["hard_gate"]["status"] == "not_evaluated_non_physical"
    assert payload["hard_gate"]["eligible_device_models"] == [
        "iPhone13,3",
        "iPhone13,4",
    ]
    assert payload["capture_isolation"] == {
        "capture_loop_connected": False,
        "writer_drops": "unmeasured",
        "capture_wait": "unmeasured",
        "keyframe_acceptance_changed": False,
    }
    physical = payload["physical_run_fields"]
    assert set(physical) == {
        "thermal_states",
        "sustained_acknowledgements_per_second",
        "paced_backlog",
        "capture_writer_interference",
    }
    assert physical["capture_writer_interference"] == "unmeasured"
    assert physical["sustained_acknowledgements_per_second"] > 0
    assert physical["paced_backlog"]["final_frames"] == 0

    gate_inputs = payload["hard_gate"]["evaluation_inputs"]
    assert gate_inputs["required_counts"] == [360, 720]
    assert gate_inputs["required_counts_present"] is False
    assert gate_inputs["per_count"] == {}
    assert gate_inputs["correctness"]["required_per_count_evidence_present"] is False

    streams = payload["progressive_streams"]
    assert streams["final_acknowledged_frame_count"] == 3
    assert streams["final_state_digest_stable"] is True
    assert streams["process_launches_unique"] is True
    unpaced = streams["unpaced"]
    paced = streams["paced"]
    assert unpaced["schema"] == (
        "capture_splat.live_sender_ack_benchmark_unpaced_stream_phase.v0.1"
    )
    assert unpaced["seed_state"]["acknowledged_frame_count"] == 0
    assert unpaced["seed_state"]["pending_frame_count"] == 3
    assert len(unpaced["acknowledgement_durations_nanoseconds"]) == 3
    assert unpaced["gate_result"] == "not_evaluated_non_physical"
    assert unpaced["queue_limits"] == {
        "maximum_frames": 3,
        "maximum_bytes": (2**63 - 1) // 4,
        "maximum_in_flight": 3,
        "scope": "benchmark_only_not_product_cap",
    }
    assert paced["schema"] == (
        "capture_splat.live_sender_ack_benchmark_paced_stream_phase.v0.1"
    )
    assert paced["configuration"] == {
        "initial_acknowledged_frame_count": 1,
        "final_acknowledged_frame_count": 3,
        "acknowledgement_count": 2,
        "acknowledgements_per_second": 2,
        "nominal_duration_seconds": 1,
    }
    assert paced["seed_state"]["acknowledged_frame_count"] == 1
    assert paced["seed_state"]["pending_frame_count"] == 2
    assert len(paced["acknowledgement_durations_nanoseconds"]) == 2
    assert paced["elapsed_nanoseconds"] >= 1_000_000_000
    assert paced["final_backlog_frames"] == 0
    assert paced["gate_result"] == "not_evaluated_non_physical"
    assert unpaced["persisted_state"] == paced["persisted_state"]

    aggregates = payload["aggregates"]
    assert [aggregate["acknowledged_frame_count"] for aggregate in aggregates] == [1, 3]
    for aggregate in aggregates:
        count = aggregate["acknowledged_frame_count"]
        assert aggregate["gate_result"] == "not_evaluated_non_physical"
        assert aggregate["measured_trials"] == 1
        assert aggregate["queue_limits"] == {
            "maximum_frames": count,
            "maximum_bytes": (2**63 - 1) // 4,
            "maximum_in_flight": min(8, count),
            "scope": "benchmark_only_not_product_cap",
        }
        assert aggregate["persisted_state"]["payload_bytes"] < 24 * 1024 * 1024
        assert aggregate["persisted_state"]["envelope_bytes"] < 32 * 1024 * 1024
        assert aggregate["correctness"] == {
            "production_seed_open_validated": True,
            "process_cold_reopen_validated": True,
            "process_launches_unique": True,
            "exact_first_middle_last_duplicate_conflict": True,
            "state_digest_stable": True,
        }
        trial = aggregate["trials"][0]
        assert trial["queue_limits"] == aggregate["queue_limits"]
        assert (
            trial["processes"]["reconcile"]["launch_id"]
            != trial["processes"]["process_cold_reopen"]["launch_id"]
        )
        assert trial["persisted_state"]["acknowledged_frame_count"] == count
        assert trial["persisted_state"]["pending_frame_count"] == 0
        assert trial["platform"]["optimized_build"] is True
        assert trial["platform"]["is_physical_device"] is False
        assert trial["platform"]["physical_gate_result"] == (
            "not_evaluated_non_physical"
        )
        assert [probe["sequence_id"] for probe in trial["sequence_probes"]] == sorted(
            {1, (count + 1) // 2, count}
        )


def test_hard_gate_passes_only_complete_eligible_acceptance_evidence() -> None:
    runner = _load_runner_module()
    inputs = _passing_gate_inputs()
    platform = _eligible_platform()
    assert runner._hard_gate_status(True, platform, inputs) == "passed"

    failures = []
    low_throughput = copy.deepcopy(inputs)
    low_throughput["sustained_acknowledgements_per_second"] = 9.99
    failures.append(low_throughput)
    wrong_pacing = copy.deepcopy(inputs)
    wrong_pacing["paced"]["acknowledgements_per_second"] = 4
    failures.append(wrong_pacing)
    missing_pacing = copy.deepcopy(inputs)
    del missing_pacing["paced"]["acknowledgements_per_second"]
    failures.append(missing_pacing)
    excessive_backlog = copy.deepcopy(inputs)
    excessive_backlog["paced"]["maximum_backlog_frames"] = 9
    failures.append(excessive_backlog)
    undrained_backlog = copy.deepcopy(inputs)
    undrained_backlog["paced"]["final_backlog_frames"] = 1
    failures.append(undrained_backlog)
    missing_count = copy.deepcopy(inputs)
    del missing_count["per_count"]["720"]
    missing_count["required_counts_present"] = False
    failures.append(missing_count)
    reused_process = copy.deepcopy(inputs)
    reused_process["correctness"]["process_launches_unique"] = False
    failures.append(reused_process)
    for failed_inputs in failures:
        assert runner._hard_gate_status(True, platform, failed_inputs) == "failed"


def test_hard_gate_rejects_ineligible_builds_and_test_profiles() -> None:
    runner = _load_runner_module()
    inputs = _passing_gate_inputs()
    platform = _eligible_platform()

    nonphysical = platform | {"is_physical_device": False}
    assert runner._hard_gate_status(True, nonphysical, inputs) == (
        "not_evaluated_non_physical"
    )
    newer_device = platform | {"is_oldest_supported_lidar_iphone": False}
    assert runner._hard_gate_status(True, newer_device, inputs) == (
        "not_evaluated_ineligible_device"
    )
    unoptimized = platform | {"optimized_build": False}
    assert runner._hard_gate_status(True, unoptimized, inputs) == (
        "not_evaluated_unoptimized_build"
    )
    assert runner._hard_gate_status(False, platform, inputs) == (
        "not_evaluated_test_override"
    )


def test_runner_refuses_to_write_a_generated_report_inside_git() -> None:
    repository = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/benchmark_ios_live_sender_ack_index.py"),
            "--counts",
            "1",
            "--warmups",
            "0",
            "--trials",
            "1",
            "--stream-count",
            "1",
            "--paced-rate",
            "1",
            "--paced-duration-seconds",
            "1",
            "--output",
            str(repository / "generated-ack-benchmark-report.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "must be written outside Git" in completed.stderr
    assert not (repository / "generated-ack-benchmark-report.json").exists()
