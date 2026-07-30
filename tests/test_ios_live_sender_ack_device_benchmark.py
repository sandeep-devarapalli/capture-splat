import base64
import copy
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the iOS benchmark collector requires macOS",
)


def _repository() -> Path:
    return Path(__file__).resolve().parents[1]


def _script() -> Path:
    return _repository() / "scripts/run_ios_live_sender_ack_device_benchmark.py"


def _load_runner() -> object:
    spec = importlib.util.spec_from_file_location(
        "ios_live_sender_ack_device_benchmark",
        _script(),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _state(acknowledged: int, pending: int) -> dict[str, object]:
    label = f"{acknowledged}:{pending}"
    return {
        "payload_bytes": 1_000 + acknowledged,
        "envelope_bytes": 2_000 + acknowledged,
        "payload_sha256": _sha256(f"payload:{label}"),
        "envelope_sha256": _sha256(f"envelope:{label}"),
        "acknowledged_frame_count": acknowledged,
        "pending_frame_count": pending,
    }


def _queue_limits(count: int) -> dict[str, object]:
    return {
        "maximum_frames": count,
        "maximum_bytes": (2**63 - 1) // 4,
        "maximum_in_flight": min(8, count),
        "scope": "benchmark_only_not_product_cap",
    }


def _process(index: int) -> dict[str, object]:
    return {
        "launch_id": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"capture-splat-device-benchmark-test:{index}",
            )
        ),
        "process_id": 1_000 + index,
    }


def _memory() -> dict[str, int]:
    return {
        "footprint_before_bytes": 8 * 1024 * 1024,
        "footprint_after_bytes": 9 * 1024 * 1024,
        "footprint_delta_bytes": 1 * 1024 * 1024,
        "kernel_reported_peak_footprint_bytes": 64 * 1024 * 1024,
    }


def _platform() -> dict[str, object]:
    return {
        "operating_system": "ios",
        "operating_system_version": "18.5",
        "machine": "iPhone17,2",
        "architecture": "arm64",
        "thermal_state": "nominal",
        "is_physical_device": True,
        "is_designated_ack_benchmark_device": True,
        "optimized_build": True,
        "physical_gate_result": (
            "physical_trial_requires_aggregate_gate_evaluation"
        ),
    }


def _probes(count: int) -> list[dict[str, object]]:
    return [
        {
            "sequence_id": sequence_id,
            "identical_disposition": "duplicate",
            "conflicting_reference_rejected": True,
        }
        for sequence_id in sorted({1, (count + 1) // 2, count})
    ]


def _correctness(count: int) -> dict[str, object]:
    return {
        "production_open_validated_external_state": True,
        "every_acknowledgement_reconciled_exactly_one_frame": True,
        "sequence_probes": _probes(count),
    }


def _reconcile(count: int, process_index: int) -> dict[str, object]:
    return {
        "schema": (
            "capture_splat.live_sender_ack_benchmark_reconcile_phase.v0.2"
        ),
        "configuration": {
            "acknowledged_frame_count": count,
            "trial_index": 0,
        },
        "queue_limits": _queue_limits(count),
        "process": _process(process_index),
        "seed_state": _state(count - 1, 1),
        "persisted_state": _state(count, 0),
        "reconcile_duration_nanoseconds": 10_000_000,
        "memory": _memory(),
        "platform": _platform(),
        "reconciled_sequence_ids": [count],
    }


def _reopen(count: int, process_index: int) -> dict[str, object]:
    return {
        "schema": (
            "capture_splat.live_sender_ack_benchmark_reopen_phase.v0.2"
        ),
        "configuration": {
            "acknowledged_frame_count": count,
            "trial_index": 0,
        },
        "queue_limits": _queue_limits(count),
        "process": _process(process_index),
        "persisted_state": _state(count, 0),
        "reopen_duration_nanoseconds": 20_000_000,
        "memory": _memory(),
        "platform": _platform(),
        "sequence_probes": _probes(count),
    }


def _unpaced(process_index: int) -> dict[str, object]:
    elapsed = 60_000_000_000
    return {
        "schema": (
            "capture_splat.live_sender_ack_benchmark_unpaced_stream_phase.v0.2"
        ),
        "final_acknowledged_frame_count": 720,
        "queue_limits": _queue_limits(720),
        "process": _process(process_index),
        "seed_state": _state(0, 720),
        "persisted_state": _state(720, 0),
        "acknowledgement_durations_nanoseconds": [10_000_000] * 720,
        "elapsed_nanoseconds": elapsed,
        "durable_acknowledgements_per_second": (
            720 * 1_000_000_000.0 / elapsed
        ),
        "memory": _memory(),
        "thermal_states": {"before": "nominal", "after": "nominal"},
        "platform": _platform(),
        "correctness": _correctness(720),
        "gate_result": (
            "measurement_passed_requires_aggregate_evaluation"
        ),
    }


def _paced(process_index: int) -> dict[str, object]:
    return {
        "schema": (
            "capture_splat.live_sender_ack_benchmark_paced_stream_phase.v0.2"
        ),
        "configuration": {
            "initial_acknowledged_frame_count": 420,
            "final_acknowledged_frame_count": 720,
            "acknowledgement_count": 300,
            "acknowledgements_per_second": 5,
            "nominal_duration_seconds": 60,
        },
        "queue_limits": _queue_limits(720),
        "process": _process(process_index),
        "seed_state": _state(420, 300),
        "persisted_state": _state(720, 0),
        "acknowledgement_durations_nanoseconds": [10_000_000] * 300,
        "elapsed_nanoseconds": 60_000_000_000,
        "drain_duration_nanoseconds": 0,
        "maximum_backlog_frames": 0,
        "backlog_at_nominal_end_frames": 0,
        "final_backlog_frames": 0,
        "memory": _memory(),
        "thermal_states": {"before": "nominal", "after": "nominal"},
        "platform": _platform(),
        "correctness": _correctness(720),
        "gate_result": (
            "measurement_passed_requires_aggregate_evaluation"
        ),
    }


def _fixture() -> dict[str, object]:
    counts = (360, 720)
    return {
        "schema": (
            "capture_splat.live_sender_ack_device_benchmark_fixture.v0.2"
        ),
        "matrix": {
            "counts": list(counts),
            "warmup_trials_per_phase_per_count": 0,
            "measured_trials_per_phase_per_count": 1,
        },
        "reconcile": {
            str(count): [_reconcile(count, index * 2)]
            for index, count in enumerate(counts)
        },
        "cold_reopen": {
            str(count): [_reopen(count, index * 2 + 1)]
            for index, count in enumerate(counts)
        },
        "unpaced_720": _unpaced(4),
        "paced_720": _paced(5),
    }


def _write_fixture(path: Path, fixture: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            fixture,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _decode_report(path: Path) -> dict[str, object]:
    envelope = json.loads(path.read_bytes())
    assert set(envelope) == {"schema", "payload_base64", "payload_sha256"}
    assert envelope["schema"] == (
        "capture_splat.live_sender_ack_device_benchmark_report_envelope.v0.2"
    )
    payload_bytes = base64.b64decode(
        envelope["payload_base64"],
        validate=True,
    )
    assert envelope["payload_sha256"] == (
        f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
    )
    return json.loads(
        payload_bytes,
        parse_constant=lambda value: pytest.fail(
            f"non-finite JSON value: {value}"
        ),
    )


@pytest.mark.parametrize(
    ("artifact", "expected_error"),
    [
        ("fixture", "unexpected device benchmark fixture schema"),
        ("phase", "unexpected reconcile phase schema"),
    ],
)
def test_v01_benchmark_artifacts_are_not_reinterpreted(
    tmp_path: Path,
    artifact: str,
    expected_error: str,
) -> None:
    runner = _load_runner()
    fixture = _fixture()
    if artifact == "fixture":
        fixture["schema"] = (
            "capture_splat.live_sender_ack_device_benchmark_fixture.v0.1"
        )
    else:
        fixture["reconcile"]["360"][0]["schema"] = (
            "capture_splat.live_sender_ack_benchmark_reconcile_phase.v0.1"
        )
    fixture_path = tmp_path / f"{artifact}.json"
    _write_fixture(fixture_path, fixture)

    with pytest.raises(ValueError, match=expected_error):
        runner._collect_fixture(fixture_path)


def test_fixture_writes_checksummed_fail_closed_report_outside_git(
    tmp_path: Path,
) -> None:
    fixture_path = tmp_path / "fixture.json"
    report_path = tmp_path / "report.json"
    _write_fixture(fixture_path, _fixture())

    completed = subprocess.run(
        [
            sys.executable,
            str(_script()),
            "--fixture",
            str(fixture_path),
            "--output",
            str(report_path),
        ],
        cwd=_repository(),
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(completed.stdout)
    assert summary["schema"] == (
        "capture_splat.live_sender_ack_device_benchmark_summary.v0.2"
    )
    assert summary["hard_gate_status"] == "not_evaluated_fixture"
    assert summary["m1b_physical_capture_acceptance"] == (
        "not_evaluated_capture_loop_disconnected"
    )
    assert Path(summary["report"]) == report_path
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600

    payload = _decode_report(report_path)
    assert payload["schema"] == (
        "capture_splat.live_sender_ack_device_benchmark_report.v0.2"
    )
    assert payload["hard_gate"]["status"] == "not_evaluated_fixture"
    assert payload["hard_gate"]["eligible_device_models"] == ["iPhone17,2"]
    assert payload["capture_isolation"] == {
        "capture_loop_connected": False,
        "writer_drops": "unmeasured",
        "capture_wait": "unmeasured",
        "keyframe_acceptance_changed": False,
    }
    assert payload["m1b_physical_capture_acceptance"]["status"] == (
        "not_evaluated_capture_loop_disconnected"
    )
    assert payload["process_provenance"] == {
        "expected_test_process_count": 6,
        "observed_test_process_count": 6,
        "required_test_process_count": 6,
        "observed_required_test_process_count": 6,
        "required_test_process_evidence_complete": True,
        "available_test_process_launch_ids_unique": True,
        "required_test_process_launch_ids_unique": True,
        "missing_future_scale_test_process_evidence_count": 0,
        "orchestrator_invocation_uuids_unique": True,
        "separate_xcodebuild_invocation_per_sample": False,
    }
    bindings = payload["aggregates"][0]["measured_evidence"]["reconcile"]
    assert bindings[0]["evidence"]["process"] == _process(0)
    assert set(payload["repository"]["source_fingerprints"]) == {
        "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveSenderQueue.swift",
        "apps/ios/CaptureSplat/CaptureSplat/Sources/LiveAuthContract.swift",
        "tests/swift/LiveSenderAckBenchmarkCore.swift",
        "tests/swift/LiveSenderAckBenchmarkTests.swift",
        "tests/swift/LiveSenderAckBenchmarkHost.swift",
        (
            "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/xcshareddata/"
            "xcschemes/CaptureSplatAckBenchmarks.xcscheme"
        ),
        "apps/ios/CaptureSplat/CaptureSplat.xcodeproj/project.pbxproj",
        "scripts/run_ios_live_sender_ack_device_benchmark.py",
    }


def test_dry_run_declares_352_distinct_one_test_process_invocations() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(_script()),
            "--dry-run",
            "--device-id",
            "TEST-DEVICE-123456",
        ],
        cwd=_repository(),
        check=True,
        capture_output=True,
        text=True,
    )
    plan = json.loads(completed.stdout)
    invocations = plan["test_invocations"]
    assert plan["schema"] == (
        "capture_splat.live_sender_ack_device_benchmark_plan.v0.2"
    )
    assert plan["eligible_device_models"] == ["iPhone17,2"]
    assert plan["test_invocation_count"] == 352
    assert len(invocations) == 352
    assert plan["one_separate_xcodebuild_process_per_sample"] is True
    assert plan["writes_report"] is False
    assert plan["capture_loop_connected"] is False

    result_bundles = []
    identifiers = []
    for invocation in invocations:
        command = invocation["command"]
        assert command[-1] == "test-without-building"
        assert "-test-iterations" not in command
        assert "-test-repetition-relaunch-enabled" not in command
        result_bundles.append(command[command.index("-resultBundlePath") + 1])
        identifiers.append(invocation["test_identifier"])
    assert len(set(result_bundles)) == 352
    assert identifiers.count(
        "CaptureSplatAckBenchmarks/LiveSenderAckBenchmarkTests/"
        "testReconcileAcknowledgedFrames360"
    ) == 35
    assert identifiers.count(
        "CaptureSplatAckBenchmarks/LiveSenderAckBenchmarkTests/"
        "testColdReopenAcknowledgedFrames50000"
    ) == 35
    assert identifiers[140:142] == [
        "CaptureSplatAckBenchmarks/LiveSenderAckBenchmarkTests/"
        "testUnpacedAcknowledgementStream720",
        "CaptureSplatAckBenchmarks/LiveSenderAckBenchmarkTests/"
        "testPacedAcknowledgementStream720",
    ]
    assert [invocation["count"] for invocation in invocations[:70]] == [
        360
    ] * 70
    assert [invocation["count"] for invocation in invocations[70:140]] == [
        720
    ] * 70
    for offset, count in enumerate((1_000, 10_000, 50_000)):
        start = 142 + offset * 70
        assert [
            invocation["count"] for invocation in invocations[start : start + 70]
        ] == [count] * 70
    assert plan["build_settings_command"][
        plan["build_settings_command"].index("-target") + 1
    ] == "CaptureSplatAckBenchmarks"
    assert plan["build_settings_command"][
        plan["build_settings_command"].index("-sdk") + 1
    ] == "iphoneos"


def test_real_collection_uses_the_declared_required_first_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()
    observed = []
    monkeypatch.setattr(runner, "_show_build_settings", lambda *_: {})
    monkeypatch.setattr(
        runner,
        "_run_command",
        lambda *_, **__: {
            "process_id": 1,
            "stdout_sha256": _sha256("build-stdout"),
            "stderr_sha256": _sha256("build-stderr"),
        },
    )

    def record_invocation(**arguments: object) -> dict[str, object]:
        observed.append(
            (
                arguments["phase"],
                arguments["count"],
                arguments["sample_index"],
                arguments.get("required_evidence", True),
            )
        )
        return {"phase": arguments["phase"], "count": arguments["count"]}

    monkeypatch.setattr(runner, "_run_test_invocation", record_invocation)
    collection = runner._collect_real(
        repository=_repository(),
        device_id="TEST-DEVICE-123456",
        work_root=tmp_path,
        timeout_seconds=1,
    )

    assert len(observed) == 352
    assert [entry[1] for entry in observed[:70]] == [360] * 70
    assert [entry[1] for entry in observed[70:140]] == [720] * 70
    assert [(entry[0], entry[1]) for entry in observed[140:142]] == [
        ("unpaced", None),
        ("paced", None),
    ]
    for offset, count in enumerate((1_000, 10_000, 50_000)):
        start = 142 + offset * 70
        assert [entry[1] for entry in observed[start : start + 70]] == [
            count
        ] * 70
    assert all(entry[3] is True for entry in observed[:142])
    assert all(entry[3] is False for entry in observed[142:])
    assert collection["matrix"]["counts"] == list(runner.COUNTS)


def test_strict_json_and_phase_contract_reject_nonfinite_and_extra_fields() -> None:
    runner = _load_runner()
    with pytest.raises(ValueError, match="duplicate JSON key"):
        runner._load_json_bytes(b'{"value":1,"value":2}', "duplicate")
    with pytest.raises(ValueError, match="non-finite JSON value"):
        runner._load_json_bytes(b'{"value":NaN}', "nonfinite")
    with pytest.raises(ValueError, match="not canonical JSON"):
        runner._load_json_bytes(
            b'{"value": 1}',
            "noncanonical",
            require_canonical=True,
        )

    phase = _reconcile(360, 0)
    phase["unexpected"] = True
    with pytest.raises(ValueError, match="missing or additional fields"):
        runner._validate_reconcile(phase, 360)


def test_aggregation_rejects_reused_test_process_launch_id(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    fixture = _fixture()
    fixture["cold_reopen"]["360"][0]["process"] = copy.deepcopy(
        fixture["reconcile"]["360"][0]["process"]
    )
    fixture_path = tmp_path / "duplicate-process.json"
    _write_fixture(fixture_path, fixture)
    collection = runner._collect_fixture(fixture_path)

    with pytest.raises(ValueError, match="launch_id was reused"):
        runner._aggregate_collection(collection)


def test_report_output_is_refused_inside_repository() -> None:
    runner = _load_runner()
    with pytest.raises(ValueError, match="outside Git"):
        runner._resolve_output(_repository() / "device-benchmark-report.json")


def test_release_build_settings_are_fail_closed() -> None:
    runner = _load_runner()
    release = "\n".join(
        [
            "    CONFIGURATION = Release",
            "    SWIFT_OPTIMIZATION_LEVEL = -O",
            "    SWIFT_COMPILATION_MODE = wholemodule",
            (
                "    SWIFT_ACTIVE_COMPILATION_CONDITIONS = "
                "CAPTURE_SPLAT_ACK_BENCHMARK_OPTIMIZED"
            ),
            "    ENABLE_NS_ASSERTIONS = NO",
            "    PLATFORM_NAME = iphoneos",
            "    PRODUCT_NAME = CaptureSplatAckBenchmarks",
            (
                "    PRODUCT_BUNDLE_IDENTIFIER = "
                "com.example.CaptureSplat.AckBenchmarks"
            ),
            "    PRODUCT_TYPE = com.apple.product-type.bundle.unit-test",
            "    TARGET_NAME = CaptureSplatAckBenchmarks",
            (
                "    TEST_HOST = /tmp/build/CaptureSplatAckBenchmarkHost.app/"
                "CaptureSplatAckBenchmarkHost"
            ),
            (
                "    BUNDLE_LOADER = /tmp/build/"
                "CaptureSplatAckBenchmarkHost.app/"
                "CaptureSplatAckBenchmarkHost"
            ),
            "    TEST_TARGET_NAME = CaptureSplatAckBenchmarkHost",
            "    WRAPPER_EXTENSION = xctest",
        ]
    )
    parsed = runner._parse_build_settings(release)
    assert parsed["SWIFT_OPTIMIZATION_LEVEL"] == "-O"
    assert parsed["TEST_TARGET_NAME"] == "CaptureSplatAckBenchmarkHost"
    assert parsed["TEST_HOST"] == parsed["BUNDLE_LOADER"]

    debug = release.replace(
        "CAPTURE_SPLAT_ACK_BENCHMARK_OPTIMIZED",
        "CAPTURE_SPLAT_ACK_BENCHMARK_OPTIMIZED DEBUG",
    )
    with pytest.raises(ValueError, match="not an optimized, dedicated-host"):
        runner._parse_build_settings(debug)

    production_host = release.replace(
        "CaptureSplatAckBenchmarkHost",
        "CaptureSplat",
    )
    with pytest.raises(ValueError, match="dedicated-host"):
        runner._parse_build_settings(production_host)


def test_xcode_graph_uses_only_the_dedicated_benchmark_host() -> None:
    runner = _load_runner()
    isolation = runner._validate_benchmark_host_isolation(_repository())
    assert isolation == {
        "host_target": "CaptureSplatAckBenchmarkHost",
        "host_product": "CaptureSplatAckBenchmarkHost.app",
        "host_source": "tests/swift/LiveSenderAckBenchmarkHost.swift",
        "test_target": "CaptureSplatAckBenchmarks",
        "production_app_target": "CaptureSplat",
        "production_app_dependency": False,
    }
    host_source = (
        _repository() / "tests/swift/LiveSenderAckBenchmarkHost.swift"
    ).read_text()
    assert "UIApplicationDelegate" in host_source
    assert "CaptureController" not in host_source
    assert "ARKit" not in host_source


def _passing_gate_arguments(runner: object) -> dict[str, object]:
    platform = _platform()
    platform.pop("thermal_state")
    platform["thermal_states"] = ["nominal"]
    aggregates = []
    for count in runner.COUNTS:
        aggregates.append(
            {
                "acknowledged_frame_count": count,
                "gate_result": (
                    "passed"
                    if count in runner.HARD_GATE_COUNTS
                    else "diagnostic_future_scale_failed"
                ),
                "persisted_state": (
                    _state(count, 0)
                    if count in runner.HARD_GATE_COUNTS
                    else None
                ),
                "platform": (
                    platform
                    if count in runner.HARD_GATE_COUNTS
                    else None
                ),
                "checks": {
                    "all_xcode_test_commands_succeeded": (
                        count in runner.HARD_GATE_COUNTS
                    )
                },
            }
        )
    latency = {
        "sample_count": 30,
        "p50_nanoseconds": 10_000_000,
        "p95_nanoseconds": 20_000_000,
        "maximum_nanoseconds": 30_000_000,
    }
    memory = {
        "maximum_phase_footprint_delta_bytes": 1 * 1024 * 1024,
        "maximum_kernel_reported_peak_footprint_bytes": 64 * 1024 * 1024,
    }
    unpaced = {
        "platform": platform,
        "persisted_state": _state(720, 0),
        "ack_persistence": latency,
        "memory": memory,
        "core_gate_result": (
            "measurement_passed_requires_aggregate_evaluation"
        ),
        "xcode_test_command_succeeded": True,
        "unpaced": {"durable_acknowledgements_per_second": 12.0},
    }
    paced = {
        "platform": platform,
        "persisted_state": _state(720, 0),
        "ack_persistence": latency,
        "memory": memory,
        "core_gate_result": (
            "measurement_passed_requires_aggregate_evaluation"
        ),
        "xcode_test_command_succeeded": True,
        "paced": {
            "maximum_backlog_frames": 0,
            "final_backlog_frames": 0,
        },
    }
    return {
        "run_profile": "acceptance",
        "matrix": {
            "counts": list(runner.COUNTS),
            "warmup_trials_per_phase_per_count": runner.WARMUP_TRIALS,
            "measured_trials_per_phase_per_count": runner.MEASURED_TRIALS,
            "separate_xcodebuild_invocation_per_sample": True,
        },
        "aggregates": aggregates,
        "unpaced": unpaced,
        "paced": paced,
        "optimized_build_settings": True,
        "process_provenance": {
            "expected_test_process_count": 352,
            "observed_test_process_count": 352,
            "required_test_process_count": 142,
            "observed_required_test_process_count": 142,
            "required_test_process_evidence_complete": True,
            "available_test_process_launch_ids_unique": True,
            "required_test_process_launch_ids_unique": True,
            "missing_future_scale_test_process_evidence_count": 1,
            "orchestrator_invocation_uuids_unique": True,
            "separate_xcodebuild_invocation_per_sample": True,
        },
    }


def test_future_scale_failure_does_not_demote_passing_360_720_gate() -> None:
    runner = _load_runner()
    gate = runner._evaluate_hard_gate(**_passing_gate_arguments(runner))
    assert gate["status"] == "passed"
    assert gate["checks"]["all_xcode_test_commands_succeeded"] is True


def test_device_gate_rejects_required_performance_and_provenance_failures() -> None:
    runner = _load_runner()

    low_throughput = _passing_gate_arguments(runner)
    low_throughput["unpaced"]["unpaced"][
        "durable_acknowledgements_per_second"
    ] = 9.99

    excessive_backlog = _passing_gate_arguments(runner)
    excessive_backlog["paced"]["paced"]["maximum_backlog_frames"] = 9

    final_backlog = _passing_gate_arguments(runner)
    final_backlog["paced"]["paced"]["final_backlog_frames"] = 1

    missing_process = _passing_gate_arguments(runner)
    missing_process["process_provenance"][
        "required_test_process_evidence_complete"
    ] = False

    missing_360 = _passing_gate_arguments(runner)
    missing_360["aggregates"] = [
        aggregate
        for aggregate in missing_360["aggregates"]
        if aggregate["acknowledged_frame_count"] != 360
    ]

    missing_720 = _passing_gate_arguments(runner)
    missing_720["aggregates"] = [
        aggregate
        for aggregate in missing_720["aggregates"]
        if aggregate["acknowledged_frame_count"] != 720
    ]

    for arguments in (
        low_throughput,
        excessive_backlog,
        final_backlog,
        missing_process,
        missing_360,
        missing_720,
    ):
        assert runner._evaluate_hard_gate(**arguments)["status"] == "failed"


@pytest.mark.parametrize(
    ("platform_updates", "expected_status"),
    [
        (
            {
                "is_physical_device": False,
                "is_designated_ack_benchmark_device": False,
                "physical_gate_result": "not_evaluated_non_physical",
            },
            "not_evaluated_non_physical",
        ),
        (
            {
                "machine": "iPhone17,1",
                "is_designated_ack_benchmark_device": False,
                "physical_gate_result": "not_evaluated_ineligible_device",
            },
            "not_evaluated_ineligible_device",
        ),
        (
            {
                "machine": "iPhone13,3",
                "is_designated_ack_benchmark_device": False,
                "physical_gate_result": "not_evaluated_ineligible_device",
            },
            "not_evaluated_ineligible_device",
        ),
        (
            {
                "machine": "iPhone13,4",
                "is_designated_ack_benchmark_device": False,
                "physical_gate_result": "not_evaluated_ineligible_device",
            },
            "not_evaluated_ineligible_device",
        ),
        (
            {
                "optimized_build": False,
                "physical_gate_result": "not_evaluated_unoptimized_build",
            },
            "not_evaluated_unoptimized_build",
        ),
    ],
)
def test_device_gate_classifies_ineligible_platforms(
    platform_updates: dict[str, object],
    expected_status: str,
) -> None:
    runner = _load_runner()
    arguments = _passing_gate_arguments(runner)
    summaries = [
        *[
            aggregate
            for aggregate in arguments["aggregates"]
            if aggregate["acknowledged_frame_count"] in (360, 720)
        ],
        arguments["unpaced"],
        arguments["paced"],
    ]
    for summary in summaries:
        summary["platform"].update(platform_updates)

    assert runner._evaluate_hard_gate(**arguments)["status"] == expected_status


@pytest.mark.parametrize(
    "platform_updates",
    [
        {
            "is_designated_ack_benchmark_device": False,
            "physical_gate_result": "not_evaluated_ineligible_device",
        },
        {
            "machine": "iPhone13,3",
            "physical_gate_result": (
                "physical_trial_requires_aggregate_gate_evaluation"
            ),
        },
    ],
)
def test_platform_rejects_forged_designated_device_claim(
    platform_updates: dict[str, object],
) -> None:
    runner = _load_runner()
    with pytest.raises(ValueError, match="designated benchmark-device"):
        runner._validate_platform(_platform() | platform_updates)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("acknowledgements_per_second", 6),
        ("nominal_duration_seconds", 61),
    ],
)
def test_paced_phase_rejects_wrong_rate_or_duration(
    field: str,
    value: int,
) -> None:
    runner = _load_runner()
    phase = _paced(0)
    phase["configuration"][field] = value
    with pytest.raises(ValueError, match="configuration changed"):
        runner._validate_paced(phase)


def test_unpaced_phase_rejects_missing_throughput() -> None:
    runner = _load_runner()
    phase = _unpaced(0)
    phase.pop("durable_acknowledgements_per_second")
    with pytest.raises(ValueError, match="missing or additional fields"):
        runner._validate_unpaced(phase)


def test_missing_future_scale_attachment_is_preserved_as_diagnostic() -> None:
    runner = _load_runner()
    missing = runner._fixture_record(
        phase="reconcile",
        count=50_000,
        sample_kind="measured",
        sample_index=0,
        evidence=None,
    )
    missing["diagnostic"] = {
        "missing_or_invalid_evidence": True,
        "error_type": "RuntimeError",
        "error_sha256": _sha256("missing"),
    }
    reopened = runner._fixture_record(
        phase="cold_reopen",
        count=50_000,
        sample_kind="measured",
        sample_index=0,
        evidence=_reopen(50_000, 1),
    )

    aggregate = runner._aggregate_count(
        50_000,
        [missing],
        [reopened],
        warmups=0,
        trials=1,
    )

    assert aggregate["gate_result"] == "diagnostic_future_scale_failed"
    assert aggregate["checks"]["missing_or_invalid_evidence_count"] == 1
    assert aggregate["persisted_state"] is None
    assert aggregate["measured_evidence"]["reconcile"][0][
        "diagnostic"
    ]["missing_or_invalid_evidence"] is True


def test_acceptance_refuses_a_dirty_repository_before_xcodebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner()

    def fake_git(repository: Path, arguments: list[str]) -> str:
        if arguments == ["rev-parse", "--show-toplevel"]:
            return str(repository)
        if arguments == ["rev-parse", "HEAD"]:
            return "a" * 40
        if arguments == [
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]:
            return " M README.md"
        raise AssertionError(f"unexpected Git arguments: {arguments}")

    monkeypatch.setattr(runner, "_git", fake_git)
    monkeypatch.setattr(
        runner,
        "_source_fingerprints",
        lambda repository: {},
    )

    with pytest.raises(ValueError, match="requires a completely clean"):
        runner._repository_identity(_repository(), require_clean=True)
