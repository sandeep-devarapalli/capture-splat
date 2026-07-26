from __future__ import annotations

import argparse
import hashlib
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .frames_extract import extract_selected_frames, frame_windows, probe_video
from .json_utils import write_json_strict

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
RIG_SCHEMA = "capture_splat.equirectangular_rig.v0.1"
SUMMARY_SCHEMA = "capture_splat.import_360_summary.v0.1"


def _file_evidence(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "size_bytes": path.stat().st_size,
        "checksum": f"sha256:{digest.hexdigest()}",
    }


def default_virtual_views() -> list[tuple[float, float]]:
    equator = [(float(yaw), 0.0) for yaw in range(0, 360, 60)]
    upper = [(float(yaw), 45.0) for yaw in range(0, 360, 90)]
    lower = [(float(yaw), -45.0) for yaw in range(0, 360, 90)]
    return equator + upper + lower


def virtual_camera_rotation(yaw_degrees: float, pitch_degrees: float) -> np.ndarray:
    yaw = math.radians(yaw_degrees)
    pitch = math.radians(pitch_degrees)
    forward = np.asarray([
        math.sin(yaw) * math.cos(pitch),
        math.sin(pitch),
        math.cos(yaw) * math.cos(pitch),
    ])
    right = np.asarray([math.cos(yaw), 0.0, -math.sin(yaw)])
    up = np.cross(forward, right)
    rotation = np.column_stack((right, up, forward))
    if not np.isfinite(rotation).all():
        raise ValueError("virtual camera rotation is non-finite")
    return rotation


def _bilinear_sample(image: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    x = np.mod(x, width)
    y = np.clip(y, 0.0, height - 1.0)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = (x0 + 1) % width
    y1 = np.minimum(y0 + 1, height - 1)
    wx = (x - x0)[..., None]
    wy = (y - y0)[..., None]
    top = image[y0, x0] * (1.0 - wx) + image[y0, x1] * wx
    bottom = image[y1, x0] * (1.0 - wx) + image[y1, x1] * wx
    return top * (1.0 - wy) + bottom * wy


def project_equirectangular(
    panorama: Image.Image,
    yaw_degrees: float,
    pitch_degrees: float,
    size: int,
    fov_degrees: float,
) -> tuple[Image.Image, Image.Image]:
    if size < 32:
        raise ValueError("projection size must be at least 32 pixels")
    if not 30.0 <= fov_degrees <= 150.0:
        raise ValueError("projection FOV must be between 30 and 150 degrees")
    rgba = np.asarray(panorama.convert("RGBA"), dtype=np.float32)
    height, width = rgba.shape[:2]
    if width < 4 or height < 2:
        raise ValueError("panorama is too small")
    focal = 0.5 * size / math.tan(0.5 * math.radians(fov_degrees))
    pixels = np.arange(size, dtype=np.float64) + 0.5
    xx, yy = np.meshgrid((pixels - size / 2.0) / focal, -(pixels - size / 2.0) / focal)
    camera_rays = np.stack((xx, yy, np.ones_like(xx)), axis=-1)
    camera_rays /= np.linalg.norm(camera_rays, axis=-1, keepdims=True)
    world_rays = camera_rays @ virtual_camera_rotation(yaw_degrees, pitch_degrees).T
    longitude = np.arctan2(world_rays[..., 0], world_rays[..., 2])
    latitude = np.arcsin(np.clip(world_rays[..., 1], -1.0, 1.0))
    sample_x = (longitude / (2.0 * math.pi) + 0.5) * width - 0.5
    sample_y = (0.5 - latitude / math.pi) * height - 0.5
    sampled = _bilinear_sample(rgba, sample_x, sample_y)
    rgb = np.clip(np.rint(sampled[..., :3]), 0, 255).astype(np.uint8)
    valid = np.where(sampled[..., 3] >= 127.5, 255, 0).astype(np.uint8)
    return Image.fromarray(rgb, "RGB"), Image.fromarray(valid, "L")


def _copy_panorama(source: Path, target: Path) -> Path:
    suffix = source.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError(f"unsupported panorama image: {source}")
    destination = target.with_suffix(suffix)
    shutil.copy2(source, destination)
    return destination


def _collect_panoramas(source: Path, out_dir: Path, target_panoramas: int) -> tuple[str, list[Path]]:
    panorama_dir = out_dir / "source_panoramas"
    panorama_dir.mkdir(parents=True)
    if source.is_dir():
        inputs = sorted(path for path in source.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES)
        if not inputs:
            raise FileNotFoundError(f"no panorama images found in {source}")
        return "directory", [
            _copy_panorama(path, panorama_dir / f"panorama_{index:06d}")
            for index, path in enumerate(inputs, start=1)
        ]
    if source.suffix.lower() in IMAGE_SUFFIXES:
        return "image", [_copy_panorama(source, panorama_dir / "panorama_000001")]
    if source.suffix.lower() in VIDEO_SUFFIXES:
        total, _ = probe_video(source)
        if total <= 0:
            raise RuntimeError("360 video contains no frames")
        windows = frame_windows(total, min(max(1, target_panoramas), 120))
        picked = [(start + end - 1) // 2 for start, end in windows]
        return "video", extract_selected_frames(source, picked, panorama_dir, max_edge=32768)
    raise ValueError(f"unsupported 360 input: {source}")


def import_equirectangular(
    source: Path,
    out_dir: Path,
    *,
    size: int = 1024,
    fov_degrees: float = 110.0,
    target_panoramas: int = 12,
) -> dict[str, Any]:
    source = source.resolve()
    out_dir = out_dir.resolve()
    if not source.exists():
        raise FileNotFoundError(f"360 input missing: {source}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"import-360 output is not empty: {out_dir}")
    if size > 4096:
        raise ValueError("projection size must not exceed 4096 pixels")
    out_dir.mkdir(parents=True, exist_ok=True)
    source_type, panoramas = _collect_panoramas(source, out_dir, target_panoramas)
    images_dir = out_dir / "images"
    masks_dir = out_dir / "masks" / "valid"
    metadata_dir = out_dir / "metadata"
    images_dir.mkdir(parents=True)
    masks_dir.mkdir(parents=True)
    metadata_dir.mkdir(parents=True)

    views = default_virtual_views()
    focal = 0.5 * size / math.tan(0.5 * math.radians(fov_degrees))
    records: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    for panorama_index, panorama_path in enumerate(panoramas, start=1):
        with Image.open(panorama_path) as panorama:
            width, height = panorama.size
            if abs(width / height - 2.0) > 0.1:
                raise ValueError(f"panorama must use an approximately 2:1 equirectangular layout: {panorama_path}")
            source_records.append({
                "panorama_id": panorama_index,
                "path": panorama_path.relative_to(out_dir).as_posix(),
                "width": width,
                "height": height,
                **_file_evidence(panorama_path),
            })
            for view_index, (yaw, pitch) in enumerate(views, start=1):
                name = f"p{panorama_index:06d}_v{view_index:02d}.png"
                projected, valid = project_equirectangular(panorama, yaw, pitch, size, fov_degrees)
                projected.save(images_dir / name)
                valid.save(masks_dir / f"{name}.png")
                image_path = images_dir / name
                mask_path = masks_dir / f"{name}.png"
                records.append({
                    "panorama_id": panorama_index,
                    "view_id": view_index,
                    "yaw_degrees": yaw,
                    "pitch_degrees": pitch,
                    "image": f"images/{name}",
                    "valid_mask": f"masks/valid/{name}.png",
                    "image_evidence": _file_evidence(image_path),
                    "valid_mask_evidence": _file_evidence(mask_path),
                    "rotation_equirect_world_from_camera": virtual_camera_rotation(yaw, pitch).tolist(),
                })

    rig = {
        "schema": RIG_SCHEMA,
        "source_type": source_type,
        "projection_model": "PINHOLE",
        "intrinsics": {
            "fl_x": focal,
            "fl_y": focal,
            "cx": size / 2.0,
            "cy": size / 2.0,
            "w": size,
            "h": size,
            "fov_degrees": fov_degrees,
        },
        "source_panoramas": source_records,
        "virtual_views": records,
        "coordinate_convention": {
            "world_axes": "x_right_y_up_z_forward_at_zero_yaw",
            "positive_yaw": "clockwise_when_viewed_from_above",
            "positive_pitch": "up",
            "rotation_columns": "camera_right_camera_up_camera_forward",
        },
        "authority": {
            "projection_provenance": True,
            "recovered_world_poses": False,
            "metric_geometry": False,
            "quality_claim": False,
        },
    }
    rig_path = metadata_dir / "equirectangular_rig.json"
    write_json_strict(rig_path, rig)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "decision": "ready",
        "source": str(source),
        "output_dir": str(out_dir),
        "source_type": source_type,
        "panorama_count": len(panoramas),
        "projections_per_panorama": len(views),
        "projection_count": len(records),
        "projection_size": size,
        "fov_degrees": fov_degrees,
        "outputs": {
            "images": str(images_dir),
            "valid_masks": str(masks_dir),
            "rig_metadata": str(rig_path),
        },
        "warnings": ["rig_constrained_sfm_not_implemented"],
        "authority": {
            "image_stage_import_only": True,
            "recovered_world_poses": False,
            "reconstruction_quality_proof": False,
        },
    }
    write_json_strict(out_dir / "capture_splat_import_360_summary.json", summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Project equirectangular image or video input into perspective SfM images.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--fov", type=float, default=110.0)
    parser.add_argument("--target-panoramas", type=int, default=12)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    summary = import_equirectangular(
        args.input,
        args.out,
        size=args.size,
        fov_degrees=args.fov,
        target_panoramas=args.target_panoramas,
    )
    print(summary["outputs"]["rig_metadata"])


if __name__ == "__main__":
    main()
