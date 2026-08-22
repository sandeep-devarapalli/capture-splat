from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from capture_splat import reduced_collider
from capture_splat.hybrid_surface import (
    COLLIDER_MESH_NAME,
    COLLIDER_REPORT_NAME,
    COLLIDER_REPORT_SCHEMA,
    HYBRID_MESH_NAME,
    HYBRID_REPORT_NAME,
    HYBRID_REPORT_SCHEMA,
    UNKNOWN_CLASSIFICATION,
    _false_authority,
)
from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.ply_stats import _parse_header
from capture_splat.reduced_collider import (
    PROBE_REPORT_NAME,
    REDUCED_MESH_NAME,
    REDUCED_REPORT_NAME,
    reduce_hybrid_collider,
)


pytest.importorskip("open3d")

_VERTEX_DTYPE = np.dtype([
    ("position", "<f8", (3,)),
    ("normal", "<f8", (3,)),
    ("color", "u1", (3,)),
])
_FACE_DTYPE = np.dtype([
    ("count", "u1"),
    ("indices", "<u4", (3,)),
    ("classification", "u1"),
    ("support", "u1"),
    ("source_face_index", "<u4"),
])


def _ref(path: Path, root: Path) -> dict[str, object]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "checksum": f"sha256:{digest}",
    }


def _write_bound_source(
    root: Path,
    vertices: np.ndarray,
    faces: np.ndarray,
    labels: np.ndarray,
) -> Path:
    root.mkdir(parents=True)
    vertex_records = np.zeros(len(vertices), dtype=_VERTEX_DTYPE)
    vertex_records["position"] = vertices
    vertex_records["color"] = [127, 127, 127]
    face_records = np.empty(len(faces), dtype=_FACE_DTYPE)
    face_records["count"] = 3
    face_records["indices"] = faces
    face_records["classification"] = labels
    face_records["support"] = np.where(labels == UNKNOWN_CLASSIFICATION, 0, 4)
    face_records["source_face_index"] = np.arange(len(faces), dtype=np.uint32)
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        "comment Capture Splat hybrid structural evidence; no physics authority\n"
        f"element vertex {len(vertices)}\n"
        "property double x\nproperty double y\nproperty double z\n"
        "property double nx\nproperty double ny\nproperty double nz\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\n"
        "property uchar semantic_classification\n"
        "property uchar semantic_support\n"
        "property uint source_face_index\nend_header\n"
    ).encode("ascii")
    hybrid_path = root / HYBRID_MESH_NAME
    with hybrid_path.open("wb") as handle:
        handle.write(header)
        vertex_records.tofile(handle)
        face_records.tofile(handle)
    collider_path = root / COLLIDER_MESH_NAME
    collider_path.write_bytes(hybrid_path.read_bytes())
    topology = {
        "source_vertex_count": len(vertices),
        "source_triangle_count": len(faces),
        "output_vertex_count": len(vertices),
        "output_triangle_count": len(faces),
        "vertex_records_copied_byte_for_byte": True,
        "triangle_indices_preserved_in_source_order": True,
        "source_face_index_mapping": "identity_zero_based",
        "synthetic_geometry_added": False,
        "fallback_floor_added": False,
        "simplification_applied": False,
    }
    unknown = int(np.count_nonzero(labels == UNKNOWN_CLASSIFICATION))
    hybrid_report_path = root / HYBRID_REPORT_NAME
    write_json_strict(hybrid_report_path, {
        "schema": HYBRID_REPORT_SCHEMA,
        "status": "held",
        "decision": "hold",
        "authority": _false_authority(),
        "coordinate_contract": {
            "coordinate_frame": "arkit_world",
            "units": "meters",
            "tsdf_and_arkit_share_input_frame": True,
        },
        "topology": topology,
        "semantics": {
            "transferred_face_count": len(faces) - unknown,
            "unknown_face_count": unknown,
            "partition_invariant": True,
        },
        "output": {"hybrid_surface": _ref(hybrid_path, root)},
    })
    write_json_strict(root / COLLIDER_REPORT_NAME, {
        "schema": COLLIDER_REPORT_SCHEMA,
        "status": "held",
        "decision": "hold",
        "authority": _false_authority(),
        "inputs": {
            "hybrid_surface": _ref(hybrid_path, root),
            "hybrid_report": _ref(hybrid_report_path, root),
        },
        "candidate": _ref(collider_path, root),
        "coordinate_contract": {"coordinate_frame": "arkit_world", "units": "meters"},
        "topology": topology,
        "semantic_partition": {
            "transferred_face_count": len(faces) - unknown,
            "unknown_face_count": unknown,
            "partition_invariant": True,
        },
    })
    return hybrid_report_path


def _room_geometry() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    vertices = np.asarray([
        [0, 0, 0], [2, 0, 0], [2, 0, 2], [0, 0, 2],
        [3, 0, 0], [3, 2, 0], [3, 2, 2], [3, 0, 2],
        [0, 0, 4], [1, 0, 4], [1, 2, 4], [0, 2, 4],
        [6, 0, 0], [6, 1, 0], [6, 0, 1],
    ], dtype=np.float64)
    faces = np.asarray([
        [0, 2, 1], [0, 3, 2],
        [4, 5, 6], [4, 6, 7],
        [8, 9, 10], [8, 10, 11],
        [12, 13, 14],
    ], dtype=np.uint32)
    labels = np.asarray([2, 2, 1, 1, 7, 7, UNKNOWN_CLASSIFICATION], dtype=np.uint8)
    return vertices, faces, labels


def _read_reduced(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        header, offset = _parse_header(handle)
        vertex, face = header["elements"]
        handle.seek(offset)
        vertices = np.frombuffer(handle.read(int(vertex["count"]) * 24), dtype="<f8").reshape(-1, 3)
        records = np.frombuffer(handle.read(int(face["count"]) * _FACE_DTYPE.itemsize), dtype=_FACE_DTYPE)
    return (
        vertices.copy(),
        records["indices"].copy(),
        records["classification"].copy(),
        records["source_face_index"].copy(),
    )


def test_reducer_preserves_mapping_unknown_and_held_authority(tmp_path: Path) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    summary = reduce_hybrid_collider(source, tmp_path / "out")
    _, reduced_faces, reduced_labels, source_mapping = _read_reduced(
        tmp_path / "out" / REDUCED_MESH_NAME
    )
    probes = load_json_strict(tmp_path / "out" / PROBE_REPORT_NAME)

    assert summary["status"] == "held"
    assert summary["candidate"]["triangle_count"] == len(faces)
    assert np.array_equal(reduced_faces, faces)
    assert np.array_equal(reduced_labels, labels)
    assert np.array_equal(source_mapping, np.arange(len(faces), dtype=np.uint32))
    assert summary["source_mapping"]["unknown_never_becomes_known"] is True
    assert summary["topology"]["synthetic_geometry_added"] is False
    assert summary["topology"]["fallback_floor_added"] is False
    assert not any(summary["authority"].values())
    assert not any(probes["authority"].values())
    assert probes["probes"]["floor_qualified_spawn"]["status"] == "accepted"
    assert probes["probes"]["wall_stop"]["status"] == "accepted"
    assert probes["probes"]["closed_door"]["status"] == "accepted"
    assert probes["probes"]["doorway"]["reason"] == "doorway_probe_missing"


def test_unknown_source_patch_relabels_known_candidate_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vertices = np.asarray([
        [0, 0, 0], [4, 0, 0], [0, 0, 4],
        [0.1, 0, 2.5], [0.2, 0, 2.5], [0.1, 0, 2.6],
    ], dtype=np.float64)
    faces = np.asarray([[0, 2, 1], [3, 5, 4]], dtype=np.uint32)
    labels = np.asarray([2, UNKNOWN_CLASSIFICATION], dtype=np.uint8)
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    def only_large_face(o3d, source_vertices, source_faces, max_faces, boundary_weight):
        return source_vertices[:3].copy(), np.asarray([[0, 1, 2]], dtype=np.int64), True

    monkeypatch.setattr(reduced_collider, "_simplify", only_large_face)
    summary = reduce_hybrid_collider(source, tmp_path / "out", max_faces=1)
    _, _, reduced_labels, _ = _read_reduced(tmp_path / "out" / REDUCED_MESH_NAME)

    assert reduced_labels.tolist() == [UNKNOWN_CLASSIFICATION]
    assert summary["source_mapping"]["fail_closed_unknown_relabel_face_count"] == 1
    assert summary["source_mapping"]["unknown_source_sample_to_known_candidate_count"] == 0


def test_open3d_qem_enforces_triangle_budget(tmp_path: Path) -> None:
    size = 12
    vertices = np.asarray(
        [[x / size, 0.0, z / size] for z in range(size + 1) for x in range(size + 1)],
        dtype=np.float64,
    )
    faces = []
    for z in range(size):
        for x in range(size):
            lower = z * (size + 1) + x
            faces.extend(([lower, lower + size + 2, lower + 1], [lower, lower + size + 1, lower + size + 2]))
    faces_array = np.asarray(faces, dtype=np.uint32)
    labels = np.full(len(faces_array), 2, dtype=np.uint8)
    source = _write_bound_source(tmp_path / "source", vertices, faces_array, labels)

    summary = reduce_hybrid_collider(source, tmp_path / "out", max_faces=60)

    assert summary["candidate"]["triangle_count"] <= 60
    assert summary["topology"]["simplification_applied"] is True
    assert summary["implementation"] == {
        "algorithm": "open3d_quadric_decimation",
        "open3d_version": "0.19.0",
        "semantic_mapping": "class_partitioned_nearest_surface",
    }


def test_topology_degradation_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    def duplicate_face(o3d, source_vertices, source_faces, max_faces, boundary_weight):
        return source_vertices.copy(), np.concatenate((source_faces, source_faces[:1])), True

    monkeypatch.setattr(reduced_collider, "_simplify", duplicate_face)
    summary = reduce_hybrid_collider(source, tmp_path / "out")

    assert summary["rails"]["topology"] == "held_degraded"
    assert "topology_degraded" in summary["hold_reasons"]
    assert summary["topology"]["boundary_comparison"]["sample_pattern"] == (
        "unique_boundary_edge_endpoints_and_midpoints"
    )


def test_partial_reduced_door_fails_capsule_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    def partial_door(o3d, source_vertices, source_faces, max_faces, boundary_weight):
        patch = np.asarray([
            [0.25, 0.6, 4], [0.75, 0.6, 4], [0.75, 1.4, 4], [0.25, 1.4, 4],
        ])
        candidate_vertices = np.concatenate((source_vertices, patch))
        candidate_faces = np.asarray([
            *source_faces[:4],
            [15, 16, 17], [15, 17, 18],
            source_faces[6],
        ], dtype=np.int64)
        return candidate_vertices, candidate_faces, True

    monkeypatch.setattr(reduced_collider, "_simplify", partial_door)
    summary = reduce_hybrid_collider(source, tmp_path / "out")
    probes = load_json_strict(tmp_path / "out" / PROBE_REPORT_NAME)

    assert summary["rails"]["door_component_retention"] == "held"
    assert probes["probes"]["closed_door"]["status"] == "held"
    assert probes["probes"]["closed_door"]["envelope_passed_sample_count"] < 9


def test_holed_reduced_floor_fails_sampled_floor_rail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    def remove_floor_half(o3d, source_vertices, source_faces, max_faces, boundary_weight):
        return source_vertices.copy(), source_faces[[0, 2, 3, 4, 5, 6]].copy(), True

    monkeypatch.setattr(reduced_collider, "_simplify", remove_floor_half)
    summary = reduce_hybrid_collider(source, tmp_path / "out")
    probes = load_json_strict(tmp_path / "out" / PROBE_REPORT_NAME)

    assert summary["rails"]["floor_wall_component_retention"] == "held"
    assert probes["probes"]["floor_qualified_spawn"]["status"] == "held"
    assert probes["probes"]["floor_continuity_and_no_fallthrough"]["status"] == "held"


def test_reduced_outputs_are_deterministic(tmp_path: Path) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    reduce_hybrid_collider(source, tmp_path / "first")
    reduce_hybrid_collider(source, tmp_path / "second")

    for name in (REDUCED_MESH_NAME, REDUCED_REPORT_NAME, PROBE_REPORT_NAME):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


def test_reducer_rejects_tampered_source(tmp_path: Path) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)
    with (source.parent / HYBRID_MESH_NAME).open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(ValueError, match="size and checksum"):
        reduce_hybrid_collider(source, tmp_path / "out")


def test_reducer_rejects_tampered_unsimplified_candidate(tmp_path: Path) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)
    with (source.parent / COLLIDER_MESH_NAME).open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(ValueError, match="size and checksum"):
        reduce_hybrid_collider(source, tmp_path / "out")


def test_reducer_rejects_invalid_units_and_symlink(tmp_path: Path) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)
    report = load_json_strict(source)
    report["coordinate_contract"]["units"] = "centimeters"
    write_json_strict(source, report)
    with pytest.raises(ValueError, match="coordinate contract"):
        reduce_hybrid_collider(source, tmp_path / "bad-units")

    valid = _write_bound_source(tmp_path / "valid", vertices, faces, labels)
    alias = tmp_path / "hybrid-report-link.json"
    alias.symlink_to(valid)
    with pytest.raises(ValueError, match="symbolic link"):
        reduce_hybrid_collider(alias, tmp_path / "bad-link")


def test_reducer_rejects_output_inside_source_and_invalid_geometry(tmp_path: Path) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)
    with pytest.raises(ValueError, match="outside the immutable source"):
        reduce_hybrid_collider(source, source.parent / "derived")

    invalid_vertices = vertices.copy()
    invalid_vertices[0, 0] = np.nan
    invalid = _write_bound_source(tmp_path / "invalid", invalid_vertices, faces, labels)
    with pytest.raises(ValueError, match="finite geometry"):
        reduce_hybrid_collider(invalid, tmp_path / "nonfinite")

    degenerate_faces = faces.copy()
    degenerate_faces[0] = [0, 0, 1]
    degenerate = _write_bound_source(
        tmp_path / "degenerate-source", vertices, degenerate_faces, labels
    )
    with pytest.raises(ValueError, match="degenerate faces"):
        reduce_hybrid_collider(degenerate, tmp_path / "degenerate")


def test_source_tiny_positive_face_uses_upstream_double_area_threshold(tmp_path: Path) -> None:
    vertices = np.asarray([
        [0.0, 0.0, 0.0],
        [2.0e-6, 0.0, 0.0],
        [0.0, 0.0, 8.0e-7],
    ])
    faces = np.asarray([[0, 2, 1]], dtype=np.uint32)
    labels = np.asarray([2], dtype=np.uint8)
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)

    loaded = reduced_collider._load_source_surface(source.parent / HYBRID_MESH_NAME)

    assert loaded[4].tolist() == pytest.approx([8.0e-13])


def test_reducer_rejects_unpinned_open3d_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vertices, faces, labels = _room_geometry()
    source = _write_bound_source(tmp_path / "source", vertices, faces, labels)
    monkeypatch.setattr(
        reduced_collider,
        "_open3d",
        lambda: SimpleNamespace(__version__="0.18.0"),
    )

    with pytest.raises(RuntimeError, match="requires Open3D 0.19.0"):
        reduce_hybrid_collider(source, tmp_path / "out")
