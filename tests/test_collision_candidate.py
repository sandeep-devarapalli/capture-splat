from pathlib import Path
import struct

import pytest

from capture_splat.collision_candidate import (
    REPORT_NAME,
    _load_mesh,
    build_collision_candidate,
)
from capture_splat.json_utils import load_json_strict, write_json_strict


def write_classified_mesh(path: Path, include_wall: bool = True) -> tuple[int, int]:
    vertices = [
        (0, 0, 0), (1, 0, 0), (0, 0, 1),
        (0, 0, 0), (0, 1, 0), (0, 0, 1),
    ]
    faces = [
        (0, 1, 2, 2),
        (0, 2, 1, 2),
    ]
    if include_wall:
        faces += [
            (3, 4, 5, 1),
            (3, 5, 4, 1),
        ]
    header = "\n".join([
            "ply",
            "format binary_little_endian 1.0",
            f"element vertex {len(vertices)}",
            "property float x",
            "property float y",
            "property float z",
            f"element face {len(faces)}",
            "property list uchar uint vertex_indices",
            "property uchar classification",
            "end_header",
        ]) + "\n"
    with path.open("wb") as handle:
        handle.write(header.encode("ascii"))
        for vertex in vertices:
            handle.write(struct.pack("<fff", *vertex))
        for a, b, c, classification in faces:
            handle.write(struct.pack("<BIIIB", 3, a, b, c, classification))
    return len(vertices), len(faces)


def write_mesh_report(path: Path, vertex_count: int, triangle_count: int) -> None:
    write_json_strict(path, {
        "schema": "capture_splat.arkit_mesh_report.v0.2",
        "status": "finite_mesh_written",
        "ply_written": True,
        "non_finite_vertex_count": 0,
        "vertex_count": vertex_count,
        "triangle_count": triangle_count,
        "budget_limited": False,
    })


def test_collision_candidate_preserves_floor_wall_groups_and_holds(tmp_path: Path) -> None:
    mesh = tmp_path / "arkit_mesh.ply"
    vertex_count, triangle_count = write_classified_mesh(mesh)
    report = tmp_path / "arkit_mesh_report.json"
    write_mesh_report(report, vertex_count, triangle_count)

    summary = build_collision_candidate(mesh, report, tmp_path / "candidate", max_faces=2)
    written = load_json_strict(tmp_path / "candidate" / REPORT_NAME)
    vertices, faces, classifications = _load_mesh(tmp_path / "candidate/collision_candidate.ply")

    assert summary["decision"] == "hold"
    assert summary["software_prerequisites"] is True
    assert summary["reason"] == "physical_floor_wall_and_splat_registration_validation_pending"
    assert summary["coverage"]["spatial_cell_coverage_ratio"] == 1.0
    assert set(classifications.tolist()) == {1, 2}
    assert len(vertices) == 6
    assert len(faces) == 2
    assert written["candidate"]["checksum"].startswith("sha256:")
    assert written["authority"]["collision_authority"] is False


def test_collision_candidate_holds_when_room_wall_evidence_is_missing(tmp_path: Path) -> None:
    mesh = tmp_path / "arkit_mesh.ply"
    vertex_count, triangle_count = write_classified_mesh(mesh, include_wall=False)
    report = tmp_path / "arkit_mesh_report.json"
    write_mesh_report(report, vertex_count, triangle_count)

    summary = build_collision_candidate(mesh, report, tmp_path / "candidate")

    assert summary["decision"] == "hold"
    assert summary["software_prerequisites"] is False
    assert summary["missing_required_classes"] == ["wall"]
    assert summary["reason"] == "floor_or_wall_evidence_missing"


def test_collision_candidate_rejects_unvalidated_source_with_strict_report(tmp_path: Path) -> None:
    mesh = tmp_path / "arkit_mesh.ply"
    vertex_count, triangle_count = write_classified_mesh(mesh)
    report = tmp_path / "arkit_mesh_report.json"
    write_mesh_report(report, vertex_count, triangle_count)
    payload = load_json_strict(report)
    payload["coverage_preserving"] = False
    payload["budget_limited"] = True
    write_json_strict(report, payload)

    with pytest.raises(ValueError, match="source_mesh_not_coverage_preserving"):
        build_collision_candidate(mesh, report, tmp_path / "candidate")

    rejected = load_json_strict(tmp_path / "candidate" / REPORT_NAME)
    assert rejected["decision"] == "reject"
    assert rejected["source_mesh_evidence"]["accepted"] is False
