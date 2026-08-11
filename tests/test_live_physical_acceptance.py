from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.live_physical_acceptance import OUTPUT_NAME, run_live_physical_acceptance


def _matrix() -> list[list[float]]:
    return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _capture(root: Path, count: int, duration: float = 10.0) -> None:
    frames = []
    for index in range(1, count + 1):
        rgb = f"rgb/{index:06d}.jpg"
        depth = f"depth/{index:06d}.npy"
        (root / rgb).parent.mkdir(parents=True, exist_ok=True)
        (root / depth).parent.mkdir(parents=True, exist_ok=True)
        (root / rgb).write_bytes(b"rgb")
        (root / depth).write_bytes(b"depth")
        frames.append(
            {
                "rgb": rgb,
                "depth": depth,
                "timestamp": (index - 1) * duration / max(count - 1, 1),
                "transform_matrix": _matrix(),
                "intrinsics": {"fl_x": 100.0, "fl_y": 100.0, "cx": 50.0, "cy": 40.0, "w": 100, "h": 80},
                "capture_quality": {"accepted": True},
            }
        )
    write_json_strict(root / "capture.json", {"schema": "capture_splat.v0.3", "frames": frames})
    write_json_strict(
        root / "metadata/finalization_report.json",
        {
            "schema": "capture_splat.finalization_report.v0.1",
            "status": "finalized",
            "manifest_written": True,
            "accepted_keyframe_count": count,
            "video_writer_status": "completed",
            "video_dropped_frame_count": 0,
        },
    )
    write_json_strict(
        root / "metadata/session_report.json",
        {"capture_duration_seconds": duration, "dropped_frames": 0, "peak_memory_bytes": 300_000_000},
    )
    write_json_strict(root / "metadata/sensor_health.json", {"thermal_state": "nominal"})
    write_json_strict(
        root / "metadata/spatial_guidance_report.json",
        {
            "schema": "capture_splat.spatial_guidance.v0.2",
            "thermal_summary": {
                "capture_duration_seconds": duration,
                "thermal_state_seconds": {"nominal": duration},
            },
            "thermal_transitions": [],
        },
    )


def _transport_reports(root: Path, count: int) -> tuple[Path, Path]:
    checksum = "sha256:" + "a" * 64
    sender = root / "sender.json"
    receiver = root / "receiver.json"
    write_json_strict(
        sender,
        {
            "schema": "capture_splat.live_sender_physical_report.v0.1",
            "session_id": "csl_fixture",
            "finalized": True,
            "final_sequence_id": count,
            "manifest_sha256": checksum,
            "queue": {"overflow_count": 0, "evidence_loss_count": 0, "pending_frame_count": 0},
            "integrity": {"checksum_mismatch_count": 0, "evidence_corruption_count": 0},
            "ack": {"mean_latency_ms": 8.0, "p95_latency_ms": 12.0, "max_latency_ms": 18.0},
            "memory": {"peak_bytes": 320_000_000},
            "thermal": {"max_state": "nominal"},
        },
    )
    write_json_strict(
        receiver,
        {
            "schema": "world_studio.live_receiver_snapshot.v0.1",
            "session_id": "csl_fixture",
            "finalized": True,
            "final_sequence_id": count,
            "received_frame_count": count,
            "manifest_sha256": checksum,
            "missing_ranges": [],
            "integrity": {"checksum_mismatch_count": 0, "evidence_corruption_count": 0},
        },
    )
    return sender, receiver


def _receiver_session(root: Path, count: int) -> Path:
    session = root / "receiver-session"
    session.mkdir(parents=True)
    session_id = "csl_fixture"
    checksum = "sha256:" + "a" * 64
    write_json_strict(
        session / "state.json",
        {
            "schema": "capture_splat.live_store_state.v0.1",
            "session_id": session_id,
            "expected_frame_count": count,
            "final_sequence_id": count,
            "received_count": count,
            "contiguous_count": count,
            "pending_count": 0,
            "next_expected_sequence_id": count + 1,
            "missing_ranges": [],
            "finalized": True,
            "updated_at": "2026-07-30T10:01:00.000Z",
        },
    )
    write_json_strict(
        session / "source-manifest-binding.json",
        {
            "schema": "capture_splat.live_finalize.v0.2",
            "session_id": session_id,
            "final_sequence_id": count,
            "source_manifest": {
                "path": "capture.json",
                "sha256": checksum,
                "size_bytes": 100,
                "schema": "capture_splat.v0.3",
            },
        },
    )
    write_json_strict(
        session / "capture-splat.world-studio.json",
        {
            "schema": "capture_splat.world_studio_handoff.v0.1",
            "status": "visual_evidence",
            "authority": "proposal_only",
            "session_id": session_id,
            "final_sequence_id": count,
            "source_frames": [{"sequence_id": index} for index in range(1, count + 1)],
        },
    )

    def sha256(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    write_json_strict(
        session / "finalized.json",
        {
            "schema": "capture_splat.live_finalized.v0.2",
            "session_id": session_id,
            "final_sequence_id": count,
            "source_manifest_binding_path": "source-manifest-binding.json",
            "source_manifest_binding_sha256": sha256(session / "source-manifest-binding.json"),
            "handoff_path": "capture-splat.world-studio.json",
            "handoff_sha256": sha256(session / "capture-splat.world-studio.json"),
            "finalized_at": "2026-07-30T10:01:00.000Z",
        },
    )
    return session


def test_live_physical_acceptance_promotes_complete_evidence(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 9)
    sender, receiver = _transport_reports(tmp_path, 9)
    scenarios = {}
    for name in ("receiver_restart", "wifi_interruption", "app_relaunch", "second_capture_cycle"):
        path = tmp_path / f"{name}.json"
        write_json_strict(
            path,
            {
                "schema": "capture_splat.physical_scenario.v0.1",
                "executed": True,
                "passed": True,
            },
        )
        scenarios[name] = path

    summary = run_live_physical_acceptance(
        baseline,
        enabled,
        sender,
        receiver,
        tmp_path / "out",
        scenario_evidence=scenarios,
    )

    assert summary["decision"] == "promote"
    assert summary["warnings"] == []
    assert summary["blockers"] == []
    assert summary["throughput_comparison"]["enabled_to_baseline_ratio"] == pytest.approx(0.9)
    assert summary["scenario_evidence"]["receiver_restart"]["executed"] is True
    assert summary["scenario_evidence"]["wifi_interruption"]["passed"] is True
    assert all(value is False for value in summary["authority"].values())
    written = load_json_strict(tmp_path / "out" / OUTPUT_NAME)
    assert written == summary


def test_live_physical_acceptance_reads_world_studio_session_directory(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 9)
    sender, _ = _transport_reports(tmp_path, 9)
    write_json_strict(
        sender,
        {
            "schema": "capture_splat.m1b_physical_acceptance_telemetry.v0.1",
            "session_id": "csl_fixture",
            "finalization_state": "receiver_finalized",
            "final_sequence_id": 9,
            "manifest_sha256": "sha256:" + "a" * 64,
            "queue_current_frames": 0,
            "queue_overflow_count": 0,
            "queue_evidence_loss_count": 0,
            "request_acknowledgement_latency_available": True,
            "request_acknowledgement_sample_count": 36,
            "request_acknowledgement_latency_mean_ms": 8.0,
            "request_acknowledgement_latency_p95_ms": 12.0,
            "request_acknowledgement_latency_max_ms": 18.0,
            "request_retry_count": 1,
        },
    )
    receiver = _receiver_session(tmp_path, 9)
    scenarios = {}
    for name in ("receiver_restart", "wifi_interruption", "app_relaunch", "second_capture_cycle"):
        path = tmp_path / f"{name}.json"
        write_json_strict(path, {"executed": True, "passed": True})
        scenarios[name] = path

    summary = run_live_physical_acceptance(
        baseline,
        enabled,
        sender,
        receiver,
        tmp_path / "out",
        scenario_evidence=scenarios,
    )

    assert summary["decision"] == "promote"
    assert summary["receiver"]["source"]["path"] == str(receiver.resolve())
    assert summary["receiver"]["source"]["components"]["finalized"]["present"] is True
    assert summary["receiver"]["received_frame_count"] == 9
    assert summary["sender"]["ack_latency"]["sample_count"] == 36


def test_live_physical_acceptance_rejects_hard_failures(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 8)
    sender, receiver = _transport_reports(tmp_path, 8)

    finalization = load_json_strict(enabled / "metadata/finalization_report.json")
    finalization["video_dropped_frame_count"] = 1
    write_json_strict(enabled / "metadata/finalization_report.json", finalization)
    (enabled / "depth/000004.npy").unlink()
    sender_data = load_json_strict(sender)
    sender_data["queue"]["overflow_count"] = 1
    sender_data["integrity"]["checksum_mismatch_count"] = 1
    sender_data["writer_drop_count"] = 1
    write_json_strict(sender, sender_data)
    receiver_data = load_json_strict(receiver)
    receiver_data["final_sequence_id"] = 7
    receiver_data["received_frame_count"] = 7
    receiver_data["missing_ranges"] = [{"start": 8, "end": 8}]
    write_json_strict(receiver, receiver_data)

    summary = run_live_physical_acceptance(baseline, enabled, sender, receiver, tmp_path / "out")

    assert summary["decision"] == "reject"
    assert "enabled_required_frame_evidence_missing" in summary["blockers"]
    assert "enabled_writer_drops_reported" in summary["blockers"]
    assert "sender_enabled_throughput_below_threshold" in summary["blockers"]
    assert "sender_queue_overflow_reported" in summary["blockers"]
    assert "sender_checksum_mismatch_reported" in summary["blockers"]
    assert "sender_writer_drops_reported" in summary["blockers"]
    assert "sender_receiver_finalization_mismatch" in summary["blockers"]
    assert "receiver_missing_ranges_nonempty" in summary["blockers"]
    assert "receiver_required_frames_missing" in summary["blockers"]


def test_live_physical_acceptance_holds_missing_optional_instrumentation(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 9)
    sender, receiver = _transport_reports(tmp_path, 9)

    for capture in (baseline, enabled):
        session = load_json_strict(capture / "metadata/session_report.json")
        session.pop("peak_memory_bytes")
        write_json_strict(capture / "metadata/session_report.json", session)
        (capture / "metadata/spatial_guidance_report.json").unlink()
        write_json_strict(capture / "metadata/sensor_health.json", {})
    sender_data = load_json_strict(sender)
    sender_data.pop("memory")
    sender_data.pop("thermal")
    sender_data.pop("ack")
    write_json_strict(sender, sender_data)

    summary = run_live_physical_acceptance(baseline, enabled, sender, receiver, tmp_path / "out")

    assert summary["decision"] == "hold"
    assert summary["blockers"] == []
    assert "baseline_memory_evidence_missing" in summary["warnings"]
    assert "enabled_memory_evidence_missing" in summary["warnings"]
    assert "baseline_thermal_evidence_incomplete" in summary["warnings"]
    assert "enabled_thermal_evidence_incomplete" in summary["warnings"]
    assert "sender_ack_latency_evidence_missing" in summary["warnings"]


def test_live_physical_acceptance_holds_unexecuted_required_scenarios(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 9)
    sender, receiver = _transport_reports(tmp_path, 9)

    summary = run_live_physical_acceptance(
        baseline,
        enabled,
        sender,
        receiver,
        tmp_path / "out",
    )

    assert summary["decision"] == "hold"
    assert summary["blockers"] == []
    assert "scenario_receiver_restart_evidence_missing" in summary["warnings"]
    assert "scenario_wifi_interruption_evidence_missing" in summary["warnings"]
    assert "scenario_app_relaunch_evidence_missing" in summary["warnings"]
    assert "scenario_second_capture_cycle_evidence_missing" in summary["warnings"]


def test_live_physical_acceptance_rejects_nonfinite_input_json(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 9)
    sender, receiver = _transport_reports(tmp_path, 9)
    sender.write_text('{"finalized": true, "ack_latency_ms": NaN}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON constant"):
        run_live_physical_acceptance(baseline, enabled, sender, receiver, tmp_path / "out")


def test_live_physical_acceptance_rejects_failed_attached_scenario(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    enabled = tmp_path / "enabled"
    _capture(baseline, 10)
    _capture(enabled, 9)
    sender, receiver = _transport_reports(tmp_path, 9)
    wifi = tmp_path / "wifi.json"
    write_json_strict(wifi, {"executed": True, "passed": False, "status": "recovery_failed"})

    summary = run_live_physical_acceptance(
        baseline,
        enabled,
        sender,
        receiver,
        tmp_path / "out",
        scenario_evidence={"wifi_interruption": wifi},
    )

    assert summary["decision"] == "reject"
    assert "scenario_wifi_interruption_failed" in summary["blockers"]
