import json
import math
from pathlib import Path

import numpy as np
import pytest

from capture_splat.json_utils import load_json_strict
from capture_splat.scene_transform import (
    SIDECAR_NAME,
    compute_gsplat_normalize_transform,
    disambiguate_flip_with_ply,
    estimate_package_orientation_transform,
    load_camera_to_worlds,
    load_named_camera_to_worlds,
    load_ply_positions,
    metric_package_status,
    resolve_normalization_policy,
    similarity_from_cameras,
    transform_points,
    write_scene_transform_sidecar,
)


def write_binary_ply(path: Path, positions: np.ndarray, extra_props: int = 2) -> None:
    props = ["x", "y", "z"] + [f"f_{i}" for i in range(extra_props)]
    header = "ply\nformat binary_little_endian 1.0\n"
    header += f"element vertex {len(positions)}\n"
    header += "".join(f"property float {name}\n" for name in props)
    header += "end_header\n"
    body = np.zeros((len(positions), len(props)), dtype="<f4")
    body[:, :3] = positions
    path.write_bytes(header.encode("ascii") + body.tobytes())


def skewed_cloud(count: int = 240) -> np.ndarray:
    rng = np.random.default_rng(7)
    cloud = rng.standard_normal((count, 3)) * np.array([2.0, 0.6, 1.1])
    cloud[:, 1] += 0.4 * cloud[:, 0] ** 2 / 4.0
    return cloud


def write_sparse(sparse: Path, camera_heights: list[float]) -> None:
    sparse.mkdir(parents=True, exist_ok=True)
    lines = ["# images"]
    for index, height in enumerate(camera_heights, start=1):
        angle = 2 * math.pi * index / len(camera_heights)
        tx, ty, tz = 2 * math.cos(angle), height, 2 * math.sin(angle)
        lines.append(f"{index} 1 0 0 0 {tx} {ty} {tz} 1 {index:06d}.jpg")
        lines.append("")
    (sparse / "images.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    points = ["# points"]
    for index in range(12):
        points.append(f"{index + 1} {index * 0.3 - 1.5} {0.1 * (index % 3)} {(index % 4) - 1.5} 128 128 128 0.5 1 0")
    (sparse / "points3D.txt").write_text("\n".join(points) + "\n", encoding="utf-8")


def test_load_camera_to_worlds_inverts_w2c(tmp_path: Path) -> None:
    sparse = tmp_path / "0"
    sparse.mkdir()
    (sparse / "images.txt").write_text("# h\n1 1 0 0 0 1 2 3 1 a.jpg\n\n", encoding="utf-8")

    c2w = load_camera_to_worlds(sparse)

    assert c2w.shape == (1, 4, 4)
    assert np.allclose(c2w[0][:3, 3], [-1, -2, -3])


def test_camera_pose_loaders_honor_blank_points_and_comments(tmp_path: Path) -> None:
    sparse = tmp_path / "0"
    sparse.mkdir()
    (sparse / "images.txt").write_text(
        "# images\n"
        "1 1 0 0 0 1 2 3 1 first.jpg\n"
        "# blank POINTS2D row follows\n"
        "\n"
        "# next image\n"
        "2 1 0 0 0 4 5 6 1 second.jpg\n"
        "100.5 200.5 1 300.5 400.5 2 500.5 600.5 3 700.5 800.5 4\n",
        encoding="utf-8",
    )

    c2w = load_camera_to_worlds(sparse)
    named = load_named_camera_to_worlds(sparse)

    assert c2w.shape == (2, 4, 4)
    assert set(named) == {"first.jpg", "second.jpg"}
    assert np.allclose(named["first.jpg"][:3, 3], [-1, -2, -3])
    assert np.allclose(named["second.jpg"][:3, 3], [-4, -5, -6])


def test_similarity_from_cameras_normalizes_median_distance() -> None:
    c2w = np.stack([np.eye(4) for _ in range(4)])
    for index in range(4):
        angle = math.pi * index / 2
        c2w[index][:3, 3] = [3 * math.cos(angle), 0.2, 3 * math.sin(angle)]

    transform = similarity_from_cameras(c2w)
    centers = transform_points(transform, c2w[:, :3, 3])

    assert np.median(np.linalg.norm(centers, axis=-1)) == pytest.approx(1.0, abs=1e-6)


def test_compute_transform_is_finite_similarity(tmp_path: Path) -> None:
    sparse = tmp_path / "0"
    write_sparse(sparse, [0.1, 0.2, 0.15, 0.1, 0.2, 0.12])

    result = compute_gsplat_normalize_transform(sparse)
    matrix = np.asarray(result["transform"])

    assert matrix.shape == (4, 4)
    assert np.isfinite(matrix).all()
    assert result["camera_count"] == 6
    assert result["point_count"] == 12
    linear = matrix[:3, :3]
    scales = np.linalg.norm(linear, axis=0)
    assert scales.max() == pytest.approx(scales.min(), rel=1e-6)


def test_sidecar_prefers_train_json(tmp_path: Path) -> None:
    ply = tmp_path / "splat.ply"
    ply.write_bytes(b"ply")
    (tmp_path / "train.json").write_text(json.dumps({"dataparser_transform": np.eye(4).tolist()}), encoding="utf-8")

    sidecar = write_scene_transform_sidecar(ply, None, "vksplat", normalized=True)
    saved = load_json_strict(tmp_path / SIDECAR_NAME)

    assert sidecar is not None
    assert saved["trainer_transform_source"] == "trainer_train_json"
    assert saved["trainer"] == "vksplat"
    assert saved["authority"]["quality_claim"] is False


def test_package_orientation_transform_fits_matched_camera_centers(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    source_centers = ([0, 0, 0], [1, 0, 0], [0, 0, 1], [1, 1, 1])
    angle = math.pi / 2
    rotation = np.array([
        [math.cos(angle), 0, math.sin(angle)],
        [0, 1, 0],
        [-math.sin(angle), 0, math.cos(angle)],
    ])
    target_centers = [2 * rotation @ np.asarray(center) + [3, 4, 5] for center in source_centers]

    def write_centers(path: Path, centers: list[np.ndarray | list[int]]) -> None:
        lines = ["# images"]
        for index, center in enumerate(centers, start=1):
            tx, ty, tz = (-np.asarray(center)).tolist()
            lines.extend((
                f"{index} 1 0 0 0 {tx} {ty} {tz} 1 {index:06d}.jpg",
                f"100.5 200.5 {index} 300.5 400.5 {index} 500.5 600.5 {index} 700.5 800.5 {index}",
            ))
        (path / "images.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    write_centers(before, source_centers)
    write_centers(after, target_centers)

    report = estimate_package_orientation_transform(before, after)

    matrix = np.asarray(report["transform"])
    transformed = transform_points(matrix, np.asarray(source_centers, dtype=float))
    assert np.allclose(transformed, target_centers, atol=1e-9)
    assert report["matched_camera_count"] == 4
    assert report["scale"] == pytest.approx(2.0)
    assert report["max_camera_center_residual"] < 1e-9


def test_sidecar_preserves_package_orientation_separately(tmp_path: Path) -> None:
    package = tmp_path / "package"
    sparse = package / "sparse" / "0"
    sparse.mkdir(parents=True)
    ply = tmp_path / "run" / "splat.ply"
    ply.parent.mkdir()
    ply.write_bytes(b"ply")
    (ply.parent / "train.json").write_text(
        json.dumps({"dataparser_transform": np.eye(4).tolist()}),
        encoding="utf-8",
    )
    orientation = np.eye(4)
    orientation[:3, 3] = [1, 2, 3]
    metadata = package / "metadata"
    metadata.mkdir()
    (metadata / "package_orientation_transform.json").write_text(json.dumps({
        "schema": "capture_splat.package_orientation_transform.v0.1",
        "transform": orientation.tolist(),
        "matched_camera_count": 12,
        "scale": 1.0,
        "median_camera_center_residual": 1e-8,
        "max_camera_center_residual": 3e-8,
    }), encoding="utf-8")

    sidecar = write_scene_transform_sidecar(ply, sparse, "gsplat", normalized=True)

    assert sidecar is not None
    assert sidecar["package_orientation_transform"] == orientation.tolist()
    assert sidecar["package_orientation_transform_source"] == "package_orientation_transform.json"
    assert sidecar["trainer_transform"] == np.eye(4).tolist()


def test_sidecar_recomputes_gsplat_normalize(tmp_path: Path) -> None:
    ply = tmp_path / "point_cloud_6999.ply"
    ply.write_bytes(b"ply")
    sparse = tmp_path / "sparse0"
    write_sparse(sparse, [0.1, 0.2, 0.15, 0.1])

    sidecar = write_scene_transform_sidecar(ply, sparse, "gsplat", normalized=True)

    assert sidecar is not None
    assert sidecar["trainer_transform_source"] == "recomputed_gsplat_parser_normalize"
    assert len(sidecar["trainer_transform"]) == 4
    assert sidecar["flip_disambiguation"]["applied"] is False


def test_load_ply_positions_reads_binary_vertices(tmp_path: Path) -> None:
    positions = skewed_cloud(50)
    ply = tmp_path / "cloud.ply"
    write_binary_ply(ply, positions)

    loaded = load_ply_positions(ply, limit=50)

    assert loaded.shape == (50, 3)
    assert np.allclose(loaded, positions, atol=1e-5)


def test_disambiguate_flip_recovers_trainer_transform() -> None:
    points = skewed_cloud()
    angle = 0.7
    rotation = np.array([
        [math.cos(angle), 0.0, math.sin(angle)],
        [0.0, 1.0, 0.0],
        [-math.sin(angle), 0.0, math.cos(angle)],
    ])
    t_true = np.eye(4)
    t_true[:3, :3] = 0.4 * rotation
    t_true[:3, 3] = [0.3, -0.2, 0.9]
    ply_positions = transform_points(t_true, points)
    flipped = t_true.copy()
    flipped[:3, :] = np.diag([1.0, -1.0, -1.0]) @ flipped[:3, :]

    resolved, report = disambiguate_flip_with_ply(flipped, points, ply_positions)

    assert report["applied"] is True
    assert report["chosen_signs"] == [1.0, -1.0, -1.0]
    assert np.allclose(resolved, t_true, atol=1e-9)
    assert report["chamfer_margin_over_best"] > 1.2


def test_sidecar_flip_disambiguation_matches_trained_ply(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse0"
    write_sparse(sparse, [0.1, 0.2, 0.15, 0.1])
    points = []
    for index, point in enumerate(skewed_cloud(80), start=1):
        points.append(f"{index} {point[0]} {point[1]} {point[2]} 128 128 128 0.5 1 0")
    (sparse / "points3D.txt").write_text("# points\n" + "\n".join(points) + "\n", encoding="utf-8")
    recomputed = np.asarray(compute_gsplat_normalize_transform(sparse)["transform"])
    trainer = recomputed.copy()
    trainer[:3, :] = np.diag([-1.0, 1.0, -1.0]) @ trainer[:3, :]
    from capture_splat.scene_transform import load_points
    ply = tmp_path / "point_cloud_6999.ply"
    write_binary_ply(ply, transform_points(trainer, load_points(sparse)))

    sidecar = write_scene_transform_sidecar(ply, sparse, "gsplat", normalized=True)

    assert sidecar is not None
    assert sidecar["flip_disambiguation"]["applied"] is True
    assert sidecar["flip_disambiguation"]["chosen_signs"] == [-1.0, 1.0, -1.0]
    assert np.allclose(np.asarray(sidecar["trainer_transform"]), trainer, atol=1e-9)


def test_sidecar_identity_when_normalization_disabled(tmp_path: Path) -> None:
    ply = tmp_path / "splat.ply"
    ply.write_bytes(b"ply")

    sidecar = write_scene_transform_sidecar(ply, None, "gsplat", normalized=False)

    assert sidecar is not None
    assert sidecar["trainer_transform_source"] == "identity_normalization_disabled"
    assert sidecar["trainer_transform"][0][0] == 1.0


def test_sidecar_absent_when_no_source(tmp_path: Path) -> None:
    ply = tmp_path / "splat.ply"
    ply.write_bytes(b"ply")

    assert write_scene_transform_sidecar(ply, None, "vksplat", normalized=True) is None
    assert not (tmp_path / SIDECAR_NAME).exists()


def test_metric_package_status_requires_current_sparse_checksums(tmp_path: Path) -> None:
    package = tmp_path / "package"
    sparse = package / "sparse/0"
    sparse.mkdir(parents=True)
    for name in ("cameras.txt", "images.txt", "points3D.txt"):
        (sparse / name).write_text(f"# {name}\n", encoding="utf-8")
    metadata = package / "metadata"
    metadata.mkdir()
    from capture_splat.scene_transform import _sha256
    (metadata / "metric_scale_report.json").write_text(json.dumps({
        "schema": "capture_splat.metric_scale_report.v0.1",
        "status": "accepted",
        "target_units": "meters",
        "authority": {"metric_scale_evidence": True},
        "output_checksums": {
            "cameras_txt": _sha256(sparse / "cameras.txt"),
            "images_txt": _sha256(sparse / "images.txt"),
            "points3D_txt": _sha256(sparse / "points3D.txt"),
        },
    }), encoding="utf-8")

    assert metric_package_status(package)["accepted"] is True
    assert resolve_normalization_policy(package, "sparse/0", "auto", True)["resolved"] == "off"

    (sparse / "points3D.txt").write_text("# stale\n", encoding="utf-8")
    stale = metric_package_status(package)
    assert stale["accepted"] is False
    assert stale["reason"].endswith("points3D_txt")
