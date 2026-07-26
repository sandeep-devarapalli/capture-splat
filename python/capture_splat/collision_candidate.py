from __future__ import annotations

import hashlib
import math
import struct
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from .json_utils import load_json_strict, write_json_strict
from .ply_stats import _parse_header

REPORT_NAME = "capture_splat_collision_candidate_report.json"
SCHEMA = "capture_splat.collision_candidate.v0.1"
CLASS_NAMES = {
    0: "none",
    1: "wall",
    2: "floor",
    3: "ceiling",
    4: "table",
    5: "seat",
    6: "window",
    7: "door",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _mesh_report_status(path: Path) -> dict[str, Any]:
    report = load_json_strict(path)
    if report.get("schema") != "capture_splat.arkit_mesh_report.v0.2":
        return {"accepted": False, "reason": "mesh_report_schema_invalid"}
    if report.get("status") != "finite_mesh_written" or report.get("ply_written") is not True:
        return {"accepted": False, "reason": "finite_mesh_not_reported"}
    if report.get("non_finite_vertex_count") != 0:
        return {"accepted": False, "reason": "source_mesh_non_finite"}
    if report.get("budget_limited") is True:
        if report.get("coverage_preserving") is not True:
            return {"accepted": False, "reason": "source_mesh_not_coverage_preserving"}
        for key in ("anchor_coverage_ratio", "spatial_cell_coverage_ratio"):
            value = report.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0.999:
                return {"accepted": False, "reason": f"source_{key}_incomplete"}
    return {"accepted": True, "reason": "finite_coverage_preserving_source"}


def _load_mesh(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        header, data_offset = _parse_header(handle)
        if header["format"] != "binary_little_endian":
            raise ValueError("collision source must be a Capture Splat binary little-endian mesh")
        elements = header["elements"]
        vertex = next((item for item in elements if item["name"] == "vertex"), None)
        face = next((item for item in elements if item["name"] == "face"), None)
        if vertex is None or face is None:
            raise ValueError("collision source must contain vertex and face elements")
        if elements.index(vertex) != 0 or elements.index(face) != 1:
            raise ValueError("collision source must store vertices before faces")
        vertex_properties = [
            (prop["kind"], prop.get("type"), prop["name"])
            for prop in vertex["properties"]
        ]
        face_properties = face["properties"]
        if vertex_properties != [
            ("scalar", "float", "x"),
            ("scalar", "float", "y"),
            ("scalar", "float", "z"),
        ]:
            raise ValueError("collision source vertex layout does not match Capture Splat ARKit mesh")
        if face_properties != [
            {
                "kind": "list",
                "count_type": "uchar",
                "value_type": "uint",
                "name": "vertex_indices",
            },
            {"kind": "scalar", "type": "uchar", "name": "classification"},
        ]:
            raise ValueError("collision source face layout does not match Capture Splat ARKit mesh")
        handle.seek(data_offset)
        vertex_count = int(vertex["count"])
        face_count = int(face["count"])
        vertex_bytes = handle.read(vertex_count * 12)
        if len(vertex_bytes) != vertex_count * 12:
            raise ValueError("collision source vertex data ended early")
        vertices = np.frombuffer(vertex_bytes, dtype="<f4").reshape(vertex_count, 3).astype(np.float64)
        face_dtype = np.dtype([
            ("count", "u1"),
            ("indices", "<u4", (3,)),
            ("classification", "u1"),
        ])
        face_bytes = handle.read(face_count * face_dtype.itemsize)
        if len(face_bytes) != face_count * face_dtype.itemsize:
            raise ValueError("collision source face data ended early")
        records = np.frombuffer(face_bytes, dtype=face_dtype)
        if np.any(records["count"] != 3):
            raise ValueError("collision source faces must be triangles")
        return (
            vertices,
            records["indices"].astype(np.int64),
            records["classification"].astype(np.uint8),
        )


def _face_cells(vertices: np.ndarray, faces: np.ndarray, cell_size: float) -> np.ndarray:
    centroids = np.mean(vertices[faces], axis=1)
    return np.floor(centroids / cell_size).astype(np.int64)


def _quotas(group_sizes: list[int], budget: int) -> list[int]:
    if sum(group_sizes) <= budget:
        return group_sizes
    quotas = [0] * len(group_sizes)
    active = [index for index, size in enumerate(group_sizes) if size > 0]
    if len(active) > budget:
        for ordinal in range(budget):
            quotas[active[round(ordinal * (len(active) - 1) / max(budget - 1, 1))]] = 1
        return quotas
    for index in active:
        quotas[index] = 1
    remaining = budget - len(active)
    capacities = [max(0, size - 1) for size in group_sizes]
    capacity_total = sum(capacities)
    if remaining <= 0 or capacity_total == 0:
        return quotas
    ideals = [remaining * capacity / capacity_total for capacity in capacities]
    additions = [min(capacities[index], int(math.floor(value))) for index, value in enumerate(ideals)]
    for index, addition in enumerate(additions):
        quotas[index] += addition
    remaining -= sum(additions)
    order = sorted(
        active,
        key=lambda index: (-(ideals[index] - math.floor(ideals[index])), -capacities[index], index),
    )
    while remaining > 0:
        progressed = False
        for index in order:
            if quotas[index] < group_sizes[index]:
                quotas[index] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
        if not progressed:
            break
    return quotas


def _select_faces(
    vertices: np.ndarray,
    faces: np.ndarray,
    classifications: np.ndarray,
    max_faces: int,
    cell_size: float,
) -> np.ndarray:
    cells = _face_cells(vertices, faces, cell_size)
    groups: dict[tuple[int, int, int, int], list[int]] = defaultdict(list)
    for index, (cell, classification) in enumerate(zip(cells, classifications)):
        groups[(*cell.tolist(), int(classification))].append(index)
    keys = sorted(groups)
    quotas = _quotas([len(groups[key]) for key in keys], max_faces)
    selected: list[int] = []
    for key, quota in zip(keys, quotas):
        indexes = groups[key]
        for ordinal in range(quota):
            sample = min(len(indexes) - 1, int((ordinal + 0.5) * len(indexes) / quota))
            selected.append(indexes[sample])
    return np.asarray(sorted(selected), dtype=np.int64)


def _write_mesh(path: Path, vertices: np.ndarray, faces: np.ndarray, classifications: np.ndarray) -> None:
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment Capture Splat collision candidate; review evidence only\n"
        f"element vertex {len(vertices)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\n"
        "property uchar classification\n"
        "end_header\n"
    ).encode("ascii")
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(vertices.astype("<f4", copy=False).tobytes())
        for face, classification in zip(faces, classifications):
            handle.write(struct.pack("<BIIIB", 3, *face.tolist(), int(classification)))


def _coverage(
    source_cells: np.ndarray,
    candidate_cells: np.ndarray,
    source_classes: np.ndarray,
    candidate_classes: np.ndarray,
) -> dict[str, Any]:
    source_cell_set = {tuple(cell.tolist()) for cell in source_cells}
    candidate_cell_set = {tuple(cell.tolist()) for cell in candidate_cells}
    source_counts = Counter(CLASS_NAMES.get(int(value), f"class_{int(value)}") for value in source_classes)
    candidate_counts = Counter(CLASS_NAMES.get(int(value), f"class_{int(value)}") for value in candidate_classes)
    class_coverage = {
        name: candidate_counts.get(name, 0) / count
        for name, count in sorted(source_counts.items())
        if count > 0
    }
    return {
        "source_spatial_cell_count": len(source_cell_set),
        "candidate_spatial_cell_count": len(candidate_cell_set),
        "spatial_cell_coverage_ratio": (
            len(source_cell_set & candidate_cell_set) / len(source_cell_set)
            if source_cell_set else 0.0
        ),
        "source_classification_counts": dict(sorted(source_counts.items())),
        "candidate_classification_counts": dict(sorted(candidate_counts.items())),
        "classification_coverage": class_coverage,
    }


def build_collision_candidate(
    source_mesh: Path,
    source_report: Path,
    out_dir: Path,
    *,
    max_faces: int = 100_000,
    cell_size: float = 0.5,
    intent: str = "room",
) -> dict[str, Any]:
    source_mesh = source_mesh.resolve()
    source_report = source_report.resolve()
    out_dir = out_dir.resolve()
    if not source_mesh.is_file() or not source_report.is_file():
        raise FileNotFoundError("source mesh and strict mesh report are required")
    if max_faces <= 0 or not math.isfinite(cell_size) or cell_size <= 0:
        raise ValueError("max-faces and cell-size must be positive")
    if intent not in {"room", "object"}:
        raise ValueError(f"unsupported collision intent: {intent}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"collision candidate output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_NAME
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "decision": "reject",
        "intent": intent,
        "source_mesh": {
            "path": str(source_mesh),
            "size_bytes": source_mesh.stat().st_size,
            "checksum": _sha256(source_mesh),
        },
        "source_report": {
            "path": str(source_report),
            "size_bytes": source_report.stat().st_size,
            "checksum": _sha256(source_report),
        },
        "thresholds": {"max_faces": max_faces, "spatial_cell_size_meters": cell_size},
        "coordinate_frame": "arkit_world",
        "units": "meters",
        "authority": {
            "capture_mesh_evidence": True,
            "collision_authority": False,
            "measurement_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
    }
    try:
        evidence = _mesh_report_status(source_report)
        base["source_mesh_evidence"] = evidence
        if not evidence["accepted"]:
            raise ValueError(evidence["reason"])
        vertices, faces, classifications = _load_mesh(source_mesh)
        if len(vertices) == 0 or len(faces) == 0:
            raise ValueError("source mesh has no collision geometry")
        if not np.isfinite(vertices).all():
            raise ValueError("source mesh contains non-finite vertices")
        if np.any(faces < 0) or np.any(faces >= len(vertices)):
            raise ValueError("source mesh contains out-of-range face indices")
        area_vectors = np.cross(vertices[faces[:, 1]] - vertices[faces[:, 0]], vertices[faces[:, 2]] - vertices[faces[:, 0]])
        valid = np.linalg.norm(area_vectors, axis=1) > 1e-8
        faces = faces[valid]
        classifications = classifications[valid]
        if len(faces) == 0:
            raise ValueError("source mesh has no finite non-degenerate triangles")
        report = load_json_strict(source_report)
        for key, actual in (("vertex_count", len(vertices)), ("triangle_count", len(faces))):
            declared = report.get(key)
            if isinstance(declared, int) and declared != actual:
                raise ValueError(f"source mesh {key} does not match its report")

        source_cells = _face_cells(vertices, faces, cell_size)
        selected = _select_faces(vertices, faces, classifications, max_faces, cell_size)
        candidate_faces = faces[selected]
        candidate_classes = classifications[selected]
        used = np.unique(candidate_faces.reshape(-1))
        remap = np.full(len(vertices), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        compact_vertices = vertices[used]
        compact_faces = remap[candidate_faces]
        candidate_path = out_dir / "collision_candidate.ply"
        _write_mesh(candidate_path, compact_vertices, compact_faces, candidate_classes)
        coverage = _coverage(
            source_cells,
            _face_cells(compact_vertices, compact_faces, cell_size),
            classifications,
            candidate_classes,
        )
        required_classes = ("floor", "wall") if intent == "room" else ()
        missing_classes = [
            name for name in required_classes
            if coverage["source_classification_counts"].get(name, 0) == 0
        ]
        coverage_preserved = coverage["spatial_cell_coverage_ratio"] >= 0.999
        software_ready = coverage_preserved and not missing_classes
        base.update({
            "decision": "hold",
            "reason": (
                "physical_floor_wall_and_splat_registration_validation_pending"
                if software_ready else
                "floor_or_wall_evidence_missing"
                if missing_classes else
                "candidate_spatial_coverage_incomplete"
            ),
            "software_prerequisites": software_ready,
            "candidate": {
                "path": str(candidate_path),
                "size_bytes": candidate_path.stat().st_size,
                "checksum": _sha256(candidate_path),
                "vertex_count": len(compact_vertices),
                "triangle_count": len(compact_faces),
            },
            "source": {
                "vertex_count": len(vertices),
                "triangle_count": len(faces),
            },
            "coverage": coverage,
            "missing_required_classes": missing_classes,
        })
    except Exception as error:
        base["error"] = str(error)
        write_json_strict(report_path, base)
        raise
    write_json_strict(report_path, base)
    return base
