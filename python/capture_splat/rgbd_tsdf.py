from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import stat
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, __version__ as pillow_version

from . import __version__ as capture_splat_version
from .capture_schema import IMAGE_KEYS, load_capture
from .json_utils import load_json_strict, write_json_strict
from .training_supervision import (
    capture_manifest_asset_conflicts,
    capture_manifest_asset_references,
    confined_capture_path,
)
from .world_studio_export import (
    MANIFEST_NAME,
    SCHEMA as HANDOFF_SCHEMA,
    _name_set_digest as _name_digest,
    _registered_image_names,
)

REPORT_NAME = "capture_splat_rgbd_tsdf_report.json"
REPORT_SCHEMA = "capture_splat.rgbd_tsdf_report.v0.1"
MESH_NAME = "rgbd_tsdf_mesh.ply"
FRAME_DIGEST_CANONICALIZATION = "canonical_json_ordered_registered_rgbd_frames_v1"
ARKIT_TO_OPENCV_CAMERA = np.diag([1.0, -1.0, -1.0, 1.0])


def _sha256(path: Path) -> str:
    before_path = path.lstat()
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"checksum input is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        before_open = os.fstat(descriptor)
        if not stat.S_ISREG(before_open.st_mode):
            raise ValueError(f"checksum input is not a regular file: {path}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if (
        identity(before_path) != identity(before_open)
        or identity(before_open) != identity(after_open)
        or identity(after_open) != identity(after_path)
    ):
        raise ValueError(f"checksum input changed while it was read: {path}")
    return f"sha256:{digest.hexdigest()}"


def _file_evidence(path: Path, root: Path) -> dict[str, Any]:
    _require_regular_file(root, path, "evidence file")
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.lstat().st_size,
        "checksum": _sha256(path),
    }


def _require_regular_file(root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} escapes the package") from error
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise ValueError(f"{label} is missing: {relative.as_posix()}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} contains a symbolic link: {relative.as_posix()}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"{label} is not a regular file: {relative.as_posix()}")


def _verify_evidence(root: Path, evidence: dict[str, Any], label: str) -> Path:
    relative = evidence["path"]
    path = confined_capture_path(root, relative)
    _require_regular_file(root, path, label)
    if path.lstat().st_size != evidence["size_bytes"] or _sha256(path) != evidence["checksum"]:
        raise ValueError(f"{label} does not match its size and checksum: {relative}")
    return path


def _asset_reference(root: Path, reference: Any, label: str) -> tuple[Path, dict[str, Any]]:
    if not isinstance(reference, dict):
        raise ValueError(f"handoff {label} asset reference is missing")
    relative = reference.get("path")
    size = reference.get("size_bytes")
    checksum = reference.get("checksum")
    if not isinstance(relative, str) or not isinstance(size, int) or size < 0:
        raise ValueError(f"handoff {label} asset reference is invalid")
    if not isinstance(checksum, str) or not checksum.startswith("sha256:") or len(checksum) != 71:
        raise ValueError(f"handoff {label} checksum is invalid")
    path = confined_capture_path(root, relative)
    evidence = {"path": relative, "size_bytes": size, "checksum": checksum}
    _verify_evidence(root, evidence, f"handoff {label} asset")
    return path, evidence


def _capture_inventory(root: Path, handoff: dict[str, Any], capture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    inventory = handoff.get("capture_manifest_assets")
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema") != "capture_splat.capture_manifest_assets.v0.1"
        or inventory.get("complete") is not True
        or inventory.get("decision") != "ready"
        or inventory.get("missing") != []
        or inventory.get("conflicts") != []
    ):
        raise ValueError("handoff capture asset inventory is not complete")
    raw_assets = inventory.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("handoff capture asset inventory is invalid")
    assets: dict[str, dict[str, Any]] = {}
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise ValueError("handoff capture asset inventory entry is invalid")
        relative = raw.get("path")
        if not isinstance(relative, str) or relative in assets:
            raise ValueError("handoff capture asset inventory has duplicate or invalid paths")
        path = confined_capture_path(root, relative)
        size = raw.get("size_bytes")
        checksum = raw.get("checksum")
        if (
            not isinstance(size, int)
            or size < 0
            or not isinstance(checksum, str)
            or not checksum.startswith("sha256:")
            or len(checksum) != 71
            or not path.exists()
        ):
            raise ValueError(f"handoff capture asset inventory entry is invalid: {relative}")
        _require_regular_file(root, path, "handoff capture asset inventory entry")
        if path.lstat().st_size != size:
            raise ValueError(f"handoff capture asset inventory entry is invalid: {relative}")
        assets[relative] = {"path": relative, "size_bytes": size, "checksum": checksum}
    references = capture_manifest_asset_references(capture)
    conflicts = capture_manifest_asset_conflicts(root, references)
    if conflicts or set(references) != set(assets):
        raise ValueError("capture manifest references do not match the handoff inventory")
    expected_count = len(assets)
    if (
        inventory.get("unique_asset_count") != expected_count
        or inventory.get("verified_asset_count") != expected_count
    ):
        raise ValueError("handoff capture asset inventory counts are inconsistent")
    return assets


def _verify_inventory_asset(root: Path, assets: dict[str, dict[str, Any]], relative: str) -> dict[str, Any]:
    evidence = assets.get(relative)
    if evidence is None:
        raise ValueError(f"consumed asset is not checksum-bound by the handoff: {relative}")
    try:
        _verify_evidence(root, evidence, "consumed asset")
    except ValueError as error:
        raise ValueError(f"consumed asset checksum mismatch: {relative}") from error
    return evidence


def _verify_selected_assets(root: Path, selected: list[dict[str, Any]]) -> None:
    for item in selected:
        for evidence in item["assets"].values():
            _verify_evidence(root, evidence, "consumed frame asset")


def _metric_coordinate_declaration(capture: dict[str, Any]) -> dict[str, Any]:
    session = capture.get("session_config")
    if not isinstance(session, dict):
        raise ValueError("capture session_config is required for metric RGB-D fusion")
    declaration = {
        "scale_authority": session.get("scale_authority"),
        "up_axis": session.get("up_axis"),
        "world_alignment": session.get("world_alignment"),
    }
    if declaration != {
        "scale_authority": "arkit_vio_metric",
        "up_axis": [0, 1, 0],
        "world_alignment": "gravity",
    }:
        raise ValueError(
            "capture does not declare the required ARKit metric gravity-aligned coordinate contract"
        )
    return declaration


def _resource_usage() -> dict[str, Any] | None:
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss_bytes: int | None
    if platform.system() == "Darwin":
        peak_rss_bytes = int(usage.ru_maxrss)
    elif platform.system() == "Linux":
        peak_rss_bytes = int(usage.ru_maxrss * 1024)
    else:
        peak_rss_bytes = None
    return {
        "user_cpu_seconds": float(usage.ru_utime),
        "system_cpu_seconds": float(usage.ru_stime),
        "peak_rss_bytes": peak_rss_bytes,
    }


def _performance_measurement(
    started_usage: dict[str, Any] | None,
    elapsed_seconds: float,
    integrated_frame_count: int,
    retained_pixel_count: int,
) -> dict[str, Any]:
    ended_usage = _resource_usage()
    result: dict[str, Any] = {
        "available": started_usage is not None and ended_usage is not None,
        "scope": "current_process; peak RSS is the process high-water mark",
        "wall_seconds": elapsed_seconds,
        "integrated_frames_per_second": (
            integrated_frame_count / elapsed_seconds if elapsed_seconds else 0.0
        ),
        "valid_megapixels_per_second": (
            retained_pixel_count / 1_000_000 / elapsed_seconds if elapsed_seconds else 0.0
        ),
    }
    if started_usage is not None and ended_usage is not None:
        result.update({
            "user_cpu_seconds": max(
                0.0, ended_usage["user_cpu_seconds"] - started_usage["user_cpu_seconds"]
            ),
            "system_cpu_seconds": max(
                0.0, ended_usage["system_cpu_seconds"] - started_usage["system_cpu_seconds"]
            ),
            "peak_rss_bytes": ended_usage["peak_rss_bytes"],
        })
    return result


def _frame_rejected(frame: dict[str, Any]) -> bool:
    quality = frame.get("capture_quality") or frame.get("quality")
    return frame.get("accepted") is False or (
        isinstance(quality, dict) and quality.get("accepted") is False
    )


def _image_path(frame: dict[str, Any]) -> str | None:
    for key in IMAGE_KEYS:
        value = frame.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _selected_frames(
    root: Path,
    capture: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    registered_names: list[str],
    expected_overlap: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    capture_frames: list[tuple[int, dict[str, Any], str]] = []
    for source_index, frame in enumerate(capture["frames"], start=1):
        if not isinstance(frame, dict) or _frame_rejected(frame):
            continue
        rgb = _image_path(frame)
        depth = frame.get("depth")
        if not isinstance(rgb, str) or not isinstance(depth, str):
            continue
        if rgb not in assets or depth not in assets:
            raise ValueError("depth-bearing capture frame is not present in the handoff inventory")
        capture_frames.append((source_index, frame, Path(rgb.replace("\\", "/")).name))
    registered_basenames = [Path(name.replace("\\", "/")).name for name in registered_names]
    registered_counts = Counter(registered_basenames)
    capture_counts = Counter(name for _, _, name in capture_frames)
    shared = set(registered_counts) & set(capture_counts)
    matched_names = sorted(
        name for name in shared if registered_counts[name] == capture_counts[name] == 1
    )
    ambiguous = [
        name for name in shared if registered_counts[name] != 1 or capture_counts[name] != 1
    ]
    overlap = {
        "depth_bearing_capture_frame_count": len(capture_frames),
        "matched_count": len(matched_names),
        "matched_name_digest": _name_digest(matched_names),
        "ambiguous_basename_count": len(ambiguous),
        "unmatched_registered_image_count": len(registered_names) - len(matched_names),
    }
    for key, value in overlap.items():
        if expected_overlap.get(key) != value:
            raise ValueError(f"registered RGB-D overlap evidence mismatch: {key}")
    if ambiguous or not matched_names:
        raise ValueError("registered RGB-D overlap is ambiguous or empty")

    matched = set(matched_names)
    selected: list[dict[str, Any]] = []
    digest_records: list[dict[str, Any]] = []
    for source_index, frame, basename in capture_frames:
        if basename not in matched:
            continue
        confidence = frame.get("confidence")
        if not isinstance(confidence, str):
            raise ValueError(f"registered RGB-D frame {source_index} has no confidence asset")
        paths = {
            "rgb": _image_path(frame),
            "depth": frame["depth"],
            "confidence": confidence,
        }
        for source_key, evidence_key in (
            ("valid_mask", "valid_mask"),
            ("person_mask", "person_mask"),
        ):
            relative = frame.get(source_key)
            if isinstance(relative, str):
                paths[evidence_key] = relative
        evidence = {
            key: _verify_inventory_asset(root, assets, str(relative))
            for key, relative in paths.items()
        }
        selected.append({
            "source_index": source_index,
            "frame": frame,
            "assets": evidence,
        })
        digest_records.append({"source_index": source_index, "assets": evidence})
    canonical = json.dumps(
        digest_records,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return selected, f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _depth_intrinsics(frame: dict[str, Any], width: int, height: int) -> tuple[float, float, float, float]:
    raw = frame.get("intrinsics")
    if not isinstance(raw, dict):
        raise ValueError("frame intrinsics are missing")
    try:
        source_width = float(raw.get("w", raw.get("width")))
        source_height = float(raw.get("h", raw.get("height")))
        values = (
            float(raw.get("fl_x", raw.get("fx"))) * width / source_width,
            float(raw.get("fl_y", raw.get("fy"))) * height / source_height,
            float(raw["cx"]) * width / source_width,
            float(raw["cy"]) * height / source_height,
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError("frame intrinsics are invalid") from error
    if not all(math.isfinite(value) for value in values) or min(values[:2]) <= 0:
        raise ValueError("frame intrinsics are invalid")
    return values


def _rigid_camera_to_world(camera_to_world: Any) -> np.ndarray:
    matrix = np.asarray(camera_to_world, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("ARKit camera-to-world transform must be a finite 4x4 matrix")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-8):
        raise ValueError("ARKit camera-to-world transform has an invalid homogeneous row")
    rotation = matrix[:3, :3]
    if (
        not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4)
        or not math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=1e-4)
    ):
        raise ValueError("ARKit camera-to-world rotation is not rigid")
    return matrix


def arkit_camera_to_open3d_extrinsic(camera_to_world: Any) -> np.ndarray:
    matrix = _rigid_camera_to_world(camera_to_world)
    camera_to_world_opencv = matrix @ ARKIT_TO_OPENCV_CAMERA
    try:
        return np.linalg.inv(camera_to_world_opencv)
    except np.linalg.LinAlgError as error:
        raise ValueError("ARKit camera-to-world transform is singular") from error


def _prevalidate_frames(
    root: Path,
    selected: list[dict[str, Any]],
    global_depth_scale: float,
) -> None:
    for item in selected:
        frame = item["frame"]
        evidence = item["assets"]
        for asset in evidence.values():
            _verify_evidence(root, asset, "prevalidated frame asset")
        frame_depth_scale = float(frame.get("depth_scale", global_depth_scale))
        if not math.isclose(frame_depth_scale, 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"frame {item['source_index']} depth_scale is not 1.0")
        depth = np.load(
            confined_capture_path(root, evidence["depth"]["path"]),
            mmap_mode="r",
            allow_pickle=False,
        )
        confidence = np.load(
            confined_capture_path(root, evidence["confidence"]["path"]),
            mmap_mode="r",
            allow_pickle=False,
        )
        if depth.dtype != np.dtype("float32") or depth.ndim != 2:
            raise ValueError(f"frame {item['source_index']} depth must be a 2D float32 NPY")
        if confidence.dtype != np.dtype("uint8") or confidence.shape != depth.shape:
            raise ValueError(f"frame {item['source_index']} confidence must be shape-matched uint8")
        height, width = depth.shape
        intrinsics = _depth_intrinsics(frame, width, height)
        camera_to_world = _rigid_camera_to_world(frame.get("transform_matrix"))
        with Image.open(confined_capture_path(root, evidence["rgb"]["path"])) as image:
            image.convert("RGB").load()
        for key in ("valid_mask", "person_mask"):
            mask = evidence.get(key)
            if isinstance(mask, dict):
                with Image.open(confined_capture_path(root, mask["path"])) as image:
                    image.convert("L").load()
        item["validated"] = {
            "depth_shape": [height, width],
            "depth_dtype": str(depth.dtype),
            "confidence_dtype": str(confidence.dtype),
            "depth_intrinsics": intrinsics,
            "camera_to_world": camera_to_world,
        }


def _open3d() -> Any:
    try:
        import open3d as o3d
    except ImportError as error:
        raise RuntimeError("Open3D 0.19.0 is required; install capture-splat[tsdf]") from error
    if o3d.__version__ != "0.19.0":
        raise RuntimeError(f"Open3D 0.19.0 is required, found {o3d.__version__}")
    return o3d


def _bounds(values: np.ndarray) -> dict[str, Any] | None:
    if not len(values):
        return None
    minimum = values.min(axis=0)
    maximum = values.max(axis=0)
    return {
        "minimum": minimum.tolist(),
        "maximum": maximum.tolist(),
        "extent": (maximum - minimum).tolist(),
    }


def _mesh_metrics(mesh: Any) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    vertices = np.asarray(mesh.vertices)
    triangles = np.asarray(mesh.triangles)
    normals = np.asarray(mesh.vertex_normals)
    colors = np.asarray(mesh.vertex_colors)
    non_finite_vertices = int(np.count_nonzero(~np.all(np.isfinite(vertices), axis=1)))
    non_finite_normals = int(np.count_nonzero(~np.all(np.isfinite(normals), axis=1))) if len(normals) else 0
    non_finite_colors = int(np.count_nonzero(~np.all(np.isfinite(colors), axis=1))) if len(colors) else 0
    invalid_indices = (
        np.any((triangles < 0) | (triangles >= len(vertices)), axis=1)
        if len(triangles) else np.zeros(0, dtype=bool)
    )
    invalid_index_count = int(np.count_nonzero(invalid_indices))
    valid_triangles = triangles[~invalid_indices]
    area_vectors = (
        np.cross(
            vertices[valid_triangles[:, 1]] - vertices[valid_triangles[:, 0]],
            vertices[valid_triangles[:, 2]] - vertices[valid_triangles[:, 0]],
        )
        if len(valid_triangles) else np.empty((0, 3))
    )
    degenerate_count = int(np.count_nonzero(np.linalg.norm(area_vectors, axis=1) <= 1e-12))
    _, component_triangle_counts, _ = mesh.cluster_connected_triangles()
    component_counts = np.asarray(component_triangle_counts, dtype=np.int64)
    non_manifold_with_boundaries = len(mesh.get_non_manifold_edges(allow_boundary_edges=False))
    non_manifold_without_boundaries = len(mesh.get_non_manifold_edges(allow_boundary_edges=True))
    return vertices, triangles, {
        "vertex_count": int(len(vertices)),
        "triangle_count": int(len(triangles)),
        "non_finite_vertex_count": non_finite_vertices,
        "non_finite_normal_count": non_finite_normals,
        "non_finite_color_count": non_finite_colors,
        "invalid_index_triangle_count": invalid_index_count,
        "degenerate_triangle_count": degenerate_count,
        "connected_component_count": int(len(component_counts)),
        "largest_component_triangle_count": int(component_counts.max()) if len(component_counts) else 0,
        "largest_component_triangle_fraction": (
            float(component_counts.max() / len(triangles)) if len(component_counts) and len(triangles) else 0.0
        ),
        "boundary_edge_count": non_manifold_with_boundaries - non_manifold_without_boundaries,
        "non_manifold_edge_count_excluding_boundaries": non_manifold_without_boundaries,
        "edge_manifold_allowing_boundaries": bool(mesh.is_edge_manifold(allow_boundary_edges=True)),
        "watertight": bool(mesh.is_watertight()),
    }


def build_rgbd_tsdf(
    handoff: Path,
    out_dir: Path,
    *,
    voxel_length: float = 0.03,
    sdf_trunc: float = 0.12,
    confidence_minimum: int = 1,
    minimum_depth: float = 0.05,
    maximum_depth: float = 7.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    started_usage = _resource_usage()
    integrated_count = 0
    retained_pixel_count = 0
    handoff = handoff.resolve()
    manifest_path = handoff / MANIFEST_NAME if handoff.is_dir() else handoff
    root = manifest_path.parent
    out_dir = out_dir.resolve()
    try:
        out_dir.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("RGB-D TSDF output must be outside the immutable handoff package")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise FileExistsError(f"RGB-D TSDF output is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / REPORT_NAME
    authority = {
        "metric_authority": False,
        "metric_geometry_authority": False,
        "collision_authority": False,
        "navigation_authority": False,
        "measurement_authority": False,
        "physics_authority": False,
        "newton_authority": False,
        "quality_claim": False,
    }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "decision": "reject",
        "reason": "structural_validation_failed",
        "budget_limited": False,
        "authority": authority,
        "parameters": {
            "implementation": "open3d_scalable_tsdf",
            "voxel_length_meters": voxel_length,
            "sdf_trunc_meters": sdf_trunc,
            "depth_scale": 1.0,
            "confidence_minimum": confidence_minimum,
            "minimum_depth_meters_exclusive": minimum_depth,
            "maximum_depth_meters_inclusive": maximum_depth,
            "color_resampling": "Pillow bilinear to depth grid",
            "mask_resampling": "Pillow nearest to depth grid",
            "frame_processing": "manifest_order_streaming_one_frame_at_a_time",
        },
        "coordinate_contract": {
            "input_camera_frame": "arkit_camera_x_right_y_up_forward_negative_z",
            "integration_camera_frame": "opencv_camera_x_right_y_down_forward_positive_z",
            "output_coordinate_frame": "arkit_world",
            "units": "meters",
            "camera_to_world_formula": "arkit_camera_to_world @ diag(1,-1,-1,1)",
            "open3d_extrinsic": "inverse(camera_to_world_opencv)",
            "arkit_to_opencv_camera_matrix": ARKIT_TO_OPENCV_CAMERA.tolist(),
        },
        "performance": {
            "decision": "hold",
            "reason": (
                "each run emits normalized measurements; production throughput requires repeated "
                "identical-condition evidence and a non-USB production lane"
            ),
            "memory_contract": "stream RGB, depth, confidence, and mask without retaining frame arrays",
        },
    }
    try:
        if (
            not manifest_path.is_file()
            or voxel_length <= 0
            or sdf_trunc <= voxel_length
            or confidence_minimum not in (0, 1, 2)
            or not 0 < minimum_depth < maximum_depth
        ):
            raise ValueError("TSDF input or parameters are invalid")
        _require_regular_file(root, manifest_path, "handoff manifest")
        initial_manifest_evidence = _file_evidence(manifest_path, root)
        validation_started = time.perf_counter()
        handoff_data = load_json_strict(manifest_path)
        if not isinstance(handoff_data, dict) or handoff_data.get("schema") != HANDOFF_SCHEMA:
            raise ValueError("RGB-D TSDF requires a World Studio handoff v0.3 manifest")
        assets = handoff_data.get("assets")
        if not isinstance(assets, dict):
            raise ValueError("handoff assets are missing")
        capture_path, capture_ref = _asset_reference(
            root, assets.get("capture_manifest"), "capture manifest"
        )
        if capture_path != root / "capture.json":
            raise ValueError("handoff capture manifest must be capture.json at the package root")
        sparse = assets.get("colmap_sparse")
        if not isinstance(sparse, dict):
            raise ValueError("handoff COLMAP sparse assets are missing")
        images_path, images_ref = _asset_reference(
            root, sparse.get("images.txt"), "COLMAP images.txt"
        )
        capture = load_capture(root)
        capture_coordinate_declaration = _metric_coordinate_declaration(capture)
        report["coordinate_contract"]["capture_declaration"] = capture_coordinate_declaration
        inventory = _capture_inventory(root, handoff_data, capture)
        registered_names, invalid_records = _registered_image_names(images_path)
        sfm = ((handoff_data.get("training_dataset") or {}).get("evidence") or {}).get("sfm")
        if not isinstance(sfm, dict) or sfm.get("registered_image_parse_status") != "complete":
            raise ValueError("handoff registered-image evidence is unavailable")
        if (
            invalid_records != 0
            or sfm.get("registered_image_invalid_record_count") != 0
            or sfm.get("registered_image_count") != len(registered_names)
            or sfm.get("registered_image_name_digest") != _name_digest(registered_names)
        ):
            raise ValueError("handoff registered-image evidence mismatch")
        expected_overlap = sfm.get("registered_rgbd_overlap")
        if not isinstance(expected_overlap, dict) or expected_overlap.get("available") is not True:
            raise ValueError("handoff registered RGB-D overlap evidence is unavailable")
        selected, frame_digest = _selected_frames(
            root, capture, inventory, registered_names, expected_overlap
        )
        global_depth_scale = float(capture.get("depth_scale", 1.0))
        if not math.isfinite(global_depth_scale) or global_depth_scale <= 0:
            raise ValueError("capture depth_scale is invalid")
        _prevalidate_frames(root, selected, global_depth_scale)
        validation_seconds = time.perf_counter() - validation_started

        o3d = _open3d()
        volume = o3d.pipelines.integration.ScalableTSDFVolume(
            voxel_length=voxel_length,
            sdf_trunc=sdf_trunc,
            color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
        )
        frame_reports: list[dict[str, Any]] = []
        camera_centers: list[np.ndarray] = []
        filter_totals = Counter()
        integration_started = time.perf_counter()
        for selected_frame in selected:
            source_index = selected_frame["source_index"]
            evidence = selected_frame["assets"]
            validated = selected_frame["validated"]
            frame_report: dict[str, Any] = {
                "source_index": source_index,
                "assets": evidence,
                "depth_shape": validated["depth_shape"],
                "depth_dtype": validated["depth_dtype"],
                "confidence_dtype": validated["confidence_dtype"],
            }
            for asset in evidence.values():
                _verify_evidence(root, asset, "integrated frame asset")
            depth = np.load(
                confined_capture_path(root, evidence["depth"]["path"]), allow_pickle=False
            )
            confidence = np.load(
                confined_capture_path(root, evidence["confidence"]["path"]), allow_pickle=False
            )
            height, width = depth.shape
            finite = np.isfinite(depth)
            depth_range = finite & (depth > minimum_depth) & (depth <= maximum_depth)
            confidence_valid = confidence >= confidence_minimum
            valid_mask = np.ones(depth.shape, dtype=bool)
            valid_mask_evidence = evidence.get("valid_mask")
            if isinstance(valid_mask_evidence, dict):
                with Image.open(confined_capture_path(root, valid_mask_evidence["path"])) as image:
                    valid_mask = np.asarray(
                        image.convert("L").resize((width, height), Image.Resampling.NEAREST)
                    ) > 0
            person_mask = np.zeros(depth.shape, dtype=bool)
            person_mask_evidence = evidence.get("person_mask")
            if isinstance(person_mask_evidence, dict):
                with Image.open(confined_capture_path(root, person_mask_evidence["path"])) as image:
                    person_mask = np.asarray(
                        image.convert("L").resize((width, height), Image.Resampling.NEAREST)
                    ) > 0
            after_confidence = depth_range & confidence_valid
            after_valid_mask = after_confidence & valid_mask
            valid = after_valid_mask & ~person_mask
            filter_counts = {
                "input_pixel_count": int(depth.size),
                "non_finite_depth_pixel_count": int(np.count_nonzero(~finite)),
                "at_or_below_minimum_depth_pixel_count": int(
                    np.count_nonzero(finite & (depth <= minimum_depth))
                ),
                "above_maximum_depth_pixel_count": int(
                    np.count_nonzero(finite & (depth > maximum_depth))
                ),
                "below_confidence_pixel_count": int(
                    np.count_nonzero(depth_range & ~confidence_valid)
                ),
                "valid_mask_zero_pixel_count": int(np.count_nonzero(~valid_mask)),
                "valid_mask_excluded_pixel_count": int(
                    np.count_nonzero(after_confidence & ~valid_mask)
                ),
                "person_mask_pixel_count": int(np.count_nonzero(person_mask)),
                "person_mask_excluded_pixel_count": int(
                    np.count_nonzero(after_valid_mask & person_mask)
                ),
                "retained_pixel_count": int(np.count_nonzero(valid)),
            }
            filter_totals.update(filter_counts)
            frame_report["filter_counts"] = filter_counts
            if not filter_counts["retained_pixel_count"]:
                frame_report.update({"status": "skipped", "reason": "no_valid_depth_pixels"})
                frame_reports.append(frame_report)
                continue
            filtered_depth = np.where(valid, depth, 0.0).astype(np.float32, copy=False)
            with Image.open(confined_capture_path(root, evidence["rgb"]["path"])) as image:
                color = np.asarray(
                    image.convert("RGB").resize((width, height), Image.Resampling.BILINEAR)
                )
            fx, fy, cx, cy = validated["depth_intrinsics"]
            camera_to_world = validated["camera_to_world"]
            rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                o3d.geometry.Image(np.ascontiguousarray(color)),
                o3d.geometry.Image(np.ascontiguousarray(filtered_depth)),
                depth_scale=1.0,
                depth_trunc=float(np.nextafter(maximum_depth, math.inf)),
                convert_rgb_to_intensity=False,
            )
            intrinsic = o3d.camera.PinholeCameraIntrinsic(width, height, fx, fy, cx, cy)
            volume.integrate(
                rgbd,
                intrinsic,
                arkit_camera_to_open3d_extrinsic(camera_to_world),
            )
            camera_centers.append(camera_to_world[:3, 3])
            frame_report.update({
                "status": "integrated",
                "valid_depth_fraction": filter_counts["retained_pixel_count"] / depth.size,
                "depth_intrinsics": {"fl_x": fx, "fl_y": fy, "cx": cx, "cy": cy},
            })
            frame_reports.append(frame_report)
        integration_seconds = time.perf_counter() - integration_started
        integrated_count = sum(item["status"] == "integrated" for item in frame_reports)
        retained_pixel_count = int(filter_totals["retained_pixel_count"])
        if integrated_count == 0:
            raise ValueError("no RGB-D frames could be integrated")

        _verify_selected_assets(root, selected)
        _verify_evidence(root, capture_ref, "capture manifest after fusion")
        _verify_evidence(root, images_ref, "COLMAP images after fusion")
        _verify_evidence(root, initial_manifest_evidence, "handoff manifest after fusion")

        extraction_started = time.perf_counter()
        mesh = volume.extract_triangle_mesh()
        mesh.compute_vertex_normals()
        vertices, triangles, mesh_metrics = _mesh_metrics(mesh)
        if (
            len(vertices) == 0
            or len(triangles) == 0
            or mesh_metrics["non_finite_vertex_count"]
            or mesh_metrics["non_finite_normal_count"]
            or mesh_metrics["non_finite_color_count"]
            or mesh_metrics["invalid_index_triangle_count"]
        ):
            raise ValueError("Open3D produced no finite triangle mesh")
        mesh_path = out_dir / MESH_NAME
        if not o3d.io.write_triangle_mesh(
            str(mesh_path), mesh, write_ascii=False, compressed=False, write_vertex_normals=True
        ):
            raise OSError("Open3D failed to write the TSDF mesh")
        extraction_seconds = time.perf_counter() - extraction_started
        centers = np.asarray(camera_centers, dtype=np.float64)
        cells = {tuple(np.floor(center / 0.5).astype(np.int64).tolist()) for center in centers}
        skipped_count = len(frame_reports) - integrated_count
        report.update({
            "decision": "hold",
            "reason": "derived_rgbd_mesh_requires_coverage_registration_and_collision_validation",
            "software_surface_candidate": "hold",
            "inputs": {
                "handoff_manifest": initial_manifest_evidence,
                "handoff_schema": HANDOFF_SCHEMA,
                "capture_manifest": capture_ref,
                "colmap_images": images_ref,
                "registered_image_count": len(registered_names),
                "registered_rgbd_frame_count": len(selected),
                "ordered_registered_rgbd_frame_digest": frame_digest,
                "frame_digest_canonicalization": FRAME_DIGEST_CANONICALIZATION,
            },
            "versions": {
                "capture_splat": capture_splat_version,
                "python": platform.python_version(),
                "numpy": np.__version__,
                "pillow": pillow_version,
                "open3d": o3d.__version__,
                "platform": platform.platform(),
            },
            "frames": frame_reports,
            "coverage": {
                "selected_frame_count": len(selected),
                "integrated_frame_count": integrated_count,
                "skipped_frame_count": skipped_count,
                "integrated_frame_fraction": integrated_count / len(selected),
                "filter_counts": dict(sorted(filter_totals.items())),
                "valid_depth_pixel_fraction": (
                    filter_totals["retained_pixel_count"] / filter_totals["input_pixel_count"]
                    if filter_totals["input_pixel_count"] else 0.0
                ),
                "person_mask_frame_count": sum(
                    isinstance(item["assets"].get("person_mask"), dict) for item in selected
                ),
                "person_mask_scope": (
                    "applied_separately_after_valid_mask_only_where_checksum_bound_person_masks_are_present; "
                    "additional_exclusion_may_be_zero_when_valid_mask_already_excludes_the_same_pixels"
                ),
                "dynamic_cleanup_complete": False,
                "trajectory_coverage": {
                    "camera_center_bounds_meters": _bounds(centers),
                    "camera_center_half_meter_cell_count": len(cells),
                },
                "rails": {
                    "depth_render_support": {
                        "status": "pending",
                        "reason": "not_implemented_in_first_slice",
                    },
                    "observed_surface_coverage": {
                        "status": "pending",
                        "reason": "trajectory_cells_are_not_surface_coverage",
                    },
                    "floor_wall_ceiling_opening_continuity": {
                        "status": "pending",
                        "reason": "semantic_surface_review_pending",
                    },
                    "splat_mesh_registration": {
                        "status": "pending",
                        "reason": "same_pose_visual_overlap_review_pending",
                    },
                    "physical_validation": {
                        "status": "pending",
                        "reason": "known_distance_and_collision_probes_pending",
                    },
                },
                "warnings": ["partial_frame_integration"] if skipped_count else [],
            },
            "mesh": {
                **_file_evidence(mesh_path, out_dir),
                **mesh_metrics,
                "finite": True,
                "budget_limited": False,
                "software_surface_candidate": "hold",
                "bounds_meters": _bounds(vertices),
                "coordinate_frame": "arkit_world",
                "units": "meters",
                "coordinate_declaration": capture_coordinate_declaration,
            },
            "timings_seconds": {
                "validation": validation_seconds,
                "integration": integration_seconds,
                "extraction_and_write": extraction_seconds,
                "total": time.perf_counter() - started,
            },
        })
    except Exception as error:
        report["error"] = str(error)
        report["error_type"] = type(error).__name__
        total_seconds = time.perf_counter() - started
        report["timings_seconds"] = {"total": total_seconds}
        report["performance"]["measurement"] = _performance_measurement(
            started_usage, total_seconds, integrated_count, retained_pixel_count
        )
        write_json_strict(report_path, report)
        raise
    total_seconds = time.perf_counter() - started
    report["timings_seconds"]["total"] = total_seconds
    report["performance"]["measurement"] = _performance_measurement(
        started_usage, total_seconds, integrated_count, retained_pixel_count
    )
    write_json_strict(report_path, report)
    return report
