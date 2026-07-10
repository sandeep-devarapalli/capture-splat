from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .json_utils import ensure_finite, load_json_strict

CAPTURE_SCHEMA = "capture_splat.v0.1"
SUPPORTED_SCHEMAS = {CAPTURE_SCHEMA, "capture_splat.v0.2", "capture_splat.v0.3"}
IMAGE_KEYS = ("rgb", "image", "image_path", "file_path")


@dataclass(frozen=True)
class CaptureFrame:
    index: int
    source_index: int
    image_path: str
    transform_matrix: list[list[float]]
    intrinsics: dict[str, float]
    timestamp: float | None
    accepted: bool | None


def load_capture(capture_dir: Path) -> dict[str, Any]:
    manifest = capture_dir / "capture.json"
    if not manifest.exists():
        raise FileNotFoundError(f"capture.json missing: {manifest}")
    data = load_json_strict(manifest)
    ensure_finite(data)
    schema = data.get("schema")
    if schema not in SUPPORTED_SCHEMAS:
        raise ValueError(f"unsupported capture schema {schema!r}; expected one of {sorted(SUPPORTED_SCHEMAS)}")
    frames = data.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("capture.json must contain a non-empty frames list")
    list(iter_frames(data))
    return data


def _image_path(frame: dict[str, Any]) -> str:
    for key in IMAGE_KEYS:
        value = frame.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("frame is missing an RGB/image path")


def _intrinsics(capture: dict[str, Any], frame: dict[str, Any]) -> dict[str, float]:
    intr = frame.get("intrinsics") or capture.get("intrinsics")
    if not isinstance(intr, dict):
        raise ValueError("frame/capture is missing intrinsics")
    required = ("fl_x", "fl_y", "cx", "cy", "w", "h")
    missing = [key for key in required if key not in intr]
    if missing:
        raise ValueError(f"intrinsics missing keys: {missing}")
    return {key: float(intr[key]) for key in required}


def _matrix(frame: dict[str, Any]) -> list[list[float]]:
    matrix = frame.get("transform_matrix") or frame.get("camera_to_world")
    if not isinstance(matrix, list) or len(matrix) != 4:
        raise ValueError("frame transform_matrix must be a 4x4 list")
    rows: list[list[float]] = []
    for row in matrix:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("frame transform_matrix must be a 4x4 list")
        rows.append([float(value) for value in row])
    return rows


def _accepted(frame: dict[str, Any]) -> bool | None:
    value = frame.get("accepted")
    if isinstance(value, bool):
        return value
    quality = frame.get("capture_quality") or frame.get("quality")
    if isinstance(quality, dict):
        quality_value = quality.get("accepted")
        if isinstance(quality_value, bool):
            return quality_value
    return None


def iter_frames(capture: dict[str, Any], accepted_only: bool = False) -> Iterable[CaptureFrame]:
    emitted_index = 0
    for source_index, frame in enumerate(capture["frames"], start=1):
        if not isinstance(frame, dict):
            raise ValueError(f"frame {source_index} is not an object")
        accepted = _accepted(frame)
        if accepted_only and accepted is False:
            continue
        emitted_index += 1
        yield CaptureFrame(
            index=emitted_index,
            source_index=source_index,
            image_path=_image_path(frame),
            transform_matrix=_matrix(frame),
            intrinsics=_intrinsics(capture, frame),
            timestamp=float(frame["timestamp"]) if frame.get("timestamp") is not None else None,
            accepted=accepted,
        )


def frame_selection_summary(capture: dict[str, Any]) -> dict[str, Any]:
    total = len(capture["frames"])
    accepted_flags = []
    for frame in capture["frames"]:
        accepted_flags.append(_accepted(frame) if isinstance(frame, dict) else None)
    rejected = sum(1 for accepted in accepted_flags if accepted is False)
    accepted = total - rejected
    return {
        "selection": "accepted_keyframes",
        "total_manifest_frames": total,
        "selected_frames": accepted,
        "excluded_rejected_frames": rejected,
        "quality_metadata_present": any(value is not None for value in accepted_flags),
    }
