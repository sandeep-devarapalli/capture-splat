from __future__ import annotations

import json
import math
import shutil
import sqlite3
import struct
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .json_utils import load_json_strict, write_json_strict

CAMERA_REPORT_SCHEMA = "capture_splat.camera_evidence.v0.1"
PHOTOMETRIC_REPORT_SCHEMA = "capture_splat.photometric_evidence.v0.1"
EVAL_SET_SCHEMA = "capture_splat.fixed_camera_evaluation_set.v0.1"
PHOTOMETRIC_KEYS = (
    "exposure_duration",
    "exposure_offset",
    "iso",
    "white_balance_gains",
    "lens_position",
    "exposure_target_bias",
    "is_adjusting_exposure",
    "is_adjusting_white_balance",
    "is_adjusting_focus",
    "exposure_mode",
    "white_balance_mode",
    "focus_mode",
    "ambient_intensity",
    "ambient_color_temperature_k",
    "pixel_format",
    "color_primaries",
    "transfer_function",
    "ycbcr_matrix",
    "projection",
)
DISTORTION_KEYS = ("k1", "k2", "k3", "k4", "p1", "p2")


def discover_capture_manifest(images_dir: Path) -> Path | None:
    for candidate in (images_dir.parent / "capture.json", images_dir.parent.parent / "capture.json"):
        if candidate.exists():
            return candidate.resolve()
    return None


def load_frame_evidence(manifest: Path | None) -> dict[str, dict[str, Any]]:
    if manifest is None:
        return {}
    capture = load_json_strict(manifest)
    evidence: dict[str, dict[str, Any]] = {}
    for frame in capture.get("frames", []):
        if not isinstance(frame, dict) or not isinstance(frame.get("rgb"), str):
            continue
        name = Path(frame["rgb"]).name
        evidence[name] = frame
        evidence.setdefault(Path(name).stem, frame)
    return evidence


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def scaled_intrinsics(frame: dict[str, Any], image_path: Path) -> dict[str, Any] | None:
    intrinsics = frame.get("intrinsics")
    if not isinstance(intrinsics, dict):
        return None
    values = {key: _finite_number(intrinsics.get(key)) for key in ("fl_x", "fl_y", "cx", "cy", "w", "h")}
    if any(value is None for value in values.values()):
        return None
    with Image.open(image_path) as image:
        width, height = image.size
    source_width = float(values["w"])
    source_height = float(values["h"])
    if source_width <= 0 or source_height <= 0 or values["fl_x"] <= 0 or values["fl_y"] <= 0:
        return None
    result: dict[str, Any] = {
        "fl_x": float(values["fl_x"]) * width / source_width,
        "fl_y": float(values["fl_y"]) * height / source_height,
        "cx": float(values["cx"]) * width / source_width,
        "cy": float(values["cy"]) * height / source_height,
        "w": float(width),
        "h": float(height),
    }
    camera_model = intrinsics.get("camera_model", frame.get("camera_model"))
    if isinstance(camera_model, str) and camera_model:
        result["camera_model"] = camera_model.upper()
    for key in DISTORTION_KEYS:
        value = _finite_number(intrinsics.get(key, frame.get(key)))
        if value is not None:
            result[key] = value
    return result


def external_camera_options(images_dir: Path, evidence: dict[str, dict[str, Any]]) -> tuple[str, str] | None:
    image_path = next(
        (path for path in sorted(images_dir.iterdir()) if path.suffix.lower() in {".jpg", ".jpeg", ".png"}),
        None,
    )
    if image_path is None:
        return None
    frame = evidence.get(image_path.name, evidence.get(image_path.stem))
    intrinsics = scaled_intrinsics(frame, image_path) if frame is not None else None
    if intrinsics is None:
        return None
    model = intrinsics.get("camera_model")
    parameter_keys = {
        "PINHOLE": ("fl_x", "fl_y", "cx", "cy"),
        "SIMPLE_RADIAL": ("fl_x", "cx", "cy", "k1"),
        "RADIAL": ("fl_x", "cx", "cy", "k1", "k2"),
        "OPENCV": ("fl_x", "fl_y", "cx", "cy", "k1", "k2", "p1", "p2"),
        "OPENCV_FISHEYE": ("fl_x", "fl_y", "cx", "cy", "k1", "k2", "k3", "k4"),
    }.get(str(model))
    if parameter_keys is None or any(key not in intrinsics for key in parameter_keys):
        return None
    params = ",".join(f"{float(intrinsics[key]):.12g}" for key in parameter_keys)
    return str(model), params


def colmap_camera_spec(intrinsics: dict[str, Any]) -> tuple[int, list[float]] | None:
    model = str(intrinsics.get("camera_model", "PINHOLE"))
    specifications = {
        "PINHOLE": (1, ("fl_x", "fl_y", "cx", "cy")),
        "SIMPLE_RADIAL": (2, ("fl_x", "cx", "cy", "k1")),
        "RADIAL": (3, ("fl_x", "cx", "cy", "k1", "k2")),
        "OPENCV": (4, ("fl_x", "fl_y", "cx", "cy", "k1", "k2", "p1", "p2")),
        "OPENCV_FISHEYE": (5, ("fl_x", "fl_y", "cx", "cy", "k1", "k2", "k3", "k4")),
    }
    specification = specifications.get(model)
    if specification is None:
        return None
    model_id, keys = specification
    if any(key not in intrinsics for key in keys):
        return None
    return model_id, [float(intrinsics[key]) for key in keys]


def camera_evidence_report(images_dir: Path, evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    invalid: list[str] = []
    for image_path in sorted(path for path in images_dir.iterdir() if path.suffix.lower() in {".jpg", ".jpeg", ".png"}):
        frame = evidence.get(image_path.name, evidence.get(image_path.stem))
        if frame is None:
            missing.append(image_path.name)
            continue
        intrinsics = scaled_intrinsics(frame, image_path)
        if intrinsics is None:
            invalid.append(image_path.name)
            continue
        rows.append({"image": image_path.name, **intrinsics})
    metrics: dict[str, dict[str, float] | None] = {}
    for key in ("fl_x", "fl_y", "cx", "cy"):
        values = [float(row[key]) for row in rows]
        metrics[key] = None if not values else {
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
            "span": max(values) - min(values),
        }
    complete = bool(rows) and not missing and not invalid
    camera_models = sorted({str(row["camera_model"]) for row in rows if row.get("camera_model")})
    distortion_keys = sorted({key for row in rows for key in DISTORTION_KEYS if key in row})
    return {
        "schema": CAMERA_REPORT_SCHEMA,
        "images": len(rows) + len(missing) + len(invalid),
        "intrinsics_present": len(rows),
        "missing_images": missing,
        "invalid_images": invalid,
        "metrics": metrics,
        "projection_model": "PINHOLE" if rows and not camera_models else (camera_models[0] if len(camera_models) == 1 else None),
        "camera_models": camera_models,
        "distortion_coefficients": distortion_keys,
        "distortion_coefficients_available": bool(distortion_keys) if rows else None,
        "complete": complete,
        "decision": "promote" if complete else ("hold" if rows else "reject"),
        "authority": {"camera_prior_evidence": True, "refined_camera_authority": False, "quality_claim": False},
    }


def photometric_evidence_report(frames: list[dict[str, Any]]) -> dict[str, Any]:
    present = {key: 0 for key in PHOTOMETRIC_KEYS}
    non_finite: list[dict[str, Any]] = []
    exposures: list[float] = []
    for index, frame in enumerate(frames, start=1):
        photometric = frame.get("photometric") if isinstance(frame.get("photometric"), dict) else frame
        for key in PHOTOMETRIC_KEYS:
            if key not in photometric or photometric[key] is None:
                continue
            value = photometric[key]
            numbers = list(value.values()) if isinstance(value, dict) else [value]
            if any(isinstance(item, (int, float)) and not math.isfinite(float(item)) for item in numbers):
                non_finite.append({"frame": index, "field": key})
                continue
            present[key] += 1
        duration = _finite_number(photometric.get("exposure_duration"))
        iso = _finite_number(photometric.get("iso"))
        if duration is not None and duration > 0 and iso is not None and iso > 0:
            exposures.append(math.log2(duration * iso / 100.0))
    exposure_summary = None if not exposures else {
        "min_ev_proxy": min(exposures),
        "max_ev_proxy": max(exposures),
        "span_ev_proxy": max(exposures) - min(exposures),
    }
    return {
        "schema": PHOTOMETRIC_REPORT_SCHEMA,
        "frames": len(frames),
        "field_counts": present,
        "exposure": exposure_summary,
        "non_finite": non_finite,
        "decision": "reject" if non_finite else ("promote" if exposures else "hold"),
        "authority": {"photometric_evidence": True, "radiometric_calibration": False, "quality_claim": False},
    }


def copy_valid_masks(source: Path | None, destination: Path) -> dict[str, Any]:
    result = {"source": str(source) if source else None, "destination": str(destination), "copied": 0}
    if source is None or not source.is_dir():
        result["status"] = "missing"
        return result
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.png")):
        shutil.copy2(path, destination / path.name)
        result["copied"] += 1
    result["status"] = "ready" if result["copied"] else "empty"
    return result


def apply_camera_priors(database: Path, images_dir: Path, evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    report = camera_evidence_report(images_dir, evidence)
    if not report["complete"]:
        raise ValueError("per-frame camera policy requires complete finite intrinsics")
    connection = sqlite3.connect(database)
    updated = 0
    try:
        images = connection.execute("SELECT image_id, name, camera_id FROM images ORDER BY image_id").fetchall()
        for image_id, name, camera_id in images:
            frame = evidence.get(Path(name).name, evidence.get(Path(name).stem))
            intrinsics = scaled_intrinsics(frame, images_dir / name) if frame is not None else None
            if intrinsics is None:
                raise ValueError(f"camera evidence missing for database image: {name}")
            camera_spec = colmap_camera_spec(intrinsics)
            if camera_spec is None:
                raise ValueError(f"unsupported or incomplete camera model for database image: {name}")
            model_id, parameter_values = camera_spec
            params = struct.pack("<" + "d" * len(parameter_values), *parameter_values)
            connection.execute(
                "UPDATE cameras SET model=?, width=?, height=?, params=?, prior_focal_length=1 WHERE camera_id=?",
                (model_id, int(intrinsics["w"]), int(intrinsics["h"]), params, camera_id),
            )
            updated += 1
        connection.commit()
    finally:
        connection.close()
    report["database_cameras_updated"] = updated
    return report


def filter_hloc_features_by_masks(features_path: Path, mask_dir: Path) -> dict[str, Any]:
    try:
        import h5py  # type: ignore
    except ImportError as error:
        raise RuntimeError("h5py is required to apply masks to HLOC features") from error
    filtered = 0
    removed = 0
    missing: list[str] = []
    dimension_mismatches: list[dict[str, Any]] = []
    with h5py.File(features_path, "r+") as handle:
        groups: list[Any] = []
        handle.visititems(lambda _name, item: groups.append(item) if isinstance(item, h5py.Group) and "keypoints" in item else None)
        for group in groups:
            image_name = group.name.lstrip("/")
            mask_path = mask_dir / f"{image_name}.png"
            if not mask_path.exists():
                missing.append(image_name)
                continue
            keypoints = np.asarray(group["keypoints"])
            with Image.open(mask_path) as image:
                mask = np.asarray(image.convert("L")) >= 128
            if "image_size" in group:
                image_size = np.asarray(group["image_size"]).reshape(-1)
                if len(image_size) >= 2 and (int(image_size[0]), int(image_size[1])) != (mask.shape[1], mask.shape[0]):
                    dimension_mismatches.append({
                        "image": image_name,
                        "feature_size": [int(image_size[0]), int(image_size[1])],
                        "mask_size": [int(mask.shape[1]), int(mask.shape[0])],
                    })
                    continue
            xy = np.rint(keypoints).astype(int)
            xy[:, 0] = np.clip(xy[:, 0], 0, mask.shape[1] - 1)
            xy[:, 1] = np.clip(xy[:, 1], 0, mask.shape[0] - 1)
            keep = mask[xy[:, 1], xy[:, 0]]
            count = len(keypoints)
            for name in list(group.keys()):
                dataset = group[name]
                if not isinstance(dataset, h5py.Dataset) or not dataset.shape:
                    continue
                values = np.asarray(dataset)
                if name == "descriptors" and values.shape[-1] == count:
                    replacement = values[..., keep]
                elif values.shape[0] == count:
                    replacement = values[keep]
                elif values.shape[-1] == count:
                    replacement = values[..., keep]
                else:
                    continue
                attributes = dict(dataset.attrs)
                del group[name]
                recreated = group.create_dataset(name, data=replacement)
                for key, value in attributes.items():
                    recreated.attrs[key] = value
            filtered += 1
            removed += int(count - np.count_nonzero(keep))
    return {
        "filtered_images": filtered,
        "removed_keypoints": removed,
        "missing_masks": missing,
        "dimension_mismatches": dimension_mismatches,
    }


def write_fixed_evaluation_set(images_txt: Path, output: Path, fraction: float = 0.125) -> dict[str, Any]:
    registered: list[tuple[int, str]] = []
    expect_header = True
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        if not expect_header:
            expect_header = True
            continue
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            image_id = int(parts[0])
            [float(value) for value in parts[1:8]]
            int(parts[8])
        except ValueError:
            continue
        registered.append((image_id, parts[9]))
        expect_header = False
    registered.sort(key=lambda item: item[1])
    interval = max(1, round(1.0 / fraction))
    selected = registered[::interval]
    summary = {
        "schema": EVAL_SET_SCHEMA,
        "registered_images": len(registered),
        "fraction": fraction,
        "frames": [name for _image_id, name in selected],
        "selection": "temporal_even_registered_cameras",
        "authority": {"fixed_camera_evaluation": True, "quality_claim": False},
    }
    write_json_strict(output, summary)
    return summary
