from __future__ import annotations

import hashlib
import math
import shutil
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import numpy as np

from .json_utils import load_json_strict, write_json_strict


SCHEMA = "capture_splat.training_supervision.v0.1"
REPORT_RELATIVE = Path("metadata/training_supervision.json")
FRAME_ASSET_KEYS = {
    "rgb", "image", "image_path", "file_path", "depth", "confidence",
    "person_mask", "valid_mask", "object_mask",
}
WINDOWS_INVALID_NAME_CHARACTERS = frozenset('<>:"|?*')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"COM{index}" for index in "¹²³"),
    *(f"LPT{index}" for index in "¹²³"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_file_if_absent(source: Path, destination: Path) -> str:
    if destination.is_symlink():
        return "conflict"
    if not source.is_file():
        return "missing"
    if destination.exists():
        same_size = destination.is_file() and source.stat().st_size == destination.stat().st_size
        return "existing" if same_size and _sha256(source) == _sha256(destination) else "conflict"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    same_size = source.stat().st_size == destination.stat().st_size
    return "copied" if same_size and _sha256(source) == _sha256(destination) else "conflict"


def _canonical_relative(relative: str) -> str:
    declared = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    if (
        not relative
        or "\\" in relative
        or declared.is_absolute()
        or windows.drive
        or ".." in declared.parts
        or declared.as_posix() == "."
    ):
        raise ValueError(f"capture asset path escapes package: {relative}")
    for component in declared.parts:
        stem = component.rstrip(" .").split(".", 1)[0].rstrip(" .").upper()
        if (
            component.endswith((" ", "."))
            or stem in WINDOWS_RESERVED_NAMES
            or any(ord(character) < 32 or character in WINDOWS_INVALID_NAME_CHARACTERS for character in component)
        ):
            raise ValueError(f"capture asset path is not portable: {relative}")
    return declared.as_posix()


def _portable_path_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative.casefold())


def confined_capture_path(root: Path, relative: str) -> Path:
    path = root / _canonical_relative(relative)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"capture asset path escapes package: {relative}") from error
    return path


def capture_manifest_asset_references(capture: dict[str, Any]) -> list[str]:
    references: list[str] = []
    for key, value in capture.items():
        if key.endswith("_file") and value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError(f"capture asset {key} must be a non-empty relative path")
            references.append(_canonical_relative(value))
    frames = capture.get("frames", [])
    if not isinstance(frames, list):
        raise ValueError("capture frames must be a list")
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("capture frame must be an object")
        for key in FRAME_ASSET_KEYS:
            value = frame.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"capture frame asset {key} must be a non-empty relative path")
            references.append(_canonical_relative(value))
    return references


def capture_manifest_asset_conflicts(target_root: Path, references: list[str]) -> list[str]:
    target_root = target_root.resolve()
    seen_declared: dict[str, str] = {}
    seen_destinations: dict[str, str] = {}
    conflicts: set[str] = set()
    for relative in sorted(set(references)):
        keys = (
            (seen_declared, relative),
            (
                seen_destinations,
                confined_capture_path(target_root, relative)
                .resolve()
                .relative_to(target_root)
                .as_posix(),
            ),
        )
        for seen, key in keys:
            key = _portable_path_key(key)
            previous = seen.setdefault(key, relative)
            if previous != relative:
                conflicts.update((previous, relative))
    return sorted(conflicts)


def copy_capture_manifest_assets(
    source_root: Path,
    target_root: Path,
    capture: dict[str, Any],
    *,
    protected: set[str] | None = None,
) -> dict[str, Any]:
    source_root = source_root.resolve()
    target_root = target_root.resolve()
    protected = {
        _portable_path_key(
            confined_capture_path(target_root, path).resolve().relative_to(target_root).as_posix()
        )
        for path in protected or set()
    }
    references = capture_manifest_asset_references(capture)
    collisions = capture_manifest_asset_conflicts(target_root, references)
    if collisions:
        return {
            "reference_count": len(references),
            "unique_asset_count": len(set(references)),
            "copied": 0,
            "copied_paths": [],
            "existing": 0,
            "verified_asset_count": 0,
            "missing": [],
            "conflicts": collisions,
            "duplicate_reference_count": len(references) - len(set(references)),
            "complete": False,
            "decision": "hold",
        }
    resolved = {
        relative: (confined_capture_path(source_root, relative), confined_capture_path(target_root, relative))
        for relative in references
    }

    copied: list[str] = []
    existing = 0
    missing: list[str] = []
    conflicts: list[str] = []
    for relative, (source, destination) in sorted(resolved.items()):
        protected_relative = _portable_path_key(
            destination.resolve().relative_to(target_root).as_posix()
        )
        if any(protected_relative == path or protected_relative.startswith(f"{path}/") for path in protected):
            conflicts.append(relative)
            continue
        status = stage_file_if_absent(source, destination)
        if status == "copied":
            copied.append(relative)
        elif status == "existing":
            existing += 1
        elif status == "missing":
            missing.append(relative)
        else:
            conflicts.append(relative)
    complete = not missing and not conflicts
    return {
        "reference_count": len(references),
        "unique_asset_count": len(resolved),
        "copied": len(copied),
        "copied_paths": copied,
        "existing": existing,
        "verified_asset_count": len(copied) + existing,
        "missing": missing,
        "conflicts": conflicts,
        "duplicate_reference_count": len(references) - len(resolved),
        "complete": complete,
        "decision": "ready" if complete else "hold",
    }


def copy_capture_supervision_assets(
    source_root: Path,
    target_root: Path,
    capture: dict[str, Any],
) -> dict[str, Any]:
    source_root = source_root.resolve()
    target_root = target_root.resolve()
    copied: list[str] = []
    existing: list[str] = []
    missing: list[str] = []
    conflicts: list[str] = []
    seen: set[str] = set()
    statuses = {"copied": copied, "existing": existing, "missing": missing, "conflict": conflicts}
    for frame in capture.get("frames", []):
        if not isinstance(frame, dict):
            continue
        for key in ("depth", "confidence"):
            relative = frame.get(key)
            if not isinstance(relative, str) or relative in seen:
                continue
            relative = _canonical_relative(relative)
            if relative in seen:
                continue
            seen.add(relative)
            source = confined_capture_path(source_root, relative)
            destination = confined_capture_path(target_root, relative)
            status = stage_file_if_absent(source, destination)
            statuses[status].append(relative)
    return {
        "copied": len(copied),
        "paths": sorted(copied),
        "existing": sorted(existing),
        "missing": sorted(missing),
        "conflicts": sorted(conflicts),
        "complete": not missing and not conflicts,
    }


def _intrinsics(frame: dict[str, Any], width: int, height: int) -> tuple[float, float, float, float] | None:
    raw = frame.get("intrinsics")
    if not isinstance(raw, dict):
        return None
    try:
        source_width = float(raw.get("w", raw.get("width", width)))
        source_height = float(raw.get("h", raw.get("height", height)))
        fx = float(raw.get("fl_x", raw.get("fx"))) * width / source_width
        fy = float(raw.get("fl_y", raw.get("fy"))) * height / source_height
        cx = float(raw.get("cx")) * width / source_width
        cy = float(raw.get("cy")) * height / source_height
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    values = (fx, fy, cx, cy)
    if not all(math.isfinite(value) for value in values) or fx <= 0 or fy <= 0:
        return None
    return values


def _normal_map(
    depth_meters: np.ndarray,
    valid: np.ndarray,
    intrinsics: tuple[float, float, float, float],
) -> np.ndarray:
    height, width = depth_meters.shape
    fx, fy, cx, cy = intrinsics
    ys, xs = np.mgrid[0:height, 0:width]
    points = np.stack(
        (
            (xs - cx) * depth_meters / fx,
            (ys - cy) * depth_meters / fy,
            depth_meters,
        ),
        axis=-1,
    )
    dx = np.zeros_like(points)
    dy = np.zeros_like(points)
    dx[:, 1:-1] = points[:, 2:] - points[:, :-2]
    dy[1:-1, :] = points[2:, :] - points[:-2, :]
    normals = -np.cross(dx, dy)
    lengths = np.linalg.norm(normals, axis=-1)
    neighborhood = np.zeros_like(valid)
    neighborhood[1:-1, 1:-1] = (
        valid[1:-1, :-2]
        & valid[1:-1, 2:]
        & valid[:-2, 1:-1]
        & valid[2:, 1:-1]
    )
    usable = neighborhood & np.isfinite(lengths) & (lengths > 1e-12)
    output = np.zeros_like(normals, dtype=np.float32)
    output[usable] = (normals[usable] / lengths[usable, None]).astype(np.float32)
    return output


def prepare_training_supervision(
    package_dir: Path,
    *,
    confidence_minimum: int = 1,
    derive_normals: bool = True,
) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    if confidence_minimum not in (0, 1, 2):
        raise ValueError("confidence minimum must be 0, 1, or 2")
    capture_path = package_dir / "capture.json"
    if not capture_path.is_file():
        raise FileNotFoundError(f"capture manifest missing: {capture_path}")
    capture = load_json_strict(capture_path)
    depth_scale = float(capture.get("depth_scale", 1.0))
    if not math.isfinite(depth_scale) or depth_scale <= 0:
        raise ValueError("capture depth_scale must be positive and finite")

    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    image_count = 0
    validated_depth_count = 0
    confidence_filtered_depth_count = 0
    normal_count = 0
    for frame_index, frame in enumerate(capture.get("frames", []), start=1):
        if not isinstance(frame, dict) or not isinstance(frame.get("rgb"), str):
            continue
        image_count += 1
        record: dict[str, Any] = {
            "frame": frame_index,
            "image": frame["rgb"],
            "status": "depth_missing",
        }
        depth_relative = frame.get("depth")
        if not isinstance(depth_relative, str):
            records.append(record)
            continue
        depth_path = confined_capture_path(package_dir, depth_relative)
        record["depth"] = depth_relative
        if not depth_path.is_file():
            record["status"] = "depth_file_missing"
            warnings.append(f"frame_{frame_index:06d}_depth_file_missing")
            records.append(record)
            continue
        record["depth_sha256"] = _sha256(depth_path)
        if depth_path.suffix.lower() != ".npy":
            record["status"] = "depth_format_preserved_not_validated"
            warnings.append(f"frame_{frame_index:06d}_depth_format_not_npy")
            records.append(record)
            continue
        depth = np.load(depth_path, allow_pickle=False)
        if depth.ndim != 2:
            record["status"] = "depth_dimension_mismatch"
            warnings.append(f"frame_{frame_index:06d}_depth_not_2d")
            records.append(record)
            continue
        frame_scale = float(frame.get("depth_scale", depth_scale))
        if not math.isfinite(frame_scale) or frame_scale <= 0:
            raise ValueError(f"frame {frame_index} depth_scale must be positive and finite")
        depth_meters = depth.astype(np.float64) * frame_scale
        valid = np.isfinite(depth_meters) & (depth_meters > 0)
        confidence_relative = frame.get("confidence")
        confidence_applied = False
        if isinstance(confidence_relative, str):
            confidence_path = confined_capture_path(package_dir, confidence_relative)
            record["confidence"] = confidence_relative
            if confidence_path.is_file() and confidence_path.suffix.lower() == ".npy":
                confidence = np.load(confidence_path, allow_pickle=False)
                if confidence.shape == depth.shape:
                    valid &= confidence >= confidence_minimum
                    record["confidence_sha256"] = _sha256(confidence_path)
                    confidence_applied = True
                    confidence_filtered_depth_count += 1
                else:
                    record["confidence_status"] = "dimension_mismatch"
                    warnings.append(f"frame_{frame_index:06d}_confidence_dimension_mismatch")
            else:
                record["confidence_status"] = "missing_or_unsupported"
                warnings.append(f"frame_{frame_index:06d}_confidence_unavailable")
        else:
            record["confidence_status"] = "not_recorded"
            warnings.append(f"frame_{frame_index:06d}_confidence_not_recorded")
        finite_values = depth_meters[np.isfinite(depth_meters)]
        record.update({
            "status": "validated",
            "shape": [int(depth.shape[0]), int(depth.shape[1])],
            "depth_scale_to_meters": frame_scale,
            "finite_fraction": float(np.mean(np.isfinite(depth_meters))),
            "valid_fraction": float(np.mean(valid)),
            "confidence_applied": confidence_applied,
            "minimum_meters": float(np.min(finite_values)) if finite_values.size else None,
            "maximum_meters": float(np.max(finite_values)) if finite_values.size else None,
        })
        validated_depth_count += 1
        if derive_normals:
            camera = _intrinsics(frame, depth.shape[1], depth.shape[0])
            if camera is None:
                record["normal_status"] = "intrinsics_missing"
                warnings.append(f"frame_{frame_index:06d}_normal_intrinsics_missing")
            else:
                normal_relative = Path("normals") / f"{Path(str(frame['rgb'])).stem}.npy"
                normal_path = package_dir / normal_relative
                normal_path.parent.mkdir(parents=True, exist_ok=True)
                normals = _normal_map(depth_meters, valid, camera)
                np.save(normal_path, normals, allow_pickle=False)
                record["normal"] = normal_relative.as_posix()
                record["normal_sha256"] = _sha256(normal_path)
                record["normal_valid_fraction"] = float(np.mean(np.linalg.norm(normals, axis=-1) > 0))
                record["normal_status"] = "derived_proposal"
                record["normal_coordinate_frame"] = "opencv_camera_toward_camera"
                normal_count += 1
        records.append(record)

    decision = "ready"
    if validated_depth_count == 0:
        decision = "hold"
        warnings.append("no_validated_metric_depth")
    elif validated_depth_count < image_count:
        decision = "hold"
        warnings.append("partial_metric_depth_coverage")
    report = {
        "schema": SCHEMA,
        "package_dir": str(package_dir),
        "capture_manifest": "capture.json",
        "capture_manifest_sha256": _sha256(capture_path),
        "confidence_minimum": confidence_minimum,
        "image_count": image_count,
        "validated_depth_count": validated_depth_count,
        "confidence_filtered_depth_count": confidence_filtered_depth_count,
        "derived_normal_count": normal_count,
        "complete_depth_coverage": image_count > 0 and validated_depth_count == image_count,
        "records": records,
        "warnings": sorted(set(warnings)),
        "decision": decision,
        "authority": {
            "metric_depth_prior": True,
            "derived_normals_are_proposals": True,
            "quality_claim": False,
            "measurement_authority": False,
        },
    }
    write_json_strict(package_dir / REPORT_RELATIVE, report)
    return report


def supervision_evidence(package_dir: Path) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    report_path = package_dir / REPORT_RELATIVE
    if not report_path.is_file():
        return {
            "available": False,
            "report": None,
            "validated_depth_count": 0,
            "derived_normal_count": 0,
            "complete_depth_coverage": False,
            "valid": False,
            "reason": "training_supervision_report_missing",
        }
    report = load_json_strict(report_path)
    if report.get("schema") != SCHEMA:
        raise ValueError("training supervision report has an unsupported schema")
    capture_path = confined_capture_path(
        package_dir,
        str(report.get("capture_manifest", "capture.json")),
    )
    if not capture_path.is_file() or _sha256(capture_path) != report.get("capture_manifest_sha256"):
        raise ValueError("training supervision report capture manifest checksum mismatch")
    for record in report.get("records", []):
        if not isinstance(record, dict):
            raise ValueError("training supervision record must be an object")
        for path_key, checksum_key in (
            ("depth", "depth_sha256"),
            ("confidence", "confidence_sha256"),
            ("normal", "normal_sha256"),
        ):
            relative = record.get(path_key)
            checksum = record.get(checksum_key)
            if checksum is None:
                continue
            if not isinstance(relative, str):
                raise ValueError(f"training supervision {path_key} path missing")
            path = confined_capture_path(package_dir, relative)
            if not path.is_file() or _sha256(path) != checksum:
                raise ValueError(f"training supervision checksum mismatch: {relative}")
    return {
        "available": True,
        "report": str(report_path),
        "validated_depth_count": int(report.get("validated_depth_count", 0)),
        "confidence_filtered_depth_count": int(report.get("confidence_filtered_depth_count", 0)),
        "derived_normal_count": int(report.get("derived_normal_count", 0)),
        "complete_depth_coverage": bool(report.get("complete_depth_coverage")),
        "valid": int(report.get("validated_depth_count", 0)) > 0,
        "decision": report.get("decision"),
        "reason": None,
    }


def resolve_supervision_policy(
    package_dir: Path,
    requested: str,
    kind: str,
    option: str | None,
) -> dict[str, Any]:
    if requested not in {"off", "auto", "required"}:
        raise ValueError(f"unsupported {kind} supervision policy: {requested}")
    if kind not in {"depth", "normal"}:
        raise ValueError(f"unsupported supervision kind: {kind}")
    evidence = supervision_evidence(package_dir)
    count_key = "validated_depth_count" if kind == "depth" else "derived_normal_count"
    available = evidence[count_key] > 0
    supported = option is not None
    if requested == "required" and not available:
        raise RuntimeError(f"required sensor {kind} supervision evidence is unavailable")
    if requested == "required" and not supported:
        raise RuntimeError(f"trainer does not expose dedicated sensor {kind} supervision")
    applied = requested != "off" and available and supported
    warning = None
    if requested == "auto" and available and not supported:
        warning = f"sensor_{kind}_evidence_preserved_but_trainer_unsupported"
    return {
        "requested": requested,
        "available": available,
        "supported": supported,
        "applied": applied,
        "manifest": evidence["report"] if applied else None,
        "evidence": evidence,
        "warning": warning,
        "semantics": (
            "metric_sensor_depth_with_confidence_coverage_report"
            if kind == "depth"
            else "depth_derived_normal_proposal"
        ),
    }
