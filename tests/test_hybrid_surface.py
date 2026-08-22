from __future__ import annotations

import hashlib
import struct
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from capture_splat.hybrid_surface import (
    COLLIDER_MESH_NAME,
    COLLIDER_REPORT_NAME,
    HYBRID_MESH_NAME,
    HYBRID_REPORT_NAME,
    UNKNOWN_CLASSIFICATION,
    build_hybrid_surface,
)
from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.ply_stats import _parse_header
from capture_splat.world_studio_export import MANIFEST_NAME


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
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _ref(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "checksum": _sha256(path),
    }


def _write_arkit_mesh(
    path: Path,
    triangles: list[list[tuple[float, float, float]]],
    classes: list[int],
) -> tuple[np.ndarray, np.ndarray]:
    vertices = np.asarray([point for triangle in triangles for point in triangle], dtype=np.float32)
    faces = np.arange(len(vertices), dtype=np.uint32).reshape(-1, 3)
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(vertices)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\n"
        "property uchar classification\nend_header\n"
    ).encode("ascii")
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(vertices.astype("<f4", copy=False).tobytes())
        for face, classification in zip(faces, classes):
            handle.write(struct.pack("<BIIIB", 3, *face.tolist(), classification))
    return vertices.astype(np.float64), faces.astype(np.int64)


def _write_tsdf_mesh(
    path: Path,
    triangles: list[list[tuple[float, float, float]]],
) -> tuple[np.ndarray, np.ndarray]:
    vertices = np.asarray([point for triangle in triangles for point in triangle], dtype=np.float64)
    faces = np.arange(len(vertices), dtype=np.uint32).reshape(-1, 3)
    face_normals = np.cross(
        vertices[faces[:, 1]] - vertices[faces[:, 0]],
        vertices[faces[:, 2]] - vertices[faces[:, 0]],
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        face_normals /= np.linalg.norm(face_normals, axis=1)[:, None]
    vertex_dtype = np.dtype([
        ("position", "<f8", (3,)),
        ("normal", "<f8", (3,)),
        ("color", "u1", (3,)),
    ])
    records = np.zeros(len(vertices), dtype=vertex_dtype)
    records["position"] = vertices
    records["normal"] = np.repeat(face_normals, 3, axis=0)
    records["color"] = [127, 127, 127]
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment Created by Open3D\n"
        f"element vertex {len(vertices)}\n"
        "property double x\nproperty double y\nproperty double z\n"
        "property double nx\nproperty double ny\nproperty double nz\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        f"element face {len(faces)}\n"
        "property list uchar uint vertex_indices\nend_header\n"
    ).encode("ascii")
    face_dtype = np.dtype([("count", "u1"), ("indices", "<u4", (3,))])
    face_records = np.empty(len(faces), dtype=face_dtype)
    face_records["count"] = 3
    face_records["indices"] = faces
    with path.open("wb") as handle:
        handle.write(header)
        records.tofile(handle)
        face_records.tofile(handle)
    return vertices, faces.astype(np.int64)


def _registration() -> dict[str, object]:
    identity = np.eye(4).tolist()
    return {
        "schema": "capture_splat.metric_registration.v0.1",
        "status": "accepted",
        "accepted": True,
        "source_coordinate_frame": "arkit_world",
        "source_units": "meters",
        "target_coordinate_frame": "trainer_world",
        "target_units": "normalized_scene_units",
        "matched_cameras": 3,
        "scale": 1.0,
        "matrix": identity,
        "arkit_to_colmap": identity,
        "authority": {
            "camera_center_alignment_evidence": True,
            "metric_mesh_registration_candidate": True,
            "collision_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
    }


def _write_fixture(
    tmp_path: Path,
    arkit_triangles: list[list[tuple[float, float, float]]],
    classes: list[int],
    tsdf_triangles: list[list[tuple[float, float, float]]] | None = None,
) -> tuple[Path, Path, np.ndarray, np.ndarray]:
    handoff_root = tmp_path / "handoff"
    tsdf_root = tmp_path / "tsdf"
    handoff_root.mkdir(parents=True)
    tsdf_root.mkdir(parents=True)
    capture = handoff_root / "capture.json"
    write_json_strict(capture, {
        "schema": "capture_splat.v0.3",
        "session_config": {
            "scale_authority": "arkit_vio_metric",
            "up_axis": [0, 1, 0],
            "world_alignment": "gravity",
        },
        "frames": [{
            "rgb": "images/000001.jpg",
            "transform_matrix": np.eye(4).tolist(),
            "intrinsics": {
                "fl_x": 100.0,
                "fl_y": 100.0,
                "cx": 50.0,
                "cy": 50.0,
                "w": 100,
                "h": 100,
            },
        }],
    })
    images = handoff_root / "sparse/0/images.txt"
    images.parent.mkdir(parents=True)
    images.write_text("# empty synthetic registration file\n", encoding="utf-8")
    arkit_mesh = handoff_root / "navigation_mesh.ply"
    arkit_vertices, arkit_faces = _write_arkit_mesh(arkit_mesh, arkit_triangles, classes)
    arkit_report = handoff_root / "navigation_mesh_report.json"
    counts = Counter(CLASS_NAMES.get(value, f"class_{value}") for value in classes)
    write_json_strict(arkit_report, {
        "schema": "capture_splat.arkit_mesh_report.v0.1",
        "status": "finite_mesh_written",
        "ply_written": True,
        "non_finite_vertex_count": 0,
        "vertex_count": len(arkit_vertices),
        "triangle_count": len(arkit_faces),
        "truncated": True,
        "classification_counts": dict(sorted(counts.items())),
    })
    manifest_path = handoff_root / MANIFEST_NAME
    manifest = {
        "schema": "capture_splat.world_studio_handoff.v0.3",
        "status": "visual_evidence_with_3dgs_proposal",
        "authority": {
            "metric_authority": False,
            "semantic_authority": False,
            "collision_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
            "source_frames": "visual_evidence",
            "trained_splats": "review_proposal",
        },
        "world_up": [0.0, 1.0, 0.0],
        "world_up_coordinate_frame": "arkit_world",
        "assets": {
            "capture_manifest": _ref(capture, handoff_root),
            "colmap_sparse": {"images.txt": _ref(images, handoff_root)},
            "navigation_mesh": {
                **_ref(arkit_mesh, handoff_root),
                "coordinate_frame": "arkit_world",
                "units": "meters",
                "authority": "metric_capture_evidence",
            },
            "mesh_report": {
                **_ref(arkit_report, handoff_root),
                "coordinate_frame": "arkit_world",
                "units": "meters",
                "authority": "capture_evidence_report",
            },
        },
        "metric_registration": _registration(),
    }
    write_json_strict(manifest_path, manifest)

    tsdf_mesh = tsdf_root / "rgbd_tsdf_mesh.ply"
    tsdf_vertices, tsdf_faces = _write_tsdf_mesh(
        tsdf_mesh, tsdf_triangles if tsdf_triangles is not None else arkit_triangles
    )
    coordinate = {
        "scale_authority": "arkit_vio_metric",
        "up_axis": [0, 1, 0],
        "world_alignment": "gravity",
    }
    tsdf_report = tsdf_root / "capture_splat_rgbd_tsdf_report.json"
    write_json_strict(tsdf_report, {
        "schema": "capture_splat.rgbd_tsdf_report.v0.1",
        "decision": "hold",
        "software_surface_candidate": "hold",
        "authority": {
            "metric_authority": False,
            "collision_authority": False,
            "navigation_authority": False,
        },
        "inputs": {
            "handoff_manifest": _ref(manifest_path, handoff_root),
            "capture_manifest": manifest["assets"]["capture_manifest"],
            "colmap_images": manifest["assets"]["colmap_sparse"]["images.txt"],
        },
        "coordinate_contract": {
            "output_coordinate_frame": "arkit_world",
            "units": "meters",
            "capture_declaration": coordinate,
        },
        "mesh": {
            **_ref(tsdf_mesh, tsdf_root),
            "coordinate_frame": "arkit_world",
            "units": "meters",
            "coordinate_declaration": coordinate,
            "finite": True,
            "budget_limited": False,
            "vertex_count": len(tsdf_vertices),
            "triangle_count": len(tsdf_faces),
            "non_finite_vertex_count": 0,
            "non_finite_normal_count": 0,
            "invalid_index_triangle_count": 0,
            "degenerate_triangle_count": 0,
        },
    })
    return manifest_path, tsdf_report, tsdf_vertices, tsdf_faces


def _rebind_handoff(tsdf_report: Path, handoff: Path) -> None:
    payload = load_json_strict(tsdf_report)
    payload["inputs"]["handoff_manifest"] = _ref(handoff, handoff.parent)
    write_json_strict(tsdf_report, payload)


def _read_output(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with path.open("rb") as handle:
        header, offset = _parse_header(handle)
        vertex, face = header["elements"]
        handle.seek(offset)
        vertex_dtype = np.dtype([
            ("position", "<f8", (3,)),
            ("normal", "<f8", (3,)),
            ("color", "u1", (3,)),
        ])
        vertex_records = np.frombuffer(
            handle.read(int(vertex["count"]) * vertex_dtype.itemsize), dtype=vertex_dtype
        )
        face_dtype = np.dtype([
            ("count", "u1"),
            ("indices", "<u4", (3,)),
            ("classification", "u1"),
            ("support", "u1"),
            ("source_face_index", "<u4"),
        ])
        records = np.frombuffer(
            handle.read(int(face["count"]) * face_dtype.itemsize), dtype=face_dtype
        )
    return (
        vertex_records["position"],
        records["indices"].astype(np.int64),
        records["classification"],
        records["support"],
        records["source_face_index"],
    )


def _vertex_bytes(path: Path) -> bytes:
    with path.open("rb") as handle:
        header, offset = _parse_header(handle)
        vertex = header["elements"][0]
        handle.seek(offset)
        return handle.read(int(vertex["count"]) * 51)


def _triangle_floor() -> list[tuple[float, float, float]]:
    return [(0, 0, 0), (0.4, 0, 0), (0, 0, 0.4)]


def _triangle_wall() -> list[tuple[float, float, float]]:
    return [(2, 0, 0), (2, 0.4, 0), (2, 0, 0.4)]


def _triangle_ceiling() -> list[tuple[float, float, float]]:
    return [(0, 2, 0), (0, 2, 0.4), (0.4, 2, 0)]


def test_three_plane_transfer_preserves_topology_partition_and_authority(tmp_path: Path) -> None:
    pytest.importorskip("open3d")
    triangles = [_triangle_floor(), _triangle_wall(), _triangle_ceiling()]
    handoff, tsdf_report, source_vertices, source_faces = _write_fixture(
        tmp_path, triangles, [2, 1, 3]
    )

    summary = build_hybrid_surface(handoff, tsdf_report, tmp_path / "out")
    vertices, faces, classes, support, source_mapping = _read_output(
        tmp_path / "out" / HYBRID_MESH_NAME
    )

    assert np.array_equal(vertices, source_vertices)
    assert np.array_equal(faces, source_faces)
    assert classes.tolist() == [2, 1, 3]
    assert support.tolist() == [4, 4, 4]
    assert source_mapping.tolist() == [0, 1, 2]
    assert _vertex_bytes(tmp_path / "tsdf/rgbd_tsdf_mesh.ply") == _vertex_bytes(
        tmp_path / "out" / HYBRID_MESH_NAME
    )
    assert _vertex_bytes(tmp_path / "tsdf/rgbd_tsdf_mesh.ply") == _vertex_bytes(
        tmp_path / "out" / COLLIDER_MESH_NAME
    )
    assert summary["status"] == "held"
    assert summary["semantics"]["partition_invariant"] is True
    assert summary["semantics"]["transferred_face_count"] == 3
    assert summary["semantics"]["unknown_face_count"] == 0
    geometry = summary["semantics"]["geometry"]
    assert geometry["component_connectivity"] == "same_partition_faces_sharing_an_undirected_mesh_edge"
    assert geometry["face_partition_invariant"] is True
    assert geometry["area_partition_invariant"] is True
    assert geometry["partitions"]["floor"]["face_count"] == 1
    assert geometry["partitions"]["floor"]["area_square_meters"] == pytest.approx(0.08)
    assert geometry["partitions"]["floor"]["connected_component_count"] == 1
    assert geometry["partitions"]["floor"]["bounds_meters"] == {
        "minimum": [0.0, 0.0, 0.0],
        "maximum": [0.4, 0.0, 0.4],
        "extent": [0.4, 0.0, 0.4],
    }
    assert not any(summary["authority"].values())
    assert summary["topology"]["vertex_records_copied_byte_for_byte"] is True


def test_door_surface_is_preserved_but_far_surface_remains_unknown(tmp_path: Path) -> None:
    pytest.importorskip("open3d")
    door = [(4, 0, 0), (4, 0.5, 0), (4, 0, 0.5)]
    far = [(8, 0, 0), (8, 0.5, 0), (8, 0, 0.5)]
    handoff, tsdf_report, _, _ = _write_fixture(
        tmp_path, [door], [7], tsdf_triangles=[door, far]
    )

    build_hybrid_surface(handoff, tsdf_report, tmp_path / "out")
    _, _, classes, support, _ = _read_output(tmp_path / "out" / HYBRID_MESH_NAME)
    report = load_json_strict(tmp_path / "out" / HYBRID_REPORT_NAME)
    collider = load_json_strict(tmp_path / "out" / COLLIDER_REPORT_NAME)

    assert classes.tolist() == [7, UNKNOWN_CLASSIFICATION]
    assert support.tolist() == [4, 0]
    assert report["surface_evidence"]["door_surface"] == 1
    assert report["surface_evidence"]["door_surface_is_not_opening_clearance"] is True
    assert report["semantics"]["unknown_reason_counts"] == {"distance_unsupported": 1}
    assert report["surface_evidence"]["free_space"]["status"] == "unavailable"
    assert collider["rails"]["doorway_clearance"] == "held_unresolved"
    assert collider["rails"]["fallback_floor"] == "not_added"
    assert not any(collider["authority"].values())


def test_overlapping_conflicting_classes_are_ambiguous_and_unknown(tmp_path: Path) -> None:
    pytest.importorskip("open3d")
    wall = _triangle_wall()
    handoff, tsdf_report, _, _ = _write_fixture(
        tmp_path, [wall, wall], [1, 7], tsdf_triangles=[wall]
    )

    report = build_hybrid_surface(handoff, tsdf_report, tmp_path / "out")
    _, _, classes, support, _ = _read_output(tmp_path / "out" / HYBRID_MESH_NAME)

    assert classes.tolist() == [UNKNOWN_CLASSIFICATION]
    assert support.tolist() == [0]
    assert report["semantics"]["unknown_reason_counts"] == {"nearest_class_ambiguous": 1}
    assert report["semantics"]["sample_statistics"]["ambiguous_sample_count"] == 4
    unknown = report["semantics"]["geometry"]["partitions"]["unknown"]
    assert unknown["face_count"] == 1
    assert unknown["connected_component_count"] == 1


def test_hybrid_outputs_and_reports_are_deterministic(tmp_path: Path) -> None:
    pytest.importorskip("open3d")
    triangles = [_triangle_floor(), _triangle_wall(), _triangle_ceiling()]
    handoff, tsdf_report, _, _ = _write_fixture(tmp_path, triangles, [2, 1, 3])

    build_hybrid_surface(handoff, tsdf_report, tmp_path / "first")
    build_hybrid_surface(handoff, tsdf_report, tmp_path / "second")

    for name in (HYBRID_MESH_NAME, COLLIDER_MESH_NAME, HYBRID_REPORT_NAME, COLLIDER_REPORT_NAME):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


@pytest.mark.parametrize("inside", ["handoff", "tsdf"])
def test_hybrid_rejects_output_inside_an_input(tmp_path: Path, inside: str) -> None:
    handoff, tsdf_report, _, _ = _write_fixture(tmp_path, [_triangle_floor()], [2])
    output = (handoff.parent if inside == "handoff" else tsdf_report.parent) / "derived"

    with pytest.raises(ValueError, match="outside every immutable input"):
        build_hybrid_surface(handoff, tsdf_report, output)
    assert not output.exists()


def test_hybrid_rejects_tampered_tsdf_hash(tmp_path: Path) -> None:
    handoff, tsdf_report, _, _ = _write_fixture(tmp_path, [_triangle_floor()], [2])
    with (tsdf_report.parent / "rgbd_tsdf_mesh.ply").open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(ValueError, match="does not match its size and checksum"):
        build_hybrid_surface(handoff, tsdf_report, tmp_path / "out")


def test_hybrid_rejects_invalid_units_and_registration(tmp_path: Path) -> None:
    handoff, tsdf_report, _, _ = _write_fixture(tmp_path, [_triangle_floor()], [2])
    manifest = load_json_strict(handoff)
    manifest["assets"]["navigation_mesh"]["units"] = "centimeters"
    write_json_strict(handoff, manifest)
    _rebind_handoff(tsdf_report, handoff)

    with pytest.raises(ValueError, match="coordinate contract"):
        build_hybrid_surface(handoff, tsdf_report, tmp_path / "bad_units")

    manifest["assets"]["navigation_mesh"]["units"] = "meters"
    manifest["metric_registration"]["accepted"] = False
    write_json_strict(handoff, manifest)
    _rebind_handoff(tsdf_report, handoff)
    with pytest.raises(ValueError, match="metric registration"):
        build_hybrid_surface(handoff, tsdf_report, tmp_path / "bad_registration")


@pytest.mark.parametrize("failure", ["nonfinite", "degenerate"])
def test_hybrid_rejects_invalid_tsdf_geometry(tmp_path: Path, failure: str) -> None:
    triangle = _triangle_floor()
    if failure == "nonfinite":
        triangle = [(float("nan"), 0, 0), triangle[1], triangle[2]]
    elif failure == "degenerate":
        triangle = [(0, 0, 0), (0.1, 0, 0), (0.2, 0, 0)]
    handoff, tsdf_report, _, _ = _write_fixture(
        tmp_path, [_triangle_floor()], [2], tsdf_triangles=[triangle]
    )

    with pytest.raises(ValueError, match="non-finite|degenerate"):
        build_hybrid_surface(handoff, tsdf_report, tmp_path / "out")


def test_hybrid_rejects_symlink_and_nonregular_tsdf_report(tmp_path: Path) -> None:
    handoff, tsdf_report, _, _ = _write_fixture(tmp_path, [_triangle_floor()], [2])
    symlink = tmp_path / "tsdf-report-link.json"
    symlink.symlink_to(tsdf_report)

    with pytest.raises(ValueError, match="symbolic link"):
        build_hybrid_surface(handoff, symlink, tmp_path / "symlink_out")
    with pytest.raises(ValueError, match="regular file"):
        build_hybrid_surface(handoff, tsdf_report.parent, tmp_path / "directory_out")


def test_hybrid_rejects_symlinked_handoff_and_output(tmp_path: Path) -> None:
    handoff, tsdf_report, _, _ = _write_fixture(tmp_path, [_triangle_floor()], [2])
    handoff_link = tmp_path / "handoff-link"
    handoff_link.symlink_to(handoff.parent, target_is_directory=True)

    with pytest.raises(ValueError, match="handoff input must not be a symbolic link"):
        build_hybrid_surface(handoff_link, tsdf_report, tmp_path / "handoff_link_out")

    output_target = tmp_path / "output-target"
    output_target.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(output_target, target_is_directory=True)
    with pytest.raises(ValueError, match="hybrid output must not be a symbolic link"):
        build_hybrid_surface(handoff, tsdf_report, output_link)
