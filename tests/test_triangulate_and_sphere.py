from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from capture_splat.background_sphere import append_background_sphere, fibonacci_sphere, read_positions
from capture_splat.json_utils import load_json_strict
from capture_splat.sfm_runner import run_triangulate


def make_pose_package(root: Path) -> Path:
    package = root / "package"
    images = package / "images"
    images.mkdir(parents=True)
    for index in range(3):
        Image.new("RGB", (8, 6), (index * 40, 90, 120)).save(images / f"{index + 1:06d}.jpg")
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    (sparse / "cameras.txt").write_text("1 PINHOLE 8 6 8 8 4 3\n", encoding="utf-8")
    lines = []
    for index in range(3):
        lines.append(f"{index + 1} 1 0 0 0 {index * 0.5} 0.1 {2 + index * 0.1} 1 {index + 1:06d}.jpg")
        lines.append("")
    (sparse / "images.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# Empty seed cloud.\n", encoding="utf-8")
    return package


def test_triangulate_dry_run_records_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("capture_splat.sfm_runner.find_binary", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("capture_splat.sfm_runner.colmap_has_cuda", lambda: True)
    package = make_pose_package(tmp_path)

    summary = run_triangulate(package, tmp_path / "out", refine_poses=True, dry_run=True)

    names = [command[1] for command in summary["commands"]]
    assert names == ["feature_extractor", "sequential_matcher", "point_triangulator", "bundle_adjuster"]
    assert summary["decision"] == "dry_run"
    assert summary["authority"]["pose_prior"] == "device_poses"
    assert summary["colmap_cuda"] is True
    saved = load_json_strict(tmp_path / "out" / "capture_splat_sfm_summary.json")
    assert saved["mode"] == "triangulate_device_pose_prior"


def test_triangulate_requires_poses(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "images").mkdir(parents=True)
    Image.new("RGB", (8, 6)).save(package / "images" / "000001.jpg")

    with pytest.raises(FileNotFoundError, match="poses missing"):
        run_triangulate(package, tmp_path / "out", dry_run=True)


def test_fibonacci_sphere_radius_and_count() -> None:
    center = np.array([1.0, 2.0, 3.0])
    points = fibonacci_sphere(64, 2.5, center)

    assert points.shape == (64, 3)
    distances = np.linalg.norm(points - center, axis=1)
    assert distances.max() == pytest.approx(2.5, rel=1e-6)
    assert distances.min() == pytest.approx(2.5, rel=1e-6)


def test_background_sphere_sizes_from_camera_centers_when_no_points(tmp_path: Path) -> None:
    package = make_pose_package(tmp_path)
    sparse = package / "sparse" / "0"

    result = append_background_sphere(sparse, count=32)

    assert result["background_sphere_size_source"] == "camera_centers"
    assert result["background_sphere_points"] == 32
    lines = [line for line in (sparse / "points3D.txt").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    assert len(lines) == 32
    first = lines[0].split()
    assert first[0] == str(result["background_sphere_first_id"])
    assert first[4:7] == ["128", "128", "128"]


def test_background_sphere_uses_points_and_appends_ids(tmp_path: Path) -> None:
    package = make_pose_package(tmp_path)
    sparse = package / "sparse" / "0"
    rows = [f"{index + 1} {np.cos(index)} {0.1 * index} {np.sin(index)} 10 10 10 0.5 1 0" for index in range(12)]
    (sparse / "points3D.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")

    positions, source = read_positions(sparse)
    result = append_background_sphere(sparse, count=16)

    assert source == "sparse_points"
    assert result["background_sphere_first_id"] == 13
    distances = np.linalg.norm(positions - np.median(positions, axis=0), axis=1)
    assert result["background_sphere_radius"] == pytest.approx(2.0 * float(np.percentile(distances, 95)))
