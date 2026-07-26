from __future__ import annotations

import hashlib
import math
import shutil
import struct
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .background_sphere import next_point_id
from .capture_schema import iter_frames, load_capture
from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply, load_ply_scalar_samples
from .scene_transform import PACKAGE_ORIENTATION_NAME, PACKAGE_ORIENTATION_SCHEMA

SUMMARY_SCHEMA = "capture_splat.rgbd_seed_summary.v0.1"
METRIC_SCALE_SCHEMA = "capture_splat.metric_scale_report.v0.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _file_evidence(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "checksum": _sha256(path),
    }


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


def _depth_intrinsics(
    intrinsics: dict[str, float],
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    source_width = float(intrinsics["w"])
    source_height = float(intrinsics["h"])
    values = [source_width, source_height, *(float(intrinsics[key]) for key in ("fl_x", "fl_y", "cx", "cy"))]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("frame intrinsics contain non-finite values")
    if source_width <= 0 or source_height <= 0 or intrinsics["fl_x"] <= 0 or intrinsics["fl_y"] <= 0:
        raise ValueError("frame intrinsics dimensions and focal lengths must be positive")
    scale_x = width / source_width
    scale_y = height / source_height
    return (
        float(intrinsics["fl_x"]) * scale_x,
        float(intrinsics["fl_y"]) * scale_y,
        float(intrinsics["cx"]) * scale_x,
        float(intrinsics["cy"]) * scale_y,
    )


def _scale_colmap_images(images_txt: Path, meters_per_colmap_unit: float) -> None:
    output: list[str] = []
    expect_pose = True
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            output.append(line)
            continue
        if not stripped:
            output.append(line)
            if not expect_pose:
                expect_pose = True
            continue
        if expect_pose:
            parts = line.split()
            if len(parts) < 10:
                raise ValueError("invalid COLMAP images.txt pose line")
            for index in range(5, 8):
                value = float(parts[index]) * meters_per_colmap_unit
                if not math.isfinite(value):
                    raise ValueError("scaled COLMAP image translation is non-finite")
                parts[index] = f"{value:.17g}"
            output.append(" ".join(parts))
            expect_pose = False
        else:
            output.append(line)
            expect_pose = True
    images_txt.write_text("\n".join(output) + "\n", encoding="utf-8")


def _scale_colmap_points(points_txt: Path, meters_per_colmap_unit: float) -> None:
    output: list[str] = []
    for line in points_txt.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        parts = line.split()
        if len(parts) < 7:
            raise ValueError("invalid COLMAP points3D.txt line")
        for index in range(1, 4):
            value = float(parts[index]) * meters_per_colmap_unit
            if not math.isfinite(value):
                raise ValueError("scaled COLMAP point is non-finite")
            parts[index] = f"{value:.17g}"
        output.append(" ".join(parts))
    points_txt.write_text("\n".join(output) + "\n", encoding="utf-8")


def _scale_package_orientation(path: Path, meters_per_colmap_unit: float) -> None:
    report = load_json_strict(path)
    matrix = np.asarray(report.get("transform"), dtype=np.float64)
    if report.get("schema") != PACKAGE_ORIENTATION_SCHEMA or matrix.shape != (4, 4):
        raise ValueError("invalid package orientation transform")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("package orientation transform is non-finite")
    matrix[:3, :] *= meters_per_colmap_unit
    report["transform"] = matrix.tolist()
    report["target_coordinate_frame"] = "metric_colmap_world"
    report["metric_package_scale_applied"] = meters_per_colmap_unit
    for key in ("scale", "median_camera_center_residual", "max_camera_center_residual"):
        value = report.get(key)
        if isinstance(value, (int, float)):
            report[key] = float(value) * meters_per_colmap_unit
    write_json_strict(path, report)


def _matched_centers(
    capture_dir: Path,
    package_dir: Path,
    sparse_dir_name: str = "sparse/0",
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    capture = load_capture(capture_dir)
    colmap = read_colmap_camera_centers(package_dir / sparse_dir_name / "images.txt")
    source: list[np.ndarray] = []
    target: list[np.ndarray] = []
    names: list[str] = []
    for frame in iter_frames(capture, accepted_only=True):
        raw = capture["frames"][frame.source_index - 1]
        depth_relative = raw.get("depth") if isinstance(raw, dict) else None
        if not isinstance(depth_relative, str) or not (capture_dir / depth_relative).exists():
            continue
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


def camera_alignment_report(
    capture_dir: Path,
    package_dir: Path,
    sparse_dir_name: str = "sparse/0",
    minimum_cameras: int = 8,
    max_median_fraction: float = 0.03,
    max_p95_fraction: float = 0.08,
) -> dict[str, Any]:
    source, target, names = _matched_centers(capture_dir, package_dir, sparse_dir_name)
    report, _ = _alignment_report(
        source,
        target,
        minimum_cameras,
        max_median_fraction,
        max_p95_fraction,
    )
    report["matched_frame_names"] = names
    return report


def _frame_points(
    capture_dir: Path,
    frame: Any,
    raw: dict[str, Any],
    points_per_frame: int,
    confidence_minimum: int,
    depth_scale: float,
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
    raw_depth_scale = raw.get("depth_scale")
    frame_depth_scale = depth_scale if raw_depth_scale is None else float(raw_depth_scale)
    if not math.isfinite(frame_depth_scale) or frame_depth_scale <= 0:
        raise ValueError("depth_scale must be positive and finite")
    sampled_meters = sampled.astype(np.float64) * frame_depth_scale
    valid = np.isfinite(sampled_meters) & (sampled_meters > 0.05) & (sampled_meters < 20.0)
    confidence_relative = raw.get("confidence")
    if isinstance(confidence_relative, str) and (capture_dir / confidence_relative).exists():
        confidence = np.load(capture_dir / confidence_relative, allow_pickle=False)
        if confidence.shape == depth.shape:
            valid &= confidence[0:height:stride, 0:width:stride] >= confidence_minimum
    if not np.any(valid):
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8)
    fl_x, fl_y, cx, cy = _depth_intrinsics(frame.intrinsics, width, height)
    d = sampled_meters[valid]
    x = (xs[valid] - cx) / fl_x * d
    y = -((ys[valid] - cy) / fl_y * d)
    local = np.stack([x, y, -d, np.ones_like(d)], axis=1)
    camera_to_world = np.asarray(frame.transform_matrix, dtype=np.float64)
    points = (camera_to_world @ local.T).T[:, :3]
    with Image.open(capture_dir / frame.image_path) as image:
        rgb = np.asarray(image.convert("RGB").resize((width, height), Image.Resampling.BILINEAR))
    colors = rgb[ys[valid], xs[valid]].astype(np.uint8)
    return points, colors


def _color_mesh_points(
    capture_dir: Path,
    capture: dict[str, Any],
    frames: list[Any],
    points: np.ndarray,
) -> tuple[np.ndarray, int]:
    colors = np.full((len(points), 3), 127, dtype=np.uint8)
    best_distance = np.full(len(points), np.inf)
    homogeneous = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    for frame in frames:
        image_path = capture_dir / frame.image_path
        if not image_path.exists():
            continue
        camera_to_world = np.asarray(frame.transform_matrix, dtype=np.float64)
        if camera_to_world.shape != (4, 4) or not np.all(np.isfinite(camera_to_world)):
            continue
        try:
            world_to_camera = np.linalg.inv(camera_to_world)
        except np.linalg.LinAlgError:
            continue
        camera_points = (world_to_camera @ homogeneous.T).T[:, :3]
        depth = -camera_points[:, 2]
        with Image.open(image_path) as image:
            rgb = np.asarray(image.convert("RGB"))
        height, width = rgb.shape[:2]
        fl_x, fl_y, cx, cy = _depth_intrinsics(frame.intrinsics, width, height)
        valid_depth = np.isfinite(depth) & (depth > 0.05)
        u = np.zeros(len(points))
        v = np.zeros(len(points))
        u[valid_depth] = fl_x * camera_points[valid_depth, 0] / depth[valid_depth] + cx
        v[valid_depth] = cy - fl_y * camera_points[valid_depth, 1] / depth[valid_depth]
        ui = np.rint(u).astype(np.int64)
        vi = np.rint(v).astype(np.int64)
        visible = valid_depth & (ui >= 0) & (ui < width) & (vi >= 0) & (vi < height)
        closer = visible & (depth < best_distance)
        if np.any(closer):
            colors[closer] = rgb[vi[closer], ui[closer]]
            best_distance[closer] = depth[closer]
    return colors, int(np.count_nonzero(np.isfinite(best_distance)))


def _mesh_points(
    capture_dir: Path,
    capture: dict[str, Any],
    frames: list[Any],
    voxel_size: float,
    max_points: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    relative = capture.get("arkit_mesh_file", "geometry/arkit_mesh.ply")
    mesh_path = capture_dir / relative if isinstance(relative, str) else capture_dir / "geometry/arkit_mesh.ply"
    if not mesh_path.exists():
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8), {"reason": "arkit_mesh_missing"}
    report_relative = capture.get("arkit_mesh_report_file", "geometry/arkit_mesh_report.json")
    report_path = (
        capture_dir / report_relative
        if isinstance(report_relative, str)
        else capture_dir / "geometry/arkit_mesh_report.json"
    )
    if not report_path.exists():
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8), {"reason": "arkit_mesh_report_missing"}
    report = load_json_strict(report_path)
    if (
        report.get("status") != "finite_mesh_written"
        or report.get("ply_written") is not True
        or int(report.get("non_finite_vertex_count", 0)) != 0
    ):
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8), {"reason": "arkit_mesh_report_not_finite"}
    stats = inspect_ply(mesh_path)
    if not stats["finite"]:
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.uint8), {"reason": "arkit_mesh_non_finite"}
    samples = load_ply_scalar_samples(mesh_path, ["x", "y", "z"], limit=500_000)
    points = np.stack([samples["x"], samples["y"], samples["z"]], axis=1)
    finite = np.all(np.isfinite(points), axis=1)
    points = points[finite]
    points, _ = _voxel_downsample(
        points,
        np.zeros((len(points), 3), dtype=np.uint8),
        voxel_size,
        max_points,
    )
    colors, colored_count = _color_mesh_points(capture_dir, capture, frames, points)
    return points, colors, {
        "reason": "mesh_vertices_projected_to_rgb",
        "source_vertex_count": stats["vertex_count"],
        "sampled_point_count": int(len(points)),
        "rgb_colored_point_count": colored_count,
        "default_gray_point_count": int(len(points) - colored_count),
        "mesh": _file_evidence(mesh_path, capture_dir),
        "mesh_report": _file_evidence(report_path, capture_dir),
    }


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
    seed_source: str = "auto",
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
    if seed_source not in {"auto", "depth", "mesh"}:
        raise ValueError("seed source must be auto, depth, or mesh")
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
        "seed_source_requested": seed_source,
        "seed_source_resolved": None,
        "package_augmented": False,
        "decision": "hold",
        "authority": {
            "arkit_depth_prior": False,
            "arkit_mesh_prior": False,
            "colmap_refined_cameras_remain_baseline": True,
            "metric_seed_is_proposal": True,
            "quality_claim": False,
        },
    }
    if transform is None:
        write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
        return summary
    capture = load_capture(capture_dir)
    raw_depth_scale = capture.get("depth_scale")
    depth_scale = 1.0 if raw_depth_scale is None else float(raw_depth_scale)
    session_config = capture.get("session_config")
    scale_authority = session_config.get("scale_authority") if isinstance(session_config, dict) else None
    if not math.isfinite(depth_scale) or depth_scale <= 0:
        raise ValueError("capture depth_scale must be positive and finite")
    metric_scale_accepted = scale_authority == "arkit_vio_metric"
    frames = list(iter_frames(capture, accepted_only=True))
    points_per_frame = max(64, max_points // max(1, len(frames)))
    point_chunks: list[np.ndarray] = []
    color_chunks: list[np.ndarray] = []
    consumed_assets: dict[str, dict[str, Any]] = {}
    if seed_source in {"auto", "depth"}:
        for frame in frames:
            raw = capture["frames"][frame.source_index - 1]
            points, colors = _frame_points(
                capture_dir, frame, raw, points_per_frame, confidence_minimum, depth_scale
            )
            if len(points):
                point_chunks.append(points)
                color_chunks.append(colors)
                for key in ("rgb", "depth", "confidence"):
                    relative = raw.get(key)
                    if isinstance(relative, str):
                        path = capture_dir / relative
                        if path.exists():
                            consumed_assets.setdefault(relative, _file_evidence(path, capture_dir))
    mesh_details: dict[str, Any] | None = None
    if point_chunks:
        resolved_source = "depth"
        points = np.concatenate(point_chunks)
        colors = np.concatenate(color_chunks)
    elif seed_source in {"auto", "mesh"}:
        points, colors, mesh_details = _mesh_points(
            capture_dir, capture, frames, voxel_size, max_points
        )
        resolved_source = "mesh" if len(points) else None
        if isinstance(mesh_details.get("mesh"), dict):
            evidence = mesh_details["mesh"]
            consumed_assets.setdefault(str(evidence["path"]), evidence)
        if isinstance(mesh_details.get("mesh_report"), dict):
            evidence = mesh_details["mesh_report"]
            consumed_assets.setdefault(str(evidence["path"]), evidence)
    else:
        points = np.empty((0, 3))
        colors = np.empty((0, 3), dtype=np.uint8)
        resolved_source = None
    if not len(points):
        summary["alignment"]["seed_reason"] = (
            mesh_details.get("reason")
            if mesh_details is not None
            else "no_confidence_filtered_depth_points"
        )
        write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
        return summary
    summary["seed_source_resolved"] = resolved_source
    summary["authority"]["arkit_depth_prior"] = resolved_source == "depth"
    summary["authority"]["arkit_mesh_prior"] = resolved_source == "mesh"
    if mesh_details is not None:
        summary["mesh_seed"] = mesh_details
    scale, rotation, translation = transform
    meters_per_colmap_unit = 1.0 / scale
    points = apply_sim3(points, scale, rotation, translation)
    if metric_scale_accepted:
        points *= meters_per_colmap_unit
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
    images_txt = output_package / "sparse/0/images.txt"
    cameras_txt = output_package / "sparse/0/cameras.txt"
    orientation_path = output_package / "metadata" / PACKAGE_ORIENTATION_NAME
    backup = output_package / "sparse/0_colmap_refined"
    shutil.copytree(points_txt.parent, backup)
    input_checksums = {
        "images_txt": _sha256(images_txt),
        "cameras_txt": _sha256(cameras_txt),
        "points3D_txt": _sha256(points_txt),
        "capture_manifest": _sha256(capture_dir / "capture.json"),
    }
    if orientation_path.exists():
        input_checksums["package_orientation_transform"] = _sha256(orientation_path)
    if metric_scale_accepted:
        _scale_colmap_images(images_txt, meters_per_colmap_unit)
        _scale_colmap_points(points_txt, meters_per_colmap_unit)
        if orientation_path.exists():
            _scale_package_orientation(orientation_path, meters_per_colmap_unit)
    for binary in points_txt.parent.glob("*.bin"):
        binary.unlink()
    first_point_id = _append_colmap_points(points_txt, points, colors)
    arkit_to_metric_colmap = np.eye(4, dtype=np.float64)
    arkit_to_metric_colmap[:3, :3] = rotation
    arkit_to_metric_colmap[:3, 3] = translation * meters_per_colmap_unit
    colmap_to_metric_colmap = np.eye(4, dtype=np.float64)
    colmap_to_metric_colmap[:3, :3] *= meters_per_colmap_unit
    metric_report_path: Path | None = None
    if metric_scale_accepted:
        metric_alignment = {
            "scene_radius_meters": alignment["scene_radius"] * meters_per_colmap_unit,
            "median_residual_meters": alignment["median_residual"] * meters_per_colmap_unit,
            "p95_residual_meters": alignment["p95_residual"] * meters_per_colmap_unit,
        }
        output_checksums = {
            "images_txt": _sha256(images_txt),
            "cameras_txt": _sha256(cameras_txt),
            "points3D_txt": _sha256(points_txt),
            "metric_seed_ply": _sha256(seed_ply),
        }
        if orientation_path.exists():
            output_checksums["package_orientation_transform"] = _sha256(orientation_path)
        metric_report = {
            "schema": METRIC_SCALE_SCHEMA,
            "status": "accepted",
            "source_coordinate_frame": "colmap_world",
            "target_coordinate_frame": "metric_colmap_world",
            "source_units": "colmap_units",
            "target_units": "meters",
            "scale_authority": scale_authority,
            "seed_source": resolved_source,
            "depth_scale_to_meters": depth_scale,
            "colmap_units_per_meter": scale,
            "meters_per_colmap_unit": meters_per_colmap_unit,
            "colmap_to_metric_colmap": colmap_to_metric_colmap.tolist(),
            "arkit_to_metric_colmap": arkit_to_metric_colmap.tolist(),
            "pre_metric_alignment": {
                **alignment,
                "absolute_value_units": "colmap_units_before_metric_scaling",
            },
            "metric_alignment": metric_alignment,
            "input_checksums": input_checksums,
            "consumed_capture_assets": list(consumed_assets.values()),
            "output_checksums": output_checksums,
            "authority": {
                "metric_scale_evidence": True,
                "colmap_refined_cameras_remain_baseline": True,
                "collision_authority": False,
                "measurement_authority": False,
                "quality_claim": False,
            },
        }
        metric_report_path = output_package / "metadata/metric_scale_report.json"
        write_json_strict(metric_report_path, metric_report)
    summary.update({
        "seed_ply": str(seed_ply),
        "seed_point_count": int(len(points)),
        "seed_source_resolved": resolved_source,
        "voxel_size": voxel_size,
        "confidence_minimum": confidence_minimum,
        "first_colmap_point_id": first_point_id,
        "colmap_model_backup": str(backup),
        "metric_scale_report": str(metric_report_path) if metric_report_path else None,
        "meters_per_colmap_unit": meters_per_colmap_unit if metric_scale_accepted else None,
        "output_coordinate_frame": "metric_colmap_world" if metric_scale_accepted else "colmap_world",
        "metric_scale_status": "accepted" if metric_scale_accepted else "unavailable",
        "warnings": [] if metric_scale_accepted else ["arkit_metric_scale_authority_missing_seed_in_colmap_units"],
        "package_augmented": True,
        "decision": "promote",
    })
    write_json_strict(out_dir / "capture_splat_rgbd_seed_summary.json", summary)
    return summary
