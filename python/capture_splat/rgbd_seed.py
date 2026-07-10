from __future__ import annotations

import math
import shutil
import struct
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .background_sphere import next_point_id
from .capture_schema import iter_frames, load_capture
from .json_utils import write_json_strict

SUMMARY_SCHEMA = "capture_splat.rgbd_seed_summary.v0.1"


def quaternion_rotation(qw: float, qx: float, qy: float, qz: float) -> np.ndarray:
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm <= 0:
        raise ValueError("COLMAP image quaternion has zero norm")
    qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
    return np.asarray([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
        [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
        [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)],
    ], dtype=np.float64)


def read_colmap_camera_centers(images_txt: Path) -> dict[str, np.ndarray]:
    centers: dict[str, np.ndarray] = {}
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) < 10:
            continue
        try:
            values = [float(value) for value in parts[1:8]]
            int(parts[8])
        except ValueError:
            continue
        rotation = quaternion_rotation(*values[:4])
        translation = np.asarray(values[4:7], dtype=np.float64)
        center = -rotation.T @ translation
        name = Path(parts[9]).name
        centers[name] = center
        centers.setdefault(Path(name).stem, center)
    return centers


def estimate_sim3(source: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("Sim(3) inputs must be matching Nx3 arrays")
    if len(source) < 3:
        raise ValueError("Sim(3) requires at least three points")
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    variance = float(np.mean(np.sum(source_centered * source_centered, axis=1)))
    if not math.isfinite(variance) or variance <= 1e-12:
        raise ValueError("Sim(3) source camera centers are degenerate")
    covariance = target_centered.T @ source_centered / len(source)
    left, singular, right_t = np.linalg.svd(covariance)
    signs = np.ones(3)
    if np.linalg.det(left @ right_t) < 0:
        signs[-1] = -1
    rotation = left @ np.diag(signs) @ right_t
    scale = float(np.sum(singular * signs) / variance)
    translation = target_mean - scale * (rotation @ source_mean)
    if not math.isfinite(scale) or scale <= 0 or not np.all(np.isfinite(translation)):
        raise ValueError("Sim(3) estimate is non-finite or reflected")
    return scale, rotation, translation


def apply_sim3(points: np.ndarray, scale: float, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    return scale * (points @ rotation.T) + translation


def _matched_centers(capture_dir: Path, package_dir: Path) -> tuple[np.ndarray, np.ndarray, list[str]]:
    capture = load_capture(capture_dir)
    colmap = read_colmap_camera_centers(package_dir / "sparse/0/images.txt")
    source: list[np.ndarray] = []
    target: list[np.ndarray] = []
    names: list[str] = []
    for frame in iter_frames(capture, accepted_only=True):
        name = Path(frame.image_path).name
        center = colmap.get(name, colmap.get(Path(name).stem))
        if center is None:
            continue
        matrix = np.asarray(frame.transform_matrix, dtype=np.float64)
        source.append(matrix[:3, 3])
        target.append(center)
        names.append(name)
    return np.asarray(source), np.asarray(target), names


def _alignment_report(
    source: np.ndarray,
    target: np.ndarray,
    minimum_cameras: int,
    max_median_fraction: float,
    max_p95_fraction: float,
) -> tuple[dict[str, Any], tuple[float, np.ndarray, np.ndarray] | None]:
    report: dict[str, Any] = {
        "matched_cameras": int(len(source)),
        "minimum_cameras": int(minimum_cameras),
        "max_median_residual_scene_fraction": max_median_fraction,
        "max_p95_residual_scene_fraction": max_p95_fraction,
    }
    if len(source) < minimum_cameras:
        report.update({"accepted": False, "reason": "insufficient_matched_cameras"})
        return report, None
    try:
        scale, rotation, translation = estimate_sim3(source, target)
    except ValueError as error:
        report.update({"accepted": False, "reason": str(error)})
        return report, None
    aligned = apply_sim3(source, scale, rotation, translation)
    residuals = np.linalg.norm(aligned - target, axis=1)
    center = np.median(target, axis=0)
    scene_radius = float(np.percentile(np.linalg.norm(target - center, axis=1), 95))
    median = float(np.median(residuals))
    p95 = float(np.percentile(residuals, 95))
    radius_valid = math.isfinite(scene_radius) and scene_radius > 0
    median_fraction = median / scene_radius if radius_valid else None
    p95_fraction = p95 / scene_radius if radius_valid else None
    accepted = (
        radius_valid
        and median_fraction is not None
        and p95_fraction is not None
        and median_fraction <= max_median_fraction
        and p95_fraction <= max_p95_fraction
    )
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = scale * rotation
    matrix[:3, 3] = translation
    report.update({
        "accepted": accepted,
        "reason": "within_residual_gate" if accepted else "residual_gate_failed",
        "scale": scale,
        "rotation": rotation.tolist(),
        "translation": translation.tolist(),
        "matrix": matrix.tolist(),
        "scene_radius": scene_radius,
        "median_residual": median,
        "p95_residual": p95,
        "median_residual_scene_fraction": median_fraction,
        "p95_residual_scene_fraction": p95_fraction,
    })
    return report, (scale, rotation, translation) if accepted else None


def _frame_points(
    capture_dir: Path,
    frame: Any,
    raw: dict[str, Any],
    points_per_frame: int,
    confidence_minimum: int,
) -> tuple[np.ndarray, np.ndarray]:
    depth_relative = raw.get("depth")
    if not isinstance(depth_relative, str) or not (capture_dir / depth_relative).exists():
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8)
    depth = np.load(capture_dir / depth_relative, allow_pickle=False)
    if depth.ndim != 2:
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8)
    height, width = depth.shape
    stride = max(1, int(math.sqrt(max(1, height * width // max(1, points_per_frame)))))
    ys, xs = np.mgrid[0:height:stride, 0:width:stride]
    sampled = depth[0:height:stride, 0:width:stride]
    valid = np.isfinite(sampled) & (sampled > 0.05) & (sampled < 20.0)
    confidence_relative = raw.get("confidence")
    if isinstance(confidence_relative, str) and (capture_dir / confidence_relative).exists():
        confidence = np.load(capture_dir / confidence_relative, allow_pickle=False)
        if confidence.shape == depth.shape:
            valid &= confidence[0:height:stride, 0:width:stride] >= confidence_minimum
    if not np.any(valid):
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8)
    d = sampled[valid].astype(np.float64)
    x = (xs[valid] - frame.intrinsics["cx"]) / frame.intrinsics["fl_x"] * d
    y = -((ys[valid] - frame.intrinsics["cy"]) / frame.intrinsics["fl_y"] * d)
    local = np.stack([x, y, -d, np.ones_like(d)], axis=1)
    camera_to_world = np.asarray(frame.transform_matrix, dtype=np.float64)
    points = (camera_to_world @ local.T).T[:, :3]
    with Image.open(capture_dir / frame.image_path) as image:
        rgb = np.asarray(image.convert("RGB").resize((width, height), Image.Resampling.BILINEAR))
    colors = rgb[ys[valid], xs[valid]].astype(np.uint8)
    return points, colors


def _voxel_downsample(points: np.ndarray, colors: np.ndarray, voxel: float, maximum: int) -> tuple[np.ndarray, np.ndarray]:
    keys = np.floor(points / voxel).astype(np.int64)
    _, indices = np.unique(keys, axis=0, return_index=True)
    indices.sort()
    if len(indices) > maximum:
        positions = np.linspace(0, len(indices) - 1, maximum, dtype=int)
        indices = indices[positions]
    return points[indices], colors[indices]


def _write_binary_ply(path: Path, points: np.ndarray, colors: np.ndarray) -> None:
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(header.encode("ascii"))
        for point, color in zip(points, colors):
            handle.write(struct.pack("<fffBBB", *point.astype(np.float32), *color.astype(np.uint8)))


def _append_colmap_points(points_txt: Path, points: np.ndarray, colors: np.ndarray) -> int:
    start = next_point_id(points_txt)
    with points_txt.open("a", encoding="utf-8") as handle:
        for offset, (point, color) in enumerate(zip(points, colors)):
            handle.write(
                f"{start + offset} {point[0]:.9g} {point[1]:.9g} {point[2]:.9g} "
                f"{int(color[0])} {int(color[1])} {int(color[2])} 0\n"
            )
    return start


def build_rgbd_metric_seed(
    capture_dir: Path,
    package_dir: Path,
    out_dir: Path,
    minimum_cameras: int = 8,
    max_median_fraction: float = 0.03,
    max_p95_fraction: float = 0.08,
    confidence_minimum: int = 1,
    voxel_size: float = 0.02,
    max_points: int = 250_000,
) -> dict[str, Any]:
    capture_dir = capture_dir.resolve()
    package_dir = package_dir.resolve()
    out_dir = out_dir.resolve()
    if not (capture_dir / "capture.json").exists():
        raise FileNotFoundError(f"capture.json missing: {capture_dir}")
    if not (package_dir / "sparse/0/images.txt").exists():
        raise FileNotFoundError(f"COLMAP images.txt missing: {package_dir / 'sparse/0/images.txt'}")
    for name in ("cameras.txt", "points3D.txt"):
        if not (package_dir / "sparse/0" / name).exists():
            raise FileNotFoundError(f"COLMAP text model file missing: {package_dir / 'sparse/0' / name}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"RGB-D seed output is not empty: {out_dir}")
    if minimum_cameras < 3:
        raise ValueError("minimum cameras must be at least three")
    if max_median_fraction <= 0 or max_p95_fraction <= 0:
        raise ValueError("alignment residual fractions must be positive")
    if confidence_minimum not in (0, 1, 2):
        raise ValueError("confidence minimum must be 0, 1, or 2")
    if voxel_size <= 0 or max_points <= 0:
        raise ValueError("voxel size and max points must be positive")
    output_package = out_dir / "package"
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_dir, output_package)
    source, target, names = _matched_centers(capture_dir, package_dir)
    alignment, transform = _alignment_report(
        source, target, minimum_cameras, max_median_fraction, max_p95_fraction
    )
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "capture_dir": str(capture_dir),
        "input_package": str(package_dir),
        "output_package": str(output_package),
        "matched_frame_names": names,
        "alignment": alignment,
        "seed_ply": None,
        "seed_point_count": 0,
        "package_augmented": False,
        "decision": "hold",
        "authority": {
            "arkit_depth_prior": True,
            "colmap_refined_cameras_remain_baseline": True,
            "metric_seed_is_proposal": True,
            "quality_claim": False,
        },
    }
    if transform is None:
        write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
        return summary
    capture = load_capture(capture_dir)
    frames = list(iter_frames(capture, accepted_only=True))
    points_per_frame = max(64, max_points // max(1, len(frames)))
    point_chunks: list[np.ndarray] = []
    color_chunks: list[np.ndarray] = []
    for frame in frames:
        raw = capture["frames"][frame.source_index - 1]
        points, colors = _frame_points(
            capture_dir, frame, raw, points_per_frame, confidence_minimum
        )
        if len(points):
            point_chunks.append(points)
            color_chunks.append(colors)
    if not point_chunks:
        summary["alignment"]["seed_reason"] = "no_confidence_filtered_depth_points"
        write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
        return summary
    points = np.concatenate(point_chunks)
    colors = np.concatenate(color_chunks)
    scale, rotation, translation = transform
    points = apply_sim3(points, scale, rotation, translation)
    finite = np.all(np.isfinite(points), axis=1)
    points, colors = points[finite], colors[finite]
    if not len(points):
        summary["alignment"]["seed_reason"] = "no_finite_transformed_depth_points"
        write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
        return summary
    points, colors = _voxel_downsample(points, colors, voxel_size, max_points)
    seed_ply = out_dir / "metric_seed.ply"
    _write_binary_ply(seed_ply, points, colors)
    points_txt = output_package / "sparse/0/points3D.txt"
    backup = output_package / "sparse/0_colmap_refined"
    shutil.copytree(points_txt.parent, backup)
    for binary in points_txt.parent.glob("*.bin"):
        binary.unlink()
    first_point_id = _append_colmap_points(points_txt, points, colors)
    summary.update({
        "seed_ply": str(seed_ply),
        "seed_point_count": int(len(points)),
        "voxel_size": voxel_size,
        "confidence_minimum": confidence_minimum,
        "first_colmap_point_id": first_point_id,
        "colmap_model_backup": str(backup),
        "package_augmented": True,
        "decision": "promote",
    })
    write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
    return summary
