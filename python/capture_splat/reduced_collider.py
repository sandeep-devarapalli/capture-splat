from __future__ import annotations

import math
import stat
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from .collision_candidate import CLASS_NAMES
from .hybrid_surface import (
    COLLIDER_MESH_NAME,
    COLLIDER_REPORT_NAME,
    COLLIDER_REPORT_SCHEMA,
    HYBRID_MESH_NAME,
    HYBRID_REPORT_NAME,
    HYBRID_REPORT_SCHEMA,
    KNOWN_CLASSIFICATIONS,
    UNKNOWN_CLASSIFICATION,
    _TSDF_VERTEX_DTYPE,
    _TSDF_VERTEX_PROPERTIES,
    _digest_json,
    _external_file_evidence,
    _false_authority,
)
from .json_utils import load_json_strict, write_json_strict
from .ply_stats import _parse_header
from .rgbd_tsdf import _file_evidence, _open3d, _require_regular_file, _sha256

REDUCED_MESH_NAME = "reduced_collider_candidate.ply"
REDUCED_REPORT_NAME = "capture_splat_reduced_collider_report.json"
PROBE_REPORT_NAME = "capture_splat_collision_probe_report.json"
REDUCED_REPORT_SCHEMA = "capture_splat.reduced_hybrid_collider.v0.1"
PROBE_REPORT_SCHEMA = "capture_splat.collision_probe.v0.1"
SAMPLES_PER_FACE = 4
SOURCE_MINIMUM_DOUBLE_AREA = 1e-12
REDUCED_MINIMUM_AREA = 1e-12

_SOURCE_FACE_PROPERTIES = [
    {
        "kind": "list",
        "count_type": "uchar",
        "value_type": "uint",
        "name": "vertex_indices",
    },
    {"kind": "scalar", "type": "uchar", "name": "semantic_classification"},
    {"kind": "scalar", "type": "uchar", "name": "semantic_support"},
    {"kind": "scalar", "type": "uint", "name": "source_face_index"},
]
_SOURCE_FACE_DTYPE = np.dtype([
    ("count", "u1"),
    ("indices", "<u4", (3,)),
    ("classification", "u1"),
    ("support", "u1"),
    ("source_face_index", "<u4"),
])
_OUTPUT_FACE_DTYPE = _SOURCE_FACE_DTYPE


def _verify_reference(
    root: Path,
    reference: Any,
    expected_name: str,
    label: str,
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(reference, dict) or reference.get("path") != expected_name:
        raise ValueError(f"{label} reference is invalid")
    if (
        not isinstance(reference.get("size_bytes"), int)
        or reference["size_bytes"] < 0
        or not isinstance(reference.get("checksum"), str)
        or len(reference["checksum"]) != 71
        or not reference["checksum"].startswith("sha256:")
    ):
        raise ValueError(f"{label} evidence is invalid")
    path = root / expected_name
    _require_regular_file(root, path, label)
    if path.lstat().st_size != reference["size_bytes"] or _sha256(path) != reference["checksum"]:
        raise ValueError(f"{label} does not match its size and checksum")
    return path, dict(reference)


def _validate_inputs(
    report_path: Path,
    report_evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any]]:
    root = report_path.parent
    hybrid = load_json_strict(report_path)
    if (
        not isinstance(hybrid, dict)
        or hybrid.get("schema") != HYBRID_REPORT_SCHEMA
        or hybrid.get("status") != "held"
        or hybrid.get("decision") != "hold"
    ):
        raise ValueError("source hybrid report is not a held v0.1 candidate")
    authority = hybrid.get("authority")
    if not isinstance(authority, dict) or not authority or any(authority.values()):
        raise ValueError("source hybrid report grants unsupported authority")
    coordinate = hybrid.get("coordinate_contract")
    if (
        not isinstance(coordinate, dict)
        or coordinate.get("coordinate_frame") != "arkit_world"
        or coordinate.get("units") != "meters"
        or coordinate.get("tsdf_and_arkit_share_input_frame") is not True
    ):
        raise ValueError("source hybrid coordinate contract is invalid")
    topology = hybrid.get("topology")
    if (
        not isinstance(topology, dict)
        or topology.get("vertex_records_copied_byte_for_byte") is not True
        or topology.get("triangle_indices_preserved_in_source_order") is not True
        or topology.get("source_face_index_mapping") != "identity_zero_based"
        or topology.get("simplification_applied") is not False
        or topology.get("synthetic_geometry_added") is not False
        or topology.get("fallback_floor_added") is not False
    ):
        raise ValueError("source hybrid topology contract is invalid")
    output = hybrid.get("output")
    source_path, source_evidence = _verify_reference(
        root,
        output.get("hybrid_surface") if isinstance(output, dict) else None,
        HYBRID_MESH_NAME,
        "hybrid surface",
    )
    collider_report_path = root / COLLIDER_REPORT_NAME
    _require_regular_file(root, collider_report_path, "hybrid collider report")
    collider_report_evidence = _file_evidence(collider_report_path, root)
    collider = load_json_strict(collider_report_path)
    if (
        not isinstance(collider, dict)
        or collider.get("schema") != COLLIDER_REPORT_SCHEMA
        or collider.get("status") != "held"
        or collider.get("decision") != "hold"
    ):
        raise ValueError("source collider report is not a held v0.1 candidate")
    collider_authority = collider.get("authority")
    if not isinstance(collider_authority, dict) or not collider_authority or any(
        collider_authority.values()
    ):
        raise ValueError("source collider report grants unsupported authority")
    collider_coordinate = collider.get("coordinate_contract")
    if collider_coordinate != {"coordinate_frame": "arkit_world", "units": "meters"}:
        raise ValueError("source collider coordinate contract is invalid")
    collider_topology = collider.get("topology")
    if collider_topology != topology:
        raise ValueError("source collider and hybrid topology contracts disagree")
    inputs = collider.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("source collider input bindings are missing")
    if inputs.get("hybrid_surface") != source_evidence or inputs.get(
        "hybrid_report"
    ) != report_evidence:
        raise ValueError("source collider is not bound to the exact hybrid candidate")
    collider_path, collider_evidence = _verify_reference(
        root,
        collider.get("candidate"),
        COLLIDER_MESH_NAME,
        "unsimplified collider",
    )
    if (
        collider_evidence["checksum"] != source_evidence["checksum"]
        or collider_evidence["size_bytes"] != source_evidence["size_bytes"]
    ):
        raise ValueError("unsimplified collider is not byte-derived from the hybrid surface")
    return hybrid, collider, source_path, {
        "hybrid_report": report_evidence,
        "hybrid_surface": source_evidence,
        "unsimplified_collider_report": collider_report_evidence,
        "unsimplified_collider": collider_evidence,
    }


def _load_source_surface(
    path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        header, data_offset = _parse_header(handle)
        if header["format"] != "binary_little_endian":
            raise ValueError("hybrid surface must be binary little-endian PLY")
        if [item["name"] for item in header["elements"]] != ["vertex", "face"]:
            raise ValueError("hybrid surface must contain only vertex and face elements")
        vertex, face = header["elements"]
        if vertex["properties"] != _TSDF_VERTEX_PROPERTIES:
            raise ValueError("hybrid surface vertex layout is invalid")
        if face["properties"] != _SOURCE_FACE_PROPERTIES:
            raise ValueError("hybrid surface face layout is invalid")
        handle.seek(data_offset)
        vertex_bytes = handle.read(int(vertex["count"]) * _TSDF_VERTEX_DTYPE.itemsize)
        face_bytes = handle.read(int(face["count"]) * _SOURCE_FACE_DTYPE.itemsize)
        if len(vertex_bytes) != int(vertex["count"]) * _TSDF_VERTEX_DTYPE.itemsize:
            raise ValueError("hybrid surface vertex data ended early")
        if len(face_bytes) != int(face["count"]) * _SOURCE_FACE_DTYPE.itemsize:
            raise ValueError("hybrid surface face data ended early")
        if handle.read(1):
            raise ValueError("hybrid surface has undeclared trailing data")
    vertex_records = np.frombuffer(vertex_bytes, dtype=_TSDF_VERTEX_DTYPE)
    vertices = vertex_records["position"].copy()
    records = np.frombuffer(face_bytes, dtype=_SOURCE_FACE_DTYPE)
    if np.any(records["count"] != 3):
        raise ValueError("hybrid surface faces must be triangles")
    faces = records["indices"].astype(np.int64)
    labels = records["classification"].copy()
    support = records["support"].copy()
    source_mapping = records["source_face_index"].copy()
    if (
        not len(vertices)
        or not len(faces)
        or not np.all(np.isfinite(vertices))
        or not np.all(np.isfinite(vertex_records["normal"]))
    ):
        raise ValueError("hybrid surface must contain finite geometry")
    if len(vertices) > np.iinfo(np.uint32).max or len(faces) > np.iinfo(np.uint32).max:
        raise ValueError("hybrid surface exceeds the uint32 PLY topology domain")
    if np.any(faces < 0) or np.any(faces >= len(vertices)):
        raise ValueError("hybrid surface contains out-of-range face indices")
    face_normals, areas = _face_geometry(vertices, faces)
    if np.any(~np.isfinite(areas)) or np.any(areas * 2.0 <= SOURCE_MINIMUM_DOUBLE_AREA):
        raise ValueError("hybrid surface contains non-finite or degenerate faces")
    allowed = np.asarray([*sorted(KNOWN_CLASSIFICATIONS), UNKNOWN_CLASSIFICATION], dtype=np.uint8)
    if not np.all(np.isin(labels, allowed)):
        raise ValueError("hybrid surface contains unsupported semantic classifications")
    if np.any(support > SAMPLES_PER_FACE):
        raise ValueError("hybrid surface semantic support is invalid")
    if np.any((labels != UNKNOWN_CLASSIFICATION) & (support != SAMPLES_PER_FACE)):
        raise ValueError("known hybrid classifications must have complete semantic support")
    if not np.array_equal(source_mapping, np.arange(len(faces), dtype=np.uint32)):
        raise ValueError("hybrid surface source-face mapping is not identity zero-based")
    return vertices, faces, labels, face_normals, areas


def _face_geometry(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cross = np.cross(
        vertices[faces[:, 1]] - vertices[faces[:, 0]],
        vertices[faces[:, 2]] - vertices[faces[:, 0]],
    )
    lengths = np.linalg.norm(cross, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        normals = cross / lengths[:, None]
    return normals, lengths * 0.5


def _legacy_mesh(o3d: Any, vertices: np.ndarray, faces: np.ndarray) -> Any:
    return o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(vertices),
        o3d.utility.Vector3iVector(faces.astype(np.int32, copy=False)),
    )


def _simplify(
    o3d: Any,
    vertices: np.ndarray,
    faces: np.ndarray,
    max_faces: int,
    boundary_weight: float,
) -> tuple[np.ndarray, np.ndarray, bool]:
    if len(faces) <= max_faces:
        return vertices.copy(), faces.copy(), False
    reduced = _legacy_mesh(o3d, vertices, faces).simplify_quadric_decimation(
        target_number_of_triangles=max_faces,
        maximum_error=math.inf,
        boundary_weight=boundary_weight,
    )
    reduced.remove_unreferenced_vertices()
    output_vertices = np.asarray(reduced.vertices, dtype=np.float64).copy()
    output_faces = np.asarray(reduced.triangles, dtype=np.int64).copy()
    if not len(output_vertices) or not len(output_faces) or len(output_faces) > max_faces:
        raise ValueError("Open3D reducer did not produce a bounded non-empty candidate")
    if not np.all(np.isfinite(output_vertices)):
        raise ValueError("reduced collider contains non-finite vertices")
    if np.any(output_faces < 0) or np.any(output_faces >= len(output_vertices)):
        raise ValueError("reduced collider contains out-of-range face indices")
    _, areas = _face_geometry(output_vertices, output_faces)
    if np.any(~np.isfinite(areas)) or np.any(areas <= REDUCED_MINIMUM_AREA):
        raise ValueError("reduced collider contains non-finite or degenerate faces")
    return output_vertices, output_faces, True


def _class_scenes(
    o3d: Any,
    vertices: np.ndarray,
    faces: np.ndarray,
    labels: np.ndarray,
) -> list[tuple[int, Any, np.ndarray]]:
    scenes: list[tuple[int, Any, np.ndarray]] = []
    for classification in sorted(int(value) for value in np.unique(labels)):
        source_indexes = np.flatnonzero(labels == classification)
        selected = faces[source_indexes]
        used, compact = np.unique(selected.reshape(-1), return_inverse=True)
        mesh = _legacy_mesh(o3d, vertices[used], compact.reshape(-1, 3))
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(mesh))
        scenes.append((classification, scene, source_indexes))
    return scenes


def _face_samples(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    face_vertices = vertices[faces]
    return np.concatenate((face_vertices.mean(axis=1, keepdims=True), face_vertices), axis=1)


def _map_reduced_faces(
    o3d: Any,
    source_vertices: np.ndarray,
    source_faces: np.ndarray,
    source_labels: np.ndarray,
    reduced_vertices: np.ndarray,
    reduced_faces: np.ndarray,
    reduced_normals: np.ndarray,
    *,
    maximum_distance: float,
    minimum_normal_dot: float,
    ambiguity_epsilon: float,
    batch_faces: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any], dict[str, np.ndarray]]:
    scenes = _class_scenes(o3d, source_vertices, source_faces, source_labels)
    labels = np.full(len(reduced_faces), UNKNOWN_CLASSIFICATION, dtype=np.uint8)
    support = np.zeros(len(reduced_faces), dtype=np.uint8)
    representative = np.zeros(len(reduced_faces), dtype=np.uint32)
    all_distances = np.empty((len(reduced_faces), SAMPLES_PER_FACE), dtype=np.float64)
    all_normal_dots = np.empty_like(all_distances)
    all_classes = np.empty((len(reduced_faces), SAMPLES_PER_FACE), dtype=np.uint16)
    all_ambiguous = np.empty((len(reduced_faces), SAMPLES_PER_FACE), dtype=bool)
    all_source_indexes = np.empty((len(reduced_faces), SAMPLES_PER_FACE), dtype=np.uint32)

    for start in range(0, len(reduced_faces), batch_faces):
        stop = min(len(reduced_faces), start + batch_faces)
        samples = _face_samples(reduced_vertices, reduced_faces[start:stop])
        flat = np.ascontiguousarray(samples.reshape(-1, 3), dtype=np.float32)
        query = o3d.core.Tensor(flat)
        best_distance = np.full(len(flat), math.inf, dtype=np.float64)
        second_distance = np.full(len(flat), math.inf, dtype=np.float64)
        best_class = np.full(len(flat), UNKNOWN_CLASSIFICATION, dtype=np.uint16)
        best_source = np.zeros(len(flat), dtype=np.uint32)
        best_normal = np.zeros((len(flat), 3), dtype=np.float64)
        for classification, scene, source_indexes in scenes:
            closest = scene.compute_closest_points(query)
            closest_points = np.asarray(closest["points"].numpy(), dtype=np.float64)
            distances = np.linalg.norm(flat - closest_points, axis=1)
            primitive_ids = np.asarray(closest["primitive_ids"].numpy(), dtype=np.int64)
            source_normals = np.asarray(closest["primitive_normals"].numpy(), dtype=np.float64)
            better = distances < best_distance
            second_distance = np.where(better, best_distance, np.minimum(second_distance, distances))
            best_distance = np.where(better, distances, best_distance)
            best_class[better] = classification
            best_source[better] = source_indexes[primitive_ids[better]].astype(np.uint32)
            best_normal[better] = source_normals[better]
        distances = best_distance.reshape(-1, SAMPLES_PER_FACE)
        classes = best_class.reshape(-1, SAMPLES_PER_FACE)
        sources = best_source.reshape(-1, SAMPLES_PER_FACE)
        source_normals = best_normal.reshape(-1, SAMPLES_PER_FACE, 3)
        normal_dots = np.abs(
            np.sum(reduced_normals[start:stop, None, :] * source_normals, axis=2)
        )
        ambiguous = (
            np.isfinite(second_distance)
            & ((second_distance - best_distance) <= ambiguity_epsilon)
        ).reshape(-1, SAMPLES_PER_FACE)
        known = np.isin(classes, tuple(sorted(KNOWN_CLASSIFICATIONS)))
        supported = (
            known
            & ~ambiguous
            & (distances <= maximum_distance)
            & (normal_dots >= minimum_normal_dot)
        )
        transferred = (
            np.all(supported, axis=1)
            & np.all(classes == classes[:, :1], axis=1)
        )
        batch_labels = labels[start:stop]
        batch_labels[transferred] = classes[transferred, 0]
        labels[start:stop] = batch_labels
        support[start:stop] = np.sum(supported, axis=1).astype(np.uint8)
        representative[start:stop] = sources[:, 0]
        all_distances[start:stop] = distances
        all_normal_dots[start:stop] = normal_dots
        all_classes[start:stop] = classes
        all_ambiguous[start:stop] = ambiguous
        all_source_indexes[start:stop] = sources

    if np.any(representative >= len(source_faces)):
        raise RuntimeError("reduced collider source-face mapping escaped the source topology")
    transferred_counts = Counter(
        CLASS_NAMES[int(value)] for value in labels if int(value) in KNOWN_CLASSIFICATIONS
    )
    return labels, support, representative, {
        "mode": "centroid_representative_with_centroid_and_vertex_support",
        "sample_pattern": "centroid_and_three_vertices",
        "mapped_face_count": len(representative),
        "mapping_in_range": True,
        "semantic_support_range": [int(support.min()), int(support.max())],
        "ambiguous_sample_count": int(np.count_nonzero(all_ambiguous)),
        "transferred_classification_counts": dict(sorted(transferred_counts.items())),
        "unknown_face_count": int(np.count_nonzero(labels == UNKNOWN_CLASSIFICATION)),
    }, {
        "distances": all_distances,
        "normal_dots": all_normal_dots,
        "classes": all_classes,
        "source_indexes": all_source_indexes,
    }


def _scene(o3d: Any, vertices: np.ndarray, faces: np.ndarray) -> Any:
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.t.geometry.TriangleMesh.from_legacy(_legacy_mesh(o3d, vertices, faces)))
    return scene


def _source_to_reduced(
    o3d: Any,
    source_vertices: np.ndarray,
    source_faces: np.ndarray,
    source_normals: np.ndarray,
    reduced_vertices: np.ndarray,
    reduced_faces: np.ndarray,
    *,
    batch_faces: int,
) -> dict[str, np.ndarray]:
    scene = _scene(o3d, reduced_vertices, reduced_faces)
    distances = np.empty((len(source_faces), SAMPLES_PER_FACE), dtype=np.float64)
    normal_dots = np.empty_like(distances)
    primitive_ids = np.empty((len(source_faces), SAMPLES_PER_FACE), dtype=np.int64)
    for start in range(0, len(source_faces), batch_faces):
        stop = min(len(source_faces), start + batch_faces)
        samples = _face_samples(source_vertices, source_faces[start:stop])
        flat = np.ascontiguousarray(samples.reshape(-1, 3), dtype=np.float32)
        closest = scene.compute_closest_points(o3d.core.Tensor(flat))
        closest_points = np.asarray(closest["points"].numpy(), dtype=np.float64)
        closest_normals = np.asarray(closest["primitive_normals"].numpy(), dtype=np.float64)
        distances[start:stop] = np.linalg.norm(flat - closest_points, axis=1).reshape(
            -1, SAMPLES_PER_FACE
        )
        normal_dots[start:stop] = np.abs(
            np.sum(
                source_normals[start:stop, None, :]
                * closest_normals.reshape(-1, SAMPLES_PER_FACE, 3),
                axis=2,
            )
        )
        primitive_ids[start:stop] = np.asarray(
            closest["primitive_ids"].numpy(), dtype=np.int64
        ).reshape(-1, SAMPLES_PER_FACE)
    return {
        "distances": distances,
        "normal_dots": normal_dots,
        "primitive_ids": primitive_ids,
    }


def _metric_summary(distances: np.ndarray, normal_dots: np.ndarray) -> dict[str, Any]:
    flat_distances = distances.reshape(-1)
    flat_dots = normal_dots.reshape(-1)
    return {
        "sample_count": int(len(flat_distances)),
        "mean_distance_meters": float(np.mean(flat_distances, dtype=np.float64)),
        "p95_distance_meters": float(np.quantile(flat_distances, 0.95)),
        "p99_distance_meters": float(np.quantile(flat_distances, 0.99)),
        "maximum_distance_meters": float(np.max(flat_distances)),
        "minimum_absolute_normal_dot": float(np.min(flat_dots)),
        "mean_absolute_normal_dot": float(np.mean(flat_dots, dtype=np.float64)),
    }


def _comparison_metrics(
    source_labels: np.ndarray,
    reduced_labels: np.ndarray,
    forward: dict[str, np.ndarray],
    reverse: dict[str, np.ndarray],
) -> dict[str, Any]:
    source_by_class: dict[str, Any] = {}
    reduced_best_classes = reverse["classes"]
    reduced_by_class: dict[str, Any] = {}
    for classification in (1, 2, 7, UNKNOWN_CLASSIFICATION):
        name = CLASS_NAMES.get(classification, "unknown")
        source_mask = source_labels == classification
        if np.any(source_mask):
            source_by_class[name] = _metric_summary(
                forward["distances"][source_mask], forward["normal_dots"][source_mask]
            )
        reduced_mask = reduced_best_classes == classification
        if np.any(reduced_mask):
            reduced_by_class[name] = _metric_summary(
                reverse["distances"][reduced_mask], reverse["normal_dots"][reduced_mask]
            )
    return {
        "source_to_reduced": {
            "all": _metric_summary(forward["distances"], forward["normal_dots"]),
            "by_source_classification": source_by_class,
        },
        "reduced_to_source": {
            "all": _metric_summary(reverse["distances"], reverse["normal_dots"]),
            "by_nearest_source_classification": reduced_by_class,
        },
        "candidate_semantic_counts": _semantic_counts(reduced_labels),
    }


def _semantic_counts(labels: np.ndarray) -> dict[str, int]:
    counts = Counter(
        CLASS_NAMES.get(int(value), "unknown")
        if int(value) != UNKNOWN_CLASSIFICATION
        else "unknown"
        for value in labels
    )
    return dict(sorted(counts.items()))


def _partition_components(
    o3d: Any,
    vertices: np.ndarray,
    faces: np.ndarray,
    labels: np.ndarray,
) -> tuple[dict[str, Any], dict[int, np.ndarray]]:
    result: dict[str, Any] = {}
    largest_faces: dict[int, np.ndarray] = {}
    _, face_areas = _face_geometry(vertices, faces)
    for classification in (1, 2, 7):
        name = CLASS_NAMES[classification]
        selected = np.flatnonzero(labels == classification)
        if not len(selected):
            result[name] = {
                "face_count": 0,
                "area_square_meters": 0.0,
                "connected_component_count": 0,
                "largest_component_face_count": 0,
                "largest_component_area_square_meters": 0.0,
            }
            largest_faces[classification] = np.empty(0, dtype=np.int64)
            continue
        selected_faces = faces[selected]
        used, compact = np.unique(selected_faces.reshape(-1), return_inverse=True)
        mesh = _legacy_mesh(o3d, vertices[used], compact.reshape(-1, 3))
        component_ids, component_counts, component_areas = mesh.cluster_connected_triangles()
        component_ids = np.asarray(component_ids, dtype=np.int64)
        component_counts = np.asarray(component_counts, dtype=np.int64)
        component_areas = np.asarray(component_areas, dtype=np.float64)
        largest = int(np.argmax(component_areas))
        largest_faces[classification] = selected[component_ids == largest]
        result[name] = {
            "face_count": int(len(selected)),
            "area_square_meters": float(np.sum(face_areas[selected], dtype=np.float64)),
            "connected_component_count": int(len(component_counts)),
            "largest_component_face_count": int(component_counts[largest]),
            "largest_component_area_square_meters": float(component_areas[largest]),
        }
    return result, largest_faces


def _topology_metrics(o3d: Any, vertices: np.ndarray, faces: np.ndarray) -> dict[str, Any]:
    mesh = _legacy_mesh(o3d, vertices, faces)
    _, component_counts, component_areas = mesh.cluster_connected_triangles()
    counts = np.asarray(component_counts, dtype=np.int64)
    areas = np.asarray(component_areas, dtype=np.float64)
    edges = np.sort(
        np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])),
        axis=1,
    )
    _, edge_counts = np.unique(edges, axis=0, return_counts=True)
    return {
        "vertex_count": len(vertices),
        "triangle_count": len(faces),
        "connected_component_count": int(len(counts)),
        "largest_component_triangle_count": int(counts.max()),
        "largest_component_area_square_meters": float(areas.max()),
        "boundary_edge_count": int(np.count_nonzero(edge_counts == 1)),
        "non_manifold_edge_count_excluding_boundaries": int(np.count_nonzero(edge_counts > 2)),
        "non_manifold_vertex_count": len(mesh.get_non_manifold_vertices()),
        "edge_manifold_allowing_boundaries": bool(mesh.is_edge_manifold(True)),
        "vertex_manifold": bool(mesh.is_vertex_manifold()),
        "watertight": bool(mesh.is_watertight()),
        "orientable": bool(mesh.is_orientable()),
    }


def _boundary_samples(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    edges = np.sort(
        np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])),
        axis=1,
    )
    unique_edges, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = unique_edges[counts == 1]
    if not len(boundary):
        return np.empty((0, 3), dtype=np.float64)
    endpoints = vertices[boundary]
    samples = np.concatenate((endpoints[:, 0], endpoints[:, 1], endpoints.mean(axis=1)))
    return np.unique(samples, axis=0)


def _nearest_point_distances(o3d: Any, source: np.ndarray, target: np.ndarray) -> np.ndarray:
    if not len(source) or not len(target):
        return np.empty(0, dtype=np.float64)
    index = o3d.core.nns.NearestNeighborSearch(
        o3d.core.Tensor(np.ascontiguousarray(target, dtype=np.float32))
    )
    if not index.knn_index():
        raise RuntimeError("Open3D could not index boundary samples")
    _, squared = index.knn_search(
        o3d.core.Tensor(np.ascontiguousarray(source, dtype=np.float32)), 1
    )
    return np.sqrt(np.asarray(squared.numpy(), dtype=np.float64).reshape(-1))


def _boundary_metrics(
    o3d: Any,
    source_vertices: np.ndarray,
    source_faces: np.ndarray,
    reduced_vertices: np.ndarray,
    reduced_faces: np.ndarray,
) -> dict[str, Any]:
    source = _boundary_samples(source_vertices, source_faces)
    reduced = _boundary_samples(reduced_vertices, reduced_faces)

    def summary(distances: np.ndarray) -> dict[str, Any]:
        if not len(distances):
            return {"sample_count": 0, "p95_distance_meters": None, "maximum_distance_meters": None}
        return {
            "sample_count": int(len(distances)),
            "p95_distance_meters": float(np.quantile(distances, 0.95)),
            "maximum_distance_meters": float(np.max(distances)),
        }

    return {
        "sample_pattern": "unique_boundary_edge_endpoints_and_midpoints",
        "source_sample_count": int(len(source)),
        "reduced_sample_count": int(len(reduced)),
        "source_to_reduced_boundary": summary(_nearest_point_distances(o3d, source, reduced)),
        "reduced_to_source_boundary": summary(_nearest_point_distances(o3d, reduced, source)),
    }


def _ray_hit(o3d: Any, scene: Any, origin: np.ndarray, direction: np.ndarray) -> tuple[float, int]:
    ray = np.concatenate((origin, direction)).astype(np.float32, copy=False)[None, :]
    answer = scene.cast_rays(o3d.core.Tensor(ray))
    return float(answer["t_hit"].numpy()[0]), int(answer["primitive_ids"].numpy()[0])


def _surface_probe(
    o3d: Any,
    source_scene: Any,
    reduced_scene: Any,
    source_vertices: np.ndarray,
    source_faces: np.ndarray,
    source_labels: np.ndarray,
    source_normals: np.ndarray,
    source_areas: np.ndarray,
    reduced_labels: np.ndarray,
    largest_faces: dict[int, np.ndarray],
    classification: int,
    direction_mode: str,
) -> dict[str, Any]:
    candidates = largest_faces[classification]
    if not len(candidates):
        return {"status": "held", "reason": f"{CLASS_NAMES[classification]}_surface_missing"}
    if direction_mode == "down":
        candidates = candidates[np.abs(source_normals[candidates, 1]) >= 0.8]
    else:
        candidates = candidates[np.abs(source_normals[candidates, 1]) <= 0.3]
    if not len(candidates):
        return {"status": "held", "reason": f"{CLASS_NAMES[classification]}_orientation_invalid"}
    ordered = candidates[np.argsort(-source_areas[candidates], kind="stable")]
    if len(ordered) > 16:
        positions = np.linspace(0, len(ordered) - 1, 16, dtype=np.int64)
        selected_faces = ordered[positions]
    else:
        selected_faces = ordered
    samples: list[dict[str, Any]] = []
    for selected_value in selected_faces:
        selected = int(selected_value)
        center = source_vertices[source_faces[selected]].mean(axis=0)
        if direction_mode == "down":
            origin = center + np.asarray([0.0, 0.1, 0.0])
            direction = np.asarray([0.0, -1.0, 0.0])
        else:
            direction = source_normals[selected]
            origin = center - direction * 0.2
        source_t, source_face = _ray_hit(o3d, source_scene, origin, direction)
        reduced_t, reduced_face = _ray_hit(o3d, reduced_scene, origin, direction)
        source_valid = math.isfinite(source_t) and source_face < len(source_labels)
        reduced_valid = math.isfinite(reduced_t) and reduced_face < len(reduced_labels)
        parity = abs(source_t - reduced_t) if source_valid and reduced_valid else None
        passed = (
            source_valid
            and reduced_valid
            and int(source_labels[source_face]) == classification
            and int(reduced_labels[reduced_face]) == classification
            and parity is not None
            and parity <= 0.03
        )
        samples.append({
            "source_face_index": selected,
            "source_hit_classification": int(source_labels[source_face]) if source_valid else None,
            "reduced_hit_classification": int(reduced_labels[reduced_face]) if reduced_valid else None,
            "source_hit_distance_meters": source_t if source_valid else None,
            "reduced_hit_distance_meters": reduced_t if reduced_valid else None,
            "hit_distance_delta_meters": parity,
            "passed": passed,
        })
    passed_count = sum(item["passed"] for item in samples)
    passed = passed_count == len(samples)
    return {
        "status": "accepted" if passed else "held",
        "reason": "sampled_surface_block_parity_passed" if passed else "sampled_surface_block_parity_failed",
        "sample_pattern": "up_to_16_largest_component_face_centroids",
        "sample_count": len(samples),
        "passed_sample_count": passed_count,
        "source_face_indices": [item["source_face_index"] for item in samples],
        "maximum_hit_distance_delta_meters": max(
            (item["hit_distance_delta_meters"] for item in samples if item["hit_distance_delta_meters"] is not None),
            default=None,
        ),
        "samples": samples,
    }


def _closed_door_probe(
    base: dict[str, Any],
    o3d: Any,
    source_scene: Any,
    reduced_scene: Any,
    source_vertices: np.ndarray,
    source_faces: np.ndarray,
    source_labels: np.ndarray,
    source_normals: np.ndarray,
    largest_door_faces: np.ndarray,
    reduced_vertices: np.ndarray,
    reduced_faces: np.ndarray,
    reduced_labels: np.ndarray,
    largest_reduced_door_faces: np.ndarray,
    area_retention_ratio: float | None,
) -> dict[str, Any]:
    if base["status"] != "accepted" or not len(largest_door_faces):
        return base
    source_used = np.unique(source_faces[largest_door_faces])
    source_points = source_vertices[source_used]
    reduced_used = np.unique(reduced_faces[largest_reduced_door_faces])
    reduced_points = reduced_vertices[reduced_used]
    normal = source_normals[int(base["source_face_indices"][0])]
    horizontal = np.cross(np.asarray([0.0, 1.0, 0.0]), normal)
    horizontal_length = np.linalg.norm(horizontal)
    if horizontal_length <= 1e-12:
        return {**base, "status": "held", "reason": "door_plane_not_vertical"}
    horizontal /= horizontal_length
    vertical = np.cross(normal, horizontal)
    vertical /= np.linalg.norm(vertical)
    if vertical[1] < 0:
        vertical *= -1.0
    source_horizontal = source_points @ horizontal
    source_vertical = source_points @ vertical
    reduced_horizontal = reduced_points @ horizontal if len(reduced_points) else np.empty(0)
    reduced_vertical = reduced_points @ vertical if len(reduced_points) else np.empty(0)
    source_width = float(np.ptp(source_horizontal))
    source_height = float(np.ptp(source_vertical))
    reduced_width = float(np.ptp(reduced_horizontal)) if len(reduced_horizontal) else 0.0
    reduced_height = float(np.ptp(reduced_vertical)) if len(reduced_vertical) else 0.0
    radius = 0.22
    half_height = 0.50
    controller_offset = 0.02
    minimum_width = 2.0 * (radius + controller_offset)
    minimum_height = 2.0 * (half_height + radius + controller_offset)
    dimensions_supported = (
        source_width >= minimum_width
        and source_height >= minimum_height
        and reduced_width >= minimum_width
        and reduced_height >= minimum_height
    )
    component_supported = area_retention_ratio is not None and area_retention_ratio >= 0.95
    plane = float(np.mean(source_points @ normal))
    center = (
        normal * plane
        + horizontal * float((source_horizontal.min() + source_horizontal.max()) * 0.5)
        + vertical * float((source_vertical.min() + source_vertical.max()) * 0.5)
    )
    envelope_samples: list[dict[str, Any]] = []
    for horizontal_offset in (-(radius + controller_offset), 0.0, radius + controller_offset):
        for vertical_offset in (
            -(half_height + radius + controller_offset),
            0.0,
            half_height + radius + controller_offset,
        ):
            point = center + horizontal * horizontal_offset + vertical * vertical_offset
            origin = point - normal * 0.2
            source_t, source_face = _ray_hit(o3d, source_scene, origin, normal)
            reduced_t, reduced_face = _ray_hit(o3d, reduced_scene, origin, normal)
            source_valid = math.isfinite(source_t) and source_face < len(source_labels)
            reduced_valid = math.isfinite(reduced_t) and reduced_face < len(reduced_labels)
            delta = abs(source_t - reduced_t) if source_valid and reduced_valid else None
            passed = (
                source_valid
                and reduced_valid
                and int(source_labels[source_face]) == 7
                and int(reduced_labels[reduced_face]) == 7
                and delta is not None
                and delta <= 0.03
            )
            envelope_samples.append({
                "horizontal_offset_meters": horizontal_offset,
                "vertical_offset_meters": vertical_offset,
                "hit_distance_delta_meters": delta,
                "passed": passed,
            })
    envelope_passed = all(item["passed"] for item in envelope_samples)
    accepted = dimensions_supported and component_supported and envelope_passed
    return {
        **base,
        "status": "accepted" if accepted else "held",
        "reason": (
            "closed_door_blocks_sampled_frozen_capsule_envelope"
            if accepted
            else "reduced_door_does_not_cover_frozen_capsule_envelope"
        ),
        "source_door_component_width_meters": source_width,
        "source_door_component_height_meters": source_height,
        "reduced_door_component_width_meters": reduced_width,
        "reduced_door_component_height_meters": reduced_height,
        "reduced_to_source_largest_component_area_retention_ratio": area_retention_ratio,
        "minimum_width_meters": minimum_width,
        "minimum_height_meters": minimum_height,
        "envelope_sample_pattern": "three_by_three_frozen_capsule_cross_section",
        "envelope_sample_count": len(envelope_samples),
        "envelope_passed_sample_count": sum(item["passed"] for item in envelope_samples),
        "envelope_samples": envelope_samples,
        "capsule": {
            "radius_meters": radius,
            "half_height_meters": half_height,
            "controller_offset_meters": controller_offset,
        },
    }


def _run_software_probes(
    o3d: Any,
    source_vertices: np.ndarray,
    source_faces: np.ndarray,
    source_labels: np.ndarray,
    reduced_vertices: np.ndarray,
    reduced_faces: np.ndarray,
    reduced_labels: np.ndarray,
    source_normals: np.ndarray,
    source_areas: np.ndarray,
    largest_source_faces: dict[int, np.ndarray],
    largest_reduced_faces: dict[int, np.ndarray],
    door_area_retention_ratio: float | None,
) -> dict[str, Any]:
    source_scene = _scene(o3d, source_vertices, source_faces)
    reduced_scene = _scene(o3d, reduced_vertices, reduced_faces)
    floor = _surface_probe(
        o3d,
        source_scene,
        reduced_scene,
        source_vertices,
        source_faces,
        source_labels,
        source_normals,
        source_areas,
        reduced_labels,
        largest_source_faces,
        2,
        "down",
    )
    wall = _surface_probe(
        o3d,
        source_scene,
        reduced_scene,
        source_vertices,
        source_faces,
        source_labels,
        source_normals,
        source_areas,
        reduced_labels,
        largest_source_faces,
        1,
        "normal",
    )
    door = _surface_probe(
        o3d,
        source_scene,
        reduced_scene,
        source_vertices,
        source_faces,
        source_labels,
        source_normals,
        source_areas,
        reduced_labels,
        largest_source_faces,
        7,
        "normal",
    )
    door = _closed_door_probe(
        door,
        o3d,
        source_scene,
        reduced_scene,
        source_vertices,
        source_faces,
        source_labels,
        source_normals,
        largest_source_faces[7],
        reduced_vertices,
        reduced_faces,
        reduced_labels,
        largest_reduced_faces[7],
        door_area_retention_ratio,
    )
    return {
        "floor_qualified_spawn": floor,
        "floor_continuity_and_no_fallthrough": {
            **floor,
            "status": "held",
            "reason": "route_contract_missing_for_no_fallthrough_claim",
            "sampled_floor_surface_status": floor["status"],
            "probe_scope": "largest_supported_floor_component_representative",
        },
        "wall_stop": wall,
        "closed_door": door,
        "fallback_floor": {"status": "accepted", "added": False},
        "doorway": {
            "status": "held",
            "reason": "doorway_probe_missing",
            "route_or_portal_contract_consumed": False,
        },
        "reset": {
            "status": "held",
            "reason": "world_studio_character_controller_reset_probe_pending",
        },
    }


def _write_reduced_mesh(
    path: Path,
    vertices: np.ndarray,
    faces: np.ndarray,
    labels: np.ndarray,
    support: np.ndarray,
    source_mapping: np.ndarray,
) -> None:
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment Capture Splat source-mapped reduced collider; no physics authority\n"
        f"element vertex {len(vertices)}\n"
        "property double x\nproperty double y\nproperty double z\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\n"
        "property uchar semantic_classification\n"
        "property uchar semantic_support\n"
        "property uint source_face_index\n"
        "end_header\n"
    ).encode("ascii")
    records = np.empty(len(faces), dtype=_OUTPUT_FACE_DTYPE)
    records["count"] = 3
    records["indices"] = faces.astype(np.uint32, copy=False)
    records["classification"] = labels
    records["support"] = support
    records["source_face_index"] = source_mapping
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(vertices.astype("<f8", copy=False).tobytes())
        records.tofile(handle)


def reduce_hybrid_collider(
    hybrid_report: Path,
    out_dir: Path,
    *,
    max_faces: int = 60_000,
    boundary_weight: float = 100.0,
    maximum_distance: float = 0.06,
    p99_distance: float = 0.03,
    minimum_normal_dot: float = 0.95,
    ambiguity_epsilon: float = 0.00001,
    batch_faces: int = 8_192,
) -> dict[str, Any]:
    report_path, report_evidence = _external_file_evidence(hybrid_report, "hybrid report")
    source_root = report_path.parent
    raw_out = out_dir.absolute()
    if raw_out.is_symlink():
        raise ValueError("reduced collider output must not be a symbolic link")
    out_dir = raw_out.resolve()
    try:
        out_dir.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("reduced collider output must be outside the immutable source directory")
    if (
        max_faces <= 0
        or max_faces > 60_000
        or not math.isfinite(boundary_weight)
        or boundary_weight <= 0
        or not math.isfinite(maximum_distance)
        or maximum_distance <= 0
        or not math.isfinite(p99_distance)
        or p99_distance <= 0
        or p99_distance > maximum_distance
        or not math.isfinite(minimum_normal_dot)
        or not 0 < minimum_normal_dot <= 1
        or not math.isfinite(ambiguity_epsilon)
        or ambiguity_epsilon < 0
        or batch_faces <= 0
    ):
        raise ValueError("reducer thresholds and budgets are invalid")
    if out_dir.exists():
        metadata = out_dir.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("reduced collider output must be a regular directory")
        if any(out_dir.iterdir()):
            raise FileExistsError(f"reduced collider output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    reduced_report_path = out_dir / REDUCED_REPORT_NAME
    report: dict[str, Any] = {
        "schema": REDUCED_REPORT_SCHEMA,
        "status": "rejected",
        "decision": "reject",
        "reason": "reduced_collider_validation_failed",
        "authority": _false_authority(),
    }
    try:
        hybrid, collider, source_path, inputs = _validate_inputs(report_path, report_evidence)
        source_vertices, source_faces, source_labels, source_normals, source_areas = (
            _load_source_surface(source_path)
        )
        source_topology = hybrid["topology"]
        if (
            source_topology.get("output_vertex_count") != len(source_vertices)
            or source_topology.get("output_triangle_count") != len(source_faces)
        ):
            raise ValueError("hybrid report counts do not match the exact source surface")
        source_semantics = hybrid.get("semantics")
        source_partition = collider.get("semantic_partition")
        source_unknown = int(np.count_nonzero(source_labels == UNKNOWN_CLASSIFICATION))
        source_known = len(source_labels) - source_unknown
        if (
            not isinstance(source_semantics, dict)
            or source_semantics.get("partition_invariant") is not True
            or source_semantics.get("transferred_face_count") != source_known
            or source_semantics.get("unknown_face_count") != source_unknown
            or not isinstance(source_partition, dict)
            or source_partition.get("partition_invariant") is not True
            or source_partition.get("transferred_face_count") != source_known
            or source_partition.get("unknown_face_count") != source_unknown
        ):
            raise ValueError("hybrid semantic partition does not match the exact source surface")
        o3d = _open3d()
        if o3d.__version__ != "0.19.0":
            raise RuntimeError(
                f"reduce-hybrid-collider requires Open3D 0.19.0, found {o3d.__version__}"
            )
        reduced_vertices, reduced_faces, simplified = _simplify(
            o3d, source_vertices, source_faces, max_faces, boundary_weight
        )
        reduced_normals, _ = _face_geometry(reduced_vertices, reduced_faces)
        reduced_labels, reduced_support, source_mapping, mapping, reverse = _map_reduced_faces(
            o3d,
            source_vertices,
            source_faces,
            source_labels,
            reduced_vertices,
            reduced_faces,
            reduced_normals,
            maximum_distance=maximum_distance,
            minimum_normal_dot=minimum_normal_dot,
            ambiguity_epsilon=ambiguity_epsilon,
            batch_faces=batch_faces,
        )
        forward = _source_to_reduced(
            o3d,
            source_vertices,
            source_faces,
            source_normals,
            reduced_vertices,
            reduced_faces,
            batch_faces=batch_faces,
        )
        source_unknown_samples = np.repeat(
            source_labels == UNKNOWN_CLASSIFICATION, SAMPLES_PER_FACE
        )
        nearest_reduced = forward["primitive_ids"].reshape(-1)
        if np.any(nearest_reduced < 0) or np.any(nearest_reduced >= len(reduced_faces)):
            raise RuntimeError("source-to-reduced mapping escaped the reduced topology")
        leaked = nearest_reduced[
            source_unknown_samples
            & (forward["distances"].reshape(-1) <= maximum_distance)
            & (reduced_labels[nearest_reduced] != UNKNOWN_CLASSIFICATION)
        ]
        fail_closed_faces = np.unique(leaked)
        reduced_labels[fail_closed_faces] = UNKNOWN_CLASSIFICATION
        reduced_support[fail_closed_faces] = 0
        remaining_known_leaks = int(np.count_nonzero(
            source_unknown_samples
            & (forward["distances"].reshape(-1) <= maximum_distance)
            & (reduced_labels[nearest_reduced] != UNKNOWN_CLASSIFICATION)
        ))
        if remaining_known_leaks:
            raise RuntimeError("unknown source evidence became known in the reduced candidate")
        mapping["fail_closed_unknown_relabel_face_count"] = int(len(fail_closed_faces))
        mapping["unknown_source_sample_to_known_candidate_count"] = remaining_known_leaks
        mapping["unknown_never_becomes_known"] = True
        mapping["unknown_never_becomes_known_scope"] = (
            "declared_centroid_and_vertex_source_to_reduced_samples"
        )
        mapping["unknown_triangle_interior_coverage"] = "held_not_exhaustive"
        mapping["transferred_classification_counts"] = _semantic_counts(
            reduced_labels[reduced_labels != UNKNOWN_CLASSIFICATION]
        )
        mapping["unknown_face_count"] = int(
            np.count_nonzero(reduced_labels == UNKNOWN_CLASSIFICATION)
        )
        mapping["semantic_support_range"] = [
            int(reduced_support.min()),
            int(reduced_support.max()),
        ]

        comparison = _comparison_metrics(source_labels, reduced_labels, forward, reverse)
        source_components, largest_source_faces = _partition_components(
            o3d, source_vertices, source_faces, source_labels
        )
        reduced_components, largest_reduced_faces = _partition_components(
            o3d, reduced_vertices, reduced_faces, reduced_labels
        )
        component_ratios: dict[str, Any] = {}
        for name in ("floor", "wall", "door"):
            source_area = source_components[name]["largest_component_area_square_meters"]
            reduced_area = reduced_components[name]["largest_component_area_square_meters"]
            component_ratios[name] = {
                "largest_component_area_retention_ratio": (
                    reduced_area / source_area if source_area > 0 else None
                ),
                "source": source_components[name],
                "reduced": reduced_components[name],
            }
        source_topology_metrics = _topology_metrics(o3d, source_vertices, source_faces)
        reduced_topology_metrics = _topology_metrics(o3d, reduced_vertices, reduced_faces)
        boundary_metrics = _boundary_metrics(
            o3d,
            source_vertices,
            source_faces,
            reduced_vertices,
            reduced_faces,
        )

        structural_geometry_passed = True
        geometry_failures: list[str] = []
        for name in ("floor", "wall", "door"):
            source_metric = comparison["source_to_reduced"]["by_source_classification"].get(name)
            reduced_metric = comparison["reduced_to_source"][
                "by_nearest_source_classification"
            ].get(name)
            if source_metric is None or reduced_metric is None:
                structural_geometry_passed = False
                geometry_failures.append(f"{name}_comparison_missing")
                continue
            for direction, metric in (("source_to_reduced", source_metric), ("reduced_to_source", reduced_metric)):
                if (
                    metric["p99_distance_meters"] > p99_distance
                    or metric["maximum_distance_meters"] > maximum_distance
                    or metric["minimum_absolute_normal_dot"] < minimum_normal_dot
                ):
                    structural_geometry_passed = False
                    geometry_failures.append(f"{name}_{direction}_threshold_failed")
        component_passed = all(
            component_ratios[name]["largest_component_area_retention_ratio"] is not None
            and component_ratios[name]["largest_component_area_retention_ratio"] >= 0.98
            for name in ("floor", "wall")
        )
        door_component_passed = (
            component_ratios["door"]["largest_component_area_retention_ratio"] is not None
            and component_ratios["door"]["largest_component_area_retention_ratio"] >= 0.95
        )
        source_boundary = boundary_metrics["source_to_reduced_boundary"]
        reduced_boundary = boundary_metrics["reduced_to_source_boundary"]
        if source_topology_metrics["boundary_edge_count"] == 0:
            boundary_passed = reduced_topology_metrics["boundary_edge_count"] == 0
        else:
            boundary_passed = (
                source_boundary["sample_count"] > 0
                and reduced_boundary["sample_count"] > 0
                and source_boundary["p95_distance_meters"] <= p99_distance
                and source_boundary["maximum_distance_meters"] <= maximum_distance
                and reduced_boundary["p95_distance_meters"] <= p99_distance
                and reduced_boundary["maximum_distance_meters"] <= maximum_distance
            )
        topology_passed = (
            reduced_topology_metrics["non_manifold_edge_count_excluding_boundaries"] == 0
            and reduced_topology_metrics["non_manifold_vertex_count"]
            <= source_topology_metrics["non_manifold_vertex_count"]
            and reduced_topology_metrics["connected_component_count"]
            == source_topology_metrics["connected_component_count"]
            and (
                not source_topology_metrics["edge_manifold_allowing_boundaries"]
                or reduced_topology_metrics["edge_manifold_allowing_boundaries"]
            )
            and (
                not source_topology_metrics["vertex_manifold"]
                or reduced_topology_metrics["vertex_manifold"]
            )
            and (
                not source_topology_metrics["orientable"]
                or reduced_topology_metrics["orientable"]
            )
            and (not source_topology_metrics["watertight"] or reduced_topology_metrics["watertight"])
            and boundary_passed
        )

        probe_runs = [
            _run_software_probes(
                o3d,
                source_vertices,
                source_faces,
                source_labels,
                reduced_vertices,
                reduced_faces,
                reduced_labels,
                source_normals,
                source_areas,
                largest_source_faces,
                largest_reduced_faces,
                component_ratios["door"]["largest_component_area_retention_ratio"],
            )
            for _ in range(3)
        ]
        probe_digests = [_digest_json(item) for item in probe_runs]
        telemetry_identical = len(set(probe_digests)) == 1
        probes = probe_runs[0]

        reduced_path = out_dir / REDUCED_MESH_NAME
        _write_reduced_mesh(
            reduced_path,
            reduced_vertices,
            reduced_faces,
            reduced_labels,
            reduced_support,
            source_mapping,
        )
        for name, evidence in inputs.items():
            _verify_reference(source_root, evidence, evidence["path"], f"{name} after reduction")

        probe_report = {
            "schema": PROBE_REPORT_SCHEMA,
            "status": "held",
            "decision": "hold",
            "reason": "doorway_probe_missing_and_downstream_reset_pending",
            "authority": _false_authority(),
            "inputs": {
                "source_hybrid_surface": inputs["hybrid_surface"],
                "reduced_collider": _file_evidence(reduced_path, out_dir),
            },
            "coordinate_contract": {"coordinate_frame": "arkit_world", "units": "meters"},
            "repetitions": {
                "count": 3,
                "telemetry_digests": probe_digests,
                "identical": telemetry_identical,
            },
            "probes": probes,
            "rails": {
                "floor": probes["floor_qualified_spawn"]["status"],
                "wall": probes["wall_stop"]["status"],
                "closed_door": probes["closed_door"]["status"],
                "no_fallback_floor": "accepted",
                "doorway": "held_missing",
                "reset": "held_downstream",
            },
        }
        probe_report_path = out_dir / PROBE_REPORT_NAME
        write_json_strict(probe_report_path, probe_report)

        hold_reasons: list[str] = []
        if not structural_geometry_passed:
            hold_reasons.append("supported_structural_geometry_threshold_failed")
        if not component_passed:
            hold_reasons.append("floor_or_wall_component_retention_failed")
        if not door_component_passed:
            hold_reasons.append("door_component_retention_failed")
        if not topology_passed:
            hold_reasons.append("topology_degraded")
        for name in ("floor_qualified_spawn", "wall_stop", "closed_door"):
            if probes[name]["status"] != "accepted":
                hold_reasons.append(f"{name}_failed")
        if not telemetry_identical:
            hold_reasons.append("software_probe_telemetry_nondeterministic")
        hold_reasons.extend([
            "unknown_triangle_interior_coverage_unproven",
            "doorway_probe_missing",
            "world_studio_character_controller_reset_probe_pending",
            "physical_validation_pending",
        ])
        report.update({
            "status": "held",
            "decision": "hold",
            "reason": hold_reasons[0],
            "hold_reasons": hold_reasons,
            "inputs": inputs,
            "coordinate_contract": {"coordinate_frame": "arkit_world", "units": "meters"},
            "implementation": {
                "algorithm": "open3d_quadric_decimation",
                "open3d_version": o3d.__version__,
                "semantic_mapping": "class_partitioned_nearest_surface",
            },
            "parameters": {
                "maximum_triangle_count": max_faces,
                "boundary_weight": boundary_weight,
                "maximum_supported_distance_meters_inclusive": maximum_distance,
                "p99_supported_distance_meters_inclusive": p99_distance,
                "minimum_absolute_normal_dot_inclusive": minimum_normal_dot,
                "nearest_class_ambiguity_epsilon_meters_inclusive": ambiguity_epsilon,
                "source_minimum_double_area_square_meters_exclusive": SOURCE_MINIMUM_DOUBLE_AREA,
                "reduced_minimum_area_square_meters_exclusive": REDUCED_MINIMUM_AREA,
                "query_batch_faces": batch_faces,
            },
            "source": {
                "vertex_count": len(source_vertices),
                "triangle_count": len(source_faces),
                "semantic_counts": _semantic_counts(source_labels),
            },
            "candidate": {
                **_file_evidence(reduced_path, out_dir),
                "vertex_count": len(reduced_vertices),
                "triangle_count": len(reduced_faces),
            },
            "probe_report": _file_evidence(probe_report_path, out_dir),
            "topology": {
                "source": source_topology_metrics,
                "reduced": reduced_topology_metrics,
                "boundary_comparison": boundary_metrics,
                "simplification_applied": simplified,
                "source_and_reduced_are_separate_files": True,
                "synthetic_geometry_added": False,
                "fallback_floor_added": False,
                "hole_fill_applied": False,
                "portal_inferred": False,
            },
            "source_mapping": mapping,
            "comparison": comparison,
            "components": component_ratios,
            "rails": {
                "triangle_budget": {
                    "status": "accepted",
                    "limit": max_faces,
                    "observed": len(reduced_faces),
                },
                "source_mapping": "accepted" if mapping["mapping_in_range"] else "held",
                "unknown_never_becomes_known": "accepted_for_declared_samples",
                "unknown_triangle_interior_coverage": "held_sampling_only",
                "supported_structural_geometry": {
                    "status": "accepted" if structural_geometry_passed else "held",
                    "failures": geometry_failures,
                },
                "floor_wall_component_retention": "accepted" if component_passed else "held",
                "door_component_retention": "accepted" if door_component_passed else "held",
                "topology": "accepted" if topology_passed else "held_degraded",
                "floor_probe": probes["floor_qualified_spawn"]["status"],
                "wall_probe": probes["wall_stop"]["status"],
                "closed_door_probe": probes["closed_door"]["status"],
                "no_fallback_floor": "accepted",
                "doorway": "held_doorway_probe_missing",
                "reset": "held_world_studio_probe_pending",
                "physical_validation": "pending",
            },
        })
        write_json_strict(reduced_report_path, report)
    except Exception as error:
        report["error"] = str(error)
        report["error_type"] = type(error).__name__
        write_json_strict(reduced_report_path, report)
        raise
    return report
