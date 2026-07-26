from __future__ import annotations

import hashlib
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .equirectangular_import import RIG_SCHEMA
from .json_utils import load_json_strict, write_json_strict
from .sfm_runner import (
    colmap_capabilities,
    colmap_has_cuda,
    decide,
    find_binary,
    model_to_text,
    read_model_stats,
)

SUMMARY_SCHEMA = "capture_splat.sfm_360_rig_summary.v0.1"
COLMAP_FROM_PROJECTION = np.diag([1.0, -1.0, 1.0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _package_file(package_dir: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"360 rig path must be relative: {value}")
    resolved = (package_dir / relative).resolve()
    try:
        resolved.relative_to(package_dir)
    except ValueError as error:
        raise ValueError(f"360 rig path escapes package: {value}") from error
    return resolved


def _verify_file(path: Path, evidence: dict[str, Any]) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"360 projection missing: {path}")
    if int(evidence.get("size_bytes", -1)) != path.stat().st_size:
        raise ValueError(f"360 projection size mismatch: {path}")
    if evidence.get("checksum") != _sha256(path):
        raise ValueError(f"360 projection checksum mismatch: {path}")


def _quaternion_wxyz(rotation: np.ndarray) -> list[float]:
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("virtual-camera rotation must be a finite 3x3 matrix")
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6) or not math.isclose(
        float(np.linalg.det(matrix)), 1.0, abs_tol=1e-6
    ):
        raise ValueError("virtual-camera rotation must be right-handed and orthonormal")
    trace = float(np.trace(matrix))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        quaternion = np.asarray([
            0.25 * scale,
            (matrix[2, 1] - matrix[1, 2]) / scale,
            (matrix[0, 2] - matrix[2, 0]) / scale,
            (matrix[1, 0] - matrix[0, 1]) / scale,
        ])
    else:
        axis = int(np.argmax(np.diag(matrix)))
        next_axis, last_axis = (axis + 1) % 3, (axis + 2) % 3
        scale = math.sqrt(1.0 + matrix[axis, axis] - matrix[next_axis, next_axis] - matrix[last_axis, last_axis]) * 2
        quaternion = np.zeros(4, dtype=np.float64)
        quaternion[axis + 1] = 0.25 * scale
        quaternion[0] = (matrix[last_axis, next_axis] - matrix[next_axis, last_axis]) / scale
        quaternion[next_axis + 1] = (matrix[next_axis, axis] + matrix[axis, next_axis]) / scale
        quaternion[last_axis + 1] = (matrix[last_axis, axis] + matrix[axis, last_axis]) / scale
    quaternion /= np.linalg.norm(quaternion)
    if quaternion[0] < 0:
        quaternion *= -1
    return quaternion.tolist()


def _load_rig(package_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rig_path = package_dir / "metadata" / "equirectangular_rig.json"
    if not rig_path.is_file():
        raise FileNotFoundError(f"360 rig metadata missing: {rig_path}")
    rig = load_json_strict(rig_path)
    if rig.get("schema") != RIG_SCHEMA:
        raise ValueError(f"unsupported 360 rig schema: {rig.get('schema')}")
    views = rig.get("virtual_views")
    if not isinstance(views, list) or not views:
        raise ValueError("360 rig has no virtual views")
    intrinsics = rig.get("intrinsics")
    if not isinstance(intrinsics, dict):
        raise ValueError("360 rig intrinsics missing")
    values = {key: float(intrinsics[key]) for key in ("fl_x", "fl_y", "cx", "cy", "w", "h")}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("360 rig intrinsics must be finite")
    width, height = int(values["w"]), int(values["h"])
    if width <= 0 or height <= 0 or values["fl_x"] <= 0 or values["fl_y"] <= 0:
        raise ValueError("360 rig dimensions and focal lengths must be positive")
    if not 0 <= values["cx"] <= width or not 0 <= values["cy"] <= height:
        raise ValueError("360 rig principal point is outside the image")
    for source in rig.get("source_panoramas", []):
        _verify_file(_package_file(package_dir, source["path"]), source)
    panorama_views: dict[int, set[int]] = {}
    rotations_by_view: dict[int, np.ndarray] = {}
    for record in views:
        panorama_id = int(record["panorama_id"])
        view_id = int(record["view_id"])
        if view_id in panorama_views.setdefault(panorama_id, set()):
            raise ValueError(f"duplicate virtual view {view_id} for panorama {panorama_id}")
        panorama_views[panorama_id].add(view_id)
        rotation = np.asarray(record["rotation_equirect_world_from_camera"], dtype=np.float64)
        _quaternion_wxyz(rotation.T)
        previous = rotations_by_view.setdefault(view_id, rotation)
        if not np.allclose(previous, rotation, atol=1e-12):
            raise ValueError(f"virtual-camera rotation changed for view {view_id}")
        image_path = _package_file(package_dir, record["image"])
        mask_path = _package_file(package_dir, record["valid_mask"])
        _verify_file(image_path, record["image_evidence"])
        _verify_file(mask_path, record["valid_mask_evidence"])
        with Image.open(image_path) as image:
            if image.size != (width, height):
                raise ValueError(f"360 projection dimensions mismatch: {image_path}")
        with Image.open(mask_path) as mask:
            if mask.size != (width, height):
                raise ValueError(f"360 mask dimensions mismatch: {mask_path}")
            mask_values = set(np.unique(np.asarray(mask.convert("L"))).tolist())
            if not mask_values <= {0, 255} or 255 not in mask_values:
                raise ValueError(f"360 mask must be nonempty binary white-valid: {mask_path}")
    expected = next(iter(panorama_views.values()))
    if any(view_ids != expected for view_ids in panorama_views.values()):
        raise ValueError("every panorama must contain the same virtual-camera views")
    return rig, sorted(views, key=lambda item: (int(item["panorama_id"]), int(item["view_id"])))


def _materialize_rig(package_dir: Path, out_dir: Path, views: list[dict[str, Any]]) -> tuple[Path, Path]:
    images_dir = out_dir / "images"
    masks_dir = out_dir / "masks"
    for record in views:
        camera_dir = f"pano_camera{int(record['view_id']) - 1:02d}"
        filename = f"p{int(record['panorama_id']):06d}.png"
        image_target = images_dir / camera_dir / filename
        mask_target = masks_dir / camera_dir / f"{filename}.png"
        image_target.parent.mkdir(parents=True, exist_ok=True)
        mask_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_package_file(package_dir, record["image"]), image_target)
        shutil.copy2(_package_file(package_dir, record["valid_mask"]), mask_target)
    return images_dir, masks_dir


def _write_colmap_rig(rig: dict[str, Any], views: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    reference = np.asarray(views[0]["rotation_equirect_world_from_camera"], dtype=np.float64)
    cameras = []
    for record in sorted(
        (item for item in views if int(item["panorama_id"]) == int(views[0]["panorama_id"])),
        key=lambda item: int(item["view_id"]),
    ):
        view_id = int(record["view_id"])
        world_from_camera = np.asarray(record["rotation_equirect_world_from_camera"], dtype=np.float64)
        camera_from_reference = (
            COLMAP_FROM_PROJECTION
            @ world_from_camera.T
            @ reference
            @ COLMAP_FROM_PROJECTION
        )
        camera: dict[str, Any] = {"image_prefix": f"pano_camera{view_id - 1:02d}/"}
        if view_id == int(views[0]["view_id"]):
            camera["ref_sensor"] = True
        else:
            camera.update({
                "cam_from_rig_rotation": _quaternion_wxyz(camera_from_reference),
                "cam_from_rig_translation": [0.0, 0.0, 0.0],
            })
        cameras.append(camera)
    config = [{"cameras": cameras}]
    write_json_strict(out_path, config)
    return {
        "path": str(out_path),
        "camera_count": len(cameras),
        "reference_camera": cameras[0]["image_prefix"],
        "zero_translation": True,
        "rotation_conversion": "projection_y_up_to_colmap_y_down_conjugation",
        "intrinsics": rig["intrinsics"],
    }


def build_rig_sfm_commands(
    images_dir: Path,
    masks_dir: Path,
    out_dir: Path,
    rig_config: Path,
    *,
    method: str,
    overlap: int,
    max_features: int,
    use_gpu: bool,
) -> list[list[str]]:
    database = out_dir / "database.db"
    sparse = out_dir / "sparse"
    intrinsics = load_json_strict(out_dir / "metadata" / "source_equirectangular_rig.json")["intrinsics"]
    params = ",".join(str(float(intrinsics[key])) for key in ("fl_x", "fl_y", "cx", "cy"))
    mapper = [
        "colmap", "global_mapper" if method == "global" else "mapper",
        "--database_path", str(database),
        "--image_path", str(images_dir),
        "--output_path", str(sparse),
    ]
    if method == "global":
        mapper += [
            "--GlobalMapper.refine_sensor_from_rig", "0",
            "--GlobalMapper.ba_refine_focal_length", "0",
            "--GlobalMapper.ba_refine_principal_point", "0",
            "--GlobalMapper.ba_refine_extra_params", "0",
        ]
        if not use_gpu:
            mapper += [
                "--GlobalMapper.gp_use_gpu", "0",
                "--GlobalMapper.ba_ceres_use_gpu", "0",
            ]
    else:
        mapper += [
            "--Mapper.ba_refine_sensor_from_rig", "0",
            "--Mapper.ba_refine_focal_length", "0",
            "--Mapper.ba_refine_principal_point", "0",
            "--Mapper.ba_refine_extra_params", "0",
        ]
        if not use_gpu:
            mapper += ["--Mapper.ba_use_gpu", "0"]
    return [
        [
            "colmap", "feature_extractor",
            "--database_path", str(database),
            "--image_path", str(images_dir),
            "--ImageReader.mask_path", str(masks_dir),
            "--ImageReader.single_camera_per_folder", "1",
            "--ImageReader.camera_model", "PINHOLE",
            "--ImageReader.camera_params", params,
            "--FeatureExtraction.use_gpu", "1" if use_gpu else "0",
            "--SiftExtraction.max_num_features", str(int(max_features)),
        ],
        ["colmap", "rig_configurator", "--database_path", str(database), "--rig_config_path", str(rig_config)],
        [
            "colmap", "sequential_matcher",
            "--database_path", str(database),
            "--FeatureMatching.rig_verification", "1",
            "--FeatureMatching.skip_image_pairs_in_same_frame", "1",
            "--FeatureMatching.use_gpu", "1" if use_gpu else "0",
            "--SequentialMatching.overlap", str(int(overlap)),
            "--SequentialMatching.loop_detection", "0",
        ],
        mapper,
    ]


def _registered_images(images_txt: Path) -> dict[int, tuple[int, str]]:
    images: dict[int, tuple[int, str]] = {}
    for line in images_txt.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) < 10:
            continue
        try:
            int(parts[0])
            float(parts[1])
        except ValueError:
            continue
        image_path = Path(parts[9])
        if not image_path.parent.name.startswith("pano_camera"):
            continue
        images[int(parts[0])] = (int(parts[8]), image_path.as_posix())
    return images


def _parse_rigs(path: Path) -> list[dict[str, Any]]:
    rigs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or line.startswith("#"):
            continue
        rig_id, count = int(parts[0]), int(parts[1])
        ref_type, ref_id = parts[2], int(parts[3])
        sensors: dict[int, list[float] | None] = {ref_id: None}
        cursor = 4
        while cursor < len(parts):
            sensor_type, sensor_id, has_pose = parts[cursor], int(parts[cursor + 1]), int(parts[cursor + 2])
            cursor += 3
            if sensor_type != "CAMERA":
                raise ValueError(f"unsupported 360 rig sensor type: {sensor_type}")
            pose = [float(value) for value in parts[cursor:cursor + 7]] if has_pose else None
            cursor += 7 if has_pose else 0
            sensors[sensor_id] = pose
        rigs.append({
            "rig_id": rig_id,
            "sensor_count": count,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "sensors": sensors,
        })
    return rigs


def _parse_frames(path: Path) -> list[dict[str, Any]]:
    frames = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or line.startswith("#"):
            continue
        data_count = int(parts[9])
        data = [
            (parts[index], int(parts[index + 1]), int(parts[index + 2]))
            for index in range(10, 10 + data_count * 3, 3)
        ]
        frames.append({"frame_id": int(parts[0]), "rig_id": int(parts[1]), "data": data})
    return frames


def _validate_output_rig(model_dir: Path, config_path: Path) -> dict[str, Any]:
    required = [model_dir / name for name in ("images.txt", "rigs.txt", "frames.txt")]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        return {"valid": False, "missing": missing, "registered_panoramas": []}
    images = _registered_images(model_dir / "images.txt")
    config = load_json_strict(config_path)[0]["cameras"]
    prefix_to_camera_ids: dict[str, set[int]] = {}
    for camera_id, name in images.values():
        prefix_to_camera_ids.setdefault(f"{Path(name).parent.name}/", set()).add(camera_id)
    if any(len(ids) != 1 for ids in prefix_to_camera_ids.values()):
        return {"valid": False, "error": "virtual camera folder uses multiple camera IDs", "registered_panoramas": []}
    if any(camera["image_prefix"] not in prefix_to_camera_ids for camera in config):
        return {"valid": False, "error": "registered model is missing virtual cameras", "registered_panoramas": []}
    camera_ids = {prefix: next(iter(ids)) for prefix, ids in prefix_to_camera_ids.items()}
    ref_config = next((camera for camera in config if camera.get("ref_sensor")), None)
    if ref_config is None:
        return {"valid": False, "error": "rig config has no reference camera", "registered_panoramas": []}
    expected_sensor_ids = {camera_ids[camera["image_prefix"]] for camera in config}
    matching_rig = next(
        (
            rig for rig in _parse_rigs(model_dir / "rigs.txt")
            if rig["ref_type"] == "CAMERA"
            and rig["ref_id"] == camera_ids[ref_config["image_prefix"]]
            and set(rig["sensors"]) == expected_sensor_ids
        ),
        None,
    )
    if matching_rig is None or matching_rig["sensor_count"] != len(config):
        return {"valid": False, "error": "output rig sensor membership mismatch", "registered_panoramas": []}
    for camera in config:
        sensor_id = camera_ids[camera["image_prefix"]]
        pose = matching_rig["sensors"][sensor_id]
        if camera.get("ref_sensor"):
            if pose is not None:
                return {"valid": False, "error": "reference camera has a relative pose", "registered_panoramas": []}
            continue
        if pose is None:
            return {"valid": False, "error": "output rig lost a sensor pose", "registered_panoramas": []}
        actual_q = np.asarray(pose[:4])
        expected_q = np.asarray(camera["cam_from_rig_rotation"])
        if min(np.linalg.norm(actual_q - expected_q), np.linalg.norm(actual_q + expected_q)) > 1e-6:
            return {"valid": False, "error": "output rig rotation mismatch", "registered_panoramas": []}
        if not np.allclose(pose[4:], camera["cam_from_rig_translation"], atol=1e-9):
            return {"valid": False, "error": "output rig translation mismatch", "registered_panoramas": []}
    frame_data = {}
    for frame in _parse_frames(model_dir / "frames.txt"):
        if frame["rig_id"] != matching_rig["rig_id"]:
            continue
        for sensor_type, sensor_id, data_id in frame["data"]:
            if sensor_type == "CAMERA":
                frame_data[data_id] = sensor_id
    if any(frame_data.get(image_id) != camera_id for image_id, (camera_id, _) in images.items()):
        return {"valid": False, "error": "registered image is not bound to the expected rig frame", "registered_panoramas": []}
    panoramas = sorted({Path(name).stem for _, name in images.values()})
    return {
        "valid": True,
        "rig_id": matching_rig["rig_id"],
        "sensor_count": matching_rig["sensor_count"],
        "registered_image_count": len(images),
        "registered_panoramas": panoramas,
        "registered_panorama_count": len(panoramas),
    }


def validate_output_rig(model_dir: Path, config_path: Path) -> dict[str, Any]:
    try:
        return _validate_output_rig(model_dir, config_path)
    except (IndexError, KeyError, TypeError, ValueError) as error:
        return {"valid": False, "error": str(error), "registered_panoramas": []}


def select_best_rig_model(sparse_dir: Path, config_path: Path) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = []
    for model_dir in sorted(path for path in sparse_dir.iterdir() if path.is_dir()):
        points = next((model_dir / name for name in ("points3D.bin", "points3D.txt") if (model_dir / name).exists()), None)
        if points is None:
            continue
        model_to_text(model_dir)
        report = validate_output_rig(model_dir, config_path)
        score = (
            int(report["valid"]),
            int(report.get("registered_panorama_count", 0)),
            int(report.get("registered_image_count", 0)),
            points.stat().st_size,
        )
        candidates.append((score, model_dir, report))
    if not candidates:
        return None, None
    _, best, _ = max(candidates, key=lambda item: item[0])
    zero = sparse_dir / "0"
    if best != zero:
        old = sparse_dir / "old_0"
        if zero.exists():
            shutil.rmtree(old, ignore_errors=True)
            shutil.move(str(zero), str(old))
        shutil.copytree(best, zero)
    return zero, validate_output_rig(zero, config_path)


def run_equirectangular_rig_sfm(
    package_dir: Path,
    out_dir: Path,
    *,
    method: str = "global",
    overlap: int = 30,
    max_features: int = 8192,
    min_reject_ratio: float = 0.60,
    min_hold_ratio: float = 0.85,
    allow_cpu_matching: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    package_dir = package_dir.resolve()
    out_dir = out_dir.resolve()
    if method not in {"global", "incremental"}:
        raise ValueError(f"unsupported 360 SfM method: {method}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"360 SfM output is not empty: {out_dir}")
    rig, views = _load_rig(package_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir = out_dir / "metadata"
    metadata_dir.mkdir()
    source_rig_path = metadata_dir / "source_equirectangular_rig.json"
    write_json_strict(source_rig_path, rig)
    images_dir, masks_dir = _materialize_rig(package_dir, out_dir, views)
    colmap_rig_path = metadata_dir / "colmap_rig_config.json"
    rig_summary = _write_colmap_rig(rig, views, colmap_rig_path)
    capabilities = colmap_capabilities()
    blockers: list[str] = []
    colmap_cuda = None
    if find_binary("colmap") is None:
        blockers.append("colmap_binary_missing")
    else:
        colmap_cuda = colmap_has_cuda()
        if colmap_cuda is not True and not allow_cpu_matching:
            blockers.append("colmap_cuda_missing")
    if not capabilities.get("rig_configurator"):
        blockers.append("colmap_rig_configurator_missing")
    if method == "global" and not capabilities.get("global_mapper"):
        blockers.append("colmap_global_mapper_missing")
    commands = build_rig_sfm_commands(
        images_dir, masks_dir, out_dir, colmap_rig_path,
        method=method,
        overlap=max(1, int(overlap)),
        max_features=max_features,
        use_gpu=colmap_cuda is True,
    )
    panorama_count = len({int(record["panorama_id"]) for record in views})
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "decision": "blocked" if blockers else ("dry_run" if dry_run else "pending"),
        "package_dir": str(package_dir),
        "output_dir": str(out_dir),
        "method": method,
        "panorama_count": panorama_count,
        "virtual_camera_count": rig_summary["camera_count"],
        "projection_count": len(views),
        "rig": rig_summary,
        "commands": commands,
        "blockers": blockers,
        "colmap_capabilities": capabilities,
        "colmap_cuda": colmap_cuda,
        "cpu_matching_override": bool(allow_cpu_matching and colmap_cuda is not True),
        "authority": {
            "projection_provenance": True,
            "rig_extrinsics_requested": True,
            "rig_config_applied": False,
            "output_rig_validated": False,
            "recovered_world_poses": False,
            "metric_geometry": False,
            "quality_claim": False,
        },
    }
    summary_path = out_dir / "capture_splat_sfm_360_rig_summary.json"
    if blockers or dry_run:
        write_json_strict(summary_path, summary)
        if blockers:
            raise RuntimeError(f"360 SfM blocked: {', '.join(blockers)}")
        return summary
    (out_dir / "sparse").mkdir()
    for command in commands:
        completed = subprocess.run(command, text=True)
        if completed.returncode != 0:
            summary.update({"decision": "reject", "failed_command": command})
            write_json_strict(summary_path, summary)
            raise RuntimeError(f"360 SfM step failed ({command[1]}), exit {completed.returncode}")
        if command[1] == "rig_configurator":
            summary["authority"]["rig_config_applied"] = True
    sparse_zero, output_rig = select_best_rig_model(out_dir / "sparse", colmap_rig_path)
    if sparse_zero is None:
        summary.update({"decision": "reject", "error": "no reconstruction produced"})
        write_json_strict(summary_path, summary)
        raise RuntimeError("360 SfM produced no reconstruction")
    assert output_rig is not None
    registered_panoramas = set(output_rig.get("registered_panoramas", []))
    decision, ratio = decide(len(registered_panoramas), panorama_count, min_reject_ratio, min_hold_ratio)
    if not output_rig["valid"]:
        decision = "reject"
    summary.update({
        "decision": decision,
        "registered_panorama_count": len(registered_panoramas),
        "registered_panorama_ratio": ratio,
        "registered_panoramas": sorted(registered_panoramas),
        "output_rig": output_rig,
        "model": read_model_stats(sparse_zero),
        "sparse_dir": str(sparse_zero),
    })
    summary["authority"]["output_rig_validated"] = bool(output_rig["valid"])
    summary["authority"]["recovered_world_poses"] = bool(output_rig["valid"] and registered_panoramas)
    write_json_strict(summary_path, summary)
    return summary
