from pathlib import Path

import numpy as np
import pytest

from capture_splat.apriltag_scale import REPORT_NAME, apriltag_status, validate_apriltag_scale
from capture_splat.json_utils import load_json_strict, write_json_strict


def make_package(root: Path) -> tuple[Path, Path]:
    package = root / "package"
    sparse = package / "sparse/0"
    sparse.mkdir(parents=True)
    (package / "images").mkdir()
    (sparse / "cameras.txt").write_text("1 PINHOLE 100 100 100 100 50 50\n", encoding="utf-8")
    poses = []
    centers = [(-0.5, 0.0, 0.0), (0.5, 0.0, 0.0), (0.0, 0.5, 0.0)]
    for index, center in enumerate(centers, start=1):
        name = f"{index:06d}.jpg"
        (package / "images" / name).write_bytes(b"image")
        translation = tuple(-value for value in center)
        poses.extend([
            f"{index} 1 0 0 0 {translation[0]} {translation[1]} {translation[2]} 1 {name}",
            "",
        ])
    (sparse / "images.txt").write_text("\n".join(poses) + "\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# empty\n", encoding="utf-8")

    corners = np.asarray([
        [-0.5, -0.5, 3.0],
        [0.5, -0.5, 3.0],
        [0.5, 0.5, 3.0],
        [-0.5, 0.5, 3.0],
    ])
    detections = []
    for index, center in enumerate(centers, start=1):
        camera = corners - np.asarray(center)
        pixels = np.column_stack([
            100.0 * camera[:, 0] / camera[:, 2] + 50.0,
            100.0 * camera[:, 1] / camera[:, 2] + 50.0,
        ])
        detections.append({
            "image": f"{index:06d}.jpg",
            "tags": [{"tag_id": 0, "corners": pixels.tolist()}],
        })
    detections_path = root / "detections.json"
    write_json_strict(detections_path, {
        "schema": "capture_splat.apriltag_detections.v0.1",
        "family": "tagStandard41h12",
        "images": detections,
    })
    return package, detections_path


def write_points(path: Path) -> None:
    path.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\n"
        "property float x\nproperty float y\nproperty float z\n"
        "end_header\n0 0 0\n",
        encoding="ascii",
    )


def test_apriltag_scale_promotes_metric_package_and_binds_artifact(tmp_path: Path) -> None:
    package, detections = make_package(tmp_path)
    artifact = tmp_path / "metric_seed.ply"
    write_points(artifact)

    summary = validate_apriltag_scale(
        package,
        tmp_path / "out",
        tag_size_meters=1.0,
        detections_json=detections,
        artifact=artifact,
    )

    assert summary["decision"] == "promote"
    assert summary["tag_count"] == 1
    assert summary["scale_meters_per_colmap_unit"] == pytest.approx(1.0)
    assert summary["reprojection_p95_pixels"] == pytest.approx(0.0, abs=1e-9)
    assert summary["validated_artifact"]["checksum"].startswith("sha256:")
    assert summary["validated_artifact"]["point_count"] == 1
    assert summary["authority"]["known_scale_validation"] is True
    assert summary["authority"]["measurement_authority"] is False
    assert load_json_strict(tmp_path / "out" / REPORT_NAME)["decision"] == "promote"


def test_apriltag_scale_rejects_metric_scale_regression(tmp_path: Path) -> None:
    package, detections = make_package(tmp_path)

    summary = validate_apriltag_scale(
        package,
        tmp_path / "out",
        tag_size_meters=2.0,
        detections_json=detections,
    )

    assert summary["decision"] == "reject"
    assert summary["scale_meters_per_colmap_unit"] == pytest.approx(2.0)
    assert summary["failures"] == ["metric_scale_error_exceeded"]
    assert summary["authority"]["known_scale_validation"] is False


def test_apriltag_scale_writes_reject_report_for_invalid_detections(tmp_path: Path) -> None:
    package, detections = make_package(tmp_path)
    detections.write_text(
        '{"schema":"capture_splat.apriltag_detections.v0.1","family":"tagStandard41h12",'
        '"images":[{"image":"000001.jpg","tags":[{"tag_id":0,"corners":[[0,0],[1,0],[1,1]]}]}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="four corners"):
        validate_apriltag_scale(
            package,
            tmp_path / "out",
            tag_size_meters=1.0,
            detections_json=detections,
        )

    assert load_json_strict(tmp_path / "out" / REPORT_NAME)["decision"] == "reject"
    assert "pupil_apriltags_available" in apriltag_status()
