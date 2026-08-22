from __future__ import annotations

import math
import stat
from pathlib import Path
from typing import Any

import numpy as np

from .hybrid_surface import _digest_json, _false_authority, _validate_registration
from .json_utils import load_json_strict, write_json_strict
from .reduced_collider import (
    PROBE_REPORT_SCHEMA,
    REDUCED_REPORT_SCHEMA,
)
from .rgbd_tsdf import _asset_reference, _sha256, _verify_evidence
from .world_studio_export import (
    MANIFEST_NAME,
    SCHEMA as HANDOFF_SCHEMA,
    _registered_image_names,
)

EVIDENCE_SCHEMA = "capture_splat.portal_route_evidence.v0.1"
FREE_SPACE_SCHEMA = "capture_splat.portal_free_space_evidence.v0.1"
ROUTE_SCHEMA = "capture_splat.portal_route_corridor_evidence.v0.1"
RGBD_SUPPORT_SCHEMA = "capture_splat.portal_rgbd_support.v0.1"
ROOMPLAN_REGISTRATION_SCHEMA = "capture_splat.roomplan_arkit_registration.v0.1"
REPORT_SCHEMA = "capture_splat.portal_route_validation.v0.1"
REPORT_NAME = "capture_splat_portal_route_validation_report.json"

_REGIONS = ("side_a", "through_opening", "side_b")
_RGBD_NAME_MATCHING = "unique_case_sensitive_rgb_basename_with_same_root_rgb_and_depth_v1"
_MAX_EVIDENCE_SAMPLES = 100_000
_MAX_DISTANCE_EVALUATIONS = 2_000_000


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_keys(
    value: dict[str, Any],
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing or unknown:
        raise ValueError(
            f"{label} keys are invalid; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    if minimum is not None and result < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return result


def _positive(value: Any, label: str) -> float:
    result = _number(value, label)
    if result <= 0.0:
        raise ValueError(f"{label} must be positive")
    return result


def _point(value: Any, label: str) -> np.ndarray:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must be a three-number point")
    return np.asarray([_number(item, label) for item in value], dtype=np.float64)


def _matrix(value: Any, label: str) -> np.ndarray:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} must be a finite rigid 4x4 matrix")
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{label} must be a finite rigid 4x4 matrix")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise ValueError(f"{label} homogeneous row is invalid")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6):
        raise ValueError(f"{label} rotation is not orthonormal")
    if not math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=1e-6):
        raise ValueError(f"{label} rotation determinant is not one")
    return matrix


def _provenance(value: Any, label: str) -> dict[str, Any]:
    provenance = _require_object(value, label)
    if not provenance or any(not isinstance(key, str) or not key for key in provenance):
        raise ValueError(f"{label} must be a non-empty object")
    for key, item in provenance.items():
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label}.{key} must be a non-empty string")
    return provenance


def _false_authority_contract(value: Any, label: str) -> None:
    authority = _require_object(value, label)
    expected = _false_authority()
    _require_keys(authority, set(expected), label)
    if authority != expected:
        raise ValueError(f"{label} grants unsupported authority")


def _handoff_authority(value: Any, label: str) -> None:
    authority = _require_object(value, label)
    _require_keys(
        authority,
        {
            "source_frames",
            "trained_splats",
            "metric_authority",
            "collision_authority",
            "semantic_authority",
            "navigation_authority",
            "quality_claim",
        },
        label,
    )
    if (
        authority["source_frames"] != "visual_evidence"
        or authority["trained_splats"] != "review_proposal"
        or any(
            authority[key] is not False
            for key in (
                "metric_authority",
                "collision_authority",
                "semantic_authority",
                "navigation_authority",
                "quality_claim",
            )
        )
    ):
        raise ValueError(f"{label} grants unsupported authority")


def _external_file(path: Path, label: str) -> tuple[Path, dict[str, Any]]:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except FileNotFoundError as error:
        raise FileNotFoundError(f"{label} is missing") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    resolved = absolute.resolve()
    return resolved, {
        "path": resolved.name,
        "size_bytes": resolved.lstat().st_size,
        "checksum": _sha256(resolved),
    }


def _binding(value: Any, evidence: dict[str, Any], label: str) -> None:
    binding = _require_object(value, label)
    _require_keys(binding, {"size_bytes", "checksum"}, label)
    if binding != {key: evidence[key] for key in ("size_bytes", "checksum")}:
        raise ValueError(f"{label} does not bind the exact input")


def _reference(root: Path, value: Any, label: str) -> tuple[Path, dict[str, Any]]:
    reference = _require_object(value, label)
    _require_keys(reference, {"path", "size_bytes", "checksum"}, label)
    path = reference.get("path")
    size = reference.get("size_bytes")
    checksum = reference.get("checksum")
    if not isinstance(path, str) or not path:
        raise ValueError(f"{label}.path is invalid")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"{label}.size_bytes is invalid")
    if not isinstance(checksum, str) or len(checksum) != 71 or not checksum.startswith("sha256:"):
        raise ValueError(f"{label}.checksum is invalid")
    return _verify_evidence(root, reference, label), reference


def _handoff_manifest(handoff: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    absolute = handoff.absolute()
    if absolute.is_symlink():
        raise ValueError("handoff must not be a symbolic link")
    manifest_path = absolute / MANIFEST_NAME if absolute.is_dir() else absolute
    manifest_path, evidence = _external_file(manifest_path, "handoff manifest")
    manifest = _require_object(load_json_strict(manifest_path), "handoff manifest")
    if manifest.get("schema") != HANDOFF_SCHEMA:
        raise ValueError("handoff must use immutable v0.3 schema")
    _handoff_authority(manifest.get("authority"), "handoff authority")
    return manifest_path, manifest_path.parent, manifest, evidence


def _coordinate_contract(value: Any) -> dict[str, Any]:
    contract = _require_object(value, "coordinate_contract")
    _require_keys(
        contract,
        {
            "coordinate_frame",
            "units",
            "scale_to_meters",
            "world_up",
            "position_uncertainty_meters",
            "dimension_uncertainty_meters",
            "plane_residual_tolerance_meters",
        },
        "coordinate_contract",
    )
    if contract["coordinate_frame"] != "arkit_world" or contract["units"] != "meters":
        raise ValueError("portal evidence must use arkit_world meters")
    if not math.isclose(_positive(contract["scale_to_meters"], "scale_to_meters"), 1.0):
        raise ValueError("portal evidence meter scale must be one")
    world_up = _point(contract["world_up"], "world_up")
    if not np.allclose(world_up, [0.0, 1.0, 0.0], atol=1e-9):
        raise ValueError("portal evidence world_up must be ARKit +Y")
    for key in (
        "position_uncertainty_meters",
        "dimension_uncertainty_meters",
        "plane_residual_tolerance_meters",
    ):
        _positive(contract[key], key)
    return contract


def _same_coordinate_contract(value: dict[str, Any], label: str) -> None:
    if (
        value.get("coordinate_frame") != "arkit_world"
        or value.get("units") != "meters"
        or not math.isclose(_positive(value.get("scale_to_meters"), f"{label}.scale_to_meters"), 1.0)
    ):
        raise ValueError(f"{label} coordinate frame, units, or scale does not match")


def _roomplan(
    value: Any,
    evidence_root: Path,
    handoff_root: Path,
    handoff: dict[str, Any],
) -> dict[str, Any]:
    roomplan = _require_object(value, "roomplan")
    _require_keys(
        roomplan,
        {"handoff_asset", "registration_evidence"},
        "roomplan",
    )
    asset_binding = _require_object(roomplan["handoff_asset"], "roomplan.handoff_asset")
    _require_keys(
        asset_binding,
        {"asset_key", "size_bytes", "checksum"},
        "roomplan.handoff_asset",
    )
    if asset_binding["asset_key"] != "room_plan":
        raise ValueError("roomplan must bind the handoff room_plan asset")
    assets = _require_object(handoff.get("assets"), "handoff assets")
    asset_path, asset = _asset_reference(handoff_root, assets.get("room_plan"), "room_plan")
    if asset_binding["size_bytes"] != asset["size_bytes"] or asset_binding["checksum"] != asset["checksum"]:
        raise ValueError("roomplan binding does not match the exact handoff asset")
    registration, registration_ref = _load_contract_artifact(
        evidence_root,
        roomplan["registration_evidence"],
        "roomplan.registration_evidence",
        ROOMPLAN_REGISTRATION_SCHEMA,
    )
    _require_keys(
        registration,
        {
            "schema",
            "source_coordinate_frame",
            "source_roomplan",
            "target_coordinate_frame",
            "source_units",
            "target_units",
            "scale_to_meters",
            "transform_to_target",
            "registration_uncertainty_meters",
            "method",
            "provenance",
            "authority",
        },
        "roomplan.registration_evidence",
    )
    source_frame = registration["source_coordinate_frame"]
    if source_frame != assets["room_plan"].get("coordinate_frame"):
        raise ValueError("roomplan source frame does not match the handoff asset")
    if registration["source_units"] != assets["room_plan"].get("units"):
        raise ValueError("roomplan source units do not match the handoff asset")
    source_roomplan = _require_object(
        registration["source_roomplan"], "roomplan registration source_roomplan"
    )
    _require_keys(
        source_roomplan,
        {"size_bytes", "checksum"},
        "roomplan registration source_roomplan",
    )
    if any(source_roomplan.get(key) != asset[key] for key in ("size_bytes", "checksum")):
        raise ValueError("roomplan registration does not bind the exact RoomPlan asset")
    if registration["target_coordinate_frame"] != "arkit_world" or registration["target_units"] != "meters":
        raise ValueError("roomplan target must be arkit_world meters")
    if not math.isclose(
        _positive(registration["scale_to_meters"], "roomplan.scale_to_meters"), 1.0
    ):
        raise ValueError("roomplan scale must preserve meters")
    _matrix(registration["transform_to_target"], "roomplan.transform_to_target")
    _positive(
        registration["registration_uncertainty_meters"], "roomplan registration uncertainty"
    )
    if not isinstance(registration["method"], str) or not registration["method"].strip():
        raise ValueError("roomplan method must be non-empty")
    _provenance(registration["provenance"], "roomplan.provenance")
    return {
        "asset": asset,
        "registration_evidence": registration_ref,
        "verified_path_name": asset_path.name,
    }


def _scalar_cross_2d(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _convex_ordered_polygon(projected: np.ndarray) -> None:
    orientation = 0
    for index in range(len(projected)):
        a = projected[(index + 1) % len(projected)] - projected[index]
        b = projected[(index + 2) % len(projected)] - projected[(index + 1) % len(projected)]
        cross = _scalar_cross_2d(a, b)
        if abs(cross) <= 1e-10:
            raise ValueError("portal polygon has a collinear or repeated edge")
        current = 1 if cross > 0.0 else -1
        if orientation and current != orientation:
            raise ValueError("portal polygon must be ordered and convex")
        orientation = current


def _portal(value: Any, contract: dict[str, Any]) -> dict[str, Any]:
    portal = _require_object(value, "portal")
    _require_keys(
        portal,
        {"id", "polygon", "plane", "clear_width_meters", "clear_height_meters", "threshold"},
        "portal",
    )
    if not isinstance(portal["id"], str) or not portal["id"].strip():
        raise ValueError("portal.id must be non-empty")
    raw_polygon = portal["polygon"]
    if not isinstance(raw_polygon, list) or len(raw_polygon) != 4:
        raise ValueError("portal polygon must contain exactly four ordered vertices")
    polygon = np.asarray([_point(item, "portal polygon vertex") for item in raw_polygon])
    if len({tuple(item) for item in polygon}) != len(polygon):
        raise ValueError("portal polygon vertices must be unique")
    plane = _require_object(portal["plane"], "portal.plane")
    _require_keys(plane, {"normal", "offset_meters"}, "portal.plane")
    normal = _point(plane["normal"], "portal.plane.normal")
    if not math.isclose(float(np.linalg.norm(normal)), 1.0, abs_tol=1e-6):
        raise ValueError("portal plane normal must be unit length")
    if abs(float(normal @ np.array([0.0, 1.0, 0.0]))) > 0.1:
        raise ValueError("portal plane must be vertical in arkit_world")
    offset = _number(plane["offset_meters"], "portal.plane.offset_meters")
    residual = np.abs(polygon @ normal + offset)
    plane_tolerance = float(contract["plane_residual_tolerance_meters"])
    if float(residual.max()) > plane_tolerance:
        raise ValueError("portal polygon does not lie on its declared plane")
    width_axis = np.cross([0.0, 1.0, 0.0], normal)
    width_axis /= np.linalg.norm(width_axis)
    projected_polygon = np.column_stack((polygon @ width_axis, polygon[:, 1]))
    _convex_ordered_polygon(projected_polygon)
    measured_width = float(np.ptp(polygon @ width_axis))
    measured_height = float(np.ptp(polygon[:, 1]))
    if measured_width <= 1e-6 or measured_height <= 1e-6:
        raise ValueError("portal polygon has degenerate width or height")
    area = 0.0
    origin = polygon[0]
    for index in range(1, len(polygon) - 1):
        area += 0.5 * float(np.linalg.norm(np.cross(polygon[index] - origin, polygon[index + 1] - origin)))
    if area <= 1e-8:
        raise ValueError("portal polygon area is degenerate")
    width = _positive(portal["clear_width_meters"], "portal clear width")
    height = _positive(portal["clear_height_meters"], "portal clear height")
    dimension_tolerance = float(contract["dimension_uncertainty_meters"])
    if abs(width - measured_width) > dimension_tolerance:
        raise ValueError("portal clear width disagrees with its polygon")
    if abs(height - measured_height) > dimension_tolerance:
        raise ValueError("portal clear height disagrees with its polygon")
    threshold = _require_object(portal["threshold"], "portal.threshold")
    _require_keys(threshold, {"segment", "height_meters"}, "portal.threshold")
    segment = threshold["segment"]
    if not isinstance(segment, list) or len(segment) != 2:
        raise ValueError("portal threshold must contain two endpoints")
    threshold_points = np.asarray([_point(item, "portal threshold endpoint") for item in segment])
    if float(np.abs(threshold_points @ normal + offset).max()) > plane_tolerance:
        raise ValueError("portal threshold does not lie on the portal plane")
    lower_edge_height = float(polygon[:, 1].min())
    if np.any(np.abs(threshold_points[:, 1] - lower_edge_height) > dimension_tolerance):
        raise ValueError("portal threshold does not lie on the polygon lower edge")
    if any(
        not _point_in_portal(point, {
            "polygon": polygon,
            "width_axis": width_axis,
        }, dimension_tolerance)
        for point in threshold_points
    ):
        raise ValueError("portal threshold endpoints lie outside the portal polygon")
    if abs(float(np.linalg.norm(threshold_points[1] - threshold_points[0])) - width) > dimension_tolerance:
        raise ValueError("portal threshold length disagrees with clear width")
    threshold_height = _number(threshold["height_meters"], "portal threshold height", minimum=0.0)
    if threshold_height > height:
        raise ValueError("portal threshold height exceeds clear height")
    return {
        "id": portal["id"],
        "normal": normal,
        "offset": offset,
        "polygon": polygon,
        "width_axis": width_axis,
        "measured_width_meters": measured_width,
        "measured_height_meters": measured_height,
        "area_square_meters": area,
        "clear_width_meters": width,
        "clear_height_meters": height,
        "threshold_height_meters": threshold_height,
    }


def _load_contract_artifact(
    root: Path,
    value: Any,
    label: str,
    schema: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path, reference = _reference(root, value, label)
    payload = _require_object(load_json_strict(path), label)
    if payload.get("schema") != schema:
        raise ValueError(f"{label} schema is invalid")
    _false_authority_contract(payload.get("authority"), f"{label}.authority")
    return payload, reference


def _free_space(root: Path, value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, reference = _load_contract_artifact(
        root, value, "free_space", FREE_SPACE_SCHEMA
    )
    _require_keys(
        payload,
        {
            "schema",
            "coordinate_frame",
            "units",
            "scale_to_meters",
            "observed_sample_count",
            "maximum_sample_spacing_meters",
            "samples",
            "uncertainty_meters",
            "method",
            "provenance",
            "authority",
        },
        "free_space",
    )
    _same_coordinate_contract(payload, "free_space")
    count = payload["observed_sample_count"]
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count <= 0
        or count > _MAX_EVIDENCE_SAMPLES
    ):
        raise ValueError("free_space observed_sample_count is outside the bounded range")
    maximum_spacing = _positive(
        payload["maximum_sample_spacing_meters"], "free_space maximum sample spacing"
    )
    if maximum_spacing < 0.001 or maximum_spacing > 1.0:
        raise ValueError("free_space maximum sample spacing is outside [0.001, 1.0] meters")
    samples = payload["samples"]
    if not isinstance(samples, list) or len(samples) != count:
        raise ValueError("free_space sample count does not match its samples")
    for index, raw in enumerate(samples):
        sample = _require_object(raw, f"free_space sample {index}")
        _require_keys(
            sample,
            {
                "position",
                "horizontal_clearance_meters",
                "vertical_clearance_meters",
                "support_capture_frame_indices",
            },
            f"free_space sample {index}",
        )
        _point(sample["position"], f"free_space sample {index} position")
        _positive(
            sample["horizontal_clearance_meters"],
            f"free_space sample {index} horizontal clearance",
        )
        _positive(
            sample["vertical_clearance_meters"],
            f"free_space sample {index} vertical clearance",
        )
        support = sample["support_capture_frame_indices"]
        if (
            not isinstance(support, list)
            or not support
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in support)
            or len(set(support)) != len(support)
        ):
            raise ValueError(f"free_space sample {index} capture support is invalid")
    _positive(payload["uncertainty_meters"], "free_space uncertainty")
    if not isinstance(payload["method"], str) or not payload["method"].strip():
        raise ValueError("free_space method must be non-empty")
    _provenance(payload["provenance"], "free_space.provenance")
    return payload, reference


def _route_free_space_support(
    free_space: dict[str, Any],
    route: dict[str, Any],
    rgbd_frame_indices: set[int],
) -> dict[str, Any]:
    sample_points = np.asarray(
        [_point(item["position"], "free_space sample position") for item in free_space["samples"]]
    )
    centerline = np.asarray(
        [_point(item, "route centerline point") for item in route["centerline"]]
    )
    maximum_spacing = float(free_space["maximum_sample_spacing_meters"])
    sampled_segments: list[np.ndarray] = [centerline[0]]
    derived_sample_count = 1
    for start, end in zip(centerline[:-1], centerline[1:]):
        length = float(np.linalg.norm(end - start))
        if not math.isfinite(length):
            raise ValueError("route corridor segment length is non-finite")
        subdivisions = max(1, math.ceil(length / maximum_spacing))
        derived_sample_count += subdivisions
        if derived_sample_count > _MAX_EVIDENCE_SAMPLES:
            raise ValueError("route corridor exceeds the bounded validation sample count")
        sampled_segments.extend(
            start + (end - start) * (step / subdivisions)
            for step in range(1, subdivisions + 1)
        )
    route_points = np.asarray(sampled_segments)
    evaluation_count = len(route_points) * len(sample_points)
    if evaluation_count > _MAX_DISTANCE_EVALUATIONS:
        raise ValueError("route/free-space comparison exceeds the bounded work limit")
    nearest = np.empty(len(route_points), dtype=np.int64)
    nearest_distances = np.empty(len(route_points), dtype=np.float64)
    for index, point in enumerate(route_points):
        squared = np.sum((sample_points - point) ** 2, axis=1)
        nearest[index] = int(np.argmin(squared))
        nearest_distances[index] = math.sqrt(float(squared[nearest[index]]))
    if np.any(nearest_distances > maximum_spacing):
        raise ValueError("route corridor lacks bounded free-space support")
    for route_index, sample_index in enumerate(nearest):
        sample = free_space["samples"][int(sample_index)]
        support = set(sample["support_capture_frame_indices"])
        if not support.issubset(rgbd_frame_indices):
            raise ValueError(f"route point {route_index} free-space support is not bound to RGB-D evidence")
        if float(sample["horizontal_clearance_meters"]) < float(route["corridor_half_width_meters"]):
            raise ValueError(f"route point {route_index} lacks horizontal free-space clearance")
        if float(sample["vertical_clearance_meters"]) < float(route["minimum_clear_height_meters"]):
            raise ValueError(f"route point {route_index} lacks vertical free-space clearance")
    return {
        "route_corridor_supported_sample_count": len(route_points),
        "distance_evaluation_count": evaluation_count,
        "distance_evaluation_limit": _MAX_DISTANCE_EVALUATIONS,
        "maximum_nearest_free_space_sample_distance_meters": float(nearest_distances.max()),
        "maximum_allowed_sample_spacing_meters": maximum_spacing,
    }


def _point_in_portal(point: np.ndarray, portal: dict[str, Any], tolerance: float) -> bool:
    polygon = portal["polygon"]
    x_axis = portal["width_axis"]
    projected = np.column_stack((polygon @ x_axis, polygon[:, 1]))
    query = np.array([point @ x_axis, point[1]])
    sign = 0
    for index in range(len(projected)):
        a = projected[index]
        b = projected[(index + 1) % len(projected)]
        cross = _scalar_cross_2d(b - a, query - a)
        if abs(cross) <= tolerance:
            continue
        current = 1 if cross > 0 else -1
        if sign and current != sign:
            return False
        sign = current
    return sign != 0


def _route(
    root: Path,
    value: Any,
    portal: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload, reference = _load_contract_artifact(
        root, value, "route_corridor", ROUTE_SCHEMA
    )
    _require_keys(
        payload,
        {
            "schema",
            "coordinate_frame",
            "units",
            "scale_to_meters",
            "portal_id",
            "centerline",
            "corridor_half_width_meters",
            "minimum_clear_height_meters",
            "uncertainty_meters",
            "method",
            "provenance",
            "authority",
        },
        "route_corridor",
    )
    _same_coordinate_contract(payload, "route_corridor")
    if payload["portal_id"] != portal["id"]:
        raise ValueError("route corridor portal id does not match")
    raw_centerline = payload["centerline"]
    if (
        not isinstance(raw_centerline, list)
        or len(raw_centerline) < 3
        or len(raw_centerline) > _MAX_EVIDENCE_SAMPLES
    ):
        raise ValueError("route corridor needs at least three centerline points")
    centerline = np.asarray([_point(item, "route centerline point") for item in raw_centerline])
    if np.any(np.linalg.norm(np.diff(centerline, axis=0), axis=1) <= 1e-8):
        raise ValueError("route corridor contains a degenerate segment")
    half_width = _positive(payload["corridor_half_width_meters"], "route half width")
    clear_height = _positive(payload["minimum_clear_height_meters"], "route clear height")
    uncertainty = _positive(payload["uncertainty_meters"], "route uncertainty")
    if half_width * 2.0 > portal["clear_width_meters"] + float(contract["dimension_uncertainty_meters"]):
        raise ValueError("route corridor is wider than the portal")
    if clear_height > portal["clear_height_meters"] + float(contract["dimension_uncertainty_meters"]):
        raise ValueError("route clear height exceeds the portal")
    signed = centerline @ portal["normal"] + portal["offset"]
    side_tolerance = max(uncertainty, float(contract["position_uncertainty_meters"]))
    if not (signed[0] < -side_tolerance and signed[-1] > side_tolerance):
        raise ValueError("route endpoints do not bind opposite portal sides")
    intersection = None
    for index in range(len(centerline) - 1):
        if signed[index] <= 0.0 <= signed[index + 1]:
            denominator = signed[index + 1] - signed[index]
            if denominator > 0.0:
                fraction = -signed[index] / denominator
                candidate = centerline[index] + fraction * (centerline[index + 1] - centerline[index])
                if _point_in_portal(candidate, portal, side_tolerance):
                    intersection = candidate
                    break
    if intersection is None:
        raise ValueError("route centerline does not cross within the portal polygon")
    if not isinstance(payload["method"], str) or not payload["method"].strip():
        raise ValueError("route corridor method must be non-empty")
    _provenance(payload["provenance"], "route_corridor.provenance")
    return payload, reference, {
        "centerline_point_count": len(centerline),
        "portal_intersection": intersection.tolist(),
        "corridor_half_width_meters": half_width,
        "minimum_clear_height_meters": clear_height,
    }


def _registered_names(handoff_root: Path, handoff: dict[str, Any]) -> dict[str, str]:
    assets = _require_object(handoff.get("assets"), "handoff assets")
    sparse = _require_object(assets.get("colmap_sparse"), "handoff colmap_sparse")
    images_path, _ = _asset_reference(handoff_root, sparse.get("images.txt"), "colmap images.txt")
    names, invalid = _registered_image_names(images_path)
    if invalid or not names:
        raise ValueError("handoff COLMAP registered image list is incomplete")
    by_basename: dict[str, str] = {}
    for name in names:
        basename = Path(name.replace("\\", "/")).name
        if basename in by_basename:
            raise ValueError("handoff COLMAP image basenames are ambiguous")
        by_basename[basename] = name
    return by_basename


def _capture_frames(
    handoff_root: Path,
    handoff: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    assets = _require_object(handoff.get("assets"), "handoff assets")
    capture_path, capture_ref = _asset_reference(
        handoff_root, assets.get("capture_manifest"), "capture manifest"
    )
    capture = _require_object(load_json_strict(capture_path), "capture manifest")
    if capture.get("schema") != "capture_splat.v0.3":
        raise ValueError("portal RGB-D support requires a v0.3 capture manifest")
    frames = capture.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("capture manifest frames are missing")
    inventory = _require_object(
        handoff.get("capture_manifest_assets"), "capture manifest asset inventory"
    )
    if (
        inventory.get("schema") != "capture_splat.capture_manifest_assets.v0.1"
        or inventory.get("complete") is not True
        or inventory.get("decision") != "ready"
        or inventory.get("missing") != []
        or inventory.get("conflicts") != []
    ):
        raise ValueError("capture manifest asset inventory is incomplete")
    references = inventory.get("assets")
    if not isinstance(references, list) or not references:
        raise ValueError("capture manifest asset inventory references are missing")
    by_path: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(references):
        _, reference = _reference(
            handoff_root, raw, f"capture manifest inventory asset {index}"
        )
        if reference["path"] in by_path:
            raise ValueError("capture manifest asset inventory contains a duplicate path")
        by_path[reference["path"]] = reference
    return capture, by_path, capture_ref


def _capture_asset(
    handoff_root: Path,
    inventory: dict[str, dict[str, Any]],
    relative: Any,
    label: str,
) -> dict[str, Any]:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} is missing")
    reference = inventory.get(relative)
    if reference is None:
        raise ValueError(f"{label} is not bound by the capture asset inventory")
    _verify_evidence(handoff_root, reference, label)
    return reference


def _frame_intrinsics(capture: dict[str, Any], frame: dict[str, Any], label: str) -> dict[str, float]:
    intrinsics = frame.get("intrinsics") or capture.get("intrinsics")
    if not isinstance(intrinsics, dict):
        raise ValueError(f"{label} intrinsics are missing")
    result = {
        key: _number(intrinsics.get(key), f"{label} intrinsics {key}")
        for key in ("fl_x", "fl_y", "cx", "cy", "w", "h")
    }
    if result["fl_x"] <= 0.0 or result["fl_y"] <= 0.0 or result["w"] <= 0.0 or result["h"] <= 0.0:
        raise ValueError(f"{label} intrinsics contain non-positive focal length or dimensions")
    return result


def _rgbd_support(
    root: Path,
    value: Any,
    handoff_root: Path,
    capture: dict[str, Any],
    capture_inventory: dict[str, dict[str, Any]],
    portal: dict[str, Any],
    registration_digest: str,
    registered_names: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int], set[int]]:
    payload, reference = _load_contract_artifact(
        root, value, "registered_rgbd_support", RGBD_SUPPORT_SCHEMA
    )
    _require_keys(
        payload,
        {
            "schema",
            "coordinate_frame",
            "units",
            "scale_to_meters",
            "registration_digest",
            "matching",
            "through_band_meters",
            "observations",
            "provenance",
            "authority",
        },
        "registered_rgbd_support",
    )
    _same_coordinate_contract(payload, "registered_rgbd_support")
    if payload["registration_digest"] != registration_digest:
        raise ValueError("RGB-D support registration digest does not match the handoff")
    if payload["matching"] != _RGBD_NAME_MATCHING:
        raise ValueError("RGB-D support registered image matching contract is invalid")
    through_band = _positive(payload["through_band_meters"], "RGB-D through band")
    observations = payload["observations"]
    if not isinstance(observations, list) or not observations:
        raise ValueError("registered RGB-D support observations are missing")
    counts = {region: 0 for region in _REGIONS}
    frame_ids: set[str] = set()
    for index, raw in enumerate(observations):
        observation = _require_object(raw, f"RGB-D observation {index}")
        _require_keys(
            observation,
            {
                "capture_frame_index",
                "registered_image_name",
                "region",
            },
            f"RGB-D observation {index}",
        )
        frame_index = observation["capture_frame_index"]
        if (
            isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or frame_index < 0
            or frame_index >= len(capture["frames"])
        ):
            raise ValueError("RGB-D support capture frame index is invalid")
        frame_id = str(frame_index)
        if frame_id in frame_ids:
            raise ValueError("RGB-D support capture frame indices must be unique")
        frame_ids.add(frame_id)
        frame = _require_object(capture["frames"][frame_index], f"capture frame {frame_index}")
        image_name = observation["registered_image_name"]
        if not isinstance(image_name, str) or image_name not in registered_names.values():
            raise ValueError("RGB-D support image is not registered in the bound handoff")
        rgb_relative = next(
            (
                frame.get(key)
                for key in ("rgb", "image", "image_path", "file_path")
                if isinstance(frame.get(key), str) and frame.get(key)
            ),
            None,
        )
        if (
            not isinstance(rgb_relative, str)
            or registered_names.get(Path(rgb_relative.replace("\\", "/")).name) != image_name
        ):
            raise ValueError("RGB-D support registered name does not match its capture frame")
        region = observation["region"]
        if region not in counts:
            raise ValueError("RGB-D support region is invalid")
        pose = _matrix(
            frame.get("transform_matrix") or frame.get("camera_to_world"),
            f"capture frame {frame_index} pose",
        )
        position = pose[:3, 3]
        _frame_intrinsics(capture, frame, f"capture frame {frame_index}")
        _number(frame.get("timestamp"), f"capture frame {frame_index} timestamp")
        signed = float(position @ portal["normal"] + portal["offset"])
        if region == "side_a" and signed >= 0.0:
            raise ValueError("side_a RGB-D support is not on portal side A")
        if region == "side_b" and signed <= 0.0:
            raise ValueError("side_b RGB-D support is not on portal side B")
        if region == "through_opening" and abs(signed) > through_band:
            raise ValueError("through-opening RGB-D support is outside its declared band")
        _capture_asset(handoff_root, capture_inventory, rgb_relative, f"capture frame {frame_index} RGB")
        for asset in ("depth", "confidence"):
            _capture_asset(
                handoff_root,
                capture_inventory,
                frame.get(asset),
                f"capture frame {frame_index} {asset}",
            )
        counts[region] += 1
    if any(count == 0 for count in counts.values()):
        raise ValueError("registered RGB-D support must cover both sides and through the opening")
    _provenance(payload["provenance"], "registered_rgbd_support.provenance")
    return payload, reference, counts, {int(item) for item in frame_ids}


def _held_report_authority(payload: dict[str, Any], label: str) -> None:
    if payload.get("decision") != "hold":
        raise ValueError(f"{label} must remain held")
    _false_authority_contract(payload.get("authority"), f"{label}.authority")


def _prior_closed_control(
    root: Path,
    value: Any,
    portal_id: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    control = _require_object(value, "prior_closed_state_control")
    _require_keys(
        control,
        {"capture_state", "portal_id", "handoff", "reduced_candidate", "reduced_report", "probe_report"},
        "prior_closed_state_control",
    )
    if control["capture_state"] != "closed" or control["portal_id"] != portal_id:
        raise ValueError("prior control must bind the same portal in a closed state")
    paths: dict[str, Path] = {}
    references: dict[str, dict[str, Any]] = {}
    for key in ("handoff", "reduced_candidate", "reduced_report", "probe_report"):
        paths[key], references[key] = _reference(root, control[key], f"prior control {key}")
    closed_handoff = _require_object(load_json_strict(paths["handoff"]), "prior closed handoff")
    if closed_handoff.get("schema") != HANDOFF_SCHEMA:
        raise ValueError("prior closed handoff schema is invalid")
    _handoff_authority(
        closed_handoff.get("authority"), "prior closed handoff authority"
    )
    reduced = _require_object(load_json_strict(paths["reduced_report"]), "prior reduced report")
    if reduced.get("schema") != REDUCED_REPORT_SCHEMA:
        raise ValueError("prior reduced report schema is invalid")
    _held_report_authority(reduced, "prior reduced report")
    probe = _require_object(load_json_strict(paths["probe_report"]), "prior probe report")
    if probe.get("schema") != PROBE_REPORT_SCHEMA:
        raise ValueError("prior probe report schema is invalid")
    _held_report_authority(probe, "prior probe report")
    reduced_candidate = _require_object(reduced.get("candidate"), "prior reduced report candidate")
    if any(
        reduced_candidate.get(key) != references["reduced_candidate"][key]
        for key in ("size_bytes", "checksum")
    ):
        raise ValueError("prior reduced report does not bind the prior reduced candidate")
    probe_inputs = _require_object(probe.get("inputs"), "prior probe report.inputs")
    probe_candidate = _require_object(
        probe_inputs.get("reduced_collider"), "prior probe report reduced collider"
    )
    if any(
        probe_candidate.get(key) != references["reduced_candidate"][key]
        for key in ("size_bytes", "checksum")
    ):
        raise ValueError("prior probe report does not bind the prior reduced candidate")
    probes = _require_object(probe.get("probes"), "prior probe report probes")
    closed_door = _require_object(probes.get("closed_door"), "prior closed-door probe")
    if closed_door.get("status") not in {"accepted", "held"}:
        raise ValueError("prior closed-door probe status is invalid")
    return control, references


def _missing_report(
    handoff: dict[str, Any],
    handoff_evidence: dict[str, Any],
) -> dict[str, Any]:
    assets = handoff.get("assets") if isinstance(handoff.get("assets"), dict) else {}
    missing = [
        "registered_roomplan",
        "metric_portal_polygon_plane_and_threshold",
        "observed_free_space",
        "route_corridor",
        "registered_rgbd_side_a",
        "registered_rgbd_through_opening",
        "registered_rgbd_side_b",
        "prior_closed_state_control",
    ]
    return {
        "schema": REPORT_SCHEMA,
        "status": "held_missing_evidence",
        "decision": "hold",
        "reason": missing[0],
        "hold_reasons": missing,
        "inputs": {"handoff": handoff_evidence, "portal_route_evidence": None},
        "observed_source_state": {
            "room_plan_asset_present": "room_plan" in assets,
            "portal_route_evidence_supplied": False,
        },
        "rails": {name: "held_missing" for name in missing},
        "outcome": {
            "producer_contract_valid": False,
            "evidence_complete_for_future_reduction_design": False,
            "reduction_started": False,
            "traversable": False,
            "collision_candidate_promoted": False,
        },
        "authority": _false_authority(),
    }


def validate_portal_route_evidence(
    handoff: Path,
    out_dir: Path,
    *,
    evidence: Path | None = None,
) -> dict[str, Any]:
    manifest_path, handoff_root, handoff_payload, handoff_evidence = _handoff_manifest(handoff)
    out_dir = out_dir.absolute()
    try:
        out_dir.resolve().relative_to(handoff_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("portal-route output must be outside the immutable handoff package")
    if evidence is not None:
        evidence_absolute = evidence.absolute()
        if evidence_absolute.is_symlink():
            raise ValueError("portal-route evidence must not be a symbolic link")
        try:
            out_dir.resolve().relative_to(evidence_absolute.parent.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("portal-route output must be outside the immutable evidence package")
    if out_dir.exists():
        metadata = out_dir.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("portal-route output must be a regular directory")
        if any(out_dir.iterdir()):
            raise FileExistsError(f"portal-route output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_NAME
    if evidence is None:
        report = _missing_report(handoff_payload, handoff_evidence)
        write_json_strict(report_path, report)
        return report

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "rejected",
        "decision": "reject",
        "reason": "portal_route_contract_validation_failed",
        "authority": _false_authority(),
    }
    try:
        evidence_path, evidence_ref = _external_file(evidence, "portal-route evidence")
        payload = _require_object(load_json_strict(evidence_path), "portal-route evidence")
        _require_keys(
            payload,
            {
                "schema",
                "capture_state",
                "source_handoff",
                "coordinate_contract",
                "roomplan",
                "portal",
                "free_space",
                "route_corridor",
                "registered_rgbd_support",
                "prior_closed_state_control",
                "provenance",
                "authority",
            },
            "portal-route evidence",
        )
        if payload["schema"] != EVIDENCE_SCHEMA or payload["capture_state"] != "open":
            raise ValueError("portal-route evidence must describe an observed open state")
        _binding(payload["source_handoff"], handoff_evidence, "source_handoff")
        _false_authority_contract(payload["authority"], "portal-route evidence authority")
        _provenance(payload["provenance"], "portal-route evidence provenance")
        contract = _coordinate_contract(payload["coordinate_contract"])
        registration = _validate_registration(handoff_payload)
        roomplan = _roomplan(
            payload["roomplan"], evidence_path.parent, handoff_root, handoff_payload
        )
        portal = _portal(payload["portal"], contract)
        free_space, free_space_ref = _free_space(evidence_path.parent, payload["free_space"])
        route, route_ref, route_summary = _route(
            evidence_path.parent, payload["route_corridor"], portal, contract
        )
        registered = _registered_names(handoff_root, handoff_payload)
        capture, capture_inventory, capture_ref = _capture_frames(
            handoff_root, handoff_payload
        )
        rgbd, rgbd_ref, rgbd_counts, rgbd_frame_indices = _rgbd_support(
            evidence_path.parent,
            payload["registered_rgbd_support"],
            handoff_root,
            capture,
            capture_inventory,
            portal,
            registration["digest"],
            registered,
        )
        free_space_summary = _route_free_space_support(
            free_space, route, rgbd_frame_indices
        )
        _, prior_refs = _prior_closed_control(
            evidence_path.parent,
            payload["prior_closed_state_control"],
            portal["id"],
        )
        for source_payload, label in (
            (free_space, "free_space"),
            (route, "route_corridor"),
            (rgbd, "registered_rgbd_support"),
        ):
            if source_payload["coordinate_frame"] != contract["coordinate_frame"]:
                raise ValueError(f"{label} frame does not match the portal contract")
        report = {
            "schema": REPORT_SCHEMA,
            "status": "held_pending_consumer_and_physical_probes",
            "decision": "hold",
            "reason": "world_studio_source_collider_probe_pending",
            "hold_reasons": [
                "world_studio_source_collider_probe_pending",
                "world_studio_reduced_collider_probe_pending",
                "world_studio_route_reset_probe_pending",
                "physical_route_clearance_probe_pending",
                "new_reduction_hypothesis_not_started",
            ],
            "inputs": {
                "handoff": handoff_evidence,
                "portal_route_evidence": evidence_ref,
                "capture_manifest": capture_ref,
                "roomplan": roomplan["asset"],
                "roomplan_registration": roomplan["registration_evidence"],
                "free_space": free_space_ref,
                "route_corridor": route_ref,
                "registered_rgbd_support": rgbd_ref,
                "prior_closed_state_control": prior_refs,
            },
            "coordinate_contract": contract,
            "registration": registration,
            "portal": {
                "id": portal["id"],
                "polygon_vertex_count": len(portal["polygon"]),
                "area_square_meters": portal["area_square_meters"],
                "measured_width_meters": portal["measured_width_meters"],
                "measured_height_meters": portal["measured_height_meters"],
                "clear_width_meters": portal["clear_width_meters"],
                "clear_height_meters": portal["clear_height_meters"],
                "threshold_height_meters": portal["threshold_height_meters"],
            },
            "route_corridor": route_summary,
            "free_space_support": free_space_summary,
            "registered_rgbd_support": {
                "observation_count": sum(rgbd_counts.values()),
                "region_counts": rgbd_counts,
            },
            "rails": {
                "immutable_v03_handoff": "accepted",
                "roomplan_registration": "accepted_evidence_only",
                "portal_geometry": "accepted_evidence_only",
                "free_space": "accepted_evidence_only",
                "route_corridor": "accepted_evidence_only",
                "registered_rgbd_both_sides_and_through": "accepted_evidence_only",
                "prior_closed_state_control": "accepted_binding_only",
                "source_collider_probe": "held_pending_world_studio",
                "reduced_collider_probe": "held_pending_world_studio",
                "physical_route_probe": "held_pending",
            },
            "contract_digest": _digest_json(payload),
            "outcome": {
                "producer_contract_valid": True,
                "evidence_complete_for_future_reduction_design": True,
                "reduction_started": False,
                "traversable": False,
                "collision_candidate_promoted": False,
            },
            "authority": _false_authority(),
        }
        write_json_strict(report_path, report)
    except Exception as error:
        report["error"] = str(error)
        report["error_type"] = type(error).__name__
        write_json_strict(report_path, report)
        raise
    return report
