from __future__ import annotations

import hashlib
import importlib.util
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply
from .rgbd_seed import quaternion_rotation

DETECTIONS_SCHEMA = "capture_splat.apriltag_detections.v0.1"
REPORT_SCHEMA = "capture_splat.apriltag_scale_validation.v0.1"
REPORT_NAME = "capture_splat_apriltag_scale_report.json"


def apriltag_status() -> dict[str, Any]:
    return {
        "pupil_apriltags_available": importlib.util.find_spec("pupil_apriltags") is not None,
        "default_family": "tagStandard41h12",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _cameras(path: Path) -> dict[int, np.ndarray]:
    cameras: dict[int, np.ndarray] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or line.startswith("#"):
            continue
        camera_id = int(parts[0])
        model = parts[1]
        params = [float(value) for value in parts[4:]]
        if model == "PINHOLE" and len(params) == 4:
            fx, fy, cx, cy = params
        elif model == "SIMPLE_PINHOLE" and len(params) == 3:
            fx, cx, cy = params
            fy = fx
        else:
            raise ValueError(f"AprilTag scale validation requires PINHOLE cameras, found {model}")
        values = (fx, fy, cx, cy)
        if not all(math.isfinite(value) for value in values) or fx <= 0 or fy <= 0:
            raise ValueError("COLMAP camera intrinsics are invalid")
        cameras[camera_id] = np.asarray([
            [fx, 0.0, cx],
            [0.0, fy, cy],
            [0.0, 0.0, 1.0],
        ])
    if not cameras:
        raise ValueError("no supported COLMAP cameras found")
    return cameras


def _projections(path: Path, cameras: dict[int, np.ndarray]) -> dict[str, np.ndarray]:
    projections: dict[str, np.ndarray] = {}
    expect_pose = True
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if not stripped and not expect_pose:
                expect_pose = True
            continue
        if not expect_pose:
            expect_pose = True
            continue
        parts = stripped.split()
        if len(parts) < 10:
            raise ValueError("invalid COLMAP images.txt pose row")
        values = [float(value) for value in parts[1:8]]
        camera_id = int(parts[8])
        if camera_id not in cameras:
            raise ValueError(f"image references unknown camera {camera_id}")
        rotation = quaternion_rotation(*values[:4])
        translation = np.asarray(values[4:7], dtype=np.float64)
        projection = cameras[camera_id] @ np.column_stack([rotation, translation])
        name = Path(parts[9]).name
        projections[name] = projection
        projections.setdefault(Path(name).stem, projection)
        expect_pose = False
    if not projections:
        raise ValueError("no registered COLMAP image poses found")
    return projections


def _normalize_detections(payload: dict[str, Any], family: str) -> dict[str, list[dict[str, Any]]]:
    if payload.get("schema") != DETECTIONS_SCHEMA or payload.get("family") != family:
        raise ValueError("AprilTag detections schema or family is invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    images = payload.get("images")
    if not isinstance(images, list):
        raise ValueError("AprilTag detections images must be a list")
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("image"), str):
            raise ValueError("AprilTag detection image entry is invalid")
        tags = image.get("tags")
        if not isinstance(tags, list):
            raise ValueError("AprilTag detection tags must be a list")
        normalized = []
        for tag in tags:
            corners = np.asarray(tag.get("corners"), dtype=np.float64)
            if not isinstance(tag.get("tag_id"), int) or corners.shape != (4, 2):
                raise ValueError("AprilTag detection must contain an integer tag_id and four corners")
            if not np.all(np.isfinite(corners)):
                raise ValueError("AprilTag detection corners are non-finite")
            normalized.append({"tag_id": tag["tag_id"], "corners": corners.tolist()})
        if normalized:
            result[Path(image["image"]).name] = normalized
    return result


def _detect(images_dir: Path, image_names: set[str], family: str) -> dict[str, list[dict[str, Any]]]:
    try:
        from pupil_apriltags import Detector
    except ImportError as error:
        raise RuntimeError(
            "pupil-apriltags is required for live detection; install the optional apriltag extra "
            "or provide --detections-json"
        ) from error
    detector = Detector(families=family)
    result: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(image_names):
        path = images_dir / name
        if not path.is_file():
            continue
        with Image.open(path) as image:
            gray = np.asarray(image.convert("L"), dtype=np.uint8)
        tags = [
            {"tag_id": int(item.tag_id), "corners": np.asarray(item.corners, dtype=float).tolist()}
            for item in detector.detect(gray)
        ]
        if tags:
            result[name] = tags
    return result


def _triangulate(observations: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    if len(observations) < 2:
        raise ValueError("tag corner needs at least two registered views")
    rows = []
    for projection, pixel in observations:
        rows.extend([
            pixel[0] * projection[2] - projection[0],
            pixel[1] * projection[2] - projection[1],
        ])
    _, _, right = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    homogeneous = right[-1]
    if abs(homogeneous[3]) <= 1e-12:
        raise ValueError("tag corner triangulated at infinity")
    point = homogeneous[:3] / homogeneous[3]
    if not np.all(np.isfinite(point)):
        raise ValueError("tag corner triangulation is non-finite")
    errors = []
    positive = 0
    for projection, pixel in observations:
        projected = projection @ np.append(point, 1.0)
        if projected[2] > 0:
            positive += 1
        if abs(projected[2]) <= 1e-12:
            errors.append(float("inf"))
        else:
            errors.append(float(np.linalg.norm(projected[:2] / projected[2] - pixel)))
    if positive < max(2, math.ceil(len(observations) * 0.75)):
        raise ValueError("tag corner fails cheirality")
    return point, np.asarray(errors)


def validate_apriltag_scale(
    package: Path,
    out_dir: Path,
    *,
    tag_size_meters: float,
    detections_json: Path | None = None,
    artifact: Path | None = None,
    family: str = "tagStandard41h12",
    min_views: int = 3,
    max_reprojection_p95: float = 3.0,
    max_edge_cv: float = 0.15,
    max_scale_error_fraction: float = 0.05,
    image_dir_name: str = "images",
    sparse_dir_name: str = "sparse/0",
) -> dict[str, Any]:
    package = package.resolve()
    out_dir = out_dir.resolve()
    if not math.isfinite(tag_size_meters) or tag_size_meters <= 0:
        raise ValueError("tag size must be a positive finite number of meters")
    if min_views < 2 or min(min_views, max_reprojection_p95) <= 0:
        raise ValueError("min-views and reprojection threshold must be positive")
    if not 0 <= max_edge_cv < 1 or not 0 <= max_scale_error_fraction < 1:
        raise ValueError("edge and scale tolerances must be fractions in [0, 1)")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"AprilTag output is not empty: {out_dir}")
    sparse = package / sparse_dir_name
    cameras_path = sparse / "cameras.txt"
    images_path = sparse / "images.txt"
    if not cameras_path.is_file() or not images_path.is_file():
        raise FileNotFoundError("COLMAP cameras.txt and images.txt are required")
    artifact = artifact.resolve() if artifact is not None else None
    if artifact is not None and not artifact.is_file():
        raise FileNotFoundError(f"measurement artifact missing: {artifact}")
    artifact_stats = inspect_ply(artifact) if artifact is not None else None
    if artifact_stats is not None and not artifact_stats["finite"]:
        raise ValueError("measurement artifact contains non-finite values")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_NAME
    base: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "decision": "reject",
        "family": family,
        "tag_size_meters": tag_size_meters,
        "package": str(package),
        "sparse_checksums": {
            "cameras_txt": _sha256(cameras_path),
            "images_txt": _sha256(images_path),
        },
        "validated_artifact": (
            {
                "path": str(artifact),
                "checksum": _sha256(artifact),
                "coordinate_frame": "metric_colmap_world",
                "units": "meters",
                "point_count": artifact_stats["vertex_count"],
            }
            if artifact is not None else None
        ),
        "thresholds": {
            "minimum_views_per_corner": min_views,
            "max_reprojection_p95_pixels": max_reprojection_p95,
            "max_tag_edge_coefficient_of_variation": max_edge_cv,
            "max_scale_error_fraction": max_scale_error_fraction,
        },
        "authority": {
            "known_scale_validation": False,
            "measurement_authority": False,
            "collision_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
    }
    try:
        cameras = _cameras(cameras_path)
        projections = _projections(images_path, cameras)
        if detections_json is not None:
            detections = _normalize_detections(load_json_strict(detections_json.resolve()), family)
            detection_source = {
                "mode": "supplied_json",
                "path": str(detections_json.resolve()),
                "checksum": _sha256(detections_json.resolve()),
            }
        else:
            detections = _detect(package / image_dir_name, set(projections), family)
            detection_source = {"mode": "pupil_apriltags"}
        observations: dict[tuple[int, int], list[tuple[np.ndarray, np.ndarray]]] = defaultdict(list)
        normalized_images = []
        detection_images = []
        for name, tags in sorted(detections.items()):
            projection = projections.get(name)
            if projection is None:
                projection = projections.get(Path(name).stem)
            if projection is None:
                continue
            source_image = package / image_dir_name / name
            if not source_image.is_file():
                raise FileNotFoundError(f"registered AprilTag source image missing: {source_image}")
            normalized_images.append({"image": name, "tags": tags})
            detection_images.append({
                "image": name,
                "size_bytes": source_image.stat().st_size,
                "checksum": _sha256(source_image),
            })
            for tag in tags:
                for corner_index, pixel in enumerate(tag["corners"]):
                    observations[(tag["tag_id"], corner_index)].append(
                        (projection, np.asarray(pixel, dtype=np.float64))
                    )
        normalized_detections_path = out_dir / "apriltag_detections.json"
        write_json_strict(normalized_detections_path, {
            "schema": DETECTIONS_SCHEMA,
            "family": family,
            "images": normalized_images,
        })
        tag_ids = sorted({tag_id for tag_id, _ in observations})
        tag_results = []
        all_errors = []
        scale_estimates = []
        for tag_id in tag_ids:
            corners = []
            tag_errors = []
            for corner_index in range(4):
                corner_observations = observations.get((tag_id, corner_index), [])
                if len(corner_observations) < min_views:
                    corners = []
                    break
                point, errors = _triangulate(corner_observations)
                corners.append(point)
                tag_errors.extend(errors.tolist())
            if len(corners) != 4:
                continue
            corner_array = np.asarray(corners)
            edges = np.linalg.norm(np.roll(corner_array, -1, axis=0) - corner_array, axis=1)
            mean_edge = float(np.mean(edges))
            edge_cv = float(np.std(edges) / mean_edge) if mean_edge > 0 else float("inf")
            scale = tag_size_meters / mean_edge
            scale_estimates.append(scale)
            all_errors.extend(tag_errors)
            tag_results.append({
                "tag_id": tag_id,
                "corners_colmap_world": corner_array.tolist(),
                "edge_lengths_colmap_units": edges.tolist(),
                "mean_edge_length_colmap_units": mean_edge,
                "edge_coefficient_of_variation": edge_cv,
                "scale_meters_per_colmap_unit": scale,
                "reprojection_p95_pixels": float(np.percentile(tag_errors, 95)),
            })
        if not tag_results:
            raise ValueError("no AprilTag had four corners observed in enough registered views")
        scale = float(np.median(scale_estimates))
        reprojection_p95 = float(np.percentile(all_errors, 95))
        worst_edge_cv = max(result["edge_coefficient_of_variation"] for result in tag_results)
        scale_error = abs(scale - 1.0)
        failures = []
        if reprojection_p95 > max_reprojection_p95:
            failures.append("reprojection_error_exceeded")
        if worst_edge_cv > max_edge_cv:
            failures.append("tag_edge_consistency_failed")
        if scale_error > max_scale_error_fraction:
            failures.append("metric_scale_error_exceeded")
        base.update({
            "decision": "promote" if not failures else "reject",
            "reason": "known_scale_validation_accepted" if not failures else "known_scale_validation_failed",
            "detection_source": detection_source,
            "normalized_detections": {
                "path": str(normalized_detections_path),
                "checksum": _sha256(normalized_detections_path),
            },
            "detection_images": detection_images,
            "registered_detection_image_count": len(normalized_images),
            "tag_count": len(tag_results),
            "tags": tag_results,
            "scale_meters_per_colmap_unit": scale,
            "scale_error_fraction": scale_error,
            "reprojection_p95_pixels": reprojection_p95,
            "worst_tag_edge_coefficient_of_variation": worst_edge_cv,
            "failures": failures,
            "warnings": ["single_tag_evidence"] if len(tag_results) == 1 else [],
            "suggested_similarity_transform": [
                [scale, 0.0, 0.0, 0.0],
                [0.0, scale, 0.0, 0.0],
                [0.0, 0.0, scale, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ],
        })
        base["authority"]["known_scale_validation"] = not failures
    except Exception as error:
        base["error"] = str(error)
        write_json_strict(report_path, base)
        raise
    write_json_strict(report_path, base)
    return base
