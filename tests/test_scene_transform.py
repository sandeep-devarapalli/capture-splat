import json
import math
from pathlib import Path

import numpy as np
import pytest

from capture_splat.json_utils import load_json_strict
from capture_splat.scene_transform import (
    SIDECAR_NAME,
    compute_gsplat_normalize_transform,
    load_camera_to_worlds,
    similarity_from_cameras,
    transform_points,
    write_scene_transform_sidecar,
)


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


def test_sidecar_recomputes_gsplat_normalize(tmp_path: Path) -> None:
    ply = tmp_path / "point_cloud_6999.ply"
    ply.write_bytes(b"ply")
    sparse = tmp_path / "sparse0"
    write_sparse(sparse, [0.1, 0.2, 0.15, 0.1])

    sidecar = write_scene_transform_sidecar(ply, sparse, "gsplat", normalized=True)

    assert sidecar is not None
    assert sidecar["trainer_transform_source"] == "recomputed_gsplat_parser_normalize"
    assert len(sidecar["trainer_transform"]) == 4


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
