import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Swift host probe requires macOS")


@pytest.fixture(scope="module")
def live_capture_sender_bridge_probe(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    repository = Path(__file__).resolve().parents[1]
    build_root = tmp_path_factory.mktemp("live-capture-sender-bridge-probe")
    executable = build_root / "live-capture-sender-bridge-probe"
    environment = os.environ.copy()
    environment["CLANG_MODULE_CACHE_PATH"] = str(build_root / "clang-module-cache")
    environment["SWIFT_MODULECACHE_PATH"] = str(build_root / "swift-module-cache")
    sources = repository / "apps/ios/CaptureSplat/CaptureSplat/Sources"
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-swift-version",
            "5",
            "-parse-as-library",
            "-D",
            "CAPTURE_SPLAT_LIVE_TESTING",
            str(sources / "LiveAuthContract.swift"),
            str(sources / "LiveAuthClient.swift"),
            str(sources / "LiveApplicationSupport.swift"),
            str(sources / "LiveBonjourResolver.swift"),
            str(sources / "LiveSenderQueue.swift"),
            str(sources / "LiveSender.swift"),
            str(sources / "LiveCaptureJournal.swift"),
            str(sources / "LiveCaptureSenderBridge.swift"),
            str(repository / "tests/swift/LiveCaptureSenderBridgeProbe.swift"),
            "-o",
            str(executable),
        ],
        check=True,
        env=environment,
    )
    return executable, repository


def _run(
    probe: tuple[Path, Path],
    scenario: str,
    working_root: Path,
) -> dict[str, object]:
    executable, _ = probe
    result = subprocess.run(
        [str(executable), scenario, str(working_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_live_capture_metadata_is_canonical_and_mirrors_v01_fixture(
    live_capture_sender_bridge_probe: tuple[Path, Path],
) -> None:
    _, repository = live_capture_sender_bridge_probe
    result = _run(live_capture_sender_bridge_probe, "metadata", repository)

    assert result == {
        "canonical_frame_round_trip": True,
        "canonical_frame_sha256": "sha256:3bf3cf33e795223042fd910c07514dc4d80dcddb240f69951c9442fa66f0f8d8",
        "canonical_session_match": True,
        "canonical_session_sha256": "sha256:2c76cc3daf221f8f40916a4bc5f9d60288ea04cc064014010a85f720da8290c3",
        "fixture_mask_round_trip": True,
        "session_expected_count_is_null": True,
        "session_id_match": True,
    }


def test_file_evidence_rejects_unsafe_or_changed_inputs(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "file-evidence", tmp_path)

    assert result == {
        "actual_jpeg_dimensions": True,
        "changed_file_rejected": True,
        "dimension_mismatch_rejected": True,
        "nan_rejected": True,
        "symlink_rejected": True,
        "traversal_rejected": True,
    }


def test_bridge_is_nonblocking_restart_safe_and_manifest_gated(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "bridge", tmp_path)

    assert result == {
        "binding_durable": True,
        "callback_nonblocking": True,
        "duplicate_conflict_preserved_one_frame": True,
        "finalization_blocked_without_manifest": True,
        "finalization_restored_from_journal": True,
        "frame_disposition": "accepted",
        "frame_metadata_canonical": True,
        "frame_ready": True,
        "no_masks": True,
        "queued_assets": True,
        "queued_frame_count": 1,
        "requester_blocked": True,
        "restart_restored_unnotified_frame": True,
        "restart_reused_seed": True,
        "start_disposition": "accepted",
    }


def test_receiver_restart_retries_without_a_new_sender_wake(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "retry-recovery", tmp_path)

    assert result == {
        "exact_capped_outer_delays": True,
        "inner_attempt_exhaustion_reentered": True,
        "no_new_wake_required": True,
        "retry_start_disposition": "accepted",
        "session_remains_durable": True,
    }


def test_pairing_and_environment_changes_cancel_or_wake_the_single_drive(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(
        live_capture_sender_bridge_probe,
        "pairing-environment",
        tmp_path,
    )

    assert result == {
        "direct_environment_initial_visible": True,
        "direct_environment_updates_visible": True,
        "foreground_cancellation_before_next_operation": True,
        "inactive_no_pending_pointer": True,
        "inactive_start_disposition": "disabled",
        "pairing_activation_woke_sender": True,
        "pairing_cancellation_stopped_current_drive": True,
        "pairing_start_disposition": "accepted",
        "pending_pointer_synchronous": True,
    }


@pytest.mark.parametrize(
    "scenario",
    ["thermal-deferral", "critical-thermal-deferral"],
)
def test_thermal_pause_defers_live_preparation_until_nominal(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
    scenario: str,
) -> None:
    result = _run(
        live_capture_sender_bridge_probe,
        scenario,
        tmp_path,
    )

    assert result == {
        "accepted_callbacks_deferred": True,
        "binding_ready_under_thermal_pause": True,
        "journal_durable_during_pause": True,
        "live_preparation_deferred": True,
        "nominal_backfill_finalized": True,
        "restart_deferred_journal_backfill": True,
        "start_disposition": "accepted",
        "transfer_pointer_cleared_after_resume": True,
    }


def test_pending_capture_is_promoted_after_a_prebinding_crash(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "pending-crash", tmp_path)

    assert result == {
        "exact_pending_capture_promoted": True,
        "fresh_bridge_needed_no_capture_event": True,
        "no_current_before_crash": True,
        "other_capture_not_enumerated": True,
        "pending_connector_was_gated": True,
        "pending_exact_session_and_metadata": True,
        "pending_pointer_synchronous": True,
        "pending_start_disposition": "accepted",
        "precrash_session_seed_committed": True,
        "repeated_start_reused_metadata": True,
    }


def test_exact_desktop_identity_gates_every_positive_sender_wake(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "desktop-gating", tmp_path)

    assert result == {
        "exact_desktop_restored_requester": True,
        "foreground_network_thermal_wakes_blocked_without_pairing": True,
        "network_state_positive": True,
        "session_was_durable": True,
        "wrong_desktop_blocked_requester": True,
    }


def test_empty_capture_abort_clears_only_empty_transfer_state(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "empty-abort", tmp_path)

    assert result == {
        "abort_disposition": "accepted",
        "accepted_frame_abort_refused": True,
        "empty_abort_cleared_matching_pointer": True,
        "empty_start_disposition": "accepted",
        "relaunch_stayed_clear": True,
    }


def test_explicit_abandon_removes_only_fixed_transfer_pointers(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "abandon-pointers", tmp_path)

    assert result == {
        "corrupt_pointer_cleared": True,
        "directory_pointer_failed_safely": True,
        "evidence_preserved": True,
        "regular_pointer_cleared": True,
        "symlink_pointer_only_cleared": True,
    }


def test_recovery_rejects_tampered_session_metadata_or_reference(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(
        live_capture_sender_bridge_probe,
        "tampered-recovery",
        tmp_path,
    )

    assert result == {
        "tampered_coordinate_system_rejected": True,
        "tampered_metadata_reference_rejected": True,
    }


def test_tiny_queue_refills_from_journal_until_finalization(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(live_capture_sender_bridge_probe, "tiny-capacity", tmp_path)

    assert result == {
        "all_journal_evidence_retained": True,
        "finalized_after_refill": True,
        "tiny_capacity_drained_in_order": True,
        "tiny_capacity_start_disposition": "accepted",
    }


def test_current_plus_mismatched_pending_recovery_fails_closed(
    live_capture_sender_bridge_probe: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    result = _run(
        live_capture_sender_bridge_probe,
        "current-pending-conflict",
        tmp_path,
    )

    assert result == {
        "both_pointer_files_retained": True,
        "mismatched_pending_was_structurally_valid": True,
        "recovery_failed_closed": True,
    }
