from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .capture_schema import frame_selection_summary, load_capture
from .json_utils import ensure_finite, load_json_strict, write_json_strict


SCHEMA = "capture_splat.live_physical_acceptance.v0.1"
OUTPUT_NAME = "capture_splat_live_physical_acceptance_summary.json"
DEFAULT_MIN_ENABLED_THROUGHPUT_RATIO = 0.90
SCENARIOS = (
    "receiver_restart",
    "wifi_interruption",
    "app_relaunch",
    "second_capture_cycle",
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} JSON missing: {path}")
    value = load_json_strict(path)
    ensure_finite(value)
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON must contain an object: {path}")
    return value


def _load_optional_object(path: Path, label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _load_object(path, label)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _source(path: Path, data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {"present": False, "path": str(path), "schema": None, "size_bytes": None, "sha256": None}
    return {
        "present": True,
        "path": str(path),
        "schema": data.get("schema") if isinstance(data.get("schema"), str) else None,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _receiver_evidence(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    if path.is_file():
        report = _load_object(path, "receiver report")
        return report, _source(path, report)
    if not path.is_dir():
        raise FileNotFoundError(f"receiver report or session directory missing: {path}")

    component_paths = {
        "state": path / "state.json",
        "finalized": path / "finalized.json",
        "source_manifest_binding": path / "source-manifest-binding.json",
        "handoff": path / "capture-splat.world-studio.json",
    }
    components = {
        name: _load_object(component, f"receiver {name}")
        for name, component in component_paths.items()
    }
    state = components["state"]
    marker = components["finalized"]
    binding = components["source_manifest_binding"]
    handoff = components["handoff"]

    session_ids = {
        value
        for value in (
            _first_string(state, (("session_id",),)),
            _first_string(marker, (("session_id",),)),
            _first_string(binding, (("session_id",),)),
            _first_string(handoff, (("session_id",),)),
        )
        if value is not None
    }
    final_sequences = {
        value
        for value in (
            _first_integer(state, (("final_sequence_id",),)),
            _first_integer(marker, (("final_sequence_id",),)),
            _first_integer(binding, (("final_sequence_id",),)),
            _first_integer(handoff, (("final_sequence_id",),)),
        )
        if value is not None
    }
    corruption_count = int(len(session_ids) != 1 or len(final_sequences) != 1)

    expected_handoff = _first_string(marker, (("handoff_sha256",),))
    if expected_handoff != _sha256(component_paths["handoff"]):
        corruption_count += 1
    expected_binding = _first_string(marker, (("source_manifest_binding_sha256",),))
    if expected_binding != _sha256(component_paths["source_manifest_binding"]):
        corruption_count += 1

    source_frames = handoff.get("source_frames")
    received_count = _first_integer(state, (("received_count",),))
    if not isinstance(source_frames, list):
        corruption_count += 1
    elif received_count is None or len(source_frames) != received_count:
        corruption_count += 1

    report = {
        "schema": "world_studio.live_receiver_evidence_bundle.v0.1",
        "session_id": next(iter(session_ids)) if len(session_ids) == 1 else None,
        "finalized": state.get("finalized"),
        "final_sequence_id": next(iter(final_sequences)) if len(final_sequences) == 1 else None,
        "received_frame_count": received_count,
        "missing_ranges": state.get("missing_ranges"),
        "manifest_sha256": _first_string(binding, (("source_manifest", "sha256"),)),
        "integrity": {"evidence_corruption_count": corruption_count},
    }
    source = {
        "present": True,
        "path": str(path),
        "schema": report["schema"],
        "size_bytes": sum(component.stat().st_size for component in component_paths.values()),
        "sha256": None,
        "components": {
            name: _source(component_paths[name], components[name])
            for name in component_paths
        },
    }
    return report, source


def _get(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _first(value: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> Any:
    for path in paths:
        candidate = _get(value, path)
        if candidate is not None:
            return candidate
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _first_number(value: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> float | None:
    return _number(_first(value, paths))


def _first_integer(value: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> int | None:
    return _integer(_first(value, paths))


def _first_bool(value: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> bool | None:
    candidate = _first(value, paths)
    return candidate if isinstance(candidate, bool) else None


def _first_string(value: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> str | None:
    candidate = _first(value, paths)
    return candidate if isinstance(candidate, str) and candidate else None


def _finalized(value: Mapping[str, Any]) -> bool | None:
    finalized = _first_bool(value, (("finalized",), ("session", "finalized"), ("finalization", "finalized")))
    if finalized is not None:
        return finalized
    status = _first_string(
        value,
        (("status",), ("session", "status"), ("finalization", "status"), ("finalization_state",)),
    )
    if status in {"finalized", "receiver_finalized"}:
        return True
    if status in {"capture_aborted", "publication_abandoned"}:
        return False
    return None


def _accepted(frame: Any) -> bool:
    if not isinstance(frame, dict):
        return False
    if frame.get("accepted") is False:
        return False
    quality = frame.get("capture_quality") or frame.get("quality")
    return not isinstance(quality, dict) or quality.get("accepted") is not False


def _safe_capture_asset(capture_dir: Path, relative: str) -> Path | None:
    if not relative or "\\" in relative:
        return None
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    candidate = capture_dir.joinpath(*pure.parts)
    resolved_root = capture_dir.resolve()
    resolved = candidate.resolve(strict=False)
    try:
        if os.path.commonpath((str(resolved_root), str(resolved))) != str(resolved_root):
            return None
    except ValueError:
        return None
    return candidate


def _required_asset_failures(capture_dir: Path, capture: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    for index, frame in enumerate(capture.get("frames", []), start=1):
        if not _accepted(frame):
            continue
        for role in ("rgb", "depth"):
            relative = frame.get(role) if isinstance(frame, dict) else None
            if not isinstance(relative, str):
                failures.append(f"frame_{index}_{role}_reference_missing")
                continue
            path = _safe_capture_asset(capture_dir, relative)
            if path is None:
                failures.append(f"frame_{index}_{role}_reference_unsafe")
            elif not path.is_file():
                failures.append(f"frame_{index}_{role}_file_missing")
        for role in ("confidence", "person_mask", "valid_mask", "object_mask"):
            relative = frame.get(role) if isinstance(frame, dict) else None
            if relative is None:
                continue
            if not isinstance(relative, str):
                failures.append(f"frame_{index}_{role}_reference_invalid")
                continue
            path = _safe_capture_asset(capture_dir, relative)
            if path is None:
                failures.append(f"frame_{index}_{role}_reference_unsafe")
            elif not path.is_file():
                failures.append(f"frame_{index}_{role}_file_missing")
    return failures


def _capture_duration(
    capture: Mapping[str, Any],
    finalization: Mapping[str, Any] | None,
    session: Mapping[str, Any] | None,
    spatial: Mapping[str, Any] | None,
) -> tuple[float | None, str | None]:
    candidates = (
        (spatial, (("thermal_summary", "capture_duration_seconds"),), "spatial_guidance.thermal_summary"),
        (session, (("capture_duration_seconds",), ("duration_seconds",)), "session_report"),
        (finalization, (("capture_duration_seconds",), ("duration_seconds",)), "finalization_report"),
        (capture, (("capture_duration_seconds",), ("duration_seconds",)), "capture_manifest"),
    )
    for source, paths, label in candidates:
        if source is None:
            continue
        duration = _first_number(source, paths)
        if duration is not None and duration > 0:
            return duration, label

    timestamps = [
        float(frame["timestamp"])
        for frame in capture.get("frames", [])
        if _accepted(frame)
        and isinstance(frame, dict)
        and _number(frame.get("timestamp")) is not None
    ]
    if len(timestamps) >= 2:
        duration = max(timestamps) - min(timestamps)
        if duration > 0:
            return duration, "accepted_frame_timestamps"
    return None, None


def _capture_memory(
    finalization: Mapping[str, Any] | None,
    session: Mapping[str, Any] | None,
    spatial: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    paths = (
        ("memory", "peak_bytes"),
        ("memory", "peak_resident_bytes"),
        ("performance", "peak_memory_bytes"),
        ("peak_memory_bytes",),
        ("peak_resident_memory_bytes",),
    )
    for source_name, source in (
        ("session_report", session),
        ("spatial_guidance_report", spatial),
        ("finalization_report", finalization),
    ):
        if source is None:
            continue
        amount = _first_number(source, paths)
        if amount is not None and amount >= 0:
            return {"peak_bytes": int(amount), "source": source_name}
    return None


def _capture_thermal(
    sensor: Mapping[str, Any] | None,
    spatial: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if spatial is not None:
        summary = _get(spatial, ("thermal_summary",))
        transitions = _get(spatial, ("thermal_transitions",))
        if isinstance(summary, Mapping) and isinstance(summary.get("thermal_state_seconds"), Mapping):
            return {
                "coverage": "duration_summary",
                "thermal_state_seconds": dict(summary["thermal_state_seconds"]),
                "transition_count": len(transitions) if isinstance(transitions, list) else None,
            }
    if sensor is not None and isinstance(sensor.get("thermal_state"), str):
        return {"coverage": "final_state_only", "final_state": sensor["thermal_state"]}
    return None


def _capture_evidence(capture_dir: Path, label: str) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    capture = load_capture(capture_dir)
    paths = {
        "finalization": capture_dir / "metadata" / "finalization_report.json",
        "session": capture_dir / "metadata" / "session_report.json",
        "sensor_health": capture_dir / "metadata" / "sensor_health.json",
        "spatial_guidance": capture_dir / "metadata" / "spatial_guidance_report.json",
    }
    reports = {
        name: _load_optional_object(path, f"{label} {name} report")
        for name, path in paths.items()
    }
    selection = frame_selection_summary(capture)
    accepted = int(selection["selected_frames"])
    duration, duration_source = _capture_duration(
        capture,
        reports["finalization"],
        reports["session"],
        reports["spatial_guidance"],
    )
    throughput = accepted / duration if duration is not None else None
    return {
        "path": str(capture_dir),
        "manifest_schema": capture.get("schema"),
        "accepted_frame_count": accepted,
        "duration_seconds": duration,
        "duration_source": duration_source,
        "accepted_frame_throughput_fps": throughput,
        "required_asset_failures": _required_asset_failures(capture_dir, capture),
        "memory": _capture_memory(
            reports["finalization"],
            reports["session"],
            reports["spatial_guidance"],
        ),
        "thermal": _capture_thermal(reports["sensor_health"], reports["spatial_guidance"]),
        "reports": {name: _source(paths[name], reports[name]) for name in paths},
        "_reports": reports,
    }


def _reported_bad_count(report: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> int | None:
    value = _first(report, paths)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, list):
        return len(value)
    return _integer(value)


def _integrity_failures(report: Mapping[str, Any], prefix: str) -> list[str]:
    failures: list[str] = []
    bad_fields = {
        "checksum_mismatch": (
            ("checksum_mismatch_count",),
            ("integrity", "checksum_mismatch_count"),
            ("integrity", "checksum_mismatches"),
        ),
        "evidence_corruption": (
            ("evidence_corruption_count",),
            ("corrupt_evidence_count",),
            ("integrity", "evidence_corruption_count"),
            ("integrity", "corrupt_evidence_count"),
        ),
        "missing_required_frames": (
            ("missing_required_frame_count",),
            ("integrity", "missing_required_frame_count"),
        ),
    }
    for name, paths in bad_fields.items():
        count = _reported_bad_count(report, paths)
        if count is not None and count > 0:
            failures.append(f"{prefix}_{name}_reported")
    for name, paths in {
        "checksum_invalid": (("checksum_valid",), ("integrity", "checksum_valid")),
        "evidence_invalid": (("evidence_intact",), ("integrity", "evidence_intact")),
    }.items():
        valid = _first_bool(report, paths)
        if valid is False:
            failures.append(f"{prefix}_{name}")
    bad_statuses = {"corrupt", "failed", "invalid", "mismatch"}
    for name, paths in {
        "checksum_invalid": (("checksum_status",), ("integrity", "checksum_status")),
        "evidence_invalid": (("evidence_status",), ("integrity", "evidence_status")),
    }.items():
        status = _first_string(report, paths)
        if status is not None and status.lower() in bad_statuses:
            failures.append(f"{prefix}_{name}")
    return failures


def _missing_ranges(report: Mapping[str, Any]) -> list[Any] | None:
    value = _first(
        report,
        (("missing_ranges",), ("receiver_missing_ranges",), ("ack", "missing_ranges"), ("session", "missing_ranges")),
    )
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("receiver missing_ranges must be an array")
    return value


def _report_memory(report: Mapping[str, Any]) -> dict[str, Any] | None:
    peak = _first_number(
        report,
        (
            ("memory", "peak_bytes"),
            ("memory", "peak_resident_bytes"),
            ("performance", "peak_memory_bytes"),
            ("peak_memory_bytes",),
            ("peak_resident_memory_bytes",),
        ),
    )
    return None if peak is None or peak < 0 else {"peak_bytes": int(peak), "source": "sender_report"}


def _report_thermal(report: Mapping[str, Any]) -> dict[str, Any] | None:
    value = _first(report, (("thermal",), ("performance", "thermal"), ("thermal_summary",)))
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return {"state": value}
    return None


def _ack_latency(report: Mapping[str, Any]) -> dict[str, Any] | None:
    available = _first_bool(
        report,
        (("request_acknowledgement_latency_available",), ("ack", "available")),
    )
    if available is False:
        return None
    fields = {
        "mean_ms": (
            ("ack", "mean_latency_ms"),
            ("ack_latency", "mean_ms"),
            ("ack_mean_latency_ms",),
            ("request_acknowledgement_latency_mean_ms",),
        ),
        "p95_ms": (
            ("ack", "p95_latency_ms"),
            ("ack_latency", "p95_ms"),
            ("ack_p95_latency_ms",),
            ("request_acknowledgement_latency_p95_ms",),
        ),
        "max_ms": (
            ("ack", "max_latency_ms"),
            ("ack_latency", "max_ms"),
            ("ack_max_latency_ms",),
            ("request_acknowledgement_latency_max_ms",),
        ),
    }
    values = {name: _first_number(report, paths) for name, paths in fields.items()}
    result: dict[str, Any] = {
        name: value for name, value in values.items() if value is not None and value >= 0
    }
    sample_count = _first_integer(
        report,
        (("request_acknowledgement_sample_count",), ("ack", "sample_count")),
    )
    retry_count = _first_integer(
        report,
        (("request_retry_count",), ("ack", "retry_count")),
    )
    if sample_count is not None:
        result["sample_count"] = sample_count
    if retry_count is not None:
        result["retry_count"] = retry_count
    if available is True and (sample_count is None or sample_count <= 0):
        return None
    return result or None


def _scenario_summary(name: str, path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "name": name,
            "evidence_provided": False,
            "executed": False,
            "passed": None,
            "status": "not_provided",
            "source": None,
        }
    path = path.resolve()
    report = _load_object(path, f"{name} scenario")
    executed = _first_bool(report, (("executed",), ("scenario", "executed")))
    passed = _first_bool(report, (("passed",), ("scenario", "passed")))
    status = _first_string(report, (("status",), ("scenario", "status")))
    return {
        "name": name,
        "evidence_provided": True,
        "executed": executed is True,
        "passed": passed if executed is True else None,
        "status": status or ("reported" if executed is True else "not_executed"),
        "source": _source(path, report),
    }


def _public_capture(evidence: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in evidence.items() if key != "_reports"}


def run_live_physical_acceptance(
    baseline_capture: Path,
    enabled_capture: Path,
    sender_report: Path,
    receiver_report: Path,
    out_dir: Path,
    *,
    scenario_evidence: Mapping[str, Path] | None = None,
    min_enabled_throughput_ratio: float = DEFAULT_MIN_ENABLED_THROUGHPUT_RATIO,
) -> dict[str, Any]:
    if not 0 < min_enabled_throughput_ratio <= 1:
        raise ValueError("min_enabled_throughput_ratio must be in (0, 1]")

    baseline = _capture_evidence(baseline_capture, "baseline")
    enabled = _capture_evidence(enabled_capture, "sender-enabled")
    sender_path = sender_report.resolve()
    receiver_path = receiver_report.resolve()
    sender = _load_object(sender_path, "sender report")
    receiver, receiver_source = _receiver_evidence(receiver_path)

    blockers: list[str] = []
    warnings: list[str] = []

    for label, evidence in (("baseline", baseline), ("enabled", enabled)):
        reports = evidence["_reports"]
        finalization = reports["finalization"]
        session = reports["session"]
        if finalization is None:
            blockers.append(f"{label}_local_finalization_report_missing")
        else:
            if finalization.get("status") != "finalized" or finalization.get("manifest_written") is not True:
                blockers.append(f"{label}_local_finalization_failed")
            reported_count = _integer(finalization.get("accepted_keyframe_count"))
            if reported_count is None or reported_count != evidence["accepted_frame_count"]:
                blockers.append(f"{label}_local_accepted_frame_count_mismatch")
        if evidence["accepted_frame_count"] <= 0:
            blockers.append(f"{label}_required_frames_missing")
        if evidence["required_asset_failures"]:
            blockers.append(f"{label}_required_frame_evidence_missing")

        writer_drop_values: list[int] = []
        if finalization is not None:
            drops = _integer(finalization.get("video_dropped_frame_count"))
            if drops is not None:
                writer_drop_values.append(drops)
        if session is not None:
            drops = _integer(session.get("dropped_frames"))
            if drops is not None:
                writer_drop_values.append(drops)
        if not writer_drop_values:
            warnings.append(f"{label}_writer_drop_evidence_missing")
        elif any(value > 0 for value in writer_drop_values):
            blockers.append(f"{label}_writer_drops_reported")

        for report_name, report in reports.items():
            if report is not None:
                blockers.extend(_integrity_failures(report, f"{label}_{report_name}"))

    baseline_throughput = baseline["accepted_frame_throughput_fps"]
    enabled_throughput = enabled["accepted_frame_throughput_fps"]
    throughput_ratio = None
    if baseline_throughput is None or enabled_throughput is None:
        warnings.append("accepted_frame_throughput_evidence_missing")
    elif baseline_throughput <= 0:
        blockers.append("baseline_accepted_frame_throughput_invalid")
    else:
        throughput_ratio = enabled_throughput / baseline_throughput
        if throughput_ratio < min_enabled_throughput_ratio:
            blockers.append("sender_enabled_throughput_below_threshold")

    sender_finalized = _finalized(sender)
    receiver_finalized = _finalized(receiver)
    if sender_finalized is not True:
        blockers.append("sender_finalization_not_proven")
    if receiver_finalized is not True:
        blockers.append("receiver_finalization_not_proven")

    sender_sequence = _first_integer(
        sender,
        (("final_sequence_id",), ("session", "final_sequence_id"), ("finalization", "final_sequence_id")),
    )
    receiver_sequence = _first_integer(
        receiver,
        (("final_sequence_id",), ("session", "final_sequence_id"), ("finalization", "final_sequence_id")),
    )
    receiver_count = _first_integer(
        receiver,
        (("received_frame_count",), ("received_count",), ("durable_received_count",), ("session", "received_count")),
    )
    expected_frames = enabled["accepted_frame_count"]
    if sender_sequence is None or receiver_sequence is None:
        warnings.append("sender_receiver_final_sequence_evidence_missing")
    elif sender_sequence != receiver_sequence:
        blockers.append("sender_receiver_finalization_mismatch")
    if sender_sequence is not None and sender_sequence != expected_frames:
        blockers.append("sender_required_frame_count_mismatch")
    if receiver_sequence is not None and receiver_sequence != expected_frames:
        blockers.append("receiver_required_frame_count_mismatch")
    if receiver_count is None:
        warnings.append("receiver_frame_count_evidence_missing")
    elif receiver_count != expected_frames:
        blockers.append("receiver_required_frames_missing")

    sender_session_id = _first_string(sender, (("session_id",), ("session", "session_id")))
    receiver_session_id = _first_string(receiver, (("session_id",), ("session", "session_id")))
    if sender_session_id is not None and receiver_session_id is not None and sender_session_id != receiver_session_id:
        blockers.append("sender_receiver_session_mismatch")

    sender_manifest = _first_string(
        sender,
        (("manifest_sha256",), ("manifest", "sha256"), ("finalization", "manifest_sha256")),
    )
    receiver_manifest = _first_string(
        receiver,
        (("manifest_sha256",), ("manifest", "sha256"), ("finalization", "manifest_sha256")),
    )
    if sender_manifest is not None and SHA256_PATTERN.fullmatch(sender_manifest) is None:
        blockers.append("sender_manifest_checksum_invalid")
    if receiver_manifest is not None and SHA256_PATTERN.fullmatch(receiver_manifest) is None:
        blockers.append("receiver_manifest_checksum_invalid")
    if sender_manifest is None or receiver_manifest is None:
        warnings.append("sender_receiver_manifest_binding_evidence_missing")
    elif sender_manifest != receiver_manifest:
        blockers.append("sender_receiver_finalization_mismatch")

    ranges = _missing_ranges(receiver)
    if ranges is None:
        warnings.append("receiver_missing_range_evidence_missing")
    elif ranges:
        blockers.append("receiver_missing_ranges_nonempty")

    queue_overflow = _reported_bad_count(
        sender,
        (
            ("queue_overflow_count",),
            ("capacity_exceeded_count",),
            ("queue", "overflow_count"),
            ("queue", "overflow_events"),
            ("queue", "overflowed"),
            ("queue", "capacity_exceeded_count"),
            ("ingress_disposition_counts", "overflow"),
        ),
    )
    evidence_loss = _reported_bad_count(
        sender,
        (
            ("evidence_loss_count",),
            ("queue", "evidence_loss"),
            ("queue", "evidence_loss_count"),
            ("queue", "dropped_frame_count"),
            ("queue_evidence_loss_count",),
        ),
    )
    pending_frames = _first_integer(
        sender,
        (
            ("pending_frame_count",),
            ("queued_frame_count",),
            ("queue", "pending_frame_count"),
            ("queue", "queued_frame_count"),
            ("queue", "final_frame_count"),
            ("queue_current_frames",),
        ),
    )
    if queue_overflow is None:
        warnings.append("sender_queue_overflow_evidence_missing")
    elif queue_overflow > 0:
        blockers.append("sender_queue_overflow_reported")
    if evidence_loss is None:
        warnings.append("sender_queue_evidence_loss_evidence_missing")
    elif evidence_loss > 0:
        blockers.append("sender_queue_evidence_loss_reported")
    if pending_frames is not None and pending_frames > 0:
        blockers.append("sender_queue_not_drained_at_finalization")

    sender_writer_drops = _reported_bad_count(
        sender,
        (("writer_drop_count",), ("writer_drops",), ("capture", "writer_drop_count")),
    )
    if sender_writer_drops is not None and sender_writer_drops > 0:
        blockers.append("sender_writer_drops_reported")

    blockers.extend(_integrity_failures(sender, "sender"))
    blockers.extend(_integrity_failures(receiver, "receiver"))

    baseline_memory = baseline["memory"]
    enabled_memory = enabled["memory"] or _report_memory(sender)
    if baseline_memory is None:
        warnings.append("baseline_memory_evidence_missing")
    if enabled_memory is None:
        warnings.append("enabled_memory_evidence_missing")

    baseline_thermal = baseline["thermal"]
    enabled_thermal = enabled["thermal"] or _report_thermal(sender)
    if baseline_thermal is None or baseline_thermal.get("coverage") == "final_state_only":
        warnings.append("baseline_thermal_evidence_incomplete")
    if enabled_thermal is None or enabled_thermal.get("coverage") == "final_state_only":
        warnings.append("enabled_thermal_evidence_incomplete")

    ack_latency = _ack_latency(sender)
    if ack_latency is None:
        warnings.append("sender_ack_latency_evidence_missing")

    supplied_scenarios = scenario_evidence or {}
    scenario_names = sorted(set(SCENARIOS) | set(supplied_scenarios))
    scenarios = {
        name: _scenario_summary(name, supplied_scenarios.get(name))
        for name in scenario_names
    }
    for name, scenario in scenarios.items():
        if name in SCENARIOS and not scenario["evidence_provided"]:
            warnings.append(f"scenario_{name}_evidence_missing")
        elif scenario["evidence_provided"] and not scenario["executed"]:
            warnings.append(f"scenario_{name}_not_executed")
        elif scenario["executed"] and scenario["passed"] is False:
            blockers.append(f"scenario_{name}_failed")
        elif scenario["executed"] and scenario["passed"] is None:
            warnings.append(f"scenario_{name}_result_missing")

    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    decision = "reject" if blockers else ("hold" if warnings else "promote")
    summary = {
        "schema": SCHEMA,
        "decision": decision,
        "interpretation": "bounded_live_sender_physical_acceptance_evidence",
        "thresholds": {
            "min_sender_enabled_accepted_frame_throughput_ratio": min_enabled_throughput_ratio,
            "max_writer_drops": 0,
            "max_receiver_missing_ranges": 0,
            "max_sender_queue_overflow_events": 0,
            "max_sender_evidence_loss_events": 0,
            "max_reported_checksum_corruption_events": 0,
            "memory_threshold_bytes": None,
            "thermal_threshold": None,
            "ack_latency_threshold_ms": None,
        },
        "captures": {
            "sender_disabled_baseline": _public_capture(baseline),
            "sender_enabled": _public_capture(enabled),
        },
        "throughput_comparison": {
            "baseline_accepted_frames_per_second": baseline_throughput,
            "enabled_accepted_frames_per_second": enabled_throughput,
            "enabled_to_baseline_ratio": throughput_ratio,
            "threshold_ratio": min_enabled_throughput_ratio,
        },
        "sender": {
            "source": _source(sender_path, sender),
            "session_id": sender_session_id,
            "finalized": sender_finalized,
            "final_sequence_id": sender_sequence,
            "manifest_sha256": sender_manifest,
            "queue_overflow_count": queue_overflow,
            "evidence_loss_count": evidence_loss,
            "pending_frame_count": pending_frames,
            "memory": enabled_memory,
            "thermal": _report_thermal(sender),
            "ack_latency": ack_latency,
        },
        "receiver": {
            "source": receiver_source,
            "session_id": receiver_session_id,
            "finalized": receiver_finalized,
            "final_sequence_id": receiver_sequence,
            "received_frame_count": receiver_count,
            "manifest_sha256": receiver_manifest,
            "missing_ranges": ranges,
        },
        "scenario_evidence": scenarios,
        "warnings": warnings,
        "blockers": blockers,
        "authority": {
            "capture_quality_authority": False,
            "reconstruction_quality_authority": False,
            "measurement_authority": False,
            "collision_authority": False,
            "semantic_authority": False,
            "navigation_authority": False,
            "physics_authority": False,
        },
    }
    write_json_strict(out_dir.resolve() / OUTPUT_NAME, summary)
    return summary
