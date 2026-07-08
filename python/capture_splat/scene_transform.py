from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .json_utils import write_json_strict

SIDECAR_SCHEMA = "capture_splat.scene_transform.v0.1"
SIDECAR_NAME = "capture_splat_scene_transform.json"


def quaternion_wxyz_to_matrix(qw: float, qx: float, qy: float, qz: float) -> np.ndarray:
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm <= 1e-12:
        return np.eye(3)
    qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
    return np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
        [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
        [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)],
    ])


def load_camera_to_worlds(sparse_dir: Path) -> np.ndarray:
    images_txt = sparse_dir / "images.txt"
    if not images_txt.exists():
        raise FileNotFoundError(f"images.txt missing: {images_txt}")
    poses = []
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 10 or line.startswith("#"):
            continue
        try:
            float(parts[9])
            continue
        except ValueError:
            pass
        qw, qx, qy, qz, tx, ty, tz = (float(value) for value in parts[1:8])
        rotation_w2c = quaternion_wxyz_to_matrix(qw, qx, qy, qz)
        c2w = np.eye(4)
        c2w[:3, :3] = rotation_w2c.T
        c2w[:3, 3] = -rotation_w2c.T @ np.array([tx, ty, tz])
        poses.append(c2w)
    if not poses:
        raise ValueError(f"no registered camera poses in {images_txt}")
    return np.stack(poses)


def load_points(sparse_dir: Path) -> np.ndarray:
    points_txt = sparse_dir / "points3D.txt"
    points = []
    if points_txt.exists():
        for line in points_txt.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 7 or line.startswith("#"):
                continue
            points.append([float(parts[1]), float(parts[2]), float(parts[3])])
    return np.asarray(points, dtype=float) if points else np.empty((0, 3))


def similarity_from_cameras(c2w: np.ndarray) -> np.ndarray:
    t = c2w[:, :3, 3]
    rotation = c2w[:, :3, :3]
    ups = np.sum(rotation * np.array([0.0, -1.0, 0.0]), axis=-1)
    world_up = np.mean(ups, axis=0)
    world_up /= np.linalg.norm(world_up)
    up_camspace = np.array([0.0, -1.0, 0.0])
    c = float((up_camspace * world_up).sum())
    cross = np.cross(world_up, up_camspace)
    skew = np.array([
        [0.0, -cross[2], cross[1]],
        [cross[2], 0.0, -cross[0]],
        [-cross[1], cross[0], 0.0],
    ])
    if c > -1:
        r_align = np.eye(3) + skew + (skew @ skew) / (1 + c)
    else:
        r_align = np.array([[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    rotation = r_align @ rotation
    fwds = np.sum(rotation * np.array([0.0, 0.0, 1.0]), axis=-1)
    t = (r_align @ t[..., None])[..., 0]
    nearest = t + (fwds * -t).sum(-1)[:, None] * fwds
    translate = -np.median(nearest, axis=0)
    transform = np.eye(4)
    transform[:3, 3] = translate
    transform[:3, :3] = r_align
    scale = 1.0 / np.median(np.linalg.norm(t + translate, axis=-1))
    transform[:3, :] *= scale
    return transform


def align_principle_axes(point_cloud: np.ndarray) -> np.ndarray:
    centroid = np.median(point_cloud, axis=0)
    translated = point_cloud - centroid
    covariance = np.cov(translated.T)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = eigenvalues.argsort()[::-1]
    eigenvectors = eigenvectors[:, order]
    if np.linalg.det(eigenvectors) < 0:
        eigenvectors[:, 0] *= -1
    rotation = eigenvectors.T
    transform = np.eye(4)
    transform[:3, :3] = rotation
    transform[:3, 3] = -rotation @ centroid
    return transform


def transform_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    if points.size == 0:
        return points
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def compute_gsplat_normalize_transform(sparse_dir: Path) -> dict[str, Any]:
    c2w = load_camera_to_worlds(sparse_dir)
    points = load_points(sparse_dir)
    t1 = similarity_from_cameras(c2w)
    transform = t1
    if len(points) >= 3:
        t2 = align_principle_axes(transform_points(t1, points))
        transform = t2 @ t1
    return {
        "transform": transform.tolist(),
        "method": "similarity_from_cameras+align_principle_axes",
        "camera_count": int(len(c2w)),
        "point_count": int(len(points)),
    }


def load_train_json_transform(ply_path: Path) -> list[list[float]] | None:
    train_json = ply_path.resolve().parent / "train.json"
    if not train_json.exists():
        return None
    try:
        meta = json.loads(train_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = meta.get("dataparser_transform")
    if not isinstance(value, list) or len(value) != 4:
        return None
    rows = []
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            return None
        floats = [float(entry) for entry in row]
        if not all(math.isfinite(entry) for entry in floats):
            return None
        rows.append(floats)
    return rows


def write_scene_transform_sidecar(
    ply_path: Path,
    sparse_dir: Path | None,
    trainer: str,
    normalized: bool,
) -> dict[str, Any] | None:
    ply_path = ply_path.resolve()
    sidecar: dict[str, Any] = {
        "schema": SIDECAR_SCHEMA,
        "trainer": trainer,
        "ply": ply_path.name,
        "trainer_transform": None,
        "trainer_transform_source": None,
        "authority": {
            "maps_package_world_to_trained_world": True,
            "quality_claim": False,
        },
    }
    train_json_transform = load_train_json_transform(ply_path)
    if train_json_transform is not None:
        sidecar["trainer_transform"] = train_json_transform
        sidecar["trainer_transform_source"] = "trainer_train_json"
    elif trainer == "gsplat" and normalized and sparse_dir is not None and (sparse_dir / "images.txt").exists():
        recomputed = compute_gsplat_normalize_transform(sparse_dir)
        sidecar["trainer_transform"] = recomputed["transform"]
        sidecar["trainer_transform_source"] = "recomputed_gsplat_parser_normalize"
        sidecar["method"] = recomputed["method"]
        sidecar["camera_count"] = recomputed["camera_count"]
        sidecar["point_count"] = recomputed["point_count"]
    elif not normalized:
        sidecar["trainer_transform"] = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
        sidecar["trainer_transform_source"] = "identity_normalization_disabled"
    if sidecar["trainer_transform"] is None:
        return None
    write_json_strict(ply_path.parent / SIDECAR_NAME, sidecar)
    return sidecar


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the scene transform sidecar next to a trained PLY.")
    parser.add_argument("--ply", type=Path, required=True)
    parser.add_argument("--sparse-dir", type=Path)
    parser.add_argument("--trainer", choices=["gsplat", "vksplat"], default="gsplat")
    parser.add_argument("--no-normalize", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    sidecar = write_scene_transform_sidecar(args.ply, args.sparse_dir, args.trainer, normalized=not args.no_normalize)
    print(json.dumps(sidecar, indent=2))


if __name__ == "__main__":
    main()
