from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .json_utils import load_json_strict, write_json_strict
from .ply_stats import inspect_ply
from .rgbd_seed import camera_alignment_report
from .scene_transform import SIDECAR_NAME, metric_package_status

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
MANIFEST_NAME = "capture-splat.world-studio.json"
SCHEMA = "capture_splat.world_studio_handoff.v0.3"
SUPPORTED_SCHEMAS = ("capture_splat.world_studio_handoff.v0.2", SCHEMA)
TRAINING_DATASET_SCHEMA = "capture_splat.training_dataset.v0.1"
CAPTURE_PROFILES = ("object", "room_interior", "walkthrough", "outdoor", "video_360")


def _ply_positions(path: Path, sample_cap: int = 200_000) -> np.ndarray | None:
    try:
        with path.open("rb") as handle:
            header_lines = []
            while True:
                raw = handle.readline()
                if raw == b"":
                    return None
                line = raw.decode("ascii", errors="replace").strip()
                header_lines.append(line)
                if line == "end_header":
                    break
            offset = handle.tell()
    except OSError:
        return None
    fmt = next((line.split()[1] for line in header_lines if line.startswith("format ")), None)
    props = [line.split() for line in header_lines if line.startswith("property ")]
    if fmt == "binary_little_endian" and props and all(part[1] == "float" for part in props):
        count = int(next(line.split()[2] for line in header_lines if line.startswith("element vertex ")))
        data = np.fromfile(path, dtype="<f4", offset=offset, count=count * len(props))
        if len(data) < count * len(props):
            return None
        return data.reshape(count, len(props))[:, :3]
    if fmt == "ascii":
        rows = []
        for line in path.read_text(encoding="ascii").split("end_header", 1)[1].strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
            if len(rows) >= sample_cap:
                break
        return np.asarray(rows) if rows else None
    return None


def _scene_extent(gaussian: Path | None) -> dict[str, float] | None:
    if gaussian is None or gaussian.suffix.lower() != ".ply":
        return None
    positions = _ply_positions(gaussian)
    if positions is None or len(positions) < 8:
        return None
    stride = max(1, len(positions) // 200_000)
    sampled = positions[::stride]
    center = np.median(sampled, axis=0)
    distances = np.linalg.norm(sampled - center, axis=1)
    finite = distances[np.isfinite(distances)]
    if len(finite) < 8:
        return None
    return {
        "scene_radius": float(np.percentile(finite, 95)),
        "median_structure_distance": float(np.percentile(finite, 50)),
    }


def _scene_transform_sidecar(gaussian: Path | None) -> dict[str, Any] | None:
    if gaussian is None:
        return None
    sidecar_path = gaussian.resolve().parent / SIDECAR_NAME
    if not sidecar_path.exists():
        return None
    try:
        sidecar = load_json_strict(sidecar_path)
    except ValueError:
        return None
    return sidecar if isinstance(sidecar, dict) else None


def _first_frame_camera(sparse_dir: Path | None) -> dict[str, list[float]] | None:
    if sparse_dir is None:
        return None
    images_txt = sparse_dir / "images.txt"
    if not images_txt.exists():
        return None
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
        rotation = [
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qw * qz), 2 * (qx * qz + qw * qy)],
            [2 * (qx * qy + qw * qz), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qw * qx)],
            [2 * (qx * qz - qw * qy), 2 * (qy * qz + qw * qx), 1 - 2 * (qx * qx + qy * qy)],
        ]
        center = np.asarray([
            -(rotation[0][0] * tx + rotation[1][0] * ty + rotation[2][0] * tz),
            -(rotation[0][1] * tx + rotation[1][1] * ty + rotation[2][1] * tz),
            -(rotation[0][2] * tx + rotation[1][2] * ty + rotation[2][2] * tz),
        ])
        rotation_array = np.asarray(rotation, dtype=np.float64)
        forward = rotation_array.T @ np.asarray([0.0, 0.0, 1.0])
        up = rotation_array.T @ np.asarray([0.0, -1.0, 0.0])
        return {
            "position": center.tolist(),
            "look_at": (center + forward).tolist(),
            "up": up.tolist(),
        }
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _file_ref(path: Path, root: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "checksum": _sha256(path),
    }


def _copy_or_link(src: Path, dst: Path, copy_files: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dst.resolve():
        return
    if copy_files:
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        shutil.copy2(src, dst)
    else:
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)


def _find_images(package: Path, image_dir_name: str) -> list[Path]:
    image_dir = package / image_dir_name
    if not image_dir.exists():
        return []
    return sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)


def _copy_images(images: list[Path], out_dir: Path, copy_files: bool) -> list[Path]:
    copied: list[Path] = []
    image_out = out_dir / "images"
    for src in images:
        dst = image_out / src.name
        _copy_or_link(src, dst, copy_files)
        copied.append(dst)
    return copied


def _copy_asset(src: Path | None, out_dir: Path, name: str, copy_files: bool) -> Path | None:
    if src is None:
        return None
    src = src.resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    dst = out_dir / name
    _copy_or_link(src, dst, copy_files)
    return dst


def _write_quality_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    write_json_strict(path, payload)
    return path


def _validated_render_source_qa(path: Path) -> dict[str, Any]:
    summary = load_json_strict(path)
    if not isinstance(summary, dict):
        raise ValueError("render/source QA summary must be a JSON object")
    if summary.get("schema") != "capture_splat.render_source_qa.v0.1":
        raise ValueError("render/source QA summary has an unsupported schema")
    if summary.get("decision") not in {"promote", "hold", "reject"}:
        raise ValueError("render/source QA summary decision must be promote, hold, or reject")
    for key in ("frame_count", "valid_frame_count"):
        value = summary.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"render/source QA summary {key} must be a non-negative integer")
    if summary["valid_frame_count"] > summary["frame_count"]:
        raise ValueError("render/source QA summary valid_frame_count cannot exceed frame_count")
    weak_frames = summary.get("weak_frames")
    if not isinstance(weak_frames, list) or not all(isinstance(value, str) for value in weak_frames):
        raise ValueError("render/source QA summary weak_frames must be a string array")
    return summary


def _capture_asset(
    capture_manifest: Path | None,
    capture: dict[str, Any] | None,
    key: str,
    fallback: str,
) -> Path | None:
    if capture_manifest is None or capture is None:
        return None
    relative = capture.get(key, fallback)
    if not isinstance(relative, str) or not relative:
        return None
    path = capture_manifest.resolve().parent / relative
    return path if path.exists() else None


def _metric_asset_ref(
    path: Path,
    root: Path,
    coordinate_frame: str,
    authority: str,
    units: str | None = None,
) -> dict[str, Any]:
    ref = _file_ref(path, root)
    ref.update({"coordinate_frame": coordinate_frame, "authority": authority})
    if units is not None:
        ref["units"] = units
    return ref


def _measurement_eligibility(
    points: Path | None,
    coordinate_frame: str,
    units: str,
    package: Path,
    sparse_dir_name: str,
    known_scale_report: Path | None,
) -> dict[str, Any]:
    authority = {
        "measurement_authority": False,
        "collision_authority": False,
        "quality_claim": False,
    }
    if points is None:
        return {"status": "missing", "reason": "measurement_points_missing", "authority": authority}
    try:
        stats = inspect_ply(points)
    except (OSError, ValueError) as error:
        return {"status": "held", "reason": f"measurement_points_invalid:{error}", "authority": authority}
    if not stats["finite"]:
        return {"status": "held", "reason": "measurement_points_non_finite", "authority": authority}
    if coordinate_frame != "metric_colmap_world" or units != "meters":
        return {"status": "held", "reason": "metric_coordinate_frame_unavailable", "authority": authority}
    metric = metric_package_status(package, sparse_dir_name)
    if not metric["accepted"]:
        return {"status": "held", "reason": metric["reason"], "authority": authority}
    report = load_json_strict(Path(metric["report"]))
    checksum = (report.get("output_checksums") or {}).get("metric_seed_ply")
    if checksum != _sha256(points):
        return {"status": "held", "reason": "metric_seed_checksum_mismatch", "authority": authority}
    if known_scale_report is None:
        return {
            "status": "held",
            "reason": "physical_known_distance_validation_pending",
            "software_prerequisites": True,
            "point_count": stats["vertex_count"],
            "coordinate_frame": coordinate_frame,
            "units": units,
            "authority": authority,
        }
    try:
        known = load_json_strict(known_scale_report)
    except (OSError, ValueError):
        return {"status": "held", "reason": "known_scale_report_invalid", "authority": authority}
    artifact = known.get("validated_artifact")
    sparse_checksums = known.get("sparse_checksums")
    sparse = package / sparse_dir_name
    if (
        known.get("schema") != "capture_splat.apriltag_scale_validation.v0.1"
        or known.get("decision") != "promote"
        or (known.get("authority") or {}).get("known_scale_validation") is not True
        or not isinstance(artifact, dict)
        or artifact.get("checksum") != _sha256(points)
        or artifact.get("coordinate_frame") != coordinate_frame
        or artifact.get("units") != units
        or not isinstance(sparse_checksums, dict)
        or sparse_checksums.get("cameras_txt") != _sha256(sparse / "cameras.txt")
        or sparse_checksums.get("images_txt") != _sha256(sparse / "images.txt")
    ):
        return {"status": "held", "reason": "known_scale_evidence_not_bound", "authority": authority}
    return {
        "status": "eligible",
        "reason": "known_scale_validation_accepted",
        "software_prerequisites": True,
        "point_count": stats["vertex_count"],
        "coordinate_frame": coordinate_frame,
        "units": units,
        "authority": {**authority, "known_scale_validation": True},
    }


def _collision_eligibility(candidate: Path | None, report_path: Path | None) -> dict[str, Any]:
    authority = {
        "collision_authority": False,
        "navigation_authority": False,
        "quality_claim": False,
    }
    if candidate is None or report_path is None:
        return {"status": "missing", "reason": "collision_candidate_or_report_missing", "authority": authority}
    try:
        report = load_json_strict(report_path)
    except (OSError, ValueError):
        return {"status": "reject", "reason": "collision_candidate_report_invalid", "authority": authority}
    if report.get("schema") != "capture_splat.collision_candidate.v0.1":
        return {"status": "reject", "reason": "collision_candidate_report_schema_invalid", "authority": authority}
    evidence = report.get("candidate")
    if not isinstance(evidence, dict) or evidence.get("checksum") != _sha256(candidate):
        return {"status": "reject", "reason": "collision_candidate_checksum_mismatch", "authority": authority}
    if report.get("coordinate_frame") != "arkit_world" or report.get("units") != "meters":
        return {"status": "reject", "reason": "collision_candidate_frame_or_units_invalid", "authority": authority}
    return {
        "status": "held",
        "reason": report.get("reason", "physical_collision_validation_pending"),
        "software_prerequisites": report.get("software_prerequisites") is True,
        "authority": authority,
    }


def _mesh_walk_evidence(report_path: Path | None) -> dict[str, Any]:
    if report_path is None or not report_path.exists():
        return {"status": "held", "reason": "mesh_report_missing"}
    try:
        report = load_json_strict(report_path)
    except (OSError, ValueError):
        return {"status": "held", "reason": "mesh_report_invalid"}
    if not isinstance(report, dict) or report.get("status") != "finite_mesh_written":
        return {"status": "held", "reason": "finite_mesh_not_reported"}
    non_finite = report.get("non_finite_vertex_count", 0)
    if not isinstance(non_finite, int) or non_finite != 0:
        return {"status": "held", "reason": "non_finite_mesh_vertices"}
    budget_limited = report.get("budget_limited", report.get("truncated", False)) is True
    if not budget_limited:
        return {"status": "accepted", "reason": "complete_finite_mesh"}
    coverage_preserving = report.get("coverage_preserving") is True
    anchor_ratio = report.get("anchor_coverage_ratio")
    spatial_ratio = report.get("spatial_cell_coverage_ratio")
    ratios_complete = all(
        isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.999 <= float(value) <= 1.001
        for value in (anchor_ratio, spatial_ratio)
    )
    counts_complete = (
        isinstance(report.get("eligible_anchor_count"), int)
        and report["eligible_anchor_count"] > 0
        and report.get("exported_anchor_count") == report["eligible_anchor_count"]
        and isinstance(report.get("source_spatial_cell_count"), int)
        and report["source_spatial_cell_count"] > 0
        and report.get("exported_spatial_cell_count") == report["source_spatial_cell_count"]
    )
    selection_policy = report.get("selection_policy")
    if (
        report.get("schema") == "capture_splat.arkit_mesh_report.v0.2"
        and coverage_preserving
        and ratios_complete
        and counts_complete
        and selection_policy == "anchor_spatial_stratified_even_faces_v1"
    ):
        return {
            "status": "accepted",
            "reason": "coverage_preserving_budgeted_mesh",
            "selection_policy": selection_policy,
        }
    return {"status": "held", "reason": "source_mesh_truncated"}


def _metric_registration(
    capture_manifest: Path | None,
    package: Path,
    sparse_dir_name: str,
    trainer_transform: list[list[float]] | None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "capture_splat.metric_registration.v0.1",
        "status": "unavailable",
        "source_coordinate_frame": "arkit_world",
        "intermediate_coordinate_frame": "colmap_world",
        "target_coordinate_frame": "trainer_world" if trainer_transform is not None else "colmap_world",
        "source_units": "meters",
        "target_units": "normalized_scene_units" if trainer_transform is not None else "colmap_units",
        "authority": {
            "camera_center_alignment_evidence": True,
            "metric_mesh_registration_candidate": False,
            "collision_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
    }
    if capture_manifest is None:
        return {**base, "reason": "capture_manifest_missing"}
    if capture_manifest.name != "capture.json":
        return {**base, "reason": "capture_manifest_must_be_named_capture.json"}
    if not (package / sparse_dir_name / "images.txt").exists():
        return {**base, "reason": "colmap_images_missing"}
    try:
        alignment = camera_alignment_report(
            capture_manifest.parent,
            package,
            sparse_dir_name=sparse_dir_name,
        )
    except (FileNotFoundError, ValueError) as error:
        return {**base, "reason": str(error)}
    status = "accepted" if alignment.get("accepted") is True else "held"
    registration = {**base, **alignment, "status": status}
    matrix = alignment.get("matrix")
    if status != "accepted" or not isinstance(matrix, list):
        registration["authority"] = base["authority"]
        return registration
    arkit_to_colmap = np.asarray(matrix, dtype=np.float64)
    colmap_to_target = np.asarray(trainer_transform or np.eye(4), dtype=np.float64)
    if arkit_to_colmap.shape != (4, 4) or colmap_to_target.shape != (4, 4):
        return {**registration, "status": "held", "reason": "invalid_transform_shape"}
    arkit_to_target = colmap_to_target @ arkit_to_colmap
    linear = arkit_to_target[:3, :3]
    units_per_meter = float(abs(np.linalg.det(linear)) ** (1.0 / 3.0))
    if not np.all(np.isfinite(arkit_to_target)) or not math.isfinite(units_per_meter) or units_per_meter <= 0:
        return {**registration, "status": "held", "reason": "non_finite_composed_transform"}
    registration.update({
        "arkit_to_colmap": arkit_to_colmap.tolist(),
        "colmap_to_target": colmap_to_target.tolist(),
        "arkit_to_target": arkit_to_target.tolist(),
        "target_units_per_meter": units_per_meter,
        "meters_per_target_unit": 1.0 / units_per_meter,
        "authority": {
            **base["authority"],
            "metric_mesh_registration_candidate": True,
        },
    })
    return registration


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = root / name
        if path.exists():
            return path
    return None


def _copy_sparse_dir(package: Path, out_dir: Path, sparse_dir_name: str, copy_files: bool) -> Path | None:
    sparse = package / sparse_dir_name
    if not sparse.exists():
        return None
    copied_any = False
    for name in ("cameras.txt", "images.txt", "points3D.txt"):
        src = sparse / name
        if src.exists():
            _copy_or_link(src, out_dir / sparse_dir_name / name, copy_files)
            copied_any = True
    return out_dir / sparse_dir_name if copied_any else None


def _frames(images: list[Path], out_dir: Path) -> list[dict[str, Any]]:
    frames = []
    for index, path in enumerate(images, start=1):
        frames.append({
            "frame_id": path.stem,
            "display_name": path.stem,
            "rgb_path": path.relative_to(out_dir).as_posix(),
            "source_role": "visual_evidence",
            "index": index,
            "size_bytes": path.stat().st_size,
            "checksum": _sha256(path),
        })
    return frames


def _canonical_frame_set(frames: list[dict[str, Any]]) -> dict[str, Any]:
    digest = hashlib.sha256()
    for frame in sorted(frames, key=lambda value: str(value["rgb_path"])):
        digest.update(str(frame["rgb_path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(frame["size_bytes"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(frame["checksum"]).encode("ascii"))
        digest.update(b"\n")
    return {
        "count": len(frames),
        "digest": f"sha256:{digest.hexdigest()}",
        "canonicalization": "utf8_relative_path_nul_size_nul_sha256_lf_v1",
    }


def _colmap_evidence(sparse_dir: Path | None) -> dict[str, Any]:
    if sparse_dir is None:
        return {
            "available": False,
            "camera_count": 0,
            "camera_models": [],
            "registered_images_available": False,
            "sparse_points_available": False,
        }
    cameras = sparse_dir / "cameras.txt"
    models: set[str] = set()
    camera_count = 0
    if cameras.is_file():
        for raw_line in cameras.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                int(parts[0])
            except ValueError:
                continue
            camera_count += 1
            model = parts[1]
            if 0 < len(model) <= 64 and all(character.isalnum() or character == "_" for character in model):
                models.add(model)
    return {
        "available": cameras.is_file() and (sparse_dir / "images.txt").is_file(),
        "camera_count": camera_count,
        "camera_models": sorted(models),
        "registered_images_available": (sparse_dir / "images.txt").is_file(),
        "sparse_points_available": (sparse_dir / "points3D.txt").is_file(),
    }


def _confined_file(root: Path, relative: str) -> bool:
    try:
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return path.is_file()


def _frame_asset_evidence(
    capture: dict[str, Any] | None,
    capture_root: Path | None,
    package: Path,
    keys: tuple[str, ...],
) -> dict[str, int]:
    referenced = 0
    available = 0
    frames = capture.get("frames", []) if isinstance(capture, dict) else []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        references = [frame.get(key) for key in keys]
        references = [value for value in references if isinstance(value, str) and value]
        if not references:
            continue
        referenced += 1
        roots = (package, capture_root) if capture_root is not None else (package,)
        if any(_confined_file(root, relative) for root in roots for relative in references):
            available += 1
    return {
        "referenced_frame_count": referenced,
        "available_frame_count": available,
    }


def _projection_evidence(
    package: Path,
    capture_manifest: Path | None,
    capture_profile: str,
) -> dict[str, Any]:
    roots = [package]
    if capture_manifest is not None:
        roots.append(capture_manifest.resolve().parent)
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for relative in (
            "metadata/source_equirectangular_rig.json",
            "metadata/equirectangular_rig.json",
        ):
            candidate = root / relative
            resolved = candidate.resolve()
            if candidate.is_file() and resolved not in seen:
                candidates.append(candidate)
                seen.add(resolved)
    for candidate in candidates:
        try:
            rig = load_json_strict(candidate)
        except (OSError, ValueError):
            continue
        if not isinstance(rig, dict) or rig.get("schema") != "capture_splat.equirectangular_rig.v0.1":
            continue
        projection_model = str(rig.get("projection_model", "")).upper()
        if projection_model == "PINHOLE":
            mode = "projected_pinhole_from_equirectangular"
        elif projection_model == "EQUIRECTANGULAR":
            mode = "native_equirectangular"
        else:
            mode = "unresolved_equirectangular_source"
        return {
            "mode": mode,
            "source_is_equirectangular": True,
            "training_images_are_projected_pinhole": mode == "projected_pinhole_from_equirectangular",
            "native_equirectangular": mode == "native_equirectangular",
            "rig_evidence": {
                "available": True,
                "schema": rig["schema"],
                "checksum": _sha256(candidate),
            },
        }
    mode = "unresolved_360_source" if capture_profile == "video_360" else "perspective"
    return {
        "mode": mode,
        "source_is_equirectangular": None if mode == "unresolved_360_source" else False,
        "training_images_are_projected_pinhole": False,
        "native_equirectangular": False,
        "rig_evidence": {"available": False},
    }


def _training_dataset(
    package: Path,
    source_frames: list[dict[str, Any]],
    copied_sparse: Path | None,
    capture_manifest: Path | None,
    capture: dict[str, Any] | None,
    capture_profile: str,
    copied_capture: Path | None,
    copied_navigation_mesh: Path | None,
    copied_mesh_report: Path | None,
) -> dict[str, Any]:
    capture_root = capture_manifest.resolve().parent if capture_manifest is not None else None
    return {
        "schema": TRAINING_DATASET_SCHEMA,
        "capture_profile": capture_profile,
        "source_frame_set": _canonical_frame_set(source_frames),
        "projection": _projection_evidence(package, capture_manifest, capture_profile),
        "evidence": {
            "capture_manifest": {
                "available": copied_capture is not None,
                "asset": "capture_manifest" if copied_capture is not None else None,
            },
            "sfm": {
                **_colmap_evidence(copied_sparse),
                "asset": "colmap_sparse" if copied_sparse is not None else None,
            },
            "depth": _frame_asset_evidence(capture, capture_root, package, ("depth",)),
            "confidence": _frame_asset_evidence(capture, capture_root, package, ("confidence",)),
            "masks": _frame_asset_evidence(
                capture,
                capture_root,
                package,
                ("valid_mask", "person_mask", "object_mask"),
            ),
            "mesh": {
                "available": copied_navigation_mesh is not None,
                "asset": "navigation_mesh" if copied_navigation_mesh is not None else None,
                "report_available": copied_mesh_report is not None,
                "report_asset": "mesh_report" if copied_mesh_report is not None else None,
            },
        },
        "authority": {
            "capture_evidence_only": True,
            "trainer_consumption_claim": False,
            "training_execution_authority": False,
            "quality_claim": False,
            "metric_authority": False,
            "collision_authority": False,
        },
    }


def _dataparser_transform(gaussian: Path | None) -> list[list[float]] | None:
    """Trainer world transform from train.json next to the trained PLY.

    VkSplat-style trainers optimize splats in a normalized world; viewers need
    this 4x4 row-major matrix to map raw COLMAP poses into the splat world.
    """
    if gaussian is None:
        return None
    train_json = gaussian.resolve().parent / "train.json"
    if not train_json.exists():
        return None
    try:
        meta = json.loads(train_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = meta.get("dataparser_transform")
    if not isinstance(value, list) or len(value) != 4:
        return None
    rows: list[list[float]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            return None
        floats = [float(entry) for entry in row]
        if not all(math.isfinite(entry) for entry in floats):
            return None
        rows.append(floats)
    return rows


def export_world_studio_handoff(
    package: Path,
    out_dir: Path,
    gaussian: Path | None = None,
    points: Path | None = None,
    capture_manifest: Path | None = None,
    transforms: Path | None = None,
    poses: Path | None = None,
    camera_poses: Path | None = None,
    splat: Path | None = None,
    spz: Path | None = None,
    navigation_mesh: Path | None = None,
    mesh_report: Path | None = None,
    room_semantics: Path | None = None,
    camera_trajectory: Path | None = None,
    planes: Path | None = None,
    metric_scale_report: Path | None = None,
    known_scale_report: Path | None = None,
    collision_candidate: Path | None = None,
    collision_report: Path | None = None,
    render_source_qa: Path | None = None,
    measurement_points: Path | None = None,
    measurement_points_frame: str = "colmap_world",
    image_dir_name: str = "images",
    sparse_dir_name: str = "sparse/0",
    copy_files: bool = False,
    capture_profile: str | None = None,
) -> dict[str, Any]:
    package = package.resolve()
    out_dir = out_dir.resolve()
    if measurement_points_frame not in {"arkit_world", "colmap_world", "metric_colmap_world", "trainer_world"}:
        raise ValueError(f"unsupported measurement points frame: {measurement_points_frame}")
    out_dir.mkdir(parents=True, exist_ok=True)
    images = _find_images(package, image_dir_name)
    if not images:
        raise FileNotFoundError(f"no source images found in {package / image_dir_name}")
    gaussian = gaussian or _first_existing(package, (
        "splat.pruned_a12.ply",
        "gaussians.pruned_a12.ply",
        "gaussian.pruned_a12.ply",
        "splat.ply",
        "gaussians.ply",
        "gaussian.ply",
    ))
    gaussian_variant = "alpha_pruned" if gaussian is not None and ".pruned_a" in gaussian.name else "raw"
    points = points or _first_existing(package, ("points.ply", "point_cloud.ply", "cloud.ply"))
    capture_manifest = capture_manifest or _first_existing(package, ("capture.json",))
    transforms = transforms or _first_existing(package, ("transforms.json",))
    poses = poses or _first_existing(package, ("poses.json",))
    camera_poses = camera_poses or _first_existing(package, ("camera_poses.json", "camera-poses.json"))
    splat = splat or _first_existing(package, ("scene.splat", "splat.splat"))
    spz = spz or _first_existing(package, ("scene.spz", "splat.spz"))
    capture_data = load_json_strict(capture_manifest) if capture_manifest and capture_manifest.exists() else None
    navigation_mesh = navigation_mesh or _capture_asset(
        capture_manifest, capture_data, "arkit_mesh_file", "geometry/arkit_mesh.ply"
    )
    mesh_report = mesh_report or _capture_asset(
        capture_manifest, capture_data, "arkit_mesh_report_file", "geometry/arkit_mesh_report.json"
    )
    room_semantics = room_semantics or _capture_asset(
        capture_manifest, capture_data, "room_plan_semantics_file", "room_plan/room_semantics.json"
    )
    camera_trajectory = camera_trajectory or _capture_asset(
        capture_manifest, capture_data, "frame_index_file", "metadata/frame_index.jsonl"
    )
    planes = planes or _capture_asset(
        capture_manifest, capture_data, "planes_file", "metadata/planes.json"
    )
    spatial_guidance = _capture_asset(
        capture_manifest, capture_data, "spatial_guidance_report_file", "metadata/spatial_guidance_report.json"
    )
    source_capture_manifest = _capture_asset(
        capture_manifest, capture_data, "source_capture_manifest_file", "metadata/source_capture.json"
    )
    room_plan = _capture_asset(
        capture_manifest, capture_data, "room_plan_file", "room_plan/room.usdz"
    )
    room_plan_report = _capture_asset(
        capture_manifest, capture_data, "room_plan_report_file", "room_plan/room_plan_report.json"
    )
    metric_scale_report = metric_scale_report or _first_existing(
        package, ("metadata/metric_scale_report.json",)
    )
    known_scale_report = known_scale_report or _first_existing(
        package, ("metadata/apriltag_scale_report.json",)
    )
    collision_candidate = collision_candidate or _first_existing(
        package, ("collision_candidate.ply", "geometry/collision_candidate.ply")
    )
    collision_report = collision_report or _first_existing(
        package,
        (
            "capture_splat_collision_candidate_report.json",
            "geometry/capture_splat_collision_candidate_report.json",
        ),
    )
    measurement_points = measurement_points or _first_existing(package, ("metric_seed.ply",))
    measurement_units = {
        "arkit_world": "meters",
        "colmap_world": "colmap_units",
        "metric_colmap_world": "meters",
        "trainer_world": "normalized_scene_units",
    }[measurement_points_frame]

    copied_images = _copy_images(images, out_dir, copy_files)
    copied_sparse = _copy_sparse_dir(package, out_dir, sparse_dir_name, copy_files)
    copied_gaussian = _copy_asset(gaussian, out_dir, "splat.ply" if gaussian and gaussian.suffix.lower() == ".ply" else f"gaussian{gaussian.suffix.lower()}" if gaussian else "splat.ply", copy_files)
    copied_points = _copy_asset(points, out_dir, "points.ply", copy_files)
    copied_capture = _copy_asset(capture_manifest, out_dir, "capture.json", copy_files)
    copied_transforms = _copy_asset(transforms, out_dir, "transforms.json", copy_files)
    copied_poses = _copy_asset(poses, out_dir, Path(poses).name if poses else "poses.json", copy_files)
    copied_camera_poses = _copy_asset(camera_poses, out_dir, Path(camera_poses).name if camera_poses else "camera_poses.json", copy_files)
    copied_splat = _copy_asset(splat, out_dir, f"splat{splat.suffix.lower()}" if splat else "splat.splat", copy_files)
    copied_spz = _copy_asset(spz, out_dir, f"scene{spz.suffix.lower()}" if spz else "scene.spz", copy_files)
    copied_navigation_mesh = _copy_asset(navigation_mesh, out_dir, "navigation_mesh.ply", copy_files)
    copied_mesh_report = _copy_asset(mesh_report, out_dir, "navigation_mesh_report.json", copy_files)
    copied_room_semantics = _copy_asset(room_semantics, out_dir, "room_semantics.json", copy_files)
    copied_camera_trajectory = _copy_asset(camera_trajectory, out_dir, "camera_trajectory.jsonl", copy_files)
    copied_planes = _copy_asset(planes, out_dir, "planes.json", copy_files)
    copied_source_capture = _copy_asset(source_capture_manifest, out_dir, "source_capture.json", copy_files)
    copied_room_plan = _copy_asset(room_plan, out_dir, "room_plan.usdz", copy_files)
    copied_room_plan_report = _copy_asset(room_plan_report, out_dir, "room_plan_report.json", copy_files)
    copied_metric_scale_report = _copy_asset(
        metric_scale_report, out_dir, "metric_scale_report.json", copy_files
    )
    copied_known_scale_report = _copy_asset(
        known_scale_report, out_dir, "apriltag_scale_report.json", copy_files
    )
    copied_collision_candidate = _copy_asset(
        collision_candidate, out_dir, "collision_candidate.ply", copy_files
    )
    copied_collision_report = _copy_asset(
        collision_report, out_dir, "collision_candidate_report.json", copy_files
    )
    copied_render_source_qa = (
        _write_quality_json(
            out_dir / "quality/render_source_qa.json",
            _validated_render_source_qa(render_source_qa),
        )
        if render_source_qa is not None
        else None
    )
    copied_ply_stats = None
    if copied_gaussian is not None and copied_gaussian.suffix.lower() == ".ply":
        stats = inspect_ply(copied_gaussian)
        stats["path"] = copied_gaussian.relative_to(out_dir).as_posix()
        copied_ply_stats = _write_quality_json(out_dir / "quality/ply_stats.json", stats)
    copied_spatial_guidance = _copy_asset(
        spatial_guidance, out_dir, "spatial_guidance_report.json", copy_files
    )
    copied_measurement_points = _copy_asset(measurement_points, out_dir, "measurement_points.ply", copy_files)

    assets: dict[str, Any] = {}
    if copied_points:
        assets["points"] = _file_ref(copied_points, out_dir)
    if copied_gaussian:
        gaussian_ref = _file_ref(copied_gaussian, out_dir)
        gaussian_ref["variant"] = gaussian_variant
        if gaussian_variant == "alpha_pruned":
            gaussian_ref["source_name"] = gaussian.name if gaussian else None
        assets["gaussian_ply" if copied_gaussian.suffix.lower() == ".ply" else "gaussian"] = gaussian_ref
    if copied_capture:
        assets["capture_manifest"] = _file_ref(copied_capture, out_dir)
    if copied_source_capture:
        assets["source_capture_manifest"] = _file_ref(copied_source_capture, out_dir)
    if copied_transforms:
        assets["transforms"] = _file_ref(copied_transforms, out_dir)
    if copied_poses:
        assets["poses"] = _file_ref(copied_poses, out_dir)
    if copied_camera_poses:
        assets["camera_poses"] = _file_ref(copied_camera_poses, out_dir)
    if copied_sparse:
        assets["colmap_sparse"] = {
            name: _file_ref(out_dir / sparse_dir_name / name, out_dir)
            for name in ("cameras.txt", "images.txt", "points3D.txt")
            if (out_dir / sparse_dir_name / name).exists()
        }
    if copied_splat:
        assets["splat"] = _file_ref(copied_splat, out_dir)
    if copied_spz:
        assets["spz"] = _file_ref(copied_spz, out_dir)
    if copied_navigation_mesh:
        assets["navigation_mesh"] = _metric_asset_ref(
            copied_navigation_mesh, out_dir, "arkit_world", "metric_capture_evidence", "meters"
        )
    if copied_mesh_report:
        assets["mesh_report"] = _metric_asset_ref(
            copied_mesh_report, out_dir, "arkit_world", "capture_evidence_report", "meters"
        )
    if copied_room_semantics:
        assets["room_semantics"] = _metric_asset_ref(
            copied_room_semantics, out_dir, "roomplan_world_unregistered", "semantic_proposal", "meters"
        )
    if copied_room_plan:
        assets["room_plan"] = _metric_asset_ref(
            copied_room_plan, out_dir, "roomplan_world_unregistered", "semantic_geometry_proposal", "meters"
        )
    if copied_room_plan_report:
        assets["room_plan_report"] = _metric_asset_ref(
            copied_room_plan_report, out_dir, "roomplan_world_unregistered", "capture_evidence_report", "meters"
        )
    if copied_camera_trajectory:
        assets["camera_trajectory"] = _metric_asset_ref(
            copied_camera_trajectory, out_dir, "arkit_world", "metric_capture_evidence", "meters"
        )
    if copied_planes:
        assets["planes"] = _metric_asset_ref(
            copied_planes, out_dir, "arkit_world", "capture_guidance_evidence", "meters"
        )
    if copied_metric_scale_report:
        assets["metric_scale_report"] = _metric_asset_ref(
            copied_metric_scale_report, out_dir, "metric_colmap_world", "metric_scale_evidence", "meters"
        )
    if copied_known_scale_report:
        assets["known_scale_report"] = _metric_asset_ref(
            copied_known_scale_report, out_dir, "metric_colmap_world", "known_scale_validation", "meters"
        )
    if copied_collision_candidate:
        assets["collision_candidate"] = _metric_asset_ref(
            copied_collision_candidate, out_dir, "arkit_world", "collision_candidate_evidence", "meters"
        )
    if copied_collision_report:
        assets["collision_candidate_report"] = _metric_asset_ref(
            copied_collision_report, out_dir, "arkit_world", "collision_candidate_report", "meters"
        )
    if copied_render_source_qa:
        assets["render_source_qa"] = _file_ref(copied_render_source_qa, out_dir)
    if copied_ply_stats:
        assets["ply_stats"] = _file_ref(copied_ply_stats, out_dir)
    if copied_spatial_guidance:
        assets["spatial_guidance_report"] = _metric_asset_ref(
            copied_spatial_guidance, out_dir, "arkit_world", "capture_guidance_evidence", "meters"
        )
    if copied_measurement_points:
        assets["measurement_points"] = _metric_asset_ref(
            copied_measurement_points,
            out_dir,
            measurement_points_frame,
            "metric_seed_proposal",
            measurement_units,
        )

    profile = capture_profile
    captured_profile = capture_data.get("capture_profile") if isinstance(capture_data, dict) else None
    if profile is None and isinstance(captured_profile, str) and captured_profile in CAPTURE_PROFILES:
        profile = captured_profile
    dataset_profile = profile or (
        captured_profile
        if isinstance(captured_profile, str)
        and 0 < len(captured_profile) <= 64
        and all(character.isalnum() or character in {"_", "-"} for character in captured_profile)
        else "unspecified"
    )
    source_frames = _frames(copied_images, out_dir)
    manifest = {
        "schema": SCHEMA,
        "status": "visual_evidence_with_3dgs_proposal",
        "source_package": package.name,
        "source_frames": source_frames,
        "frames": source_frames,
        "assets": assets,
        "training_dataset": _training_dataset(
            package,
            source_frames,
            copied_sparse,
            capture_manifest,
            capture_data,
            dataset_profile,
            copied_capture,
            copied_navigation_mesh,
            copied_mesh_report,
        ),
        "authority": {
            "source_frames": "visual_evidence",
            "trained_splats": "review_proposal",
            "metric_authority": False,
            "collision_authority": False,
            "semantic_authority": False,
            "navigation_authority": False,
            "quality_claim": False,
        },
        "notes": [
            "Source frames are visual evidence.",
            "Trained splats are review proposals, not metric, collision, semantic, or navigation authority.",
            "Attached render/source QA and PLY statistics are validation evidence, not a high-quality claim.",
            "training_dataset inventories capture evidence and does not claim that any trainer consumed it.",
        ],
    }
    dataparser_transform = _dataparser_transform(gaussian)
    if dataparser_transform is not None:
        manifest["dataparser_transform"] = dataparser_transform
        manifest["notes"].append(
            "dataparser_transform maps raw COLMAP world poses into the trained splat world (row-major 4x4, from the trainer's train.json)."
        )
    scene_transform = _scene_transform_sidecar(gaussian)
    if scene_transform is not None:
        manifest["scene_transform"] = scene_transform
        if dataparser_transform is None and isinstance(scene_transform.get("trainer_transform"), list):
            manifest["dataparser_transform"] = scene_transform["trainer_transform"]
    registration = _metric_registration(
        capture_manifest,
        package,
        sparse_dir_name,
        manifest.get("dataparser_transform"),
    )
    manifest["metric_registration"] = registration
    manifest["measurement_eligibility"] = _measurement_eligibility(
        copied_measurement_points,
        measurement_points_frame,
        measurement_units,
        package,
        sparse_dir_name,
        copied_known_scale_report,
    )
    manifest["collision_eligibility"] = _collision_eligibility(
        copied_collision_candidate,
        copied_collision_report,
    )
    mesh_walk_evidence = _mesh_walk_evidence(copied_mesh_report)
    manifest["mesh_walk_evidence"] = mesh_walk_evidence
    if (
        copied_navigation_mesh
        and registration["status"] == "accepted"
        and mesh_walk_evidence["status"] == "accepted"
    ):
        manifest["walk_eligibility"] = {
            "status": "eligible",
            "reason": mesh_walk_evidence["reason"],
            "authority": "capture_metric_evidence_not_collision_validation",
        }
    elif copied_navigation_mesh and registration["status"] == "accepted":
        manifest["walk_eligibility"] = {
            "status": "held",
            "reason": mesh_walk_evidence["reason"],
            "authority": "fly_only",
        }
    elif copied_navigation_mesh:
        manifest["walk_eligibility"] = {
            "status": "held",
            "reason": "metric_registration_not_accepted",
            "authority": "fly_only",
        }
    else:
        manifest["walk_eligibility"] = {
            "status": "missing",
            "reason": "metric_geometry_missing",
            "authority": "fly_only",
        }
    extent = _scene_extent(gaussian)
    if extent is not None:
        manifest.update(extent)
    if profile in CAPTURE_PROFILES:
        manifest["capture_profile"] = profile
    session_config = capture_data.get("session_config") if isinstance(capture_data, dict) else None
    up_axis = session_config.get("up_axis") if isinstance(session_config, dict) else None
    if (
        isinstance(up_axis, list)
        and len(up_axis) == 3
        and all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in up_axis)
    ):
        manifest["world_up"] = [float(value) for value in up_axis]
        manifest["world_up_coordinate_frame"] = "arkit_world"
    first_camera = _first_frame_camera(out_dir / sparse_dir_name if copied_sparse else None)
    if first_camera is not None:
        manifest["initial_camera"] = {
            **first_camera,
            "coordinate_frame": "colmap_world",
            "mode": "orbit" if profile == "object" else "inside",
        }
    write_json_strict(out_dir / MANIFEST_NAME, manifest)
    return manifest
