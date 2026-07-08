from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np


def fibonacci_sphere(samples: int, radius: float, center: np.ndarray) -> np.ndarray:
    golden = math.pi * (3.0 - math.sqrt(5.0))
    indices = np.arange(samples, dtype=float)
    y = 1.0 - (indices / max(1, samples - 1)) * 2.0
    ring = np.sqrt(np.clip(1.0 - y * y, 0.0, 1.0))
    theta = golden * indices
    points = np.stack([np.cos(theta) * ring, y, np.sin(theta) * ring], axis=1)
    return points * radius + center


def read_positions(sparse_dir: Path) -> tuple[np.ndarray, str]:
    points_txt = sparse_dir / "points3D.txt"
    positions: list[list[float]] = []
    if points_txt.exists():
        for line in points_txt.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 7 and not line.startswith("#"):
                positions.append([float(parts[1]), float(parts[2]), float(parts[3])])
    if len(positions) >= 8:
        return np.asarray(positions), "sparse_points"
    images_txt = sparse_dir / "images.txt"
    centers: list[list[float]] = []
    if images_txt.exists():
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
            norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz) or 1.0
            qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
            rotation = np.array([
                [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
                [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
                [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)],
            ])
            centers.append(list(-rotation.T @ np.array([tx, ty, tz])))
    if not centers:
        raise ValueError(f"no sparse points or camera poses to size a background sphere in {sparse_dir}")
    return np.asarray(centers), "camera_centers"


def next_point_id(points_txt: Path) -> int:
    highest = 0
    if points_txt.exists():
        for line in points_txt.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts and not line.startswith("#"):
                try:
                    highest = max(highest, int(parts[0]))
                except ValueError:
                    continue
    return highest + 1


def append_background_sphere(sparse_dir: Path, count: int = 1500, scale_factor: float = 2.0) -> dict[str, Any]:
    sparse_dir = sparse_dir.resolve()
    points_txt = sparse_dir / "points3D.txt"
    positions, size_source = read_positions(sparse_dir)
    center = np.median(positions, axis=0)
    distances = np.linalg.norm(positions - center, axis=1)
    radius = float(np.percentile(distances, 95)) * scale_factor
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("background sphere radius is not finite/positive")
    sphere = fibonacci_sphere(count, radius, center)
    start_id = next_point_id(points_txt)
    lines = [
        f"{start_id + index} {point[0]:.8g} {point[1]:.8g} {point[2]:.8g} 128 128 128 0"
        for index, point in enumerate(sphere)
    ]
    with points_txt.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return {
        "background_sphere_points": count,
        "background_sphere_radius": radius,
        "background_sphere_center": [float(value) for value in center],
        "background_sphere_size_source": size_source,
        "background_sphere_first_id": start_id,
    }
