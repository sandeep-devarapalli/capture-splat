from __future__ import annotations

import hashlib
import json
import math
import stat
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from .capture_schema import load_capture
from .collision_candidate import CLASS_NAMES, _load_mesh
from .json_utils import load_json_strict, write_json_strict
from .ply_stats import _parse_header
from .rgbd_tsdf import (
    MESH_NAME as TSDF_MESH_NAME,
    REPORT_SCHEMA as TSDF_REPORT_SCHEMA,
    _asset_reference,
    _file_evidence,
    _metric_coordinate_declaration,
    _open3d,
    _require_regular_file,
    _sha256,
    _verify_evidence,
)
from .world_studio_export import MANIFEST_NAME, SCHEMA as HANDOFF_SCHEMA

HYBRID_MESH_NAME = "hybrid_structural_surface.ply"
HYBRID_REPORT_NAME = "capture_splat_hybrid_surface_report.json"
HYBRID_REPORT_SCHEMA = "capture_splat.hybrid_structural_surface.v0.1"
COLLIDER_MESH_NAME = "collider_candidate.ply"
COLLIDER_REPORT_NAME = "capture_splat_hybrid_collider_candidate_report.json"
COLLIDER_REPORT_SCHEMA = "capture_splat.hybrid_collider_candidate.v0.1"
UNKNOWN_CLASSIFICATION = 255
UNSUPPORTED_CLASSIFICATION = 254
SAMPLES_PER_FACE = 4
KNOWN_CLASSIFICATIONS = frozenset(CLASS_NAMES) - {0}

_TSDF_VERTEX_PROPERTIES = [
    {"kind": "scalar", "type": "double", "name": name}
    for name in ("x", "y", "z", "nx", "ny", "nz")
] + [
    {"kind": "scalar", "type": "uchar", "name": name}
    for name in ("red", "green", "blue")
]
_TSDF_FACE_PROPERTIES = [{
    "kind": "list",
    "count_type": "uchar",
    "value_type": "uint",
    "name": "vertex_indices",
}]
_TSDF_VERTEX_DTYPE = np.dtype([
    ("position", "<f8", (3,)),
    ("normal", "<f8", (3,)),
    ("color", "u1", (3,)),
])
_TSDF_FACE_DTYPE = np.dtype([
    ("count", "u1"),
    ("indices", "<u4", (3,)),
])


def _external_file_evidence(path: Path, label: str) -> tuple[Path, dict[str, Any]]:
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


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _false_authority() -> dict[str, bool]:
    return {
        "metric_authority": False,
        "semantic_authority": False,
        "collision_authority": False,
        "navigation_authority": False,
        "measurement_authority": False,
        "physics_authority": False,
        "newton_authority": False,
        "quality_claim": False,
    }


def _validate_registration(handoff: dict[str, Any]) -> dict[str, Any]:
    registration = handoff.get("metric_registration")
    if not isinstance(registration, dict):
        raise ValueError("handoff metric registration is missing")
    if (
        registration.get("schema") != "capture_splat.metric_registration.v0.1"
        or registration.get("status") != "accepted"
        or registration.get("accepted") is not True
        or registration.get("source_coordinate_frame") != "arkit_world"
        or registration.get("source_units") != "meters"
    ):
        raise ValueError("handoff metric registration is not accepted ARKit-world meter evidence")
    matched = registration.get("matched_cameras")
    scale = registration.get("scale")
    if (
        not isinstance(matched, int)
        or matched <= 0
        or not isinstance(scale, (int, float))
        or not math.isfinite(float(scale))
        or float(scale) <= 0
    ):
        raise ValueError("handoff metric registration counts or scale are invalid")
    matrices: dict[str, np.ndarray] = {}
    for name in ("matrix", "arkit_to_colmap"):
        matrix = np.asarray(registration.get(name), dtype=np.float64)
        if (
            matrix.shape != (4, 4)
            or not np.all(np.isfinite(matrix))
            or not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-10)
        ):
            raise ValueError(f"handoff metric registration {name} is invalid")
        matrices[name] = matrix
    if not np.array_equal(matrices["matrix"], matrices["arkit_to_colmap"]):
        raise ValueError("handoff metric registration matrices disagree")
    authority = registration.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("metric_mesh_registration_candidate") is not True
        or authority.get("collision_authority") is not False
        or authority.get("navigation_authority") is not False
    ):
        raise ValueError("handoff metric registration authority is invalid")
    if handoff.get("world_up") != [0.0, 1.0, 0.0] or handoff.get(
        "world_up_coordinate_frame"
    ) != "arkit_world":
        raise ValueError("handoff world-up declaration is invalid")
    return {
        "schema": registration["schema"],
        "status": registration["status"],
        "matched_cameras": matched,
        "source_coordinate_frame": registration["source_coordinate_frame"],
        "source_units": registration["source_units"],
        "digest": _digest_json(registration),
    }


def _load_tsdf_mesh(path: Path) -> tuple[bytes, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        header, data_offset = _parse_header(handle)
        if header["format"] != "binary_little_endian":
            raise ValueError("TSDF mesh must be binary little-endian PLY")
        if [item["name"] for item in header["elements"]] != ["vertex", "face"]:
            raise ValueError("TSDF mesh must contain only vertex and face elements")
        vertex, face = header["elements"]
        if vertex["properties"] != _TSDF_VERTEX_PROPERTIES:
            raise ValueError("TSDF vertex layout does not match the Open3D producer contract")
        if face["properties"] != _TSDF_FACE_PROPERTIES:
            raise ValueError("TSDF face layout does not match the Open3D producer contract")
        handle.seek(data_offset)
        vertex_bytes = handle.read(int(vertex["count"]) * _TSDF_VERTEX_DTYPE.itemsize)
        if len(vertex_bytes) != int(vertex["count"]) * _TSDF_VERTEX_DTYPE.itemsize:
            raise ValueError("TSDF vertex data ended early")
        face_bytes = handle.read(int(face["count"]) * _TSDF_FACE_DTYPE.itemsize)
        if len(face_bytes) != int(face["count"]) * _TSDF_FACE_DTYPE.itemsize:
            raise ValueError("TSDF face data ended early")
        if handle.read(1):
            raise ValueError("TSDF mesh has undeclared trailing data")
    vertex_records = np.frombuffer(vertex_bytes, dtype=_TSDF_VERTEX_DTYPE)
    face_records = np.frombuffer(face_bytes, dtype=_TSDF_FACE_DTYPE)
    if np.any(face_records["count"] != 3):
        raise ValueError("TSDF mesh faces must be triangles")
    vertices = vertex_records["position"]
    normals = vertex_records["normal"]
    faces = face_records["indices"].astype(np.int64)
    if not np.all(np.isfinite(vertices)) or not np.all(np.isfinite(normals)):
        raise ValueError("TSDF mesh contains non-finite vertex data")
    if np.any(faces < 0) or np.any(faces >= len(vertices)):
        raise ValueError("TSDF mesh contains out-of-range face indices")
    area_vectors = np.cross(
        vertices[faces[:, 1]] - vertices[faces[:, 0]],
        vertices[faces[:, 2]] - vertices[faces[:, 0]],
    )
    lengths = np.linalg.norm(area_vectors, axis=1)
    if np.any(~np.isfinite(lengths)) or np.any(lengths <= 1e-12):
        raise ValueError("TSDF mesh contains non-finite or degenerate triangles")
    return vertex_bytes, vertices, normals, faces, area_vectors / lengths[:, None]


def _validate_tsdf_report(
    report_path: Path,
    handoff_evidence: dict[str, Any],
    capture_ref: dict[str, Any],
    colmap_images_ref: dict[str, Any],
    coordinate_declaration: dict[str, Any],
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    report = load_json_strict(report_path)
    if (
        not isinstance(report, dict)
        or report.get("schema") != TSDF_REPORT_SCHEMA
        or report.get("decision") != "hold"
        or report.get("software_surface_candidate") != "hold"
    ):
        raise ValueError("TSDF report is not a held v0.1 software surface candidate")
    authority = report.get("authority")
    if not isinstance(authority, dict) or not authority or any(authority.values()):
        raise ValueError("TSDF report grants unsupported authority")
    inputs = report.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("TSDF report input bindings are missing")
    if inputs.get("handoff_manifest") != handoff_evidence:
        raise ValueError("TSDF report is not bound to the exact handoff manifest")
    if inputs.get("capture_manifest") != capture_ref:
        raise ValueError("TSDF report capture binding does not match the handoff")
    if inputs.get("colmap_images") != colmap_images_ref:
        raise ValueError("TSDF report COLMAP binding does not match the handoff")
    coordinate = report.get("coordinate_contract")
    if (
        not isinstance(coordinate, dict)
        or coordinate.get("output_coordinate_frame") != "arkit_world"
        or coordinate.get("units") != "meters"
        or coordinate.get("capture_declaration") != coordinate_declaration
    ):
        raise ValueError("TSDF report coordinate contract is invalid")
    mesh = report.get("mesh")
    if (
        not isinstance(mesh, dict)
        or mesh.get("path") != TSDF_MESH_NAME
        or mesh.get("coordinate_frame") != "arkit_world"
        or mesh.get("units") != "meters"
        or mesh.get("coordinate_declaration") != coordinate_declaration
        or mesh.get("finite") is not True
        or mesh.get("budget_limited") is not False
        or mesh.get("non_finite_vertex_count") != 0
        or mesh.get("non_finite_normal_count") != 0
        or mesh.get("invalid_index_triangle_count") != 0
        or mesh.get("degenerate_triangle_count") != 0
    ):
        raise ValueError("TSDF mesh report is not finite ARKit-world meter evidence")
    mesh_path, mesh_evidence = _asset_reference(report_path.parent, mesh, "TSDF mesh")
    if mesh_path != report_path.parent / TSDF_MESH_NAME:
        raise ValueError("TSDF mesh must be beside its strict report")
    return report, mesh_path, mesh_evidence


def _validate_arkit_report(
    report_path: Path,
    vertex_count: int,
    face_count: int,
    classifications: np.ndarray,
) -> dict[str, Any]:
    report = load_json_strict(report_path)
    if report.get("schema") not in {
        "capture_splat.arkit_mesh_report.v0.1",
        "capture_splat.arkit_mesh_report.v0.2",
    }:
        raise ValueError("ARKit mesh report schema is unsupported")
    if (
        report.get("status") != "finite_mesh_written"
        or report.get("ply_written") is not True
        or report.get("non_finite_vertex_count") != 0
        or report.get("vertex_count") != vertex_count
        or report.get("triangle_count") != face_count
    ):
        raise ValueError("ARKit mesh report does not match the classified mesh")
    declared_counts = report.get("classification_counts")
    if isinstance(declared_counts, dict):
        if sum(value for value in declared_counts.values() if isinstance(value, int)) != face_count:
            raise ValueError("ARKit classification counts do not partition the source faces")
        actual = Counter(CLASS_NAMES.get(int(value), f"class_{int(value)}") for value in classifications)
        if dict(sorted(actual.items())) != dict(sorted(declared_counts.items())):
            raise ValueError("ARKit classification counts do not match the source mesh")
    return {
        "schema": report["schema"],
        "status": report["status"],
        "truncated": report.get("truncated") is True or report.get("budget_limited") is True,
        "semantic_scope": "local_geometric_support_only",
    }


def _classify_faces(
    tsdf_vertices: np.ndarray,
    tsdf_faces: np.ndarray,
    tsdf_face_normals: np.ndarray,
    arkit_vertices: np.ndarray,
    arkit_faces: np.ndarray,
    arkit_classifications: np.ndarray,
    *,
    maximum_distance: float,
    minimum_normal_dot: float,
    ambiguity_epsilon: float,
    batch_faces: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    o3d = _open3d()
    source_groups = list(sorted(CLASS_NAMES)) + [UNSUPPORTED_CLASSIFICATION]
    scenes: list[tuple[int, Any]] = []
    supported_source_class = np.isin(arkit_classifications, tuple(sorted(CLASS_NAMES)))
    for classification in source_groups:
        mask = (
            ~supported_source_class
            if classification == UNSUPPORTED_CLASSIFICATION
            else arkit_classifications == classification
        )
        group_faces = arkit_faces[mask]
        if not len(group_faces):
            continue
        used, compact = np.unique(group_faces.reshape(-1), return_inverse=True)
        legacy = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(arkit_vertices[used]),
            o3d.utility.Vector3iVector(compact.reshape(-1, 3).astype(np.int32, copy=False)),
        )
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(legacy))
        scenes.append((classification, scene))
    if not scenes:
        raise ValueError("classified ARKit mesh has no nearest-surface query groups")
    labels = np.full(len(tsdf_faces), UNKNOWN_CLASSIFICATION, dtype=np.uint8)
    support = np.zeros(len(tsdf_faces), dtype=np.uint8)
    unknown_reasons: Counter[str] = Counter()
    transferred_counts: Counter[str] = Counter()
    distance_sum = 0.0
    normal_dot_sum = 0.0
    sample_count = 0
    maximum_observed_distance = 0.0
    minimum_observed_normal_dot = 1.0
    ambiguous_sample_count = 0

    for start in range(0, len(tsdf_faces), batch_faces):
        stop = min(len(tsdf_faces), start + batch_faces)
        face_vertices = tsdf_vertices[tsdf_faces[start:stop]]
        samples = np.concatenate((face_vertices.mean(axis=1, keepdims=True), face_vertices), axis=1)
        flat_samples = np.ascontiguousarray(samples.reshape(-1, 3), dtype=np.float32)
        query = o3d.core.Tensor(flat_samples)
        best_distance = np.full(len(flat_samples), math.inf, dtype=np.float64)
        second_distance = np.full(len(flat_samples), math.inf, dtype=np.float64)
        best_class = np.full(len(flat_samples), UNSUPPORTED_CLASSIFICATION, dtype=np.uint16)
        best_normal = np.zeros((len(flat_samples), 3), dtype=np.float64)
        for classification, scene in scenes:
            closest = scene.compute_closest_points(query)
            closest_points = np.asarray(closest["points"].numpy(), dtype=np.float64)
            distances = np.linalg.norm(flat_samples - closest_points, axis=1)
            source_normals = np.asarray(closest["primitive_normals"].numpy(), dtype=np.float64)
            better = distances < best_distance
            second_distance = np.where(better, best_distance, np.minimum(second_distance, distances))
            best_distance = np.where(better, distances, best_distance)
            best_class[better] = classification
            best_normal[better] = source_normals[better]
        distances = best_distance.reshape(-1, SAMPLES_PER_FACE)
        classes = best_class.reshape(-1, SAMPLES_PER_FACE)
        source_normals = best_normal.reshape(-1, SAMPLES_PER_FACE, 3)
        normal_dots = np.abs(
            np.sum(tsdf_face_normals[start:stop, None, :] * source_normals, axis=2)
        )
        ambiguous = (
            np.isfinite(second_distance)
            & ((second_distance - best_distance) <= ambiguity_epsilon)
        ).reshape(-1, SAMPLES_PER_FACE)
        ambiguous_sample_count += int(np.count_nonzero(ambiguous))
        known = np.isin(classes, tuple(sorted(KNOWN_CLASSIFICATIONS)))
        geometrically_supported = (
            ~ambiguous & (distances <= maximum_distance) & (normal_dots >= minimum_normal_dot)
        )
        support[start:stop] = np.sum(known & geometrically_supported, axis=1).astype(np.uint8)

        distance_ok = np.all(distances <= maximum_distance, axis=1)
        normal_ok = np.all(normal_dots >= minimum_normal_dot, axis=1)
        ambiguity_ok = ~np.any(ambiguous, axis=1)
        known_ok = np.all(known, axis=1)
        consensus = np.all(classes == classes[:, :1], axis=1)
        transferred = distance_ok & normal_ok & ambiguity_ok & known_ok & consensus
        batch_labels = labels[start:stop]
        batch_labels[transferred] = classes[transferred, 0]
        labels[start:stop] = batch_labels

        reasons = np.full(stop - start, "classification_disagreement", dtype=object)
        reasons[~distance_ok] = "distance_unsupported"
        reasons[distance_ok & ~normal_ok] = "normal_unsupported"
        semantic_rows = distance_ok & normal_ok
        reasons[semantic_rows & ~ambiguity_ok] = "nearest_class_ambiguous"
        semantic_rows &= ambiguity_ok
        all_none = np.all(classes == 0, axis=1)
        any_none = np.any(classes == 0, axis=1)
        unsupported_class = np.any(classes == UNSUPPORTED_CLASSIFICATION, axis=1)
        reasons[semantic_rows & all_none] = "source_none"
        reasons[semantic_rows & ~all_none & any_none] = "ambiguous_with_none"
        reasons[semantic_rows & unsupported_class] = "unsupported_source_classification"
        for value, count in zip(*np.unique(reasons[~transferred], return_counts=True)):
            unknown_reasons[str(value)] += int(count)
        for value, count in zip(*np.unique(batch_labels[transferred], return_counts=True)):
            transferred_counts[CLASS_NAMES[int(value)]] += int(count)

        finite = np.isfinite(distances) & np.isfinite(normal_dots)
        if np.any(finite):
            distance_sum += float(np.sum(distances[finite], dtype=np.float64))
            normal_dot_sum += float(np.sum(normal_dots[finite], dtype=np.float64))
            sample_count += int(np.count_nonzero(finite))
            maximum_observed_distance = max(
                maximum_observed_distance, float(np.max(distances[finite]))
            )
            minimum_observed_normal_dot = min(
                minimum_observed_normal_dot, float(np.min(normal_dots[finite]))
            )

    transferred_count = int(np.count_nonzero(labels != UNKNOWN_CLASSIFICATION))
    unknown_count = len(labels) - transferred_count
    if transferred_count + unknown_count != len(labels) or sum(unknown_reasons.values()) != unknown_count:
        raise RuntimeError("semantic face partition invariant failed")
    return labels, support, {
        "face_count": len(labels),
        "transferred_face_count": transferred_count,
        "unknown_face_count": unknown_count,
        "transferred_fraction": transferred_count / len(labels) if len(labels) else 0.0,
        "unknown_fraction": unknown_count / len(labels) if len(labels) else 0.0,
        "transferred_classification_counts": dict(sorted(transferred_counts.items())),
        "unknown_reason_counts": dict(sorted(unknown_reasons.items())),
        "partition_invariant": transferred_count + unknown_count == len(labels),
        "sample_statistics": {
            "finite_sample_count": sample_count,
            "mean_nearest_distance_meters": distance_sum / sample_count if sample_count else None,
            "maximum_nearest_distance_meters": maximum_observed_distance if sample_count else None,
            "mean_absolute_normal_dot": normal_dot_sum / sample_count if sample_count else None,
            "minimum_absolute_normal_dot": minimum_observed_normal_dot if sample_count else None,
            "ambiguous_sample_count": ambiguous_sample_count,
        },
    }


def _bounds(vertices: np.ndarray) -> dict[str, list[float]] | None:
    if not len(vertices):
        return None
    minimum = vertices.min(axis=0)
    maximum = vertices.max(axis=0)
    return {
        "minimum": minimum.tolist(),
        "maximum": maximum.tolist(),
        "extent": (maximum - minimum).tolist(),
    }


def _partition_geometry(
    vertices: np.ndarray,
    faces: np.ndarray,
    labels: np.ndarray,
) -> dict[str, Any]:
    o3d = _open3d()
    area_vectors = np.cross(
        vertices[faces[:, 1]] - vertices[faces[:, 0]],
        vertices[faces[:, 2]] - vertices[faces[:, 0]],
    )
    face_areas = 0.5 * np.linalg.norm(area_vectors, axis=1)
    partitions: dict[str, Any] = {}
    partition_specs = [
        (classification, CLASS_NAMES[classification])
        for classification in sorted(KNOWN_CLASSIFICATIONS)
    ] + [(UNKNOWN_CLASSIFICATION, "unknown")]
    for classification, name in partition_specs:
        selected = np.flatnonzero(labels == classification)
        if not len(selected):
            partitions[name] = {
                "classification_value": classification,
                "face_count": 0,
                "area_square_meters": 0.0,
                "bounds_meters": None,
                "connected_component_count": 0,
                "largest_component_face_count": 0,
                "largest_component_area_square_meters": 0.0,
            }
            continue
        selected_faces = faces[selected]
        used, compact = np.unique(selected_faces.reshape(-1), return_inverse=True)
        mesh = o3d.geometry.TriangleMesh(
            o3d.utility.Vector3dVector(vertices[used]),
            o3d.utility.Vector3iVector(compact.reshape(-1, 3).astype(np.int32, copy=False)),
        )
        _, component_counts, component_areas = mesh.cluster_connected_triangles()
        counts = np.asarray(component_counts, dtype=np.int64)
        areas = np.asarray(component_areas, dtype=np.float64)
        partitions[name] = {
            "classification_value": classification,
            "face_count": int(len(selected)),
            "area_square_meters": float(np.sum(face_areas[selected], dtype=np.float64)),
            "bounds_meters": _bounds(vertices[used]),
            "connected_component_count": int(len(counts)),
            "largest_component_face_count": int(counts.max()) if len(counts) else 0,
            "largest_component_area_square_meters": float(areas.max()) if len(areas) else 0.0,
        }
    partition_face_count = sum(item["face_count"] for item in partitions.values())
    partition_area = sum(item["area_square_meters"] for item in partitions.values())
    total_area = float(np.sum(face_areas, dtype=np.float64))
    if partition_face_count != len(faces) or not math.isclose(
        partition_area, total_area, rel_tol=1e-12, abs_tol=1e-12
    ):
        raise RuntimeError("semantic geometry partition invariant failed")
    return {
        "component_connectivity": "same_partition_faces_sharing_an_undirected_mesh_edge",
        "partitions": partitions,
        "partition_face_count": partition_face_count,
        "total_surface_area_square_meters": total_area,
        "partition_area_square_meters": partition_area,
        "face_partition_invariant": True,
        "area_partition_invariant": True,
    }


def _write_surface(
    path: Path,
    vertex_bytes: bytes,
    faces: np.ndarray,
    classifications: np.ndarray,
    support: np.ndarray,
) -> None:
    source_mapping = "property uint source_face_index\n"
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment Capture Splat hybrid structural evidence; no physics authority\n"
        f"element vertex {len(vertex_bytes) // _TSDF_VERTEX_DTYPE.itemsize}\n"
        "property double x\nproperty double y\nproperty double z\n"
        "property double nx\nproperty double ny\nproperty double nz\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\n"
        "property uchar semantic_classification\n"
        "property uchar semantic_support\n"
        f"{source_mapping}"
        "end_header\n"
    ).encode("ascii")
    record_dtype = np.dtype([
        ("count", "u1"),
        ("indices", "<u4", (3,)),
        ("classification", "u1"),
        ("support", "u1"),
        ("source_face_index", "<u4"),
    ])
    records = np.empty(len(faces), dtype=record_dtype)
    records["count"] = 3
    records["indices"] = faces.astype(np.uint32, copy=False)
    records["classification"] = classifications
    records["support"] = support
    records["source_face_index"] = np.arange(len(faces), dtype=np.uint32)
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(vertex_bytes)
        records.tofile(handle)


def _verify_unchanged(root: Path, evidence: dict[str, Any], label: str) -> None:
    _verify_evidence(root, evidence, label)


def build_hybrid_surface(
    handoff: Path,
    tsdf_report: Path,
    out_dir: Path,
    *,
    maximum_distance: float = 0.06,
    minimum_normal_dot: float = 0.8,
    ambiguity_epsilon: float = 0.00001,
    collider_triangle_budget: int = 60_000,
    batch_faces: int = 8_192,
) -> dict[str, Any]:
    raw_handoff = handoff.absolute()
    if raw_handoff.is_symlink():
        raise ValueError("handoff input must not be a symbolic link")
    handoff = raw_handoff.resolve()
    manifest_path = handoff / MANIFEST_NAME if handoff.is_dir() else handoff
    root = manifest_path.parent
    tsdf_report_path, tsdf_report_evidence = _external_file_evidence(
        tsdf_report, "TSDF report"
    )
    tsdf_root = tsdf_report_path.parent
    raw_out_dir = out_dir.absolute()
    if raw_out_dir.is_symlink():
        raise ValueError("hybrid output must not be a symbolic link")
    out_dir = raw_out_dir.resolve()
    for input_root in (root, tsdf_root):
        try:
            out_dir.relative_to(input_root)
        except ValueError:
            continue
        raise ValueError("hybrid output must be outside every immutable input directory")
    if (
        not math.isfinite(maximum_distance)
        or maximum_distance <= 0
        or not math.isfinite(minimum_normal_dot)
        or not 0 < minimum_normal_dot <= 1
        or not math.isfinite(ambiguity_epsilon)
        or ambiguity_epsilon < 0
        or collider_triangle_budget <= 0
        or batch_faces <= 0
    ):
        raise ValueError("hybrid thresholds and budgets must be positive and finite")
    if out_dir.exists():
        metadata = out_dir.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("hybrid output must be a regular directory")
        if any(out_dir.iterdir()):
            raise FileExistsError(f"hybrid output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / HYBRID_REPORT_NAME
    authority = _false_authority()
    report: dict[str, Any] = {
        "schema": HYBRID_REPORT_SCHEMA,
        "status": "rejected",
        "decision": "reject",
        "reason": "structural_validation_failed",
        "authority": authority,
    }
    try:
        _require_regular_file(root, manifest_path, "handoff manifest")
        handoff_evidence = _file_evidence(manifest_path, root)
        handoff_data = load_json_strict(manifest_path)
        if (
            not isinstance(handoff_data, dict)
            or handoff_data.get("schema") != HANDOFF_SCHEMA
            or handoff_data.get("status") != "visual_evidence_with_3dgs_proposal"
        ):
            raise ValueError("hybrid compilation requires a canonical v0.3 visual-evidence handoff")
        handoff_authority = handoff_data.get("authority")
        if not isinstance(handoff_authority, dict) or any(
            handoff_authority.get(name) is not False
            for name in (
                "metric_authority",
                "semantic_authority",
                "collision_authority",
                "navigation_authority",
                "quality_claim",
            )
        ):
            raise ValueError("handoff grants authority that the hybrid compiler cannot consume")
        assets = handoff_data.get("assets")
        if not isinstance(assets, dict):
            raise ValueError("handoff assets are missing")
        capture_path, capture_ref = _asset_reference(
            root, assets.get("capture_manifest"), "capture manifest"
        )
        if capture_path != root / "capture.json":
            raise ValueError("handoff capture manifest must be capture.json at the package root")
        sparse = assets.get("colmap_sparse")
        if not isinstance(sparse, dict):
            raise ValueError("handoff COLMAP sparse assets are missing")
        _, colmap_images_ref = _asset_reference(
            root, sparse.get("images.txt"), "COLMAP images.txt"
        )
        capture = load_capture(root)
        coordinate_declaration = _metric_coordinate_declaration(capture)
        registration = _validate_registration(handoff_data)

        for name in ("navigation_mesh", "mesh_report"):
            reference = assets.get(name)
            if (
                not isinstance(reference, dict)
                or reference.get("coordinate_frame") != "arkit_world"
                or reference.get("units") != "meters"
            ):
                raise ValueError(f"handoff {name} coordinate contract is invalid")
        arkit_path, arkit_ref = _asset_reference(
            root, assets.get("navigation_mesh"), "navigation mesh"
        )
        arkit_report_path, arkit_report_ref = _asset_reference(
            root, assets.get("mesh_report"), "navigation mesh report"
        )
        tsdf_data, tsdf_mesh_path, tsdf_mesh_ref = _validate_tsdf_report(
            tsdf_report_path,
            handoff_evidence,
            capture_ref,
            colmap_images_ref,
            coordinate_declaration,
        )

        vertex_bytes, tsdf_vertices, _, tsdf_faces, tsdf_face_normals = _load_tsdf_mesh(
            tsdf_mesh_path
        )
        tsdf_mesh_report = tsdf_data["mesh"]
        if (
            tsdf_mesh_report.get("vertex_count") != len(tsdf_vertices)
            or tsdf_mesh_report.get("triangle_count") != len(tsdf_faces)
        ):
            raise ValueError("TSDF mesh counts do not match its strict report")
        arkit_vertices, arkit_faces, arkit_classes = _load_mesh(arkit_path)
        if (
            not len(arkit_vertices)
            or not len(arkit_faces)
            or not np.all(np.isfinite(arkit_vertices))
            or np.any(arkit_faces < 0)
            or np.any(arkit_faces >= len(arkit_vertices))
        ):
            raise ValueError("classified ARKit mesh is invalid")
        arkit_areas = np.linalg.norm(
            np.cross(
                arkit_vertices[arkit_faces[:, 1]] - arkit_vertices[arkit_faces[:, 0]],
                arkit_vertices[arkit_faces[:, 2]] - arkit_vertices[arkit_faces[:, 0]],
            ),
            axis=1,
        )
        if np.any(~np.isfinite(arkit_areas)) or np.any(arkit_areas <= 1e-12):
            raise ValueError("classified ARKit mesh contains non-finite or degenerate triangles")
        arkit_report = _validate_arkit_report(
            arkit_report_path, len(arkit_vertices), len(arkit_faces), arkit_classes
        )

        classifications, semantic_support, semantic = _classify_faces(
            tsdf_vertices,
            tsdf_faces,
            tsdf_face_normals,
            arkit_vertices,
            arkit_faces,
            arkit_classes,
            maximum_distance=maximum_distance,
            minimum_normal_dot=minimum_normal_dot,
            ambiguity_epsilon=ambiguity_epsilon,
            batch_faces=batch_faces,
        )
        semantic_geometry = _partition_geometry(tsdf_vertices, tsdf_faces, classifications)
        semantic["geometry"] = semantic_geometry
        hybrid_path = out_dir / HYBRID_MESH_NAME
        collider_path = out_dir / COLLIDER_MESH_NAME
        _write_surface(
            hybrid_path,
            vertex_bytes,
            tsdf_faces,
            classifications,
            semantic_support,
        )
        _write_surface(
            collider_path,
            vertex_bytes,
            tsdf_faces,
            classifications,
            semantic_support,
        )

        _verify_unchanged(root, handoff_evidence, "handoff manifest after compilation")
        _verify_unchanged(root, capture_ref, "capture manifest after compilation")
        _verify_unchanged(root, colmap_images_ref, "COLMAP images after compilation")
        _verify_unchanged(root, arkit_ref, "navigation mesh after compilation")
        _verify_unchanged(root, arkit_report_ref, "navigation mesh report after compilation")
        _verify_unchanged(tsdf_root, tsdf_report_evidence, "TSDF report after compilation")
        _verify_unchanged(tsdf_root, tsdf_mesh_ref, "TSDF mesh after compilation")

        topology = {
            "source_vertex_count": len(tsdf_vertices),
            "source_triangle_count": len(tsdf_faces),
            "output_vertex_count": len(tsdf_vertices),
            "output_triangle_count": len(tsdf_faces),
            "vertex_records_copied_byte_for_byte": True,
            "triangle_indices_preserved_in_source_order": True,
            "source_face_index_mapping": "identity_zero_based",
            "synthetic_geometry_added": False,
            "fallback_floor_added": False,
            "simplification_applied": False,
        }
        report.update({
            "status": "held",
            "decision": "hold",
            "reason": "semantic_transfer_candidate_requires_opening_continuity_and_physical_validation",
            "inputs": {
                "handoff_manifest": handoff_evidence,
                "handoff_schema": HANDOFF_SCHEMA,
                "capture_manifest": capture_ref,
                "colmap_images": colmap_images_ref,
                "tsdf_report": tsdf_report_evidence,
                "tsdf_mesh": tsdf_mesh_ref,
                "navigation_mesh": arkit_ref,
                "navigation_mesh_report": arkit_report_ref,
                "navigation_mesh_evidence": arkit_report,
                "registration": registration,
            },
            "coordinate_contract": {
                "coordinate_frame": "arkit_world",
                "units": "meters",
                "capture_declaration": coordinate_declaration,
                "tsdf_and_arkit_share_input_frame": True,
            },
            "parameters": {
                "sample_points_per_face": SAMPLES_PER_FACE,
                "sample_pattern": "centroid_and_three_vertices",
                "maximum_nearest_distance_meters_inclusive": maximum_distance,
                "minimum_absolute_normal_dot_inclusive": minimum_normal_dot,
                "nearest_class_ambiguity_epsilon_meters_inclusive": ambiguity_epsilon,
                "transfer_consensus": "all_samples_known_and_unanimous_class",
                "unknown_classification_value": UNKNOWN_CLASSIFICATION,
                "semantic_support_value": "count_of_geometrically_supported_known_samples_0_to_4",
                "query_batch_faces": batch_faces,
            },
            "topology": topology,
            "semantics": semantic,
            "surface_evidence": {
                "floor": semantic["transferred_classification_counts"].get("floor", 0),
                "wall": semantic["transferred_classification_counts"].get("wall", 0),
                "ceiling_or_overhang": semantic["transferred_classification_counts"].get("ceiling", 0),
                "door_surface": semantic["transferred_classification_counts"].get("door", 0),
                "unknown": semantic["unknown_face_count"],
                "door_surface_is_not_opening_clearance": True,
                "free_space": {
                    "status": "unavailable",
                    "reason": "surface_only_compiler_does_not_infer_free_space",
                },
            },
            "rails": {
                "wall_and_opening_continuity": {
                    "status": "held",
                    "reason": "weak_source_rail_and_no_opening_clearance_probe",
                },
                "doorway_clearance": {
                    "status": "held",
                    "reason": "door_label_is_surface_evidence_not_traversable_opening_proof",
                },
                "unknown_coverage": {
                    "status": "held" if semantic["unknown_face_count"] else "software_only_complete",
                    "unknown_face_count": semantic["unknown_face_count"],
                },
                "physical_validation": {
                    "status": "pending",
                    "reason": "known_distance_and_collision_probes_not_run",
                },
            },
            "output": {
                "hybrid_surface": _file_evidence(hybrid_path, out_dir),
            },
        })
        write_json_strict(report_path, report)

        budget_exceeded = len(tsdf_faces) > collider_triangle_budget
        collider_report = {
            "schema": COLLIDER_REPORT_SCHEMA,
            "status": "held",
            "decision": "hold",
            "reason": (
                "triangle_budget_exceeded_without_simplification"
                if budget_exceeded
                else "opening_clearance_continuity_and_physical_probes_pending"
            ),
            "authority": _false_authority(),
            "inputs": {
                "hybrid_surface": _file_evidence(hybrid_path, out_dir),
                "hybrid_report": _file_evidence(report_path, out_dir),
                "tsdf_mesh": tsdf_mesh_ref,
            },
            "candidate": _file_evidence(collider_path, out_dir),
            "coordinate_contract": {
                "coordinate_frame": "arkit_world",
                "units": "meters",
            },
            "topology": topology,
            "triangle_budget": {
                "limit": collider_triangle_budget,
                "observed": len(tsdf_faces),
                "status": "exceeded" if budget_exceeded else "within",
                "simplification_applied": False,
            },
            "semantic_partition": {
                "transferred_face_count": semantic["transferred_face_count"],
                "unknown_face_count": semantic["unknown_face_count"],
                "partition_invariant": semantic["partition_invariant"],
            },
            "rails": {
                "doorway_clearance": "held_unresolved",
                "wall_and_opening_continuity": "held_weak",
                "unknown_coverage": "held" if semantic["unknown_face_count"] else "software_only_complete",
                "physical_collision_probes": "pending_none_recorded",
                "fallback_floor": "not_added",
                "synthetic_geometry": "not_added",
            },
        }
        write_json_strict(out_dir / COLLIDER_REPORT_NAME, collider_report)
    except Exception as error:
        report["error"] = str(error)
        report["error_type"] = type(error).__name__
        write_json_strict(report_path, report)
        raise
    return report
