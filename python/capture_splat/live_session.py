from __future__ import annotations

import hashlib
import math
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import numpy as np
from PIL import Image

from .capture_schema import IMAGE_KEYS, load_capture
from .json_utils import ensure_finite

LIVE_SESSION_SCHEMA = "capture_splat.live_session.v0.1"
LIVE_FRAME_SCHEMA = "capture_splat.live_frame.v0.1"
LIVE_ACK_SCHEMA = "capture_splat.live_ack.v0.1"
LIVE_FINALIZE_SCHEMA = "capture_splat.live_finalize.v0.1"
LIVE_REPLAY_SUMMARY_SCHEMA = "capture_splat.live_replay_summary.v0.1"

SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
MEDIA_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
RFC3339_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
MASK_KEYS = {"person_mask": "person", "valid_mask": "valid", "object_mask": "object"}
QUALITY_NUMBER_KEYS = (
    "score",
    "blur_score",
    "exposure_mean",
    "exposure_delta",
    "clipped_highlight_fraction",
    "near_clipped_highlight_fraction",
    "clipped_shadow_fraction",
    "feature_grid_coverage",
    "parallax_meters",
    "angular_velocity_deg_s",
    "translation_speed_m_s",
    "colmap_overlap_score",
    "valid_depth_ratio",
)


@dataclass(frozen=True)
class ReplayAsset:
    role: str
    path: Path
    reference: dict[str, Any]


@dataclass(frozen=True)
class ReplayFrame:
    metadata: dict[str, Any]
    assets: tuple[ReplayAsset, ...]

    @property
    def sequence_id(self) -> int:
        return int(self.metadata["sequence_id"])


@dataclass(frozen=True)
class LiveReplayPlan:
    session: dict[str, Any]
    frames: tuple[ReplayFrame, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def validate_safe_relative_path(value: Any, field: str = "path") -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty POSIX-relative path")
    if "\x00" in value or "\\" in value or value.startswith("/") or URI_SCHEME_PATTERN.match(value):
        raise ValueError(f"{field} must be a safe POSIX-relative path")
    path = PurePosixPath(value)
    if path.as_posix() != value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must be a safe POSIX-relative path")
    return value


def resolve_capture_asset(capture_root: Path, value: Any, field: str) -> tuple[str, Path]:
    relative = validate_safe_relative_path(value, field)
    candidate = capture_root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{field} does not exist: {relative}") from exc
    try:
        resolved.relative_to(capture_root)
    except ValueError as exc:
        raise ValueError(f"{field} escapes the capture root: {relative}") from exc
    if not resolved.is_file():
        raise ValueError(f"{field} is not a file: {relative}")
    return relative, resolved


def _exact_keys(value: Any, required: set[str], optional: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing:
        raise ValueError(f"{field} missing keys: {sorted(missing)}")
    if extra:
        raise ValueError(f"{field} has unexpected keys: {sorted(extra)}")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _number(value: Any, field: str, *, minimum: float | None = None, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    number = float(value)
    if positive and number <= 0:
        raise ValueError(f"{field} must be positive")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return number


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer at least {minimum}")
    return value


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise ValueError("session_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be sha256:<64 lowercase hex>")
    return value


def _validate_asset_reference(value: Any, field: str, *, dimensions_required: bool) -> None:
    required = {"path", "sha256", "size_bytes", "media_type"}
    if dimensions_required:
        required |= {"width", "height"}
    reference = _exact_keys(value, required, {"width", "height"} - required, field)
    validate_safe_relative_path(reference["path"], f"{field}.path")
    _sha(reference["sha256"], f"{field}.sha256")
    _integer(reference["size_bytes"], f"{field}.size_bytes", minimum=1)
    media_type = _string(reference["media_type"], f"{field}.media_type")
    if not MEDIA_TYPE_PATTERN.fullmatch(media_type):
        raise ValueError(f"{field}.media_type must be a lowercase MIME type")
    for name in ("width", "height"):
        if name in reference:
            _integer(reference[name], f"{field}.{name}", minimum=1)


def validate_live_session(value: Any) -> dict[str, Any]:
    ensure_finite(value)
    session = _exact_keys(
        value,
        {"schema", "session_id", "created_at", "source_manifest", "coordinate_system", "authority"},
        {"expected_frame_count"},
        "session",
    )
    if session["schema"] != LIVE_SESSION_SCHEMA:
        raise ValueError(f"session.schema must be {LIVE_SESSION_SCHEMA}")
    _session_id(session["session_id"])
    created_at = _string(session["created_at"], "session.created_at")
    if not RFC3339_DATETIME_PATTERN.fullmatch(created_at):
        raise ValueError("session.created_at must be an RFC 3339 date-time")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("session.created_at must be an RFC 3339 date-time") from exc
    if parsed_created_at.tzinfo is None:
        raise ValueError("session.created_at must include a timezone")
    source = _exact_keys(session["source_manifest"], {"path", "sha256", "size_bytes", "schema"}, set(), "session.source_manifest")
    validate_safe_relative_path(source["path"], "session.source_manifest.path")
    _sha(source["sha256"], "session.source_manifest.sha256")
    _integer(source["size_bytes"], "session.source_manifest.size_bytes", minimum=1)
    _string(source["schema"], "session.source_manifest.schema")
    coordinate = _exact_keys(
        session["coordinate_system"],
        {"id", "units", "handedness", "world_up", "camera_forward", "matrix_layout", "vector_convention"},
        set(),
        "session.coordinate_system",
    )
    _string(coordinate["id"], "session.coordinate_system.id")
    if coordinate["units"] not in {"meters", "unknown"}:
        raise ValueError("session.coordinate_system.units must be meters or unknown")
    expected_coordinate = {
        "handedness": "right",
        "world_up": "+Y",
        "camera_forward": "-Z",
        "matrix_layout": "row-major",
        "vector_convention": "column-vector",
    }
    for key, expected in expected_coordinate.items():
        if coordinate[key] != expected:
            raise ValueError(f"session.coordinate_system.{key} must be {expected}")
    if session["authority"] != "proposal_only":
        raise ValueError("session.authority must be proposal_only")
    if "expected_frame_count" in session:
        _integer(session["expected_frame_count"], "session.expected_frame_count", minimum=1)
    return session


def validate_live_frame(value: Any) -> dict[str, Any]:
    ensure_finite(value)
    frame = _exact_keys(
        value,
        {
            "schema", "session_id", "sequence_id", "timestamp", "source_frame", "intrinsics",
            "camera_to_world", "coordinate_frame", "tracking", "quality",
        },
        {"assets"},
        "frame",
    )
    if frame["schema"] != LIVE_FRAME_SCHEMA:
        raise ValueError(f"frame.schema must be {LIVE_FRAME_SCHEMA}")
    _session_id(frame["session_id"])
    _integer(frame["sequence_id"], "frame.sequence_id", minimum=1)
    timestamp = _exact_keys(frame["timestamp"], {"value", "clock_domain"}, set(), "frame.timestamp")
    _number(timestamp["value"], "frame.timestamp.value", minimum=0)
    if timestamp["clock_domain"] not in {"arkit_session", "media", "monotonic", "unknown"}:
        raise ValueError("frame.timestamp.clock_domain is invalid")
    _validate_asset_reference(frame["source_frame"], "frame.source_frame", dimensions_required=True)
    intrinsics = _exact_keys(
        frame["intrinsics"],
        {"model", "fl_x", "fl_y", "cx", "cy", "calibration_width", "calibration_height", "applies_to"},
        set(),
        "frame.intrinsics",
    )
    if intrinsics["model"] != "pinhole":
        raise ValueError("frame.intrinsics.model must be pinhole")
    _number(intrinsics["fl_x"], "frame.intrinsics.fl_x", positive=True)
    _number(intrinsics["fl_y"], "frame.intrinsics.fl_y", positive=True)
    _number(intrinsics["cx"], "frame.intrinsics.cx")
    _number(intrinsics["cy"], "frame.intrinsics.cy")
    _integer(intrinsics["calibration_width"], "frame.intrinsics.calibration_width", minimum=1)
    _integer(intrinsics["calibration_height"], "frame.intrinsics.calibration_height", minimum=1)
    if intrinsics["applies_to"] not in {"source_frame", "depth", "confidence", "unknown"}:
        raise ValueError("frame.intrinsics.applies_to is invalid")
    matrix = frame["camera_to_world"]
    if not isinstance(matrix, list) or len(matrix) != 16:
        raise ValueError("frame.camera_to_world must contain 16 row-major values")
    for index, number in enumerate(matrix):
        _number(number, f"frame.camera_to_world[{index}]")
    _string(frame["coordinate_frame"], "frame.coordinate_frame")
    tracking = _exact_keys(frame["tracking"], {"state"}, set(), "frame.tracking")
    _string(tracking["state"], "frame.tracking.state")
    quality = _exact_keys(
        frame["quality"], {"accepted"}, {"reason", "feature_point_count", *QUALITY_NUMBER_KEYS}, "frame.quality"
    )
    if not isinstance(quality["accepted"], bool):
        raise ValueError("frame.quality.accepted must be a boolean")
    if "reason" in quality:
        _string(quality["reason"], "frame.quality.reason")
    if "feature_point_count" in quality:
        _integer(quality["feature_point_count"], "frame.quality.feature_point_count", minimum=0)
    for key in QUALITY_NUMBER_KEYS:
        if key in quality:
            _number(quality[key], f"frame.quality.{key}")
    if "assets" in frame:
        assets = _exact_keys(frame["assets"], set(), {"depth", "confidence", "masks"}, "frame.assets")
        if not assets:
            raise ValueError("frame.assets must not be empty")
        for key in ("depth", "confidence"):
            if key in assets:
                _validate_asset_reference(assets[key], f"frame.assets.{key}", dimensions_required=False)
        if "masks" in assets:
            if not isinstance(assets["masks"], list) or not assets["masks"]:
                raise ValueError("frame.assets.masks must be a non-empty list")
            kinds: set[str] = set()
            for index, mask in enumerate(assets["masks"]):
                item = _exact_keys(mask, {"kind", "path", "sha256", "size_bytes", "media_type"}, {"width", "height"}, f"frame.assets.masks[{index}]")
                if item["kind"] not in {"person", "valid", "object"} or item["kind"] in kinds:
                    raise ValueError("frame asset mask kinds must be unique person, valid, or object")
                kinds.add(item["kind"])
                _validate_asset_reference({key: val for key, val in item.items() if key != "kind"}, f"frame.assets.masks[{index}]", dimensions_required=False)
    return frame


def validate_live_ack(value: Any) -> dict[str, Any]:
    ensure_finite(value)
    ack = _exact_keys(
        value,
        {
            "schema", "session_id", "operation", "status", "received_count", "contiguous_count",
            "pending_count", "expected_frame_count", "next_expected_sequence_id", "missing_ranges", "finalized",
        },
        {"sequence_id", "asset_role", "message"},
        "ack",
    )
    if ack["schema"] != LIVE_ACK_SCHEMA:
        raise ValueError(f"ack.schema must be {LIVE_ACK_SCHEMA}")
    _session_id(ack["session_id"])
    if ack["operation"] not in {"session", "frame", "asset", "resume", "finalize"}:
        raise ValueError("ack.operation is invalid")
    if ack["status"] not in {"accepted", "duplicate", "incomplete", "finalized"}:
        raise ValueError("ack.status is invalid")
    for key in ("received_count", "contiguous_count", "pending_count"):
        _integer(ack[key], f"ack.{key}", minimum=0)
    if ack["expected_frame_count"] is not None:
        _integer(ack["expected_frame_count"], "ack.expected_frame_count", minimum=1)
    _integer(ack["next_expected_sequence_id"], "ack.next_expected_sequence_id", minimum=1)
    if not isinstance(ack["missing_ranges"], list):
        raise ValueError("ack.missing_ranges must be a list")
    previous_end = 0
    for index, value_range in enumerate(ack["missing_ranges"]):
        item = _exact_keys(value_range, {"start", "end"}, set(), f"ack.missing_ranges[{index}]")
        start = _integer(item["start"], f"ack.missing_ranges[{index}].start", minimum=1)
        end = _integer(item["end"], f"ack.missing_ranges[{index}].end", minimum=start)
        if start <= previous_end:
            raise ValueError("ack.missing_ranges must be sorted and disjoint")
        previous_end = end
    if not isinstance(ack["finalized"], bool):
        raise ValueError("ack.finalized must be a boolean")
    if "sequence_id" in ack:
        _integer(ack["sequence_id"], "ack.sequence_id", minimum=1)
    if "asset_role" in ack:
        if ack["asset_role"] not in {"source", "depth", "confidence", "mask-person", "mask-valid", "mask-object"}:
            raise ValueError("ack.asset_role is invalid")
    if "message" in ack:
        _string(ack["message"], "ack.message")
    return ack


def _image_dimensions(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception as exc:
        raise ValueError(f"cannot inspect image dimensions: {path.name}") from exc


def _array_dimensions(path: Path) -> tuple[int, int] | None:
    if path.suffix.lower() != ".npy":
        return None
    try:
        array = np.load(path, mmap_mode="r", allow_pickle=False)
    except Exception as exc:
        raise ValueError(f"cannot inspect NPY dimensions: {path.name}") from exc
    if array.ndim < 2:
        raise ValueError(f"NPY asset must have at least two dimensions: {path.name}")
    return int(array.shape[1]), int(array.shape[0])


def _media_type(path: Path) -> str:
    if path.suffix.lower() == ".npy":
        return "application/x-npy"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _asset_reference(relative: str, path: Path, *, require_image_dimensions: bool = False) -> dict[str, Any]:
    reference: dict[str, Any] = {
        "path": relative,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "media_type": _media_type(path),
    }
    dimensions: tuple[int, int] | None = None
    if require_image_dimensions:
        dimensions = _image_dimensions(path)
    elif path.suffix.lower() == ".npy":
        dimensions = _array_dimensions(path)
    elif reference["media_type"].startswith("image/"):
        try:
            dimensions = _image_dimensions(path)
        except ValueError:
            dimensions = None
    if dimensions is not None:
        reference["width"], reference["height"] = dimensions
    return reference


def _raw_frame_path(frame: dict[str, Any]) -> Any:
    for key in IMAGE_KEYS:
        if key in frame:
            return frame[key]
    raise ValueError("frame is missing an RGB/image path")


def _is_explicitly_rejected(frame: dict[str, Any]) -> bool:
    if frame.get("accepted") is False:
        return True
    quality = frame.get("capture_quality") or frame.get("quality")
    return isinstance(quality, dict) and quality.get("accepted") is False


def _frame_intrinsics(capture: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any]:
    raw = frame.get("intrinsics") or capture.get("intrinsics")
    if not isinstance(raw, dict):
        raise ValueError("frame/capture is missing intrinsics")
    required = ("fl_x", "fl_y", "cx", "cy", "w", "h")
    if any(key not in raw for key in required):
        raise ValueError("frame/capture intrinsics are incomplete")
    return {
        "model": "pinhole",
        "fl_x": float(raw["fl_x"]),
        "fl_y": float(raw["fl_y"]),
        "cx": float(raw["cx"]),
        "cy": float(raw["cy"]),
        "calibration_width": int(raw["w"]),
        "calibration_height": int(raw["h"]),
        "applies_to": "unknown",
    }


def _coordinate_system(capture: dict[str, Any]) -> dict[str, str]:
    config = capture.get("session_config")
    metric_arkit = (
        capture.get("schema") == "capture_splat.v0.3"
        and isinstance(config, dict)
        and config.get("scale_authority") == "arkit_vio_metric"
        and config.get("up_axis") == [0, 1, 0]
    )
    return {
        "id": "arkit_world" if metric_arkit else "capture_local",
        "units": "meters" if metric_arkit else "unknown",
        "handedness": "right",
        "world_up": "+Y",
        "camera_forward": "-Z",
        "matrix_layout": "row-major",
        "vector_convention": "column-vector",
    }


def _timestamp_clock_domain(capture: dict[str, Any], frame: dict[str, Any]) -> str:
    value = frame.get("timestamp_clock_domain") or capture.get("timestamp_clock_domain")
    if value in {"arkit_session", "media", "monotonic", "unknown"}:
        return str(value)
    return "arkit_session" if capture.get("schema") == "capture_splat.v0.3" else "unknown"


def _quality(frame: dict[str, Any]) -> dict[str, Any]:
    source = frame.get("capture_quality") or frame.get("quality")
    source = source if isinstance(source, dict) else {}
    quality: dict[str, Any] = {"accepted": True}
    reason = source.get("reason")
    if isinstance(reason, str) and reason:
        quality["reason"] = reason
    for key in QUALITY_NUMBER_KEYS:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            quality[key] = value
    feature_count = source.get("feature_point_count")
    if isinstance(feature_count, int) and not isinstance(feature_count, bool) and feature_count >= 0:
        quality["feature_point_count"] = feature_count
    return quality


def _flatten_matrix(frame: dict[str, Any]) -> list[float]:
    raw = frame.get("transform_matrix") or frame.get("camera_to_world")
    if not isinstance(raw, list) or len(raw) != 4 or any(not isinstance(row, list) or len(row) != 4 for row in raw):
        raise ValueError("frame transform_matrix must be a 4x4 list")
    return [float(value) for row in raw for value in row]


def _created_at(manifest: Path) -> str:
    return datetime.fromtimestamp(manifest.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_live_replay_plan(capture_dir: Path, session_id: str) -> LiveReplayPlan:
    _session_id(session_id)
    capture_root = capture_dir.resolve(strict=True)
    if not capture_root.is_dir():
        raise ValueError(f"capture is not a directory: {capture_dir}")
    manifest = (capture_root / "capture.json").resolve(strict=True)
    try:
        manifest.relative_to(capture_root)
    except ValueError as exc:
        raise ValueError("capture.json escapes the capture root") from exc
    capture = load_capture(capture_root)
    coordinate_system = _coordinate_system(capture)
    selected: list[dict[str, Any]] = []
    for source_index, raw in enumerate(capture["frames"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"frame {source_index} is not an object")
        if not _is_explicitly_rejected(raw):
            selected.append(raw)
    if not selected:
        raise ValueError("capture has no non-rejected frames")

    replay_frames: list[ReplayFrame] = []
    for sequence_id, raw in enumerate(selected, start=1):
        timestamp = raw.get("timestamp")
        if timestamp is None:
            raise ValueError(f"frame {sequence_id} is missing timestamp")
        rgb_relative, rgb_path = resolve_capture_asset(capture_root, _raw_frame_path(raw), f"frame {sequence_id} source_frame")
        source_reference = _asset_reference(rgb_relative, rgb_path, require_image_dimensions=True)
        intrinsics = _frame_intrinsics(capture, raw)
        asset_objects: dict[str, Any] = {}
        replay_assets: list[ReplayAsset] = [ReplayAsset("source", rgb_path, source_reference)]

        for key in ("depth", "confidence"):
            if raw.get(key) is None:
                continue
            relative, path = resolve_capture_asset(capture_root, raw[key], f"frame {sequence_id} {key}")
            reference = _asset_reference(relative, path)
            asset_objects[key] = reference
            replay_assets.append(ReplayAsset(key, path, reference))

        mask_refs: list[dict[str, Any]] = []
        for key, kind in MASK_KEYS.items():
            if raw.get(key) is None:
                continue
            relative, path = resolve_capture_asset(capture_root, raw[key], f"frame {sequence_id} {key}")
            reference = {"kind": kind, **_asset_reference(relative, path)}
            mask_refs.append(reference)
            replay_assets.append(ReplayAsset(f"mask-{kind}", path, {k: v for k, v in reference.items() if k != "kind"}))
        if mask_refs:
            asset_objects["masks"] = mask_refs

        calibration_size = (intrinsics["calibration_width"], intrinsics["calibration_height"])
        if "depth" in asset_objects and (asset_objects["depth"].get("width"), asset_objects["depth"].get("height")) == calibration_size:
            intrinsics["applies_to"] = "depth"
        elif "confidence" in asset_objects and (asset_objects["confidence"].get("width"), asset_objects["confidence"].get("height")) == calibration_size:
            intrinsics["applies_to"] = "confidence"
        elif (source_reference["width"], source_reference["height"]) == calibration_size:
            intrinsics["applies_to"] = "source_frame"

        metadata: dict[str, Any] = {
            "schema": LIVE_FRAME_SCHEMA,
            "session_id": session_id,
            "sequence_id": sequence_id,
            "timestamp": {"value": float(timestamp), "clock_domain": _timestamp_clock_domain(capture, raw)},
            "source_frame": source_reference,
            "intrinsics": intrinsics,
            "camera_to_world": _flatten_matrix(raw),
            "coordinate_frame": coordinate_system["id"],
            "tracking": {"state": str(raw.get("tracking_state") or "unknown")},
            "quality": _quality(raw),
        }
        if asset_objects:
            metadata["assets"] = asset_objects
        validate_live_frame(metadata)
        replay_frames.append(ReplayFrame(metadata=metadata, assets=tuple(replay_assets)))

    session = {
        "schema": LIVE_SESSION_SCHEMA,
        "session_id": session_id,
        "created_at": _created_at(manifest),
        "source_manifest": {
            "path": "capture.json",
            "sha256": _sha256(manifest),
            "size_bytes": manifest.stat().st_size,
            "schema": str(capture["schema"]),
        },
        "expected_frame_count": len(replay_frames),
        "coordinate_system": coordinate_system,
        "authority": "proposal_only",
    }
    validate_live_session(session)
    return LiveReplayPlan(session=session, frames=tuple(replay_frames))


def expand_missing_ranges(ranges: Iterable[dict[str, int]]) -> set[int]:
    result: set[int] = set()
    for value_range in ranges:
        result.update(range(int(value_range["start"]), int(value_range["end"]) + 1))
    return result
