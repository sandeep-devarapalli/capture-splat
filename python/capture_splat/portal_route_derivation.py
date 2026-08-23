from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .hybrid_surface import _false_authority
from .json_utils import reject_constant, write_json_strict
from .training_supervision import confined_capture_path

REPORT_SCHEMA = "capture_splat.portal_route_derivation.v0.1"
REPORT_NAME = "capture_splat_portal_route_derivation_report.json"
DEFAULT_THROUGH_BAND_METERS = 0.15

_REGIONS = ("side_a", "through_opening", "side_b")
_MAX_PORTALS = 256
_MAX_TRAJECTORY_SAMPLES = 1_000_000
_MAX_DISTANCE_EVALUATIONS = 2_000_000
_MAX_REPORTED_CROSSINGS = 10_000
_MATRIX_TOLERANCE = 1e-4
_MAX_CROSSING_DELTA_SECONDS = 0.5
_MAX_CROSSING_DISTANCE_METERS = 0.5
_MAX_CROSSING_SPEED_METERS_PER_SECOND = 3.0
_READ_CHUNK_BYTES = 1024 * 1024


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _matrix(value: Any, label: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} must be a finite rigid 4x4 matrix")
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError(f"{label} must be a finite rigid 4x4 matrix")
        rows.append([_number(item, label) for item in row])
    if any(abs(rows[3][index]) > _MATRIX_TOLERANCE for index in range(3)) or not math.isclose(
        rows[3][3], 1.0, abs_tol=_MATRIX_TOLERANCE
    ):
        raise ValueError(f"{label} homogeneous row is invalid")
    columns = [[rows[row][column] for row in range(3)] for column in range(3)]
    for left in range(3):
        for right in range(3):
            expected = 1.0 if left == right else 0.0
            if not math.isclose(_dot(columns[left], columns[right]), expected, abs_tol=1e-3):
                raise ValueError(f"{label} rotation is not orthonormal")
    determinant = _dot(columns[0], _cross(columns[1], columns[2]))
    if not math.isclose(determinant, 1.0, abs_tol=1e-3):
        raise ValueError(f"{label} rotation determinant is not one")
    return rows


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: list[float], right: list[float]) -> list[float]:
    return [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]


def _center(matrix: list[list[float]]) -> list[float]:
    return [matrix[index][3] for index in range(3)]


def _regular_file(path: Path, label: str) -> Path:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    return absolute.resolve()


def _regular_directory(path: Path, label: str) -> Path:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink directory")
    return absolute.resolve()


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _snapshot(
    path: Path, label: str, relative: str, *, collect: bool
) -> tuple[bytes | None, dict[str, Any]]:
    path = _regular_file(path, label)
    before_path = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    chunks: list[bytes] | None = [] if collect else None
    bytes_read = 0
    try:
        before_open = os.fstat(descriptor)
        if not stat.S_ISREG(before_open.st_mode):
            raise ValueError(f"{label} must be a regular non-symlink file")
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            digest.update(chunk)
            bytes_read += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    if (
        _identity(before_path) != _identity(before_open)
        or _identity(before_open) != _identity(after_open)
        or _identity(after_open) != _identity(after_path)
        or bytes_read != before_open.st_size
    ):
        raise ValueError(f"{label} changed while it was read")
    return (b"".join(chunks) if chunks is not None else None), {
        "path": relative,
        "size_bytes": bytes_read,
        "checksum": f"sha256:{digest.hexdigest()}",
    }


def _json_snapshot(path: Path, label: str, relative: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw, reference = _snapshot(path, label, relative, collect=True)
    assert raw is not None
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from error
    return _object(value, label), reference


def _asset(root: Path, relative: Any, label: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = confined_capture_path(root, relative)
    path = _regular_file(path, label)
    _, reference = _snapshot(path, label, relative, collect=False)
    return path, reference


def _json_asset(
    root: Path, relative: Any, label: str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = _regular_file(confined_capture_path(root, relative), label)
    value, reference = _json_snapshot(path, label, relative)
    return path, value, reference


def _existing_asset(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} must be a non-empty relative path")
    return _regular_file(confined_capture_path(root, relative), label)


def _canonical_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a canonical relative POSIX path")
    declared = PurePosixPath(value)
    windows = PureWindowsPath(value)
    canonical = declared.as_posix()
    if (
        declared.is_absolute()
        or windows.drive
        or ".." in declared.parts
        or canonical == "."
        or canonical != value
    ):
        raise ValueError(f"{label} must be a canonical relative POSIX path")
    return canonical


def _same_matrix(left: Any, right: Any, label: str) -> None:
    left_matrix = _matrix(left, f"{label} prepared pose")
    right_matrix = _matrix(right, f"{label} source pose")
    if any(
        not math.isclose(left_matrix[row][column], right_matrix[row][column], abs_tol=1e-5)
        for row in range(4)
        for column in range(4)
    ):
        raise ValueError(f"{label} pose does not match its source binding")


def _intrinsics(value: Any, label: str) -> tuple[float, ...]:
    intrinsics = _object(value, label)
    keys = ("fl_x", "fl_y", "cx", "cy", "w", "h")
    result = tuple(_number(intrinsics.get(key), f"{label}.{key}") for key in keys)
    if result[0] <= 0.0 or result[1] <= 0.0 or result[4] <= 0.0 or result[5] <= 0.0:
        raise ValueError(f"{label} focal lengths and dimensions must be positive")
    return result


def _portals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema") != "capture_splat.room_semantics.v0.1":
        raise ValueError("RoomPlan semantics schema is unsupported")
    authority = _object(payload.get("authority"), "RoomPlan semantics authority")
    if authority.get("room_semantic_proposal") is not True or any(
        authority.get(key) is not False
        for key in ("metric_authority", "collision_geometry", "planning_authority", "semantic_authority")
    ):
        raise ValueError("RoomPlan semantics must remain a non-authoritative proposal")
    doors = payload.get("doors", [])
    openings = payload.get("openings", [])
    if not isinstance(doors, list) or not isinstance(openings, list) or not doors + openings:
        raise ValueError("RoomPlan semantics contains no door or opening proposals")
    proposals = [("door", value) for value in doors] + [("opening", value) for value in openings]
    if len(proposals) > _MAX_PORTALS:
        raise ValueError("RoomPlan portal proposal count exceeds the bounded work limit")
    portals: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, (kind, raw) in enumerate(proposals):
        door = _object(raw, f"RoomPlan {kind} {index}")
        portal_id = door.get("id")
        if not isinstance(portal_id, str) or not portal_id or portal_id in ids:
            raise ValueError("RoomPlan door ids must be unique non-empty strings")
        ids.add(portal_id)
        transform = _matrix(door.get("transform_matrix"), f"RoomPlan {kind} {portal_id}")
        dimensions = _object(door.get("dimensions_meters"), f"RoomPlan {kind} {portal_id} dimensions")
        width = _number(dimensions.get("x"), f"RoomPlan door {portal_id} width")
        height = _number(dimensions.get("y"), f"RoomPlan door {portal_id} height")
        if width <= 0.0 or height <= 0.0:
            raise ValueError("RoomPlan door dimensions must be positive")
        width_axis = [transform[row][0] for row in range(3)]
        vertical_axis = [transform[row][1] for row in range(3)]
        normal = [transform[row][2] for row in range(3)]
        if abs(_dot(vertical_axis, [0.0, 1.0, 0.0])) < 0.99 or abs(normal[1]) > 0.01:
            raise ValueError("RoomPlan door proposal is not vertical in ARKit world")
        portals.append(
            {
                "id": portal_id,
                "kind": kind,
                "center": _center(transform),
                "width_axis": width_axis,
                "vertical_axis": vertical_axis,
                "normal": normal,
                "width_meters": width,
                "height_meters": height,
                "crossings": [],
                "rejected_crossings": [],
            }
        )
    return portals


def _signed_distance(point: list[float], portal: dict[str, Any]) -> float:
    return _dot([point[index] - portal["center"][index] for index in range(3)], portal["normal"])


def _inside_portal(point: list[float], portal: dict[str, Any]) -> bool:
    relative = [point[index] - portal["center"][index] for index in range(3)]
    return (
        abs(_dot(relative, portal["width_axis"])) <= portal["width_meters"] / 2.0
        and abs(_dot(relative, portal["vertical_axis"])) <= portal["height_meters"] / 2.0
    )


def _crossing(
    previous: dict[str, Any], current: dict[str, Any], portal: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    start = _signed_distance(previous["position"], portal)
    end = _signed_distance(current["position"], portal)
    if not ((start < 0.0 < end) or (end < 0.0 < start)):
        return None, None
    fraction = start / (start - end)
    position = [
        previous["position"][index]
        + fraction * (current["position"][index] - previous["position"][index])
        for index in range(3)
    ]
    if not _inside_portal(position, portal):
        return None, None
    delta_seconds = current["timestamp"] - previous["timestamp"]
    distance_meters = math.dist(previous["position"], current["position"])
    speed = distance_meters / delta_seconds
    event = {
        "from_video_frame": previous["video_frame"],
        "to_video_frame": current["video_frame"],
        "from_timestamp": previous["timestamp"],
        "to_timestamp": current["timestamp"],
        "delta_seconds": delta_seconds,
        "distance_meters": distance_meters,
        "speed_meters_per_second": speed,
        "from_tracking_state": previous["tracking_state"],
        "to_tracking_state": current["tracking_state"],
        "position_meters": position,
        "direction": "side_a_to_side_b" if start < end else "side_b_to_side_a",
    }
    reasons: list[str] = []
    if current["video_frame"] != previous["video_frame"] + 1:
        reasons.append("video_frame_gap")
    if previous["tracking_state"] != "normal" or current["tracking_state"] != "normal":
        reasons.append("tracking_not_normal")
    if delta_seconds > _MAX_CROSSING_DELTA_SECONDS:
        reasons.append("timestamp_gap_exceeds_limit")
    if distance_meters > _MAX_CROSSING_DISTANCE_METERS:
        reasons.append("translation_exceeds_limit")
    if speed > _MAX_CROSSING_SPEED_METERS_PER_SECOND:
        reasons.append("speed_exceeds_limit")
    if reasons:
        return None, {**event, "reasons": reasons}
    return event, None


def _prepared_frames(
    root: Path, prepared: dict[str, Any], source: dict[str, Any]
) -> tuple[
    list[dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[str, int],
    dict[str, Path],
]:
    if prepared.get("schema") != "capture_splat.v0.3" or prepared.get("source") != "capture_splat.prepare_capture":
        raise ValueError("prepared capture must use capture_splat.v0.3 prepare_capture schema")
    frames = prepared.get("frames")
    source_frames = source.get("frames")
    if not isinstance(frames, list) or not isinstance(source_frames, list):
        raise ValueError("prepared and source capture frames must be arrays")
    video_bindings: dict[int, dict[str, Any]] = {}
    source_indices: set[int] = set()
    prepared_images: dict[str, Path] = {}
    counts = {"continuous_video": 0, "accepted_rgbd": 0}
    for index, raw in enumerate(frames):
        frame = _object(raw, f"prepared frame {index}")
        if frame.get("accepted") is not True:
            raise ValueError("prepared portal analysis accepts only retained frames")
        kind = frame.get("source_kind")
        if kind not in counts:
            raise ValueError(f"prepared frame {index} has unsupported source_kind")
        counts[kind] += 1
        _matrix(frame.get("transform_matrix"), f"prepared frame {index} pose")
        _number(frame.get("timestamp"), f"prepared frame {index} timestamp")
        _intrinsics(frame.get("intrinsics"), f"prepared frame {index} intrinsics")
        rgb_relative = _canonical_relative_path(frame.get("rgb"), f"prepared frame {index} RGB")
        try:
            image_name = PurePosixPath(rgb_relative).relative_to("images").as_posix()
        except ValueError as error:
            raise ValueError("prepared RGB must be below the prepared images directory") from error
        rgb_path = _existing_asset(root, rgb_relative, f"prepared frame {index} RGB")
        if image_name in prepared_images:
            raise ValueError("prepared RGB paths are duplicated")
        prepared_images[image_name] = rgb_path
        if kind == "continuous_video":
            source_video_frame = frame.get("source_video_frame")
            if (
                isinstance(source_video_frame, bool)
                or not isinstance(source_video_frame, int)
                or source_video_frame < 0
            ):
                raise ValueError("continuous-video frame source index is invalid")
            if source_video_frame in video_bindings:
                raise ValueError("continuous-video frame source index is duplicated")
            video_bindings[source_video_frame] = frame
            continue
        source_index = frame.get("source_frame_index")
        if (
            isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or not 1 <= source_index <= len(source_frames)
        ):
            raise ValueError("accepted RGB-D source frame index is invalid")
        if source_index in source_indices:
            raise ValueError("accepted RGB-D source frame index is duplicated")
        source_indices.add(source_index)
        source_frame = _object(source_frames[source_index - 1], f"source frame {source_index}")
        source_quality = _object(
            source_frame.get("capture_quality"), f"source frame {source_index} quality"
        )
        if source_quality.get("accepted") is not True:
            raise ValueError("accepted RGB-D frame is not accepted by the source capture")
        if not math.isclose(
            _number(frame.get("timestamp"), "prepared RGB-D timestamp"),
            _number(source_frame.get("timestamp"), "source RGB-D timestamp"),
            abs_tol=1e-6,
        ):
            raise ValueError("accepted RGB-D timestamp does not match its source binding")
        _same_matrix(frame.get("transform_matrix"), source_frame.get("transform_matrix"), "accepted RGB-D")
        if _intrinsics(frame.get("intrinsics"), "prepared RGB-D intrinsics") != _intrinsics(
            source_frame.get("intrinsics"), "source RGB-D intrinsics"
        ):
            raise ValueError("accepted RGB-D intrinsics do not match its source binding")
        _existing_asset(root, frame.get("depth"), f"prepared frame {index} depth")
        _existing_asset(root, frame.get("confidence"), f"prepared frame {index} confidence")
    return frames, video_bindings, counts, prepared_images


def _trajectory(
    path: Path,
    relative: str,
    expected_sample_count: int,
    portals: list[dict[str, Any]],
    video_bindings: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = _regular_file(path, "full trajectory")
    before_path = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    previous: dict[str, Any] | None = None
    matched: set[int] = set()
    sample_count = 0
    bytes_read = 0
    normal_tracking_samples = 0
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    try:
        before_open = os.fstat(descriptor)
        if not stat.S_ISREG(before_open.st_mode):
            raise ValueError("full trajectory must be a regular non-symlink file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for line_number, raw_line in enumerate(handle, 1):
                digest.update(raw_line)
                bytes_read += len(raw_line)
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError("full trajectory is not strict UTF-8 JSONL") from error
                if not line.strip():
                    continue
                sample_count += 1
                if (
                    sample_count > _MAX_TRAJECTORY_SAMPLES
                    or sample_count * len(portals) > _MAX_DISTANCE_EVALUATIONS
                ):
                    raise ValueError("trajectory analysis exceeds the bounded work limit")
                try:
                    value = json.loads(line, parse_constant=reject_constant)
                except json.JSONDecodeError as error:
                    raise ValueError(f"trajectory line {line_number} is invalid JSON") from error
                sample = _object(value, f"trajectory line {line_number}")
                video_frame = sample.get("video_frame_idx")
                expected_video_frame = sample_count - 1
                if (
                    isinstance(video_frame, bool)
                    or not isinstance(video_frame, int)
                    or video_frame != expected_video_frame
                ):
                    raise ValueError("full trajectory video_frame_idx must be exactly 0..N-1")
                timestamp = _number(sample.get("ar_timestamp"), "trajectory ar_timestamp")
                pose = _matrix(sample.get("camera_to_world"), "trajectory camera_to_world")
                tracking_state = sample.get("tracking_state")
                if not isinstance(tracking_state, str) or not tracking_state:
                    raise ValueError("trajectory tracking_state must be a non-empty string")
                normal_tracking_samples += int(tracking_state == "normal")
                current = {
                    "video_frame": video_frame,
                    "timestamp": timestamp,
                    "position": _center(pose),
                    "tracking_state": tracking_state,
                }
                if previous is not None and timestamp <= previous["timestamp"]:
                    raise ValueError("full trajectory timestamps are not strictly ordered")
                binding = video_bindings.get(video_frame)
                if binding is not None:
                    if not math.isclose(
                        timestamp,
                        _number(binding.get("timestamp"), "prepared video timestamp"),
                        abs_tol=1e-6,
                    ):
                        raise ValueError("prepared video timestamp does not match the full trajectory")
                    _same_matrix(binding.get("transform_matrix"), pose, "continuous-video frame")
                    if _intrinsics(
                        binding.get("intrinsics"), "prepared video intrinsics"
                    ) != _intrinsics(sample.get("intrinsics"), "trajectory video intrinsics"):
                        raise ValueError("prepared video intrinsics do not match the full trajectory")
                    matched.add(video_frame)
                if previous is not None:
                    for portal in portals:
                        event, rejected = _crossing(previous, current, portal)
                        if event is not None:
                            if len(portal["crossings"]) >= _MAX_REPORTED_CROSSINGS:
                                raise ValueError("portal crossing count exceeds the bounded report limit")
                            portal["crossings"].append(event)
                        if rejected is not None:
                            if len(portal["rejected_crossings"]) >= _MAX_REPORTED_CROSSINGS:
                                raise ValueError(
                                    "rejected portal crossing count exceeds the bounded report limit"
                                )
                            portal["rejected_crossings"].append(rejected)
                first = first or current
                last = current
                previous = current
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    if (
        _identity(before_path) != _identity(before_open)
        or _identity(before_open) != _identity(after_open)
        or _identity(after_open) != _identity(after_path)
        or bytes_read != before_open.st_size
    ):
        raise ValueError("full trajectory changed while it was read")
    if sample_count != expected_sample_count:
        raise ValueError("full trajectory sample count does not match source video_frame_count")
    if matched != set(video_bindings):
        raise ValueError("full trajectory does not bind every prepared continuous-video frame")
    assert first is not None and last is not None
    report = {
        "sample_count": sample_count,
        "source_video_frame_count": expected_sample_count,
        "first_video_frame": first["video_frame"],
        "last_video_frame": last["video_frame"],
        "first_timestamp": first["timestamp"],
        "last_timestamp": last["timestamp"],
        "prepared_video_bindings": len(matched),
        "normal_tracking_samples": normal_tracking_samples,
        "non_normal_tracking_samples": sample_count - normal_tracking_samples,
        "index_contract": "exact_contiguous_0_to_source_video_frame_count_minus_1",
        "crossing_bracket_limits": {
            "tracking_state": "normal_on_both_samples",
            "maximum_delta_seconds": _MAX_CROSSING_DELTA_SECONDS,
            "maximum_distance_meters": _MAX_CROSSING_DISTANCE_METERS,
            "maximum_speed_meters_per_second": _MAX_CROSSING_SPEED_METERS_PER_SECOND,
        },
    }
    reference = {
        "path": relative,
        "size_bytes": bytes_read,
        "checksum": f"sha256:{digest.hexdigest()}",
    }
    return report, reference


def _select_portal(portals: list[dict[str, Any]], requested: str | None) -> tuple[dict[str, Any] | None, str]:
    if requested is not None:
        matches = [portal for portal in portals if portal["id"] == requested]
        if not matches:
            raise ValueError(f"requested RoomPlan portal is missing: {requested}")
        return matches[0], "explicit"
    crossed = [portal for portal in portals if portal["crossings"]]
    if len(crossed) == 1:
        return crossed[0], "unique_observed_crossing"
    return None, "missing" if not crossed else "ambiguous"


def _region(point: list[float], portal: dict[str, Any], through_band: float) -> str:
    signed = _signed_distance(point, portal)
    if signed < -through_band:
        return "side_a"
    if signed > through_band:
        return "side_b"
    return "through_opening" if _inside_portal(point, portal) else "outside_portal_band"


def _registered_image_names(raw: bytes) -> tuple[list[str], int]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("COLMAP images.txt is not UTF-8") from error
    names: list[str] = []
    invalid_records = 0
    expect_pose = True
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if not expect_pose:
            expect_pose = True
            if stripped:
                points = stripped.split()
                try:
                    if len(points) % 3:
                        raise ValueError
                    for index in range(0, len(points), 3):
                        coordinates = (float(points[index]), float(points[index + 1]))
                        if not all(math.isfinite(value) for value in coordinates):
                            raise ValueError
                        int(points[index + 2])
                except ValueError:
                    invalid_records += 1
            continue
        if not stripped:
            continue
        parts = stripped.split(maxsplit=9)
        try:
            if len(parts) < 10:
                raise ValueError
            int(parts[0])
            pose = [float(value) for value in parts[1:8]]
            if not all(math.isfinite(value) for value in pose):
                raise ValueError
            if sum(value * value for value in pose[:4]) == 0.0:
                raise ValueError
            int(parts[8])
        except ValueError:
            invalid_records += 1
            continue
        names.append(parts[9])
        expect_pose = False
    if not expect_pose:
        invalid_records += 1
    return names, invalid_records


def _registration(
    sfm_root: Path | None, prepared_images: dict[str, Path]
) -> tuple[dict[str, Any], set[str]]:
    if sfm_root is None:
        return {
            "supplied": False,
            "reason": "colmap_registration_missing",
            "metric_roomplan_registration": False,
        }, set()
    images_txt = _regular_file(
        confined_capture_path(sfm_root, "sparse/0/images.txt"), "COLMAP images.txt"
    )
    image_root = _regular_directory(
        confined_capture_path(sfm_root, "images"), "SfM image root"
    )
    raw, images_ref = _snapshot(
        images_txt, "COLMAP images.txt", "sparse/0/images.txt", collect=True
    )
    assert raw is not None
    names, invalid = _registered_image_names(raw)
    if invalid or len(names) != len(set(names)):
        raise ValueError("COLMAP images.txt registration records are invalid or duplicated")
    canonical_names = [
        _canonical_relative_path(name, "COLMAP registered image name") for name in names
    ]
    if len(canonical_names) != len(set(canonical_names)):
        raise ValueError("COLMAP registered image paths are duplicated")
    registered_prepared: set[str] = set()
    parity_records: list[tuple[str, int, str]] = []
    for name in canonical_names:
        sfm_image = _regular_file(
            confined_capture_path(image_root, name), f"registered SfM image {name}"
        )
        prepared_image = prepared_images.get(name)
        if prepared_image is None:
            continue
        _, sfm_ref = _snapshot(sfm_image, f"registered SfM image {name}", name, collect=False)
        _, prepared_ref = _snapshot(
            prepared_image, f"prepared image matching {name}", name, collect=False
        )
        if any(sfm_ref[key] != prepared_ref[key] for key in ("size_bytes", "checksum")):
            raise ValueError(f"registered SfM image bytes do not match prepared RGB: {name}")
        registered_prepared.add(name)
        parity_records.append((name, sfm_ref["size_bytes"], sfm_ref["checksum"]))
    parity_digest = hashlib.sha256()
    for name, size, checksum in sorted(parity_records):
        parity_digest.update(name.encode("utf-8"))
        parity_digest.update(b"\0")
        parity_digest.update(str(size).encode("ascii"))
        parity_digest.update(b"\0")
        parity_digest.update(checksum.encode("ascii"))
        parity_digest.update(b"\n")
    return {
        "supplied": True,
        "sfm_package": {
            "path": sfm_root.name,
            "layout": "images_and_sparse_0_images_txt",
        },
        "images_txt": images_ref,
        "image_root": "images",
        "registered_image_count": len(names),
        "registered_prepared_image_count": len(registered_prepared),
        "registered_prepared_image_parity": {
            "count": len(parity_records),
            "digest": f"sha256:{parity_digest.hexdigest()}",
            "canonicalization": "utf8_relative_path_nul_size_nul_sha256_lf_v1",
        },
        "matching": "canonical_case_sensitive_relative_path_with_exact_size_and_sha256_parity",
        "metric_roomplan_registration": False,
    }, registered_prepared


def _derive(
    capture_path: Path,
    *,
    sfm_package: Path | None,
    portal_id: str | None,
    through_band_meters: float,
) -> dict[str, Any]:
    through_band = _number(through_band_meters, "through band")
    if through_band <= 0.0:
        raise ValueError("through band must be positive")
    root = capture_path.parent
    prepared, prepared_ref = _json_snapshot(capture_path, "prepared capture", capture_path.name)
    _, source, source_ref = _json_asset(
        root, prepared.get("source_capture_manifest_file"), "source capture manifest"
    )
    trajectory_relative = _canonical_relative_path(
        prepared.get("frame_index_file"), "prepared full trajectory"
    )
    trajectory_path = _regular_file(
        confined_capture_path(root, trajectory_relative), "full trajectory"
    )
    _, semantics, semantics_ref = _json_asset(
        root, prepared.get("room_plan_semantics_file"), "RoomPlan semantics"
    )
    _, roomplan_ref = _asset(root, prepared.get("room_plan_file"), "RoomPlan USDZ")
    _, roomplan_report, roomplan_report_ref = _json_asset(
        root, prepared.get("room_plan_report_file"), "RoomPlan report"
    )
    if source.get("schema") != "capture_splat.v0.3":
        raise ValueError("source capture schema is unsupported")
    if source.get("frame_index_file") != trajectory_relative:
        raise ValueError("source and prepared trajectory references do not match")
    expected_sample_count = source.get("video_frame_count")
    if (
        isinstance(expected_sample_count, bool)
        or not isinstance(expected_sample_count, int)
        or not 2 <= expected_sample_count <= _MAX_TRAJECTORY_SAMPLES
    ):
        raise ValueError("source video_frame_count is invalid or exceeds the bounded work limit")
    if (
        roomplan_report.get("schema") != "capture_splat.room_plan_report.v0.1"
        or roomplan_report.get("room_plan_file") != prepared.get("room_plan_file")
        or roomplan_report.get("room_semantics_file") != prepared.get("room_plan_semantics_file")
        or roomplan_report.get("doors") != len(semantics.get("doors", []))
        or roomplan_report.get("openings") != len(semantics.get("openings", []))
    ):
        raise ValueError("RoomPlan report does not bind the prepared RoomPlan proposal")
    portals = _portals(semantics)
    frames, video_bindings, source_counts, prepared_images = _prepared_frames(
        root, prepared, source
    )
    trajectory, trajectory_ref = _trajectory(
        trajectory_path,
        trajectory_relative,
        expected_sample_count,
        portals,
        video_bindings,
    )
    selected, selection = _select_portal(portals, portal_id)
    registration, registered = _registration(sfm_package, prepared_images)

    prepared_counts = {region: 0 for region in (*_REGIONS, "outside_portal_band")}
    rgbd_counts = {region: 0 for region in _REGIONS}
    registered_rgbd_counts = {region: 0 for region in _REGIONS}
    if selected is not None:
        for frame in frames:
            pose = _matrix(frame.get("transform_matrix"), "prepared frame pose")
            region = _region(_center(pose), selected, through_band)
            prepared_counts[region] += 1
            if frame["source_kind"] != "accepted_rgbd" or region not in rgbd_counts:
                continue
            rgbd_counts[region] += 1
            image_name = PurePosixPath(frame["rgb"]).relative_to("images").as_posix()
            if image_name in registered:
                registered_rgbd_counts[region] += 1

    hold_reasons: list[str] = ["registered_roomplan_missing"]
    if selected is None:
        hold_reasons.append(f"portal_selection_{selection}")
    elif not selected["crossings"]:
        hold_reasons.append("trajectory_portal_crossing_missing")
    if any(portal["rejected_crossings"] for portal in portals):
        hold_reasons.append("trajectory_portal_crossing_bracket_invalid")
    for region in _REGIONS:
        if rgbd_counts[region] == 0:
            hold_reasons.append(f"accepted_rgbd_{region}_missing")
    if not registration["supplied"]:
        hold_reasons.append("colmap_registration_missing")
    else:
        for region in _REGIONS:
            if registered_rgbd_counts[region] == 0:
                hold_reasons.append(f"registered_rgbd_{region}_missing")
    hold_reasons.extend(
        ["observed_free_space_missing", "route_corridor_missing", "prior_closed_state_control_missing"]
    )
    portal_summaries = [
        {
            "id": portal["id"],
            "kind": portal["kind"],
            "width_meters": portal["width_meters"],
            "height_meters": portal["height_meters"],
            "crossing_count": len(portal["crossings"]),
            "crossings": portal["crossings"],
            "rejected_crossing_count": len(portal["rejected_crossings"]),
            "rejected_crossings": portal["rejected_crossings"],
        }
        for portal in portals
    ]
    return {
        "schema": REPORT_SCHEMA,
        "status": "held_missing_evidence",
        "decision": "hold",
        "reason": hold_reasons[0],
        "hold_reasons": hold_reasons,
        "inputs": {
            "prepared_capture": prepared_ref,
            "source_capture": source_ref,
            "full_trajectory": trajectory_ref,
            "roomplan_semantics": semantics_ref,
            "roomplan_usdz": roomplan_ref,
            "roomplan_report": roomplan_report_ref,
            "sfm_package": registration.get("sfm_package"),
            "colmap_images": registration.get("images_txt"),
        },
        "coordinate_contract": {
            "frame": "arkit_world_shared_session_proposal",
            "units": "meters",
            "roomplan_registration": "missing",
            "shared_session_events": "not_packaged_by_prepare_capture",
            "through_band_meters": through_band,
        },
        "portal_analysis": {
            "selection": selection,
            "requested_portal_id": portal_id,
            "selected_portal_id": selected["id"] if selected is not None else None,
            "candidates": portal_summaries,
        },
        "trajectory": trajectory,
        "frame_bindings": {
            "prepared_frame_count": len(frames),
            "source_capture_frame_count": len(source.get("frames", [])),
            "source_kind_counts": source_counts,
            "prepared_region_counts": prepared_counts,
            "accepted_rgbd_region_counts": rgbd_counts,
            "registered_accepted_rgbd_region_counts": registered_rgbd_counts,
            "synthetic_rgbd_generated": False,
            "rgbd_source": "prepared_accepted_rgbd_with_existing_depth_and_confidence_only",
        },
        "colmap_registration": registration,
        "rails": {
            "roomplan_geometry": "accepted_proposal_only",
            "full_trajectory": "accepted_exact_count_contiguous_source_evidence",
            "prepared_source_frame_bindings": "accepted_pose_timestamp_intrinsics_and_prepared_asset_presence",
            "trajectory_portal_crossing": (
                "observed_proposal_only" if selected and selected["crossings"] else "held_missing"
            ),
            "accepted_rgbd_both_sides_and_through": (
                "held_missing"
                if any(value == 0 for value in rgbd_counts.values())
                else "accepted_capture_evidence_only"
            ),
            "registered_rgbd_both_sides_and_through": (
                "held_missing"
                if any(value == 0 for value in registered_rgbd_counts.values())
                else "accepted_registration_evidence_only"
            ),
            "free_space": "held_missing",
            "route_corridor": "held_missing",
            "prior_closed_state_control": "held_missing",
            "source_asset_byte_parity": "unavailable_source_assets_not_in_prepared_package",
        },
        "outcome": {
            "producer_contract_valid": False,
            "evidence_complete_for_future_reduction_design": False,
            "reduction_started": False,
            "traversable": False,
            "collision_candidate_promoted": False,
        },
        "authority": _false_authority(),
    }


def derive_portal_route_evidence(
    prepared_capture: Path,
    out_dir: Path,
    *,
    sfm_package: Path | None = None,
    portal_id: str | None = None,
    through_band_meters: float = DEFAULT_THROUGH_BAND_METERS,
) -> dict[str, Any]:
    capture_path = _regular_file(prepared_capture, "prepared capture")
    sfm_root = _regular_directory(sfm_package, "SfM package") if sfm_package is not None else None
    out_dir = out_dir.absolute()
    try:
        out_dir.resolve().relative_to(capture_path.parent.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("portal derivation output must be outside the immutable prepared capture")
    if sfm_root is not None:
        try:
            out_dir.resolve().relative_to(sfm_root)
        except ValueError:
            pass
        else:
            raise ValueError("portal derivation output must be outside the immutable SfM package")
    if out_dir.exists():
        metadata = out_dir.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("portal derivation output must be a regular directory")
        if any(out_dir.iterdir()):
            raise FileExistsError(f"portal derivation output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_NAME
    try:
        report = _derive(
            capture_path,
            sfm_package=sfm_root,
            portal_id=portal_id,
            through_band_meters=through_band_meters,
        )
    except Exception as error:
        report = {
            "schema": REPORT_SCHEMA,
            "status": "rejected",
            "decision": "reject",
            "reason": "portal_route_derivation_failed",
            "error": str(error),
            "error_type": type(error).__name__,
            "authority": _false_authority(),
        }
        write_json_strict(report_path, report)
        raise
    write_json_strict(report_path, report)
    return report
