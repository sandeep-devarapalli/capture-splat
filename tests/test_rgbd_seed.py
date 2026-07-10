from pathlib import Path

import numpy as np
from PIL import Image

from capture_splat.json_utils import load_json_strict, write_json_strict
from capture_splat.rgbd_seed import apply_sim3, build_rgbd_metric_seed, estimate_sim3


def _source_positions() -> np.ndarray:
    return np.asarray([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.1],
        [0.1, 1.0, 0.2],
        [1.0, 1.0, 0.4],
        [0.3, 0.2, 1.0],
        [1.2, 0.4, 1.1],
        [0.2, 1.3, 0.8],
        [1.1, 1.2, 1.4],
    ])


def test_estimate_sim3_recovers_known_transform() -> None:
    source = _source_positions()
    angle = np.deg2rad(25)
    rotation = np.asarray([
        [np.cos(angle), -np.sin(angle), 0],
        [np.sin(angle), np.cos(angle), 0],
        [0, 0, 1],
    ])
    target = apply_sim3(source, 1.7, rotation, np.asarray([2.0, -1.0, 0.5]))

    scale, estimated_rotation, translation = estimate_sim3(source, target)

    np.testing.assert_allclose(scale, 1.7, atol=1e-9)
    np.testing.assert_allclose(estimated_rotation, rotation, atol=1e-9)
    np.testing.assert_allclose(translation, [2.0, -1.0, 0.5], atol=1e-9)


def _write_capture(root: Path, positions: np.ndarray) -> Path:
    root.mkdir(parents=True)
    frames = []
    for index, position in enumerate(positions, start=1):
        image = root / "images" / f"{index:06d}.jpg"
        depth = root / "depth" / f"{index:06d}.npy"
        confidence = root / "confidence" / f"{index:06d}.npy"
        image.parent.mkdir(parents=True, exist_ok=True)
        depth.parent.mkdir(parents=True, exist_ok=True)
        confidence.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 6), (20 * index, 80, 120)).save(image)
        np.save(depth, np.full((3, 4), 1.0, dtype=np.float32), allow_pickle=False)
        np.save(confidence, np.full((3, 4), 2, dtype=np.uint8), allow_pickle=False)
        transform = np.eye(4)
        transform[:3, 3] = position
        frames.append({
            "rgb": image.relative_to(root).as_posix(),
            "depth": depth.relative_to(root).as_posix(),
            "confidence": confidence.relative_to(root).as_posix(),
            "timestamp": float(index),
            "transform_matrix": transform.tolist(),
            "intrinsics": {"fl_x": 4, "fl_y": 4, "cx": 2, "cy": 1.5, "w": 4, "h": 3},
            "capture_quality": {"accepted": True},
        })
    write_json_strict(root / "capture.json", {"schema": "capture_splat.v0.3", "frames": frames})
    return root


def _write_package(root: Path, centers: np.ndarray) -> Path:
    sparse = root / "sparse/0"
    images = root / "images"
    sparse.mkdir(parents=True)
    images.mkdir(parents=True)
    (sparse / "cameras.txt").write_text("# cameras\n1 PINHOLE 8 6 4 4 4 3\n", encoding="utf-8")
    lines = ["# images"]
    for index, center in enumerate(centers, start=1):
        translation = -center
        lines.extend([
            f"{index} 1 0 0 0 {translation[0]} {translation[1]} {translation[2]} 1 {index:06d}.jpg",
            "",
        ])
    (sparse / "images.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# points\n", encoding="utf-8")
    (sparse / "points3D.bin").write_bytes(b"original-binary")
    return root


def test_build_rgbd_seed_augments_only_copied_package(tmp_path: Path) -> None:
    source = _source_positions()
    target = source * 2.0 + np.asarray([1.0, 2.0, 3.0])
    capture = _write_capture(tmp_path / "capture", source)
    package = _write_package(tmp_path / "package", target)

    summary = build_rgbd_metric_seed(capture, package, tmp_path / "out", max_points=100)

    assert summary["decision"] == "promote"
    assert summary["alignment"]["accepted"] is True
    assert 0 < summary["seed_point_count"] <= 100
    assert summary["package_augmented"] is True
    assert (tmp_path / "out/metric_seed.ply").exists()
    assert not (tmp_path / "out/package/sparse/0/points3D.bin").exists()
    assert (tmp_path / "out/package/sparse/0_colmap_refined/points3D.bin").exists()
    assert (tmp_path / "package/sparse/0/points3D.txt").read_text(encoding="utf-8") == "# points\n"
    assert len((tmp_path / "out/package/sparse/0/points3D.txt").read_text(encoding="utf-8").splitlines()) > 1


def test_build_rgbd_seed_holds_on_bad_alignment_without_augmentation(tmp_path: Path) -> None:
    source = _source_positions()
    target = source * 2.0
    target[::2] += np.asarray([3.0, -2.0, 1.0])
    capture = _write_capture(tmp_path / "capture", source)
    package = _write_package(tmp_path / "package", target)

    summary = build_rgbd_metric_seed(capture, package, tmp_path / "out")
    saved = load_json_strict(tmp_path / "out/capture_splat_rgbd_seed_summary.json")

    assert summary["decision"] == "hold"
    assert saved["alignment"]["accepted"] is False
    assert saved["package_augmented"] is False
    assert (tmp_path / "out/package/sparse/0/points3D.bin").exists()
    assert not (tmp_path / "out/metric_seed.ply").exists()
